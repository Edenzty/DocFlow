"""Metric denominators, frozen material integrity and model/label isolation."""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from docflow_lab.config import Settings
import docflow_lab.evaluation as evaluation
from docflow_lab.model import DeepSeekModel, ModelError, validate_draft
from docflow_lab.retrieval import load_chunks
from docflow_lab.storage import Conflict, RequestBudget


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def frozen_project(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    for index in range(1, 13):
        (knowledge / f"d{index:02}.md").write_text(f"document_id: d{index:02}\ntitle: 测试文档{index}\nsource: original-test-fixture\nversion: 1.0\nkind: {'guide' if index <= 6 else 'faq'}\n---\n## S1\n设备归属检查使用401或403。\n## S2\n模型调用与人工审核彼此独立。", encoding="utf-8")
    directory = tmp_path / "data" / "evaluation"
    directory.mkdir(parents=True)
    development = []
    for index in range(10):
        evidence = []
        if index == 0:
            evidence = [{"document_id": "d01", "section": "S1"}]
        elif index == 1:
            evidence = [{"document_id": "d01", "section": "S1"}, {"document_id": "d01", "section": "S2"}]
        development.append({"id": f"dev-{index + 1:02}", "question": f"fixture-question-{index}", "expected_decision": "answer" if evidence else "out_of_scope", "evidence": evidence, "required_facts": ["private-required-label"], "forbidden_facts": ["private-forbidden-label"]})
    holdout = [{**development[index % 10], "id": f"test-{index + 1:02}"} for index in range(20)]
    dump(directory / "development.json", development)
    dump(directory / "holdout.json", holdout)
    (directory / "protocol.md").write_text("Frozen offline metric fixture, not production accuracy.", encoding="utf-8")
    files = sorted([*knowledge.glob("*.md"), directory / "development.json", directory / "holdout.json", directory / "protocol.md"])
    manifest = {"version": "docflow-evaluation-v1", "frozen_date": "2026-10-25", "source_commit": "offline-fixture", "splits": {"development": 10, "holdout": 20}, "files": {path.relative_to(tmp_path).as_posix(): sha256(path.read_bytes()).hexdigest() for path in files}}
    dump(directory / "freeze_manifest.json", manifest)
    return Settings(root=tmp_path, data_dir=tmp_path / "state"), manifest


class FixtureKeywordRetriever:
    """Intentionally retrieves S1 only to expose micro/macro denominator differences."""

    def __init__(self, chunks):
        self.chunk = next(chunk for chunk in chunks if chunk.document_id == "d01" and chunk.section == "S1")

    def search(self, query, top_k=5):
        return [{**self.chunk.as_dict(), "score": 1.0, "method": "keyword"}]


class FixtureGenerationModel:
    """No network; checks invocation boundaries rather than model answer quality."""

    calls = []

    def generate(self, question, evidence):
        self.calls.append({"question": question, "evidence": evidence})
        answerable = question in {"fixture-question-0", "fixture-question-1"}
        return validate_draft({"decision": "answer" if answerable else "out_of_scope", "answer": "Offline fixture output, not a semantic accuracy claim.", "citations": [evidence[0]["chunk_id"]] if answerable else []}, evidence)


def test_freeze_manifest_accepts_exact_materials_and_detects_modified_bytes(frozen_project):
    settings, manifest = frozen_project
    assert evaluation.verify_materials(settings.root)["version"] == manifest["version"]
    file = settings.knowledge_dir / "d01.md"
    file.write_bytes(file.read_bytes() + b"\nChanged content.\n")
    with pytest.raises(ValueError, match="冻结材料已改变"):
        evaluation.verify_materials(settings.root)


def test_freeze_manifest_rejects_new_unfrozen_knowledge(frozen_project):
    settings, _ = frozen_project
    (settings.knowledge_dir / "unfrozen.txt").write_text("A late answer leak must not enter a frozen retrieval corpus.", encoding="utf-8")
    with pytest.raises(ValueError):
        evaluation.verify_materials(settings.root)


def test_freeze_manifest_rejects_empty_or_missing_required_file_list(frozen_project):
    settings, manifest = frozen_project
    manifest["files"] = {}
    dump(settings.root / "data/evaluation/freeze_manifest.json", manifest)
    with pytest.raises(ValueError):
        evaluation.verify_materials(settings.root)


def test_freeze_manifest_cannot_read_outside_project(frozen_project):
    settings, manifest = frozen_project
    outside = settings.root.parent / "outside-docflow-fixture.txt"
    outside.write_bytes(b"not in the project")
    manifest["files"]["../outside-docflow-fixture.txt"] = sha256(outside.read_bytes()).hexdigest()
    dump(settings.root / "data/evaluation/freeze_manifest.json", manifest)
    with pytest.raises(ValueError):
        evaluation.verify_materials(settings.root)


def test_retrieval_score_counts_sections_once_and_does_not_score_unknown_scope():
    expected = [{"document_id": "d01", "section": "A"}, {"document_id": "d01", "section": "B"}]
    # Two chunks from A do not count as two evidence sections. C is not B.
    results = [{"document_id": "d01", "section": "A"}, {"document_id": "d01", "section": "A"}, {"document_id": "d01", "section": "C"}]
    score = evaluation.retrieval_score(results, expected)
    assert score["expected_sections"] == 2
    assert score["recalled_sections"] == 1
    assert score["recall_at_5"] == 0.5
    assert score["all_evidence_recalled"] is False
    unknown = evaluation.retrieval_score(results, [])
    assert unknown["recall_at_5"] is None
    assert unknown["all_evidence_recalled"] is None


def test_metric_summary_distinguishes_micro_macro_and_keeps_semantics_pending(frozen_project, monkeypatch):
    settings, _ = frozen_project
    FixtureGenerationModel.calls = []
    monkeypatch.setattr(evaluation, "KeywordRetriever", FixtureKeywordRetriever)
    monkeypatch.setattr(evaluation, "MockModel", FixtureGenerationModel)
    output = settings.root / "evaluation-output"
    report = evaluation.evaluate(settings, output, suite="development", method="keyword")
    retrieval = report["summary"]["retrieval"]["keyword"]
    assert retrieval["macro_recall_at_5"] == pytest.approx(0.75)
    assert retrieval["micro_recall_at_5"] == pytest.approx(2 / 3)
    assert retrieval["questions_with_evidence"] == 2
    assert retrieval["all_evidence_recalled_questions"] == 1
    assert report["summary"]["model_requests"] == 0
    assert "pending" in report["summary"]["semantic_support"]
    assert all(row["semantic_review"] is None for row in report["rows"])
    review = json.loads((output / "semantic_review_template.json").read_text(encoding="utf-8"))
    assert all(row["supported"] is None for row in review)
    # No frozen answer/decision/fact labels cross the generator boundary.
    assert len(FixtureGenerationModel.calls) == 10
    calls = json.dumps(FixtureGenerationModel.calls)
    assert "private-required-label" not in calls
    assert "private-forbidden-label" not in calls
    assert "expected_decision" not in calls


def test_stop_after_provider_failure_keeps_unrun_questions_in_total_denominator(frozen_project, monkeypatch):
    settings, _ = frozen_project

    class StopOnSecond(FixtureGenerationModel):
        calls = []

        def generate(self, question, evidence):
            if self.calls:
                self.calls.append({"question": question, "evidence": evidence})
                raise ModelError("DeepSeek HTTP 429; offline fixture, no network request")
            return super().generate(question, evidence)

    monkeypatch.setattr(evaluation, "KeywordRetriever", FixtureKeywordRetriever)
    monkeypatch.setattr(evaluation, "MockModel", StopOnSecond)
    report = evaluation.evaluate(settings, settings.root / "stopped-evaluation", suite="development", method="keyword")
    summary = report["summary"]
    assert summary["questions"] == 10
    assert summary["attempted"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["not_run"] == 8
    assert summary["decision_correct"] == 1
    assert summary["decision_denominator"] == 10
    assert report["rows"][1]["draft"] is None
    assert report["rows"][1]["citation_valid"] is not True
    assert report["stop_reason"] is not None


def test_existing_evaluation_directory_is_never_overwritten(frozen_project, monkeypatch):
    settings, _ = frozen_project
    output = settings.root / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_bytes(b"original historical result")
    with pytest.raises(ValueError, match="已存在"):
        evaluation.evaluate(settings, output, suite="development", method="keyword")
    assert marker.read_bytes() == b"original historical result"


def test_evaluation_split_quotas_do_not_refund_failed_calls(tmp_path):
    budget = RequestBudget(tmp_path / "budget.sqlite3")
    calls = [budget.reserve("development") for _ in range(10)]
    budget.finish(calls[0], 1.0, None, "offline failure fixture")
    with pytest.raises(Conflict):
        budget.reserve("development")
    assert len([budget.reserve("holdout") for _ in range(20)]) == 20
    with pytest.raises(Conflict):
        budget.reserve("holdout")
    assert budget.summary()["used"] == 30
    assert len([budget.reserve("interactive") for _ in range(10)]) == 10
    with pytest.raises(Conflict):
        budget.reserve("interactive")


def test_invalid_model_candidate_is_preserved_without_exposing_active_key(tmp_path, monkeypatch):
    key = "offline-key-never-real"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)

    class CandidateResponse:
        status_code = 200
        content = b"offline invalid output fixture"

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"invalid": "debug echoed " + key, "answer": "bad schema"})}}]}

    class CandidateTransport:
        def post(self, url, **kwargs):
            return CandidateResponse()

    budget = RequestBudget(tmp_path / "budget.sqlite3")
    model = DeepSeekModel(budget, transport=CandidateTransport())
    with pytest.raises(ModelError):
        model.generate("offline candidate fixture", [])
    assert model.last_raw_output is not None
    assert "bad schema" in model.last_raw_output
    assert key not in model.last_raw_output
    assert budget.records()[0]["status"] == "failed"
    assert budget.records()[0]["prompt_tokens"] is None
    assert budget.records()[0]["completion_tokens"] is None
