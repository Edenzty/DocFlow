"""Offline, paired full-document sections vs BM25 context evaluation.

The bundled model is a deterministic test fixture, not an LLM. No endpoint,
credential or budget ledger is touched. Semantic support requires a reviewer.
"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

from .evaluation import retrieval_score, verify_materials, write_json
from .model import MockModel, SYSTEM, validate_draft
from .retrieval import KeywordRetriever, _metadata_and_body, _sections, corpus_fingerprint, load_chunks


ARMS = ("full_sections", "bm25_top5")
CONTEXT_KEYS = ("chunk_id", "document_id", "title", "section", "source", "version", "text")


def _canonical(item):
    """Give both arms identical evidence fields; no score or label goes to model."""
    return {key: item[key] for key in CONTEXT_KEYS}


def _full_document_sections(knowledge_dir):
    """Read complete section bodies in source order; never overlapping windows.

    Section IDs are independent of retrieval chunk IDs. Both use the same
    citation field contract and are scored on document_id plus section.
    """
    paths = sorted(path for path in Path(knowledge_dir).iterdir()
                   if path.is_file() and path.suffix.lower() in {".md", ".txt"})
    result = []
    for path in paths:
        metadata, body = _metadata_and_body(path)
        for index, (section, text) in enumerate(_sections(body, metadata["title"])):
            identity = json.dumps({"document_id": metadata["document_id"], "section": section,
                                   "position": index, "text": text}, ensure_ascii=False, sort_keys=True)
            section_id = metadata["document_id"] + "-section-" + sha256(identity.encode("utf-8")).hexdigest()[:16]
            result.append({"chunk_id": section_id, "document_id": metadata["document_id"],
                           "title": metadata["title"], "section": section, "source": metadata["source"],
                           "version": metadata["version"], "text": text})
    return result


def _load_cases(root, suite, limit):
    names = ("development", "holdout") if suite == "all" else (suite,)
    rows = []
    for split in names:
        for case in json.loads((root / "data" / "evaluation" / (split + ".json")).read_text(encoding="utf-8")):
            rows.append({**case, "split": split})
    if limit is not None:
        if isinstance(limit, bool) or not 1 <= limit <= len(rows):
            raise ValueError("limit 必须在 1 和当前分区题数之间")
        rows = rows[:limit]
    return rows


def _fraction(num, den):
    return {"numerator": num, "denominator": den, "rate": num / den if den else None}


def summarize(rows):
    summary = {}
    for arm in ARMS:
        all_rows = [r for r in rows if r["arm"] == arm]
        done = [r for r in all_rows if r["status"] == "completed"]
        answer = [r for r in done if r["draft"]["decision"] == "answer"]
        expected_outside = [r for r in all_rows if r["expected_decision"] == "out_of_scope"]
        expected_answer = [r for r in all_rows if r["expected_decision"] == "answer"]
        outside_done = [r for r in expected_outside if r["status"] == "completed"]
        answer_done = [r for r in expected_answer if r["status"] == "completed"]
        summary[arm] = {
            "planned": len(all_rows), "context_attempted": len(all_rows),
            "generation_attempted": sum(r["generation_attempted"] for r in all_rows),
            "completed": len(done), "failed": len(all_rows) - len(done),
            "decision_match_all_planned": _fraction(sum(r["decision_correct"] is True for r in all_rows), len(all_rows)),
            "out_of_scope_match_planned": _fraction(sum(r["draft"]["decision"] == "out_of_scope" for r in outside_done), len(expected_outside)),
            "out_of_scope_match_completed": _fraction(sum(r["draft"]["decision"] == "out_of_scope" for r in outside_done), len(outside_done)),
            "out_of_scope_uncompleted": len(expected_outside) - len(outside_done),
            "false_refusal_on_completed_answerable": _fraction(sum(r["draft"]["decision"] == "out_of_scope" for r in answer_done), len(answer_done)),
            "answerable_uncompleted": len(expected_answer) - len(answer_done),
            "answer_citation_ids_in_context": _fraction(sum(bool(r["draft"]["citations"]) for r in answer), len(answer)),
            "semantic_support": "pending_human_review",
            "required_facts_coverage": "pending_human_review",
            "context_characters_total": sum(r["context_characters"] for r in all_rows),
            "context_preparation_ms_total": sum(r["context_preparation_ms"] for r in all_rows),
            "generation_ms_total": sum(r["generation_ms"] for r in all_rows),
            "provider_requests": 0, "prompt_tokens": None, "completion_tokens": None,
            "usage_note": "offline fixture; no provider usage; character count is not tokens",
        }
    return summary


def run_comparison(settings, output: Path, suite="development", limit=None, *, fixture_factory=MockModel):
    """Run the fixed offline fixture twice per case; never call a provider.

    ``fixture_factory`` is a unit-test seam, not an external model adapter.
    Semantic assessments remain pending until separate human review.
    """
    if suite not in ("development", "holdout", "all"):
        raise ValueError("未知题集")
    output = Path(output)
    if output.exists():
        raise ValueError("输出目录已存在，请使用新目录保留历史结果")
    verify_materials(settings.root)
    cases = _load_cases(settings.root, suite, limit)
    chunks = load_chunks(settings.knowledge_dir)
    full_context = _full_document_sections(settings.knowledge_dir)
    retriever = KeywordRetriever(chunks)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_deterministic_fixture", "not_real_model_evaluation": True,
        "model": "MockModel", "provider_requests": 0, "suite": suite, "limit": limit,
        "corpus_sha256": corpus_fingerprint(chunks), "corpus_chunk_count": len(chunks),
        "full_section_count": len(full_context),
        "full_sections_sha256": sha256(json.dumps(full_context, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "reference_system_prompt_sha256": sha256(SYSTEM.encode("utf-8")).hexdigest(),
        "arms": list(ARMS), "case_count": len(cases),
        "baseline_definition": "all original document section bodies in document order without overlapping retrieval chunks; headings preserved as metadata",
        "same_contract": "same frozen question, same MockModel and output validator; context selection and unit granularity differ; MockModel uses no system prompt",
        "limitations": ["mock extracts only first two supplied context items and does not represent LLM performance", "citation validation checks IDs only, not exact quotes or semantic support", "no model or semantic support measurements", "previous holdout has been inspected; results are regression, not new blind test", "full sections and retrieval chunks use different citation granularity", "context window must be checked before any future live comparison", "BM25 index construction is excluded from per-row timing"],
        "rows": [],
    }
    for case in cases:
        for arm in ARMS:
            started = time.perf_counter()
            selection_error = None
            try:
                context = [dict(section) for section in full_context] if arm == "full_sections" else [_canonical(r) for r in retriever.search(case["question"], top_k=5)]
            except Exception as exc:
                context = []
                selection_error = "context_selection_failed:" + type(exc).__name__
            preparation = (time.perf_counter() - started) * 1000
            coverage = retrieval_score(context, case["evidence"])
            coverage["context_section_recall"] = coverage.pop("recall_at_5")
            # The model sees only question plus context. Labels, split and ID remain outside.
            row = {"id": case["id"], "split": case["split"], "question": case["question"], "arm": arm,
                   "expected_decision": case["expected_decision"], "expected_evidence": case["evidence"],
                   "status": "failed", "draft": None, "decision_correct": False, "error": None,
                   "generation_attempted": selection_error is None,
                   "context": context, "context_characters": len(json.dumps(context, ensure_ascii=False)),
                   "context_preparation_ms": preparation, "generation_ms": None,
                   "context_coverage": coverage,
                   "semantic_review": "pending_human_review", "provider_usage": None}
            started = time.perf_counter()
            if selection_error:
                row["error"] = selection_error
            else:
                try:
                    # A fresh, identical fixture prevents state leaking between arms.
                    raw = fixture_factory().generate(case["question"], context)
                    draft = validate_draft(raw, context)
                    row.update(status="completed", draft=draft,
                               decision_correct=(draft["decision"] == case["expected_decision"]))
                except Exception as exc:
                    # A failure counts in the total denominator; don't serialize exception text.
                    row["error"] = "fixture_or_validation_failed:" + type(exc).__name__
            row["generation_ms"] = (time.perf_counter() - started) * 1000
            report["rows"].append(row)
    report["summary"] = summarize(report["rows"])
    report_path = output / "comparison.json"
    write_json(report_path, report)
    cases_by_id = {case["id"]: case for case in cases}
    reviews = {
        "comparison_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "instructions": "仅评审已完成的回答；引用存在不等于支持。记录审阅者和 notes；尚不确定用 uncertain，不要改原始记录。",
        "rows": [{"id": r["id"], "arm": r["arm"], "status": "pending", "reviewer": "",
                  "citation_support": "uncertain", "fact_coverage": "uncertain", "notes": "",
                  "required_facts": cases_by_id[r["id"]]["required_facts"],
                  "forbidden_facts": cases_by_id[r["id"]]["forbidden_facts"]}
                 for r in report["rows"] if r["status"] == "completed"],
    }
    write_json(output / "review_template.json", reviews)
    lines = ["# 完整文档章节与 BM25 对照：离线管线演示", "", "**这是确定性模拟结果，仅验证对照评测管线，不是大模型效果或真实对比。**", "",
             f"题数：{len(cases)}；每题两组；完整章节：{len(full_context)}；原检索片段：{len(chunks)}；真实模型请求：0。", "",
             "| 方式 | 完成/全部 | 决策匹配/全部 | 答复引用ID在上下文/答复 | 语义支持 |",
             "|---|---:|---:|---:|---|"]
    for arm in ARMS:
        s = report["summary"][arm]
        d, c = s["decision_match_all_planned"], s["answer_citation_ids_in_context"]
        lines.append(f"| {arm} | {s['completed']}/{s['planned']} | {d['numerator']}/{d['denominator']} | {c['numerator']}/{c['denominator']} | 待人工复核 |")
    lines += ["", "无证据题不计入证据召回；未完成计入决策总分母。程序检查引用ID属于上下文，不检查逐字引文，也不能证明语义支持。", "",
              "字符数不是 token，模拟耗时不是供应商耗时；旧 holdout 已观察，此处仅为回归。", "",
              "逐题上下文、草稿、失败、耗时和标签见 comparison.json；人工复核见 review_template.json。", "",
              "全文组使用原文完整章节正文并保留来源与标题，引用粒度与检索片段不同；未来真实对照须先确认上下文窗口并冻结协议。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def summarize_review(comparison_path: Path, review_path: Path):
    comparison_path, review_path = Path(comparison_path), Path(review_path)
    raw = comparison_path.read_bytes()
    comparison = json.loads(raw)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("comparison_sha256") != sha256(raw).hexdigest():
        raise ValueError("复核记录与原始对照报告不匹配")
    expected = {(r["id"], r["arm"]) for r in comparison["rows"] if r["status"] == "completed"}
    entries = review.get("rows")
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("复核项目数量不匹配")
    keys = [(r.get("id"), r.get("arm")) for r in entries]
    if len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError("复核项目重复或缺失")
    for row in entries:
        if row.get("status") not in ("pending", "done"):
            raise ValueError("复核状态不合法")
        if row.get("citation_support") not in ("supported", "unsupported", "uncertain", "not_applicable"):
            raise ValueError("引用支持结论不合法")
        if row.get("fact_coverage") not in ("complete", "incomplete", "uncertain", "not_applicable"):
            raise ValueError("事实覆盖结论不合法")
        if row["status"] == "done" and (not isinstance(row.get("reviewer"), str) or not row["reviewer"].strip()):
            raise ValueError("已完成复核必须标注审阅者")
    result = {"comparison_sha256": review["comparison_sha256"], "arms": {}}
    for arm in ARMS:
        selected = [r for r in entries if r["arm"] == arm]
        done = [r for r in selected if r["status"] == "done"]
        s = Counter(r["citation_support"] for r in done)
        c = Counter(r["fact_coverage"] for r in done)
        result["arms"][arm] = {"completed_reviews": len(done), "pending_reviews": len(selected) - len(done),
            "citation_support": _fraction(s["supported"], s["supported"] + s["unsupported"]),
            "citation_uncertain_or_na": s["uncertain"] + s["not_applicable"],
            "fact_coverage": _fraction(c["complete"], c["complete"] + c["incomplete"]),
            "coverage_uncertain_or_na": c["uncertain"] + c["not_applicable"]}
    return result
