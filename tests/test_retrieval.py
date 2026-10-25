"""Offline retrieval checks.  Fake embeddings below are never quality evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from docflow_lab.retrieval import (
    BGE_MODEL,
    MAX_CHUNK_CHARS,
    QUERY_INSTRUCTION,
    Chunk,
    KeywordRetriever,
    RetrievalError,
    VectorRetriever,
    corpus_fingerprint,
    load_chunks,
)


def write_doc(directory: Path, name: str = "authentication", body: str | None = None, *, version: str = "2.0", frontmatter: bool = False, extension: str = "md") -> Path:
    path = directory / f"{name}.{extension}"
    path.write_text(("---\n" if frontmatter else "") + f"document_id: {name}\ntitle: 身份认证指南\nsource: EdgeSecLab/docs/WEEK2_GUIDE.md\nversion: {version}\nkind: guide\n---\n" + (body or "# 身份认证指南\n## 401 身份校验失败\n缺少身份信息或签名错误时返回401。检查 HMAC 签名和请求头。\n## 403 归属校验失败\n身份正确但访问其他设备时返回403。设备A不能访问设备B的数据。\n"), encoding="utf-8")
    return path


def test_load_retains_metadata_exact_sections_and_stable_ids(tmp_path):
    write_doc(tmp_path)
    chunks = load_chunks(tmp_path)
    assert len(chunks) == 2
    assert chunks[0].section == "401 身份校验失败"
    assert chunks[1].section == "403 归属校验失败"
    assert chunks[0].source == "EdgeSecLab/docs/WEEK2_GUIDE.md"
    assert chunks[0].version == "2.0"
    assert chunks[0].document_id == "authentication"
    assert load_chunks(tmp_path) == chunks
    assert set(chunks[0].as_dict()) == {"chunk_id", "document_id", "title", "section", "source", "version", "text"}


def test_yaml_style_frontmatter_and_text_supported_but_unrelated_files_ignored(tmp_path):
    write_doc(tmp_path, frontmatter=True, extension="txt")
    (tmp_path / "untrusted.json").write_text("not a document", encoding="utf-8")
    assert len(load_chunks(tmp_path)) == 2


def test_long_sections_have_bounded_overlapping_windows(tmp_path):
    text = "这是设备身份认证的排查文档，先检查请求头，再检查签名和设备归属。" * 30
    write_doc(tmp_path, body=f"## 长章节\n{text}")
    chunks = load_chunks(tmp_path)
    assert len(chunks) > 3
    assert all(0 < len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert len(set(chunk.chunk_id for chunk in chunks)) == len(chunks)
    assert chunks[0].text[-32:] == chunks[1].text[:32]
    # Consecutive overlap must not lose any original section text.
    reconstructed = chunks[0].text + "".join(chunk.text[32:] for chunk in chunks[1:])
    assert reconstructed == text


def test_fenced_code_does_not_create_a_heading(tmp_path):
    write_doc(tmp_path, body="## 示例\n正常文本。\n```python\n# 这不是章节\nprint('hello')\n```\n## 结论\n签名错误返回401。")
    chunks = load_chunks(tmp_path)
    assert [chunk.section for chunk in chunks] == ["示例", "结论"]
    assert "# 这不是章节" in chunks[0].text


@pytest.mark.parametrize("change", ["text", "version", "source"])
def test_index_fingerprint_changes_for_content_or_provenance(tmp_path, change):
    path = write_doc(tmp_path)
    original = load_chunks(tmp_path)
    text = path.read_text(encoding="utf-8")
    replacements = {"text": ("签名错误", "签名不匹配"), "version": ("version: 2.0", "version: 2.1"), "source": ("WEEK2_GUIDE.md", "CURRENT_GUIDE.md")}
    path.write_text(text.replace(*replacements[change]), encoding="utf-8")
    changed = load_chunks(tmp_path)
    assert corpus_fingerprint(original) != corpus_fingerprint(changed)
    assert {chunk.chunk_id for chunk in original} != {chunk.chunk_id for chunk in changed}


def test_duplicate_document_ids_and_missing_metadata_are_rejected(tmp_path):
    path = write_doc(tmp_path)
    (tmp_path / "duplicate.md").write_bytes(path.read_bytes())
    with pytest.raises(RetrievalError, match="document_id 重复"):
        load_chunks(tmp_path)
    (tmp_path / "duplicate.md").unlink()
    path.write_text("document_id: incomplete\n---\n有正文", encoding="utf-8")
    with pytest.raises(RetrievalError, match="缺少元数据"):
        load_chunks(tmp_path)


def test_empty_library_and_bad_metadata_fail_actionably(tmp_path):
    with pytest.raises(RetrievalError, match="知识库为空"):
        load_chunks(tmp_path)
    (tmp_path / "bad.md").write_text("not metadata", encoding="utf-8")
    with pytest.raises(RetrievalError, match="缺少元数据结束行"):
        load_chunks(tmp_path)


def test_keyword_bm25_distinguishes_chinese_authentication_and_authorization(tmp_path):
    write_doc(tmp_path)
    chunks = load_chunks(tmp_path)
    chunks += [Chunk("unrelated", "report", "报告导出", "CSV格式", "original FAQ", "1.0", "使用导出命令生成CSV报告，字段包括request_id和状态码。")]
    retriever = KeywordRetriever(chunks)
    signature = retriever.search("401签名错误该检查什么？")
    permission = retriever.search("403访问其他设备时归属校验失败")
    assert signature[0]["section"] == "401 身份校验失败"
    assert permission[0]["section"] == "403 归属校验失败"
    assert signature[0]["score"] > signature[-1]["score"]
    assert all(result["method"] == "keyword" for result in signature)
    assert len(signature) == 3


def test_keyword_zero_overlap_is_explicit_zero_not_semantic_confidence(tmp_path):
    write_doc(tmp_path)
    results = KeywordRetriever(load_chunks(tmp_path)).search("火星咖啡豆价格")
    assert len(results) == 2
    assert all(result["score"] == 0 for result in results)


@pytest.mark.parametrize("query,top_k", [(" ", 5), ("有效", 0), ("有效", 21), ("有效", True)])
def test_search_rejects_invalid_input(tmp_path, query, top_k):
    write_doc(tmp_path)
    with pytest.raises(ValueError):
        KeywordRetriever(load_chunks(tmp_path)).search(query, top_k)


class FakeEmbedder:
    """Two dimensions encode fixture labels only; NOT a BGE quality experiment."""

    identity = "offline-fixture-401-403-v1"
    max_seq_length = 512

    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append(list(texts))
        return [[1.0, 0.0] if "401" in text else [0.0, 1.0] for text in texts]


def vector_fixture(tmp_path):
    pytest.importorskip("chromadb")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    write_doc(knowledge)
    chunks = load_chunks(knowledge)
    fake = FakeEmbedder()
    return chunks, fake, VectorRetriever(chunks, tmp_path / "vectors", embedder=fake)


def test_vector_requires_explicit_build_and_preserves_cosine_evidence(tmp_path):
    chunks, fake, retriever = vector_fixture(tmp_path)
    assert not retriever.ready
    assert not fake.calls
    with pytest.raises(RetrievalError, match="先显式运行 build-index"):
        retriever.search("401签名失败")
    assert not fake.calls
    info = retriever.build()
    assert info["embedding_mode"] == "injected_test_double"
    assert "injected_test_double" in info["model_identity"]
    assert retriever.ready
    results = retriever.search("401签名失败")
    assert results[0]["chunk_id"] == chunks[0].chunk_id
    assert results[0]["score"] == pytest.approx(1.0)
    assert results[0]["method"] == "vector"
    assert fake.calls[-1] == [QUERY_INSTRUCTION + "401签名失败"]


def test_vector_reuses_complete_index_after_restart_without_reencoding(tmp_path):
    chunks, fake, retriever = vector_fixture(tmp_path)
    retriever.build()
    restarted_fake = FakeEmbedder()
    restarted = VectorRetriever(chunks, retriever.persist_dir, embedder=restarted_fake)
    assert restarted.ready
    assert restarted.build()["reused"] is True
    assert not restarted_fake.calls
    assert restarted.search("403跨设备访问")[0]["section"] == "403 归属校验失败"
    assert len(restarted_fake.calls) == 1


def test_changed_corpus_or_embedder_cannot_silently_reuse_old_vectors(tmp_path):
    chunks, fake, retriever = vector_fixture(tmp_path)
    retriever.build()
    changed = [replace(chunks[0], text=chunks[0].text + "更新文本。"), chunks[1]]
    changed_retriever = VectorRetriever(changed, retriever.persist_dir, embedder=FakeEmbedder())
    assert changed_retriever.collection_name != retriever.collection_name
    assert not changed_retriever.ready
    with pytest.raises(RetrievalError, match="知识库/模型已变化"):
        changed_retriever.search("401")
    other_fake = FakeEmbedder()
    other_fake.identity = "different-test-vector-model"
    assert VectorRetriever(chunks, retriever.persist_dir, embedder=other_fake).collection_name != retriever.collection_name


def test_partial_index_is_never_ready_and_build_can_recover(tmp_path):
    chunks, fake, retriever = vector_fixture(tmp_path)
    retriever.build()
    retriever._collection.delete(ids=[chunks[0].chunk_id])
    restarted = VectorRetriever(chunks, retriever.persist_dir, embedder=FakeEmbedder())
    assert not restarted.ready
    restarted.build()
    assert restarted.ready
    assert len(restarted.search("401")) == 2


def test_real_vector_loading_is_lazy_offline_and_cpu_without_download(tmp_path, monkeypatch):
    """Inspect constructor options using a fake module, NOT a real model claim."""
    import sys
    import types
    import docflow_lab.retrieval as module

    module._MODEL_CACHE.clear()
    chunks = [Chunk("c1", "d1", "标题", "章节", "original", "1", "401签名失败")]
    constructors = []

    class PretendSentenceTransformer(FakeEmbedder):
        def __init__(self, name, **kwargs):
            super().__init__()
            constructors.append((name, kwargs))

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=PretendSentenceTransformer))
    retriever = VectorRetriever(chunks, tmp_path / "unused")
    assert not constructors
    retriever._load_model(allow_download=False)
    second = VectorRetriever(chunks, tmp_path / "unused")
    second._load_model(allow_download=False)
    assert len(constructors) == 1
    assert constructors[0][0] == BGE_MODEL
    assert constructors[0][1]["device"] == "cpu"
    assert constructors[0][1]["local_files_only"] is True
    assert constructors[0][1]["trust_remote_code"] is False
    module._MODEL_CACHE.clear()


def test_tokenizer_overflow_is_rejected_before_encoding(tmp_path):
    class TokenizerFake(FakeEmbedder):
        identity = "overflow-test-double"
        max_seq_length = 5

        def tokenizer(self, texts, **kwargs):
            assert kwargs["truncation"] is False
            return {"input_ids": [[1] * 6 for _ in texts]}

    fake = TokenizerFake()
    chunks = [Chunk("c1", "d1", "标题", "章节", "original", "1", "超长输入")]
    retriever = VectorRetriever(chunks, tmp_path / "unused", embedder=fake)
    with pytest.raises(RetrievalError, match="未执行静默截断"):
        retriever._encode(["超长输入"], allow_download=False)
    assert not fake.calls
