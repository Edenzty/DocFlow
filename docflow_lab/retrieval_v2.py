"""Traceable reciprocal-rank fusion and bounded adjacent evidence expansion.

The v1 corpus, chunk IDs and retrieval scores remain unchanged.  Hybrid scores
are ranks fused with RRF, not calibrated probabilities or comparable BM25/cosine
values.  This module never invokes a language model or downloads embeddings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any

from .config import Settings
from .retrieval import (
    Chunk, KeywordRetriever, RetrievalError, VectorRetriever, _check_search,
    corpus_fingerprint, load_chunks,
)


RRF_K = 60
CANDIDATE_K = 20
MAX_CONTEXT_CHUNKS = 15


def _canonical(chunks: list[Chunk]) -> dict[str, Chunk]:
    if not chunks:
        raise ValueError("chunks 不能为空。")
    values = {chunk.chunk_id: chunk for chunk in chunks}
    if len(values) != len(chunks):
        raise ValueError("chunk_id 重复。")
    return values


class HybridRetriever:
    """RRF over two real retrieval paths, with at most two hits per section.

    Zero-score BM25 results are absent from the keyword ranking.  Source ranks
    retain their original 1-based positions, including after that filtering.
    Both retrievers must describe this exact corpus.  Explicit test doubles can
    implement the same ``chunks/search/ready`` interface for offline tests.
    """

    def __init__(self, chunks: list[Chunk], keyword: KeywordRetriever,
                 vector: VectorRetriever, candidate_k: int = CANDIDATE_K,
                 rrf_k: int = RRF_K):
        self.chunks = list(chunks)
        self._by_id = _canonical(self.chunks)
        if isinstance(candidate_k, bool) or not isinstance(candidate_k, int) or not 1 <= candidate_k <= 20:
            raise ValueError("candidate_k 必须为 1 到 20 的整数。")
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1:
            raise ValueError("rrf_k 必须为正整数。")
        fingerprint = corpus_fingerprint(self.chunks)
        for retriever in (keyword, vector):
            if corpus_fingerprint(retriever.chunks) != fingerprint:
                raise ValueError("融合检索器必须使用完全相同的知识库片段和顺序。")
        self.keyword = keyword
        self.vector = vector
        self.candidate_k = candidate_k
        self.rrf_k = rrf_k

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        query = _check_search(query, top_k)
        if not self.vector.ready:
            raise RetrievalError("融合检索需要可用的向量索引；请先执行 build-index，不会隐式退回关键词检索。")
        sources = {
            "keyword": self.keyword.search(query, top_k=self.candidate_k),
            "vector": self.vector.search(query, top_k=self.candidate_k),
        }
        candidates: dict[str, dict[str, Any]] = {}
        for name, results in sources.items():
            seen: set[str] = set()
            for rank, result in enumerate(results, 1):
                chunk_id = result.get("chunk_id")
                if chunk_id not in self._by_id or chunk_id in seen:
                    raise RetrievalError(f"{name} 检索返回未知或重复片段，无法融合。")
                seen.add(chunk_id)
                score = result.get("score")
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise RetrievalError(f"{name} 检索返回非法分数，无法融合。")
                if name == "keyword" and score <= 0:
                    continue
                item = candidates.setdefault(chunk_id, {
                    **self._by_id[chunk_id].as_dict(), "score": 0.0,
                    "method": "hybrid", "ranks": {"keyword": None, "vector": None},
                    "component_scores": {"keyword": None, "vector": None},
                })
                item["ranks"][name] = rank
                item["component_scores"][name] = score
                item["score"] += 1.0 / (self.rrf_k + rank)
        ranked = sorted(candidates.values(), key=lambda value: (-value["score"], value["chunk_id"]))
        selected, counts = [], Counter()
        for value in ranked:
            section = (value["document_id"], value["section"])
            if counts[section] >= 2:
                continue
            counts[section] += 1
            selected.append({**value, "score": round(value["score"], 12)})
            if len(selected) == top_k:
                break
        return selected


def expand_evidence(chunks: list[Chunk], seed_ids: list[str | dict[str, Any]],
                    max_chunks: int = MAX_CONTEXT_CHUNKS) -> list[dict[str, Any]]:
    """Keep ranked seeds and add nearby windows from their original sections.

    Canonical text/provenance always comes from the corpus, never a caller's
    supplied dictionary.  Only seed ranking metadata is retained.  New windows
    are chosen nearest to a seed, ties follow corpus order, and are returned in
    corpus order after the seeds.  This is context expansion, not another Top5.
    """
    by_id = _canonical(chunks)
    if isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or not 1 <= max_chunks <= MAX_CONTEXT_CHUNKS:
        raise ValueError("max_chunks 必须为 1 到 15 的整数。")
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seed in seed_ids:
        chunk_id = seed if isinstance(seed, str) else seed.get("chunk_id") if isinstance(seed, dict) else None
        if chunk_id not in by_id:
            raise RetrievalError("证据扩展收到未知片段编号。")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        value: dict[str, Any] = by_id[chunk_id].as_dict()
        if isinstance(seed, dict):
            value.update({key: seed[key] for key in ("score", "method", "ranks", "component_scores") if key in seed})
        ordered.append(value)
    if len(ordered) > max_chunks:
        raise ValueError("种子证据数超过 max_chunks；不能静默丢弃已经引用的证据。")
    if not ordered:
        return []
    groups: dict[tuple[str, str], list[tuple[int, Chunk]]] = defaultdict(list)
    for index, chunk in enumerate(chunks):
        groups[(chunk.document_id, chunk.section)].append((index, chunk))
    distances: dict[str, tuple[int, int, Chunk]] = {}
    for group in groups.values():
        positions = [i for i, (_, chunk) in enumerate(group) if chunk.chunk_id in seen]
        if not positions:
            continue
        for position, (corpus_index, chunk) in enumerate(group):
            if chunk.chunk_id not in seen:
                distances[chunk.chunk_id] = (min(abs(position - seed) for seed in positions), corpus_index, chunk)
    neighbors = sorted(distances.values(), key=lambda item: (item[0], item[1]))[:max_chunks - len(ordered)]
    ordered.extend(chunk.as_dict() for _, _, chunk in sorted(neighbors, key=lambda item: item[1]))
    return ordered


def _score(results: list[dict], expected: list[dict], *, top_five: bool) -> dict[str, Any]:
    targets = {(value["document_id"], value["section"]) for value in expected}
    got = {(value["document_id"], value["section"]) for value in results}
    hits = len(targets & got)
    reciprocal = next((1.0 / rank for rank, value in enumerate(results, 1)
                       if (value["document_id"], value["section"]) in targets), 0.0) if targets else None
    return {
        "expected_sections": len(targets), "recalled_sections": hits,
        "recall_at_5" if top_five else "context_recall": hits / len(targets) if targets else None,
        "any_evidence_recalled": bool(hits) if targets else None,
        "all_evidence_recalled": targets <= got if targets else None,
        **({"reciprocal_rank_at_5": reciprocal} if top_five else {}),
    }


def _aggregate(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    success = [row["retrieval"][method] for row in rows if row["retrieval"][method]["error"] is None]
    # Count failed evidence-bearing queries as zero recall, so a broken path
    # cannot gain a higher score by disappearing from the denominator.
    values = [row["retrieval"][method] for row in rows if row["retrieval"][method]["expected_sections"]]
    context = [value["expanded_context"] for value in values]
    total = sum(value["expected_sections"] for value in values)
    return {
        "questions": len(rows), "successful_queries": len(success), "failed_queries": len(rows) - len(success),
        "questions_with_evidence": len(values),
        "expected_sections": total, "recalled_sections": sum(value["recalled_sections"] for value in values),
        "micro_recall_at_5": sum(value["recalled_sections"] for value in values) / total if total else None,
        "macro_recall_at_5": sum(value["recall_at_5"] for value in values) / len(values) if values else None,
        "mrr_at_5": sum(value["reciprocal_rank_at_5"] for value in values) / len(values) if values else None,
        "any_evidence_recalled_questions": sum(value["any_evidence_recalled"] for value in values),
        "all_evidence_recalled_questions": sum(value["all_evidence_recalled"] for value in values),
        "expanded_context": {
            "max_chunks": MAX_CONTEXT_CHUNKS,
            "micro_recall": sum(value["recalled_sections"] for value in context) / total if total else None,
            "macro_recall": sum(value["context_recall"] for value in context) / len(context) if context else None,
            "all_evidence_recalled_questions": sum(value["all_evidence_recalled"] for value in context),
            "note": "邻窗只扩展同一章节，章节级召回不会凭空提高；上下文覆盖不等于Top5指标。",
        },
        "metric_denominator": "所有存在标注证据的问题；失败按零召回计，知识库外且无标注证据题不进入召回分母。",
    }


def evaluate_retrieval(settings: Settings, output: Path, suite: str = "all") -> dict[str, Any]:
    """Compare three retrieval paths on immutable regression data; no model API.

    Labels are used only in scoring, never in search or fusion.  The previously
    inspected holdout is explicitly a regression set, not a fresh blind test.
    """
    from .evaluation import verify_materials

    if suite not in {"all", "development", "holdout"}:
        raise ValueError("suite 必须为 all、development 或 holdout。")
    output = Path(output)
    if output.exists():
        raise ValueError("评测输出目录已存在，请换新目录保留历史。")
    verify_materials(settings.root)
    chunks = load_chunks(settings.knowledge_dir)
    keyword = KeywordRetriever(chunks)
    vector = VectorRetriever(chunks, settings.vector_dir, model_path=settings.model_path)
    if not vector.ready:
        raise RetrievalError("向量索引不可用；请先运行 build-index。未生成伪造的融合评测。")
    hybrid = HybridRetriever(chunks, keyword, vector)
    retrievers = {"keyword": keyword, "vector": vector, "hybrid": hybrid}
    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(), "suite": suite,
        "mode": "retrieval_only_real_bge_cpu", "model_requests": 0,
        "corpus_sha256": corpus_fingerprint(chunks), "vector_index": vector.info(),
        "algorithm": {"name": "reciprocal_rank_fusion", "candidate_k": CANDIDATE_K,
                      "rrf_k": RRF_K, "max_chunks_per_section": 2, "parameters_tuned_on_labels": False,
                      "score_note": "RRF融合排名；BM25与余弦分数分开记录，均不作为回答可信概率。"},
        "protocol_note": "沿用v1冻结材料；已观察的holdout现在是回归集，不是新的盲测。无生成或语义支持评分。",
        "rows": [],
    }
    splits = ["development", "holdout"] if suite == "all" else [suite]
    output.mkdir(parents=True)
    for split in splits:
        questions = json.loads((settings.root / f"data/evaluation/{split}.json").read_text(encoding="utf-8"))
        for question in questions:
            row = {**question, "split": split, "retrieval": {}}
            for name, retriever in retrievers.items():
                started = time.perf_counter()
                try:
                    results = retriever.search(question["question"], top_k=5)
                    expanded = expand_evidence(chunks, results)
                    value = {"results": results, "expanded_context_results": expanded,
                             **_score(results, question["evidence"], top_five=True),
                             "expanded_context": _score(expanded, question["evidence"], top_five=False), "error": None}
                except Exception as exc:
                    value = {"results": [], "expanded_context_results": [],
                             **_score([], question["evidence"], top_five=True),
                             "expanded_context": _score([], question["evidence"], top_five=False),
                             "error": f"{type(exc).__name__}: {exc}"}
                value["elapsed_ms"] = (time.perf_counter() - started) * 1000
                row["retrieval"][name] = value
            report["rows"].append(row)
    report["summary"] = {name: _aggregate(report["rows"], name) for name in retrievers}
    report["split_summary"] = {split: {name: _aggregate([row for row in report["rows"] if row["split"] == split], name)
                                      for name in retrievers} for split in splits}
    differences = []
    for row in report["rows"]:
        if any(value["error"] is not None for value in row["retrieval"].values()):
            continue
        keyword_hit = row["retrieval"]["keyword"]["recalled_sections"]
        vector_hit = row["retrieval"]["vector"]["recalled_sections"]
        hybrid_hit = row["retrieval"]["hybrid"]["recalled_sections"]
        if keyword_hit != hybrid_hit or vector_hit != hybrid_hit:
            differences.append({"id": row["id"], "keyword_recalled": keyword_hit,
                                "vector_recalled": vector_hit, "hybrid_recalled": hybrid_hit,
                                "hybrid_minus_keyword": hybrid_hit - keyword_hit,
                                "hybrid_minus_vector": hybrid_hit - vector_hit})
    report["differences"] = differences
    from .retrieval_diagnostics import diagnose_report
    report["ranking_diagnostics"] = diagnose_report(settings, report)
    (output / "retrieval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# DocFlow V2 检索回归", "", report["protocol_note"], "", "真实BGE CPU；模型API请求0次。",
             "", "| 检索 | 成功/总题 | 标注章节召回micro@5 | 按题macro@5 | MRR@5 | 全证据/有证据题 |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, score in report["summary"].items():
        def percent(value):
            return f"{value:.2%}" if value is not None else "未计算"
        lines.append(f"| {name} | {score['successful_queries']}/{score['questions']} | {percent(score['micro_recall_at_5'])} | {percent(score['macro_recall_at_5'])} | {percent(score['mrr_at_5'])} | {score['all_evidence_recalled_questions']}/{score['questions_with_evidence']} |")
    lines += ["", "固定RRF(k=60)，各取20候选，关键词零分不参与融合，每章节最多2片段。",
              "邻窗上下文最多15片段，独立记录为expanded_context，不伪装为Top5。",
              "由于邻窗只取同一章节，章节级召回不会提高；扩展用于补足同章节被切开的事实。",
              "分数不是概率；本报告不评判答案语义正确。完整排名、失败、耗时与差异见retrieval.json。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
