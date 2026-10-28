"""Paired evaluation uses fixed material, isolates labels and counts failures."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import requests

from docflow_lab.config import Settings
import docflow_lab.context_comparison as comparison
from docflow_lab.context_comparison import ARMS, run_comparison, summarize_review
from docflow_lab.model import MockModel


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path):
    return Settings(root=ROOT, data_dir=tmp_path / "unused-state")


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline comparison attempted network IO")
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


class SpyFixture:
    calls = []

    def generate(self, question, evidence):
        self.calls.append((question, evidence))
        return MockModel().generate(question, evidence)


def test_paired_contexts_have_same_question_schema_no_labels_and_no_budget(settings, tmp_path):
    SpyFixture.calls.clear()
    out = tmp_path / "compare"
    budget_existed = settings.budget_path.exists()
    result = run_comparison(settings, out, limit=2, fixture_factory=SpyFixture)
    assert result["mode"] == "offline_deterministic_fixture"
    assert result["provider_requests"] == 0 and result["not_real_model_evaluation"] is True
    assert len(SpyFixture.calls) == 4
    assert [question for question, _ in SpyFixture.calls] == [result["rows"][i]["question"] for i in range(4)]
    assert SpyFixture.calls[0][0] == SpyFixture.calls[1][0]
    assert result["corpus_chunk_count"] == 71
    assert len(SpyFixture.calls[0][1]) == result["full_section_count"] == 41
    assert sum(len(section["text"]) for section in SpyFixture.calls[0][1]) == 10452
    assert max(len(section["text"]) for section in SpyFixture.calls[0][1]) > 220
    assert all("-section-" in item["chunk_id"] for item in SpyFixture.calls[0][1])
    assert len(SpyFixture.calls[1][1]) == 5
    allowed = {"chunk_id", "document_id", "title", "section", "source", "version", "text"}
    assert all(set(item) == allowed for _, evidence in SpyFixture.calls for item in evidence)
    assert all(r["semantic_review"] == "pending_human_review" for r in result["rows"])
    assert all(r["provider_usage"] is None for r in result["rows"])
    assert settings.budget_path.exists() == budget_existed
    for arm in ARMS:
        metrics = result["summary"][arm]
        assert metrics["decision_match_all_planned"]["denominator"] == 2
        assert metrics["prompt_tokens"] is None and metrics["provider_requests"] == 0
    assert (out / "review_template.json").exists()


def test_failures_count_in_denominator_and_other_arm_still_runs(settings, tmp_path):
    class OneArmFails:
        def generate(self, question, evidence):
            if len(evidence) > 5:
                raise RuntimeError("should not be printed, possibly sensitive")
            return MockModel().generate(question, evidence)
    result = run_comparison(settings, tmp_path / "failed", limit=1, fixture_factory=OneArmFails)
    a, b = result["rows"]
    assert a["status"] == "failed" and a["error"] == "fixture_or_validation_failed:RuntimeError"
    assert b["status"] == "completed"
    assert result["summary"]["full_sections"]["decision_match_all_planned"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert result["summary"]["full_sections"]["false_refusal_on_completed_answerable"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert result["summary"]["full_sections"]["answerable_uncompleted"] == 1
    assert result["summary"]["bm25_top5"]["completed"] == 1


def test_refusal_denominators_do_not_hide_failed_answerable_or_out_of_scope(settings, tmp_path):
    class FullContextFails:
        def generate(self, question, evidence):
            if len(evidence) > 5:
                raise RuntimeError("test only")
            return MockModel().generate(question, evidence)
    report = run_comparison(settings, tmp_path / "refusal-denominators", limit=10, fixture_factory=FullContextFails)
    metrics = report["summary"]["full_sections"]
    assert metrics["out_of_scope_match_planned"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert metrics["out_of_scope_match_completed"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert metrics["out_of_scope_uncompleted"] == 1
    assert metrics["false_refusal_on_completed_answerable"]["denominator"] == 0
    assert metrics["answerable_uncompleted"] == 7


def test_review_pending_does_not_become_zero_or_perfect_and_requires_matching_report(settings, tmp_path):
    out = tmp_path / "review"
    run_comparison(settings, out, limit=1)
    report, review_path = out / "comparison.json", out / "review_template.json"
    pending = summarize_review(report, review_path)
    assert pending["arms"]["full_sections"]["citation_support"] == {"numerator": 0, "denominator": 0, "rate": None}
    review = json.loads(review_path.read_text(encoding="utf-8"))
    first = review["rows"][0]
    first.update(status="done", reviewer="reviewer-1", citation_support="unsupported", fact_coverage="incomplete", notes="example manual review")
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    checked = summarize_review(report, review_path)
    assert checked["arms"]["full_sections"]["citation_support"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert checked["arms"]["bm25_top5"]["pending_reviews"] == 1
    report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="不匹配"):
        summarize_review(report, review_path)


def test_cannot_overwrite_previous_report(settings, tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    with pytest.raises(ValueError, match="已存在"):
        run_comparison(settings, out, limit=1)
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_retrieval_failure_is_counted_without_calling_that_arm(settings, tmp_path, monkeypatch):
    def fail_search(*args, **kwargs):
        raise RuntimeError("sensitive internal detail")
    monkeypatch.setattr(comparison.KeywordRetriever, "search", fail_search)
    result = run_comparison(settings, tmp_path / "retrieval-fails", limit=1)
    a, b = result["rows"]
    assert a["status"] == "completed"
    assert b["status"] == "failed" and b["error"] == "context_selection_failed:RuntimeError"
    assert b["generation_attempted"] is False
    assert result["summary"]["bm25_top5"]["generation_attempted"] == 0
    assert result["summary"]["bm25_top5"]["decision_match_all_planned"]["denominator"] == 1


def test_cli_rejects_live_flag_before_writing_or_calling_provider(tmp_path):
    output = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, "-m", "docflow_lab", "compare-context", "--live", "--output", str(output)],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "unrecognized arguments" in result.stderr
    assert not output.exists()
