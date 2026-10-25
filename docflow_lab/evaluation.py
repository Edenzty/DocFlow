"""Frozen labelled questions never enter the retrieval/generation interfaces."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

from .config import Settings
from .model import DeepSeekModel, MockModel, ModelError, SYSTEM, safe_model_text
from .retrieval import KeywordRetriever, VectorRetriever, load_chunks, corpus_fingerprint
from .storage import RequestBudget, Conflict


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_materials(root: Path):
    manifest = json.loads((root / "data/evaluation/freeze_manifest.json").read_text(encoding="utf-8"))
    # Accept only repository-relative files; the manifest is data, not code.
    entries = manifest.get("files", manifest)
    if isinstance(entries, list):
        pairs = [(entry["path"], entry["sha256"]) for entry in entries]
    elif isinstance(entries, dict):
        pairs = list(entries.items())
    else:
        raise ValueError("冻结清单格式错误")
    actual_knowledge = {file.relative_to(root).as_posix() for file in (root / "knowledge").iterdir()
                        if file.is_file() and file.suffix.lower() in {".md", ".txt"}}
    expected_paths = actual_knowledge | {"data/evaluation/development.json", "data/evaluation/holdout.json", "data/evaluation/protocol.md"}
    if len(actual_knowledge) != 12 or len(pairs) != 15 or {rel for rel, _ in pairs} != expected_paths:
        raise ValueError("冻结清单必须精确覆盖12篇知识文档、两个题集和协议；发现新增、缺失或重复资料")
    for rel, expected in pairs:
        file = (root / rel).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            raise ValueError("冻结材料路径不合法")
        if sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"冻结材料已改变：{rel}；不能沿用原测试结论")
    seen_ids = set()
    for split, count in [("development", 10), ("holdout", 20)]:
        dataset = json.loads((root / f"data/evaluation/{split}.json").read_text(encoding="utf-8"))
        if len(dataset) != count or len({q["id"] for q in dataset}) != count or seen_ids & {q["id"] for q in dataset}:
            raise ValueError("冻结题集数量或编号不合法")
        seen_ids.update(q["id"] for q in dataset)
    return manifest


def retrieval_score(results: list, expected: list) -> dict:
    got = {(r["document_id"], r["section"]) for r in results}
    target = {(r["document_id"], r["section"]) for r in expected}
    hit = len(got & target)
    return {"expected_sections": len(target), "recalled_sections": hit,
            "recall_at_5": hit / len(target) if target else None,
            "all_evidence_recalled": target <= got if target else None}


def evaluate(settings: Settings, output: Path, suite="all", method="both", live=False):
    if output.exists():
        raise ValueError("评测输出目录已存在，请使用新目录保留历史结果")
    verify_materials(settings.root)
    chunks = load_chunks(settings.knowledge_dir)
    methods = ["keyword", "vector"] if method == "both" else [method]
    retrievers = {"keyword": KeywordRetriever(chunks)}
    if "vector" in methods:
        retrievers["vector"] = VectorRetriever(chunks, settings.vector_dir, model_path=settings.model_path)
        if not retrievers["vector"].ready:
            raise ValueError("向量索引未建立，请先执行 build-index；不自动用关键词结果替代")
    generation_method = "vector" if "vector" in methods else "keyword"
    names = ["development", "holdout"] if suite == "all" else [suite]
    questions = []
    for name in names:
        items = json.loads((settings.root / f"data/evaluation/{name}.json").read_text(encoding="utf-8"))
        questions.extend([{**item, "split": name} for item in items])
    budget = RequestBudget(settings.budget_path)
    model = DeepSeekModel(budget, settings.model) if live else MockModel()
    output.mkdir(parents=True)
    initial_calls = {row["id"] for row in budget.records()}
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": "live" if live else "mock",
              "model": settings.model if live else "deterministic-extractive-fixture",
              "suite": suite, "methods": methods, "generation_method": generation_method,
              "corpus_sha256": corpus_fingerprint(chunks),
              "provider": "DeepSeek" if live else "offline-fixture",
              "generation_config": {"system_sha256": sha256(SYSTEM.encode()).hexdigest(), "temperature": 0, "max_tokens": 1800, "thinking": "disabled", "automatic_retries": 0},
              "vector_index": retrievers["vector"].info() if "vector" in methods else None,
              "protocol": "data/evaluation/protocol.md", "rows": [], "stop_reason": None}
    consecutive_failures = 0
    for item in questions:
        row = {**item, "retrieval": {}, "status": "not_run", "draft": None, "error": None,
               "citation_valid": None, "decision_correct": None, "semantic_review": None}
        for name in methods:
            start = time.perf_counter()
            results = retrievers[name].search(item["question"], top_k=5)
            row["retrieval"][name] = {"results": results, "elapsed_ms": (time.perf_counter() - start) * 1000,
                                       **retrieval_score(results, item["evidence"])}
        if report["stop_reason"]:
            row["error"] = "前序错误已停止新模型请求"
        else:
            started = time.perf_counter()
            try:
                if live:
                    model.purpose = item["split"]
                # No expected decision, target evidence or facts cross this boundary.
                draft = model.generate(item["question"], row["retrieval"][generation_method]["results"])
                row.update(status="completed", draft=draft, citation_valid=True,
                           decision_correct=draft["decision"] == item["expected_decision"])
                consecutive_failures = 0
            except (ModelError, Conflict) as exc:
                consecutive_failures += 1
                row.update(status="failed", error=str(exc), decision_correct=False)
                if live and model.last_raw_output is not None:
                    row["invalid_model_output"] = safe_model_text(model.last_raw_output)
                if isinstance(exc, Conflict) or any(f"HTTP {code}" in str(exc) for code in (401, 402, 403, 429)) or consecutive_failures >= 2:
                    report["stop_reason"] = str(exc)
            row["generation_elapsed_ms"] = (time.perf_counter() - started) * 1000
        report["rows"].append(row)
        write_json(output / "evaluation.json", report)
    completed = [row for row in report["rows"] if row["status"] == "completed"]
    attempted = [row for row in report["rows"] if row["status"] != "not_run"]
    calls = [row for row in budget.records() if row["id"] not in initial_calls]
    report["model_calls"] = calls
    report["budget"] = budget.summary()
    report["summary"] = {
        "questions": len(questions), "attempted": len(attempted), "completed": len(completed),
        "failed": len(attempted) - len(completed), "not_run": len(questions) - len(attempted),
        "decision_correct": sum(row["decision_correct"] is True for row in attempted),
        "decision_denominator": len(questions), "attempted_decision_denominator": len(attempted),
        "valid_citation_drafts": sum(row["citation_valid"] is True for row in completed),
        "citation_denominator": len(attempted),
        "citation_metric": "draft structure validated / attempted questions; includes empty citations for clarification/refusal",
        "answer_drafts_with_citations": sum(r["draft"]["decision"] == "answer" and bool(r["draft"]["citations"]) for r in completed),
        "answer_drafts": sum(r["draft"]["decision"] == "answer" for r in completed),
        "semantic_support": "pending human review; citation validity is not support",
        "model_requests": len(calls), "usage_missing_calls": sum(c["prompt_tokens"] is None for c in calls),
        "reported_prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
        "reported_completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
        "reported_total_tokens": sum(c["total_tokens"] or 0 for c in calls),
        "total_tokens_missing_calls": sum(c["total_tokens"] is None for c in calls),
        "retrieval": {},
    }
    for name in methods:
        values = [r["retrieval"][name] for r in report["rows"] if r["retrieval"][name]["expected_sections"]]
        report["summary"]["retrieval"][name] = {
            "questions_with_evidence": len(values),
            "macro_recall_at_5": sum(v["recall_at_5"] for v in values) / len(values) if values else None,
            "micro_recall_at_5": sum(v["recalled_sections"] for v in values) / sum(v["expected_sections"] for v in values) if values else None,
            "recalled_sections": sum(v["recalled_sections"] for v in values),
            "expected_sections": sum(v["expected_sections"] for v in values),
            "any_evidence_recalled_questions": sum(v["recalled_sections"] > 0 for v in values),
            "all_evidence_recalled_questions": sum(v["all_evidence_recalled"] for v in values),
        }
    write_json(output / "evaluation.json", report)
    write_json(output / "semantic_review_template.json", [
        {"id": r["id"], "reviewer": "", "reviewer_type": "human", "supported": None,
         "appropriate_decision": None, "notes": "逐条核查实际引用、required_facts和forbidden_facts，不按字面命中评分"}
        for r in completed])
    lines = ["# DocFlow 实际评测", "", f"模式：{report['mode']}；生成检索：{generation_method}；题数：{len(questions)}。", "",
             "离线模拟结果只验证管线。引用合法率不能视为事实支持率；人工语义核查待填写。", "",
             "| 检索 | 有证据题数 | Top5章节召回micro | 按题平均macro | 全部证据召回题数 |", "|---|---:|---:|---:|---:|"]
    for name, score in report["summary"]["retrieval"].items():
        lines.append(f"| {name} | {score['questions_with_evidence']} | {score['micro_recall_at_5']:.2%} | {score['macro_recall_at_5']:.2%} | {score['all_evidence_recalled_questions']} |")
    lines += ["", f"完成 {len(completed)}/{len(questions)}；失败 {len(attempted)-len(completed)}；未运行 {len(questions)-len(attempted)}。",
              f"决策匹配 {report['summary']['decision_correct']}/{len(questions)}（所有题，未完成按未通过）；草稿结构校验通过 {len(completed)}/{len(attempted)}（尝试题）。",
              f"模型请求 {len(calls)}；缺失用量 {report['summary']['usage_missing_calls']}。", "",
              "完整问题、召回、草稿、错误、耗时和用量见 evaluation.json；逐题语义核查见 semantic_review_template.json。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
