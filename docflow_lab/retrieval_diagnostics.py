"""Offline, label-ID based retrieval diagnostics; never calls a model.

Inspired by Ragas IDBasedContextPrecision, independently implemented for this
project's section labels. This is annotation matching, not semantic relevance
or answer quality. Historical rankings are validated against the frozen corpus.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from .config import Settings
from .retrieval import corpus_fingerprint, load_chunks


SCHEMA_VERSION = 1
TOP_K = 5
METHODS = ("keyword", "vector", "hybrid")
RAGAS_COMMIT = "298b68274234c060deacab3cf5fb52aa3a20e885"


def rank_metrics(results: list[dict], expected: list[dict], *, failed: bool = False) -> dict[str, Any]:
    """Score at most five original chunk slots, crediting each section once.

    Empty precision is undefined per-row. Macro aggregation uses zero for an
    empty/failed evidence-bearing query rather than dropping it. Questions
    without annotated evidence have no relevance metric, even with results.
    """
    if len(results) > TOP_K:
        raise ValueError("诊断只接受原始Top5，不能把扩展上下文当作Top5。")
    if failed and results:
        raise ValueError("失败检索不能同时带有效排名。")
    targets = {(x["document_id"], x["section"]) for x in expected}
    sections = [(x["document_id"], x["section"]) for x in results]
    unique = set(sections)
    seen, ranks = set(), []
    for rank, section in enumerate(sections, 1):
        if section in targets and section not in seen:
            ranks.append(rank)
        seen.add(section)
    hits = len(ranks)
    ndcg = None
    if targets:
        dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(targets), TOP_K) + 1))
        ndcg = dcg / ideal
    if failed:
        outcome = "retrieval_error"
    elif not targets:
        outcome = "no_gold_not_scored"
    elif not results:
        outcome = "no_results"
    elif not hits:
        outcome = "no_target_retrieved"
    elif hits < len(targets):
        outcome = "partial_targets"
    else:
        outcome = "all_targets_retrieved"
    return {
        "outcome": outcome, "failed": failed, "expected_sections": len(targets),
        "returned_chunks": len(results), "unique_returned_sections": len(unique),
        "duplicate_section_slots": len(results) - len(unique),
        "matched_sections": hits, "first_target_rank": ranks[0] if ranks else None,
        "target_ranks": ranks,
        "recall_at_5": hits / len(targets) if targets else None,
        "hit_at_5": bool(hits) if targets else None,
        "reciprocal_rank_at_5": 1 / ranks[0] if targets and ranks else 0.0 if targets else None,
        "annotated_section_precision": hits / len(unique) if targets and unique else None,
        "ndcg_at_5": ndcg,
    }


def summarize(values: list[dict]) -> dict[str, Any]:
    scored = [x for x in values if x["expected_sections"]]
    expected = sum(x["expected_sections"] for x in scored)
    def mean(field: str):
        return sum(x[field] or 0 for x in scored) / len(scored) if scored else None
    return {
        "questions": len(values), "questions_with_gold": len(scored),
        "no_gold_questions": len(values) - len(scored),
        "failed_queries": sum(x["failed"] for x in values),
        "expected_sections": expected, "matched_sections": sum(x["matched_sections"] for x in scored),
        "micro_recall_at_5": sum(x["matched_sections"] for x in scored) / expected if expected else None,
        "hit_rate_at_5": mean("hit_at_5"), "mrr_at_5": mean("reciprocal_rank_at_5"),
        "macro_annotated_section_precision": mean("annotated_section_precision"),
        "mean_ndcg_at_5": mean("ndcg_at_5"),
        "undefined_empty_precision_count": sum(x["annotated_section_precision"] is None for x in scored),
        "duplicate_section_slots": sum(x["duplicate_section_slots"] for x in values),
        "outcomes": dict(sorted(Counter(x["outcome"] for x in values).items())),
    }


def diagnose_report(settings: Settings, report: dict) -> dict[str, Any]:
    """Validate provenance/labels/rank IDs; ignore every precomputed score."""
    from .evaluation import verify_materials
    if not isinstance(report, dict):
        raise ValueError("历史排名顶层必须为对象。")
    verify_materials(settings.root)
    chunks = load_chunks(settings.knowledge_dir)
    by_id = {x.chunk_id: x for x in chunks}
    fingerprint = corpus_fingerprint(chunks)
    if report.get("corpus_sha256") != fingerprint:
        raise ValueError("历史排名与当前冻结语料哈希不匹配。")
    suite = report.get("suite")
    if not isinstance(suite, str) or suite not in {"all", "development", "holdout"}:
        raise ValueError("历史排名suite不合法。")
    splits = ["development", "holdout"] if suite == "all" else [suite]
    gold = {}
    for split in splits:
        data = json.loads((settings.root / f"data/evaluation/{split}.json").read_text(encoding="utf-8"))
        for item in data:
            gold[item["id"]] = {**item, "split": split}
    rows = report.get("rows")
    if (not isinstance(rows, list) or len(rows) != len(gold)
            or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows)
            or {r.get("id") for r in rows} != set(gold)):
        raise ValueError("历史排名必须精确覆盖所选冻结题集，不能漏题或重复。")
    diagnostics = []
    for row in rows:
        item = gold[row["id"]]
        if row.get("split") != item["split"]:
            raise ValueError("历史排名split与冻结题集不匹配。")
        if "evidence" in row and row["evidence"] != item["evidence"]:
            raise ValueError("历史排名附带的标注已改变。")
        for field in ("question", "expected_decision", "required_facts", "forbidden_facts"):
            if field in row and row[field] != item[field]:
                raise ValueError(f"历史排名附带的{field}与冻结题目不一致。")
        if not isinstance(row.get("retrieval"), dict) or set(row["retrieval"]) != set(METHODS):
            raise ValueError("诊断需要keyword、vector、hybrid三路原始排名。")
        values = {}
        for method in METHODS:
            saved = row["retrieval"][method]
            if not isinstance(saved, dict):
                raise ValueError("历史排名结构不合法。")
            failed = saved.get("failed", False)
            if not isinstance(failed, bool):
                raise ValueError("failed必须为布尔值。")
            failed = failed or saved.get("error") is not None
            raw = saved.get("results")
            if not isinstance(raw, list) or len(raw) > TOP_K:
                raise ValueError("历史排名results必须为最多5条片段。")
            canonical, seen = [], set()
            for result in raw:
                if not isinstance(result, dict):
                    raise ValueError("历史排名片段结构不合法。")
                identifier = result.get("chunk_id")
                if not isinstance(identifier, str) or identifier not in by_id or identifier in seen:
                    raise ValueError("历史排名包含未知或重复片段编号。")
                seen.add(identifier)
                chunk = by_id[identifier]
                if any(key in result and result[key] != getattr(chunk, key) for key in ("document_id", "section")):
                    raise ValueError("历史排名章节与片段编号不匹配。")
                canonical.append(chunk.as_dict())
            values[method] = rank_metrics(canonical, item["evidence"], failed=failed)
            values[method]["ranked_chunk_ids"] = [x["chunk_id"] for x in canonical]
        diagnostics.append({"id": item["id"], "split": item["split"],
                            "expected_decision": item["expected_decision"], "retrieval": values})
    return {
        "schema_version": SCHEMA_VERSION, "mode": "offline_saved_ranking_diagnostics", "model_requests": 0,
        "source_mode_claim": report.get("mode"), "suite": suite, "corpus_sha256": fingerprint,
        "note": "只校验冻结语料、编号与标签并重算排名；不能独立证明历史运行来源，不能评判回答或语义支持。",
        "metric_policy": {
            "top_k": TOP_K, "labels": "冻结(document_id, section)二值目标；不是完整的相关性标注",
            "precision": "命中标注的唯一章节/返回的唯一章节；重复章节不重复得分；不含空排名。",
            "macro_precision": "所有有证据题平均；空排名或失败以0计，逐题undefined单列。",
            "ndcg": "原始片段排名上唯一目标章节首次出现得1/log2(rank+1)；重复章节得0。",
            "no_gold": "无标注证据题的相关性指标null，不把返回内容直接判为误报。",
            "limitations": "已观察题集只作回归；召回、精确率和nDCG均非回答正确率。",
        },
        "summary": {m: summarize([r["retrieval"][m] for r in diagnostics]) for m in METHODS},
        "by_expected_decision": {
            decision: {m: summarize([r["retrieval"][m] for r in diagnostics if r["expected_decision"] == decision]) for m in METHODS}
            for decision in sorted({r["expected_decision"] for r in diagnostics})
        },
        "failure_cases": [{"id": r["id"], "method": m, "outcome": r["retrieval"][m]["outcome"]}
                          for r in diagnostics for m in METHODS
                          if r["retrieval"][m]["outcome"] not in {"all_targets_retrieved", "no_gold_not_scored"}],
        "rows": diagnostics,
    }


def analyze_saved(settings: Settings, source: Path, output: Path) -> dict[str, Any]:
    output, source = Path(output), Path(source)
    if output.exists():
        raise ValueError("诊断输出目录已存在，请换新目录保留历史。")
    if source.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("历史排名文件超过8MiB上限。")
    with source.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("历史排名文件超过8MiB上限。")
    result = diagnose_report(settings, json.loads(raw.decode("utf-8")))
    result["source_sha256"] = sha256(raw).hexdigest()
    result["created_at"] = datetime.now(timezone.utc).isoformat()
    output.mkdir(parents=True, exist_ok=False)
    (output / "diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# DocFlow 检索诊断", "", result["note"], "", "本次模型API请求：0。旧报告不覆盖，旧分数不信任，使用冻结标签与实际排名重算。", "",
             "| 检索 | 有证据题 | micro Recall@5 | Hit@5 | MRR@5 | 标注章节精确率macro | nDCG@5 | 重复章节槽位 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(x):
        return f"{x:.4f}" if x is not None else "不适用"
    for method, score in result["summary"].items():
        fields = [fmt(score[k]) for k in ("micro_recall_at_5", "hit_rate_at_5", "mrr_at_5", "macro_annotated_section_precision", "mean_ndcg_at_5")]
        lines.append(f"| {method} | {score['questions_with_gold']} | {' | '.join(fields)} | {score['duplicate_section_slots']} |")
    lines += ["", "标注章节精确率只统计既有目标，不是全部语义相关性；空结果/失败在macro按0计，无证据题不计分。",
              "nDCG按原始片段位置计，重复章节只首个命中得分；未对排名去重后重排。", "",
              "## 需要查看的案例", "", "| 题号 | 检索 | 原因 |", "|---|---|---|"]
    lines += [f"| {r['id']} | {r['method']} | {r['outcome']} |" for r in result["failure_cases"]]
    lines += ["", "按预期决策分组的分母、逐题排名编号与重复槽位见diagnostics.json；没有评估模型回答。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result
