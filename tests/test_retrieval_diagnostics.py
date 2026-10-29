"""Ranking examples exercise distinct annotation, duplicate and failure cases."""
import copy
import json
from pathlib import Path

import pytest

from docflow_lab.config import Settings
from docflow_lab.retrieval_diagnostics import analyze_saved, diagnose_report, rank_metrics, summarize


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "docs/examples/v2-retrieval-01/retrieval.json"


def section(name):
    return {"document_id": "manual", "section": name}


def test_repeated_section_does_not_inflate_precision_or_ndcg_and_keeps_original_rank():
    score = rank_metrics([section("wrong"), section("a"), section("a"), section("b")],
                         [section("a"), section("b")])
    assert score["annotated_section_precision"] == pytest.approx(2 / 3)
    assert score["target_ranks"] == [2, 4]
    assert score["duplicate_section_slots"] == 1
    assert score["reciprocal_rank_at_5"] == .5
    import math
    assert score["ndcg_at_5"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3)))


def test_perfect_rank_and_later_rank_have_same_recall_but_different_ndcg():
    first = rank_metrics([section("a"), section("wrong")], [section("a")])
    second = rank_metrics([section("wrong"), section("a")], [section("a")])
    assert first["recall_at_5"] == second["recall_at_5"] == 1
    assert first["ndcg_at_5"] == 1
    assert second["ndcg_at_5"] < first["ndcg_at_5"]


def test_empty_and_failed_queries_stay_in_macro_denominator_but_no_gold_is_excluded():
    hit = rank_metrics([section("a")], [section("a")])
    empty = rank_metrics([], [section("a")])
    failed = rank_metrics([], [section("a")], failed=True)
    no_gold = rank_metrics([section("a")], [])
    assert empty["annotated_section_precision"] is None
    assert no_gold["ndcg_at_5"] is None
    assert no_gold["outcome"] == "no_gold_not_scored"
    result = summarize([hit, empty, failed, no_gold])
    assert result["questions_with_gold"] == 3
    assert result["macro_annotated_section_precision"] == pytest.approx(1 / 3)
    assert result["mean_ndcg_at_5"] == pytest.approx(1 / 3)
    assert result["undefined_empty_precision_count"] == 2
    assert result["failed_queries"] == 1
    assert result["no_gold_questions"] == 1
    assert summarize([no_gold])["mean_ndcg_at_5"] is None


def test_rank_metrics_rejects_extended_context_or_failed_rankings():
    with pytest.raises(ValueError, match="Top5"):
        rank_metrics([section("a")] * 6, [section("a")])
    with pytest.raises(ValueError, match="失败"):
        rank_metrics([section("a")], [section("a")], failed=True)


def test_actual_archived_rankings_are_rescored_against_labels_not_saved_scores(tmp_path):
    report = json.loads(SAVED.read_text(encoding="utf-8"))
    report["summary"] = {"keyword": {"micro_recall_at_5": -100}}
    report["rows"][0]["retrieval"]["keyword"]["recalled_sections"] = -100
    source = tmp_path / "source.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    result = analyze_saved(Settings(root=ROOT), source, tmp_path / "out")
    assert result["model_requests"] == 0
    assert result["summary"]["keyword"]["expected_sections"] == 29
    assert result["summary"]["keyword"]["matched_sections"] == 28
    assert result["summary"]["keyword"]["questions_with_gold"] == 26
    assert (tmp_path / "out/report.md").is_file()
    assert len(result["source_sha256"]) == 64
    with pytest.raises(ValueError, match="已存在"):
        analyze_saved(Settings(root=ROOT), source, tmp_path / "out")


@pytest.mark.parametrize("mutation,match", [
    (lambda r: r.update(corpus_sha256="different"), "哈希"),
    (lambda r: r.update(suite=[]), "suite"),
    (lambda r: r["rows"].pop(), "精确覆盖"),
    (lambda r: r["rows"].append(copy.deepcopy(r["rows"][0])), "精确覆盖"),
    (lambda r: r["rows"][0].update(evidence=[]), "标注"),
    (lambda r: r["rows"][0].update(question="改成另一道题"), "question"),
    (lambda r: r["rows"][0].update(expected_decision="different"), "expected_decision"),
    (lambda r: r["rows"][0]["retrieval"]["keyword"]["results"][0].update(chunk_id="not-real"), "未知"),
    (lambda r: r["rows"][0]["retrieval"]["keyword"]["results"].__setitem__(1, copy.deepcopy(r["rows"][0]["retrieval"]["keyword"]["results"][0])), "重复"),
    (lambda r: r["rows"][0]["retrieval"]["keyword"]["results"][0].update(section="false"), "不匹配"),
    (lambda r: r["rows"][0]["retrieval"]["keyword"].update(failed="false"), "布尔"),
    (lambda r: r["rows"].__setitem__(0, None), "精确覆盖"),
    (lambda r: r["rows"][0].update(retrieval=[]), "三路"),
    (lambda r: r["rows"][0]["retrieval"]["keyword"]["results"][0].update(chunk_id=[]), "未知"),
])
def test_untrusted_saved_report_cannot_silently_change_labels_or_rankings(mutation, match):
    report = json.loads(SAVED.read_text(encoding="utf-8"))
    mutation(report)
    with pytest.raises(ValueError, match=match):
        diagnose_report(Settings(root=ROOT), report)
