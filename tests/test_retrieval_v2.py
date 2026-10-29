"""Algorithm/contract tests use labelled doubles, never claim BGE quality."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from docflow_lab.retrieval import Chunk, RetrievalError
from docflow_lab.retrieval_v2 import HybridRetriever, _aggregate, _score, expand_evidence


def chunk(identifier, *, document="manual", section=None):
    return Chunk(identifier, document, "技术手册", section or identifier, "original FAQ", "2.0", f"原始证据 {identifier}")


class RankedDouble:
    """Predetermined rankings are only for deterministic fusion tests."""

    def __init__(self, chunks, ranks, *, ready=True):
        self.chunks = chunks
        self.ranks = ranks
        self.ready = ready
        self.calls = []

    def search(self, query, top_k=5):
        self.calls.append((query, top_k))
        corpus = {value.chunk_id: value.as_dict() for value in self.chunks}
        return [{**corpus.get(identifier, {}), "chunk_id": identifier, "score": score}
                for identifier, score in self.ranks[:top_k]]


def hybrid(chunks, keyword, vector):
    return HybridRetriever(chunks, RankedDouble(chunks, keyword), RankedDouble(chunks, vector))


def test_rrf_uses_ranks_and_retains_separate_component_scores():
    chunks = [chunk("a"), chunk("b"), chunk("c")]
    retriever = hybrid(chunks, [("a", 1000000), ("b", 0.01)], [("b", 0.9), ("c", 0.8), ("a", 0.7)])
    result = retriever.search("设备授权")
    assert [value["chunk_id"] for value in result] == ["b", "a", "c"]
    assert result[0]["score"] == pytest.approx(1 / 62 + 1 / 61)
    assert result[0]["ranks"] == {"keyword": 2, "vector": 1}
    assert result[0]["component_scores"] == {"keyword": 0.01, "vector": 0.9}
    assert all(value["method"] == "hybrid" for value in result)
    assert retriever.keyword.calls == [("设备授权", 20)]
    assert retriever.vector.calls == [("设备授权", 20)]
    assert result[0]["text"] == chunks[1].text


def test_zero_bm25_results_do_not_add_artificial_keyword_votes():
    chunks = [chunk("a"), chunk("b")]
    result = hybrid(chunks, [("a", 0), ("b", 0)], [("b", 0.9), ("a", -0.1)]).search("无词汇交集")
    assert result[0]["chunk_id"] == "b"
    assert result[0]["score"] == pytest.approx(1 / 61)
    assert result[0]["ranks"]["keyword"] is None
    assert result[1]["component_scores"]["vector"] == -0.1


def test_deterministic_ties_use_canonical_id_not_input_order():
    chunks = [chunk("b"), chunk("a")]
    result = hybrid(chunks, [("b", 99), ("a", 1)], [("a", 0.9), ("b", 0.8)]).search("问题")
    assert [value["chunk_id"] for value in result] == ["a", "b"]


def test_section_diversity_avoids_crowding_without_fabricating_fillers():
    chunks = [chunk(str(i), section="same") for i in range(5)] + [chunk("other", section="different")]
    ranks = [(value.chunk_id, 10 - index) for index, value in enumerate(chunks)]
    result = hybrid(chunks, ranks, ranks).search("问题")
    assert [value["chunk_id"] for value in result] == ["0", "1", "other"]


def test_section_diversity_is_per_document_not_shared_heading():
    chunks = [chunk(str(i), document=f"doc{i}", section="概述") for i in range(5)]
    ranks = [(value.chunk_id, 10 - i) for i, value in enumerate(chunks)]
    assert len(hybrid(chunks, ranks, ranks).search("问题")) == 5


def test_missing_vector_fails_without_keyword_fallback():
    chunks = [chunk("a")]
    keyword = RankedDouble(chunks, [("a", 3)])
    vector = RankedDouble(chunks, [("a", 1)], ready=False)
    with pytest.raises(RetrievalError, match="不会隐式退回"):
        HybridRetriever(chunks, keyword, vector).search("问题")
    assert not keyword.calls
    assert not vector.calls


def test_vector_error_propagates_instead_of_disguised_fallback():
    chunks = [chunk("a")]
    retriever = hybrid(chunks, [("a", 3)], [("a", 1)])

    def fail(*args, **kwargs):
        raise RetrievalError("损坏索引")

    retriever.vector.search = fail
    with pytest.raises(RetrievalError, match="损坏索引"):
        retriever.search("问题")


@pytest.mark.parametrize("ranks", [[("unknown", 1)], [("a", 1), ("a", 1)], [("a", float("nan"))], [("a", True)]])
def test_malformed_source_results_are_rejected(ranks):
    chunks = [chunk("a")]
    with pytest.raises(RetrievalError):
        hybrid(chunks, ranks, [("a", 1)]).search("问题")


@pytest.mark.parametrize("candidate,constant", [(0, 60), (21, 60), (True, 60), (20, 0), (20, True)])
def test_constructor_rejects_invalid_parameters(candidate, constant):
    chunks = [chunk("a")]
    with pytest.raises(ValueError):
        HybridRetriever(chunks, RankedDouble(chunks, []), RankedDouble(chunks, []), candidate, constant)


def test_mismatched_corpus_and_duplicate_ids_rejected():
    chunks = [chunk("a")]
    with pytest.raises(ValueError, match="完全相同"):
        HybridRetriever(chunks, RankedDouble(chunks, []), RankedDouble([replace(chunks[0], text="changed")], []))
    with pytest.raises(ValueError, match="重复"):
        hybrid(chunks * 2, [], [])


@pytest.mark.parametrize("query,top_k", [("", 5), ("问题", 0), ("问题", True)])
def test_hybrid_query_validation(query, top_k):
    chunks = [chunk("a")]
    with pytest.raises(ValueError):
        hybrid(chunks, [], []).search(query, top_k)


def test_expansion_preserves_actual_ids_text_and_seed_ranking_metadata():
    chunks = [chunk(str(i), section="长章节") for i in range(5)]
    seed = {**chunks[2].as_dict(), "text": "篡改文本", "source": "invented", "score": 0.05, "method": "hybrid"}
    result = expand_evidence(chunks, [seed], max_chunks=3)
    assert [value["chunk_id"] for value in result] == ["2", "1", "3"]
    assert result[0]["text"] == chunks[2].text
    assert result[0]["source"] == chunks[2].source
    assert result[0]["score"] == 0.05
    assert result[1] == chunks[1].as_dict()


def test_expansion_is_same_section_and_same_document_only():
    chunks = [chunk("a", section="same"), chunk("b", section="same"), chunk("c", section="other"), chunk("d", document="other", section="same")]
    assert [value["chunk_id"] for value in expand_evidence(chunks, ["a"])] == ["a", "b"]


def test_expansion_keeps_seed_order_deduplicates_and_respects_bound():
    chunks = [chunk(str(i), section="长章节") for i in range(30)]
    result = expand_evidence(chunks, ["10", "20", "10"])
    assert len(result) == 15
    assert [value["chunk_id"] for value in result[:2]] == ["10", "20"]
    assert len({value["chunk_id"] for value in result}) == 15
    assert [int(value["chunk_id"]) for value in result[2:]] == sorted(int(value["chunk_id"]) for value in result[2:])


def test_expansion_rejects_unknown_ids_or_overflow_instead_of_losing_citations():
    chunks = [chunk("a"), chunk("b")]
    with pytest.raises(RetrievalError, match="未知"):
        expand_evidence(chunks, ["a", "hallucinated"])
    with pytest.raises(ValueError, match="不能静默丢弃"):
        expand_evidence(chunks, ["a", "b"], 1)
    with pytest.raises(ValueError):
        expand_evidence(chunks, ["a"], 16)
    assert expand_evidence(chunks, []) == []


def test_metrics_deduplicate_sections_and_keep_context_separate():
    results = [chunk("a").as_dict(), chunk("b").as_dict(), chunk("c", section="b").as_dict()]
    expected = [{"document_id": "manual", "section": "b"}, {"document_id": "manual", "section": "missing"}]
    score = _score(results, expected, top_five=True)
    assert score["recalled_sections"] == 1
    assert score["recall_at_5"] == 0.5
    assert score["reciprocal_rank_at_5"] == 0.5
    assert score["any_evidence_recalled"]
    assert not score["all_evidence_recalled"]
    context = _score(results, expected, top_five=False)
    assert "context_recall" in context
    assert "recall_at_5" not in context
    assert "reciprocal_rank_at_5" not in context
    assert _score(results, [], top_five=True)["recall_at_5"] is None


def test_evaluator_refuses_invalid_suite_or_existing_output_before_loading(tmp_path):
    from docflow_lab.config import Settings
    from docflow_lab.retrieval_v2 import evaluate_retrieval

    with pytest.raises(ValueError, match="suite"):
        evaluate_retrieval(Settings(root=tmp_path), tmp_path / "new", "unknown")
    with pytest.raises(ValueError, match="已存在"):
        evaluate_retrieval(Settings(root=tmp_path), tmp_path)


def test_aggregate_keeps_failed_query_in_denominator():
    expected = [{"document_id": "manual", "section": "a"}]
    hit = {**_score([chunk("a").as_dict()], expected, top_five=True),
           "expanded_context": _score([chunk("a").as_dict()], expected, top_five=False), "error": None}
    failed = {**_score([], expected, top_five=True),
              "expanded_context": _score([], expected, top_five=False), "error": "broken"}
    score = _aggregate([{"retrieval": {"hybrid": hit}}, {"retrieval": {"hybrid": failed}}], "hybrid")
    assert score["successful_queries"] == 1
    assert score["failed_queries"] == 1
    assert score["expected_sections"] == 2
    assert score["micro_recall_at_5"] == 0.5
    assert score["mrr_at_5"] == 0.5
    assert score["expanded_context"]["micro_recall"] == 0.5


def test_evaluator_exports_all_three_rankings_without_passing_labels_to_search(tmp_path, monkeypatch):
    import json
    import docflow_lab.evaluation as evaluation
    import docflow_lab.retrieval_v2 as module
    import docflow_lab.retrieval_diagnostics as diagnostics

    chunks = [chunk("a"), chunk("b")]
    question = {"id": "dev-example", "question": "唯一问题内容", "expected_decision": "answer",
                "evidence": [{"document_id": "manual", "section": "a"}],
                "required_facts": ["不得进入检索器的标签"], "forbidden_facts": []}
    evaluation_dir = tmp_path / "data" / "evaluation"
    evaluation_dir.mkdir(parents=True)
    (evaluation_dir / "development.json").write_text(json.dumps([question]), encoding="utf-8")
    checked = []
    monkeypatch.setattr(evaluation, "verify_materials", lambda root: checked.append(root))
    monkeypatch.setattr(module, "load_chunks", lambda path: chunks)
    monkeypatch.setattr(diagnostics, "load_chunks", lambda path: chunks)
    keyword = RankedDouble(chunks, [("a", 3), ("b", 0)])
    vector = RankedDouble(chunks, [("b", 0.9), ("a", 0.8)])
    vector.info = lambda: {"embedding_mode": "test_double"}
    monkeypatch.setattr(module, "KeywordRetriever", lambda corpus: keyword)
    monkeypatch.setattr(module, "VectorRetriever", lambda *args, **kwargs: vector)
    settings = SimpleNamespace(root=tmp_path, knowledge_dir=tmp_path / "knowledge", vector_dir=tmp_path / "chroma", model_path=None)
    report = module.evaluate_retrieval(settings, tmp_path / "results", "development")
    assert checked == [tmp_path, tmp_path]  # run preflight and diagnostic provenance check
    assert report["model_requests"] == 0
    assert report["summary"]["keyword"]["micro_recall_at_5"] == 1.0
    assert report["summary"]["vector"]["mrr_at_5"] == 0.5
    assert report["rows"][0]["retrieval"]["hybrid"]["results"][0]["ranks"] == {"keyword": 1, "vector": 2}
    assert (tmp_path / "results" / "retrieval.json").is_file()
    assert (tmp_path / "results" / "report.md").is_file()
    assert all(call[0] == "唯一问题内容" for call in keyword.calls + vector.calls)
