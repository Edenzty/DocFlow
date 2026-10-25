"""Heading-aware local retrieval, with an explicit, reproducible vector index.

BM25 scores and cosine similarities are deliberately separate quantities.  Neither
is a probability that an answer is correct.  Model downloads happen only in
``VectorRetriever.build``; serving/searching uses the local model cache only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import threading
from typing import Any


BGE_MODEL = "BAAI/bge-small-zh-v1.5"
# Immutable upstream revision, verified from the official model API on 2026-10-25.
BGE_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
MAX_CHUNK_CHARS = 220
CHUNK_OVERLAP = 32
INDEX_SCHEMA = "docflow-heading-char220-overlap32-v1"
REQUIRED_METADATA = ("document_id", "title", "source", "version", "kind")
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_MODEL_LOCK = threading.RLock()
_STOP_WORDS = frozenset("的 了 是 我 你 他 她 它 我们 你们 他们 请 请问 怎么 如何 为什么 什么 哪些 这个 那个 一个 一下 吗 呢 和 与 或 在 对 将 把 有 没有 可以 能否 是否".split())


class RetrievalError(RuntimeError):
    """An actionable retrieval/index failure, without a disguised fallback."""


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    title: str
    section: str
    source: str
    version: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def corpus_fingerprint(chunks: list[Chunk]) -> str:
    """Fingerprint metadata as well as text, so version/source changes invalidate."""
    return _digest({"schema": INDEX_SCHEMA, "chunks": [chunk.as_dict() for chunk in chunks]})


def _metadata_and_body(path: Path) -> tuple[dict[str, str], str]:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    lines = content.splitlines()
    start = 1 if lines and lines[0].strip() == "---" else 0
    try:
        separator = next(i for i in range(start, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise RetrievalError(f"{path.name}: 缺少元数据结束行 ---。") from exc
    metadata: dict[str, str] = {}
    for line in lines[start:separator]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, delimiter, value = line.partition(":")
        if not delimiter or not key.strip():
            raise RetrievalError(f"{path.name}: 元数据必须采用 key: value 格式。")
        key = key.strip()
        if key in metadata:
            raise RetrievalError(f"{path.name}: 元数据 {key} 重复。")
        metadata[key] = value.strip().strip('"\'')
    missing = [key for key in REQUIRED_METADATA if not metadata.get(key)]
    if missing:
        raise RetrievalError(f"{path.name}: 缺少元数据 {', '.join(missing)}。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", metadata["document_id"]):
        raise RetrievalError(f"{path.name}: document_id 只能包含字母、数字、点、下划线和短横线。")
    body = "\n".join(lines[separator + 1 :]).strip()
    if not body:
        raise RetrievalError(f"{path.name}: 正文为空。")
    return metadata, body


def _sections(body: str, title: str) -> list[tuple[str, str]]:
    """Preserve exact heading labels; fenced-code headings remain ordinary text."""
    result: list[tuple[str, str]] = []
    heading, buffer = title, []
    fence: str | None = None
    for line in body.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            fence_char = marker.group(1)[0]
            if fence is None:
                fence = fence_char
            elif fence == fence_char:
                fence = None
            buffer.append(line)
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if match:
            text = "\n".join(buffer).strip()
            if text:
                result.append((heading, text))
            heading, buffer = match.group(1).strip(), []
        else:
            buffer.append(line)
    text = "\n".join(buffer).strip()
    if text:
        result.append((heading, text))
    return result


def _windows(text: str) -> list[str]:
    """Conservative character windows; the real tokenizer is checked before encode."""
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + MAX_CHUNK_CHARS, len(text))
        # Prefer a nearby sentence/paragraph boundary, keeping the overlap exact.
        if end < len(text):
            boundaries = [text.rfind(char, start + MAX_CHUNK_CHARS // 2, end) for char in "\n。！？；"]
            boundary = max(boundaries)
            if boundary >= start + MAX_CHUNK_CHARS // 2:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return pieces


def load_chunks(knowledge_dir: Path) -> list[Chunk]:
    """Read Markdown/text documents with required provenance, never arbitrary files."""
    directory = Path(knowledge_dir)
    paths = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in {".md", ".txt"}) if directory.is_dir() else []
    if not paths:
        raise RetrievalError(f"知识库为空：{directory}。请先整理带元数据的 .md/.txt 文档。")
    chunks: list[Chunk] = []
    documents: set[str] = set()
    for path in paths:
        metadata, body = _metadata_and_body(path)
        document_id = metadata["document_id"]
        if document_id in documents:
            raise RetrievalError(f"document_id 重复：{document_id}。")
        documents.add(document_id)
        for section_number, (section, text) in enumerate(_sections(body, metadata["title"])):
            for window_number, window in enumerate(_windows(text)):
                identity = {"metadata": metadata, "section": section, "section_number": section_number, "window": window_number, "text": window, "schema": INDEX_SCHEMA}
                chunk_id = f"{document_id}-{_digest(identity)[:16]}"
                chunks.append(Chunk(chunk_id, document_id, metadata["title"], section, metadata["source"], metadata["version"], window))
    if not chunks:
        raise RetrievalError("知识库没有可检索的正文片段。")
    return chunks


def _check_search(query: str, top_k: int) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空。")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须为 1 到 20 的整数。")
    return query.strip()


class KeywordRetriever:
    """Jieba search segmentation + positive-IDF BM25 (k1=1.5, b=0.75)."""

    def __init__(self, chunks: list[Chunk]):
        if not chunks:
            raise ValueError("chunks 不能为空。")
        try:
            import jieba
        except ImportError as exc:
            raise RetrievalError("缺少 jieba；请在项目环境执行 pip install -e .。") from exc
        self.chunks = list(chunks)
        self.fingerprint = corpus_fingerprint(self.chunks)
        self._tokenizer = jieba.Tokenizer()
        jieba.setLogLevel(logging.WARNING)
        self._documents = [Counter(self._tokens(f"{c.title}\n{c.section}\n{c.text}")) for c in chunks]
        self._lengths = [sum(document.values()) for document in self._documents]
        self._average_length = sum(self._lengths) / len(self._lengths) or 1.0
        self._df = Counter(token for document in self._documents for token in document)

    def _tokens(self, text: str) -> list[str]:
        return [token for token in self._tokenizer.cut_for_search(text.lower(), HMM=False) if re.fullmatch(r"[a-z0-9_]+|[\u3400-\u9fff]+", token) and token not in _STOP_WORDS]

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        query = _check_search(query, top_k)
        tokens = set(self._tokens(query))
        count = len(self.chunks)
        scored: list[tuple[float, Chunk]] = []
        for chunk, frequencies, length in zip(self.chunks, self._documents, self._lengths, strict=True):
            score = 0.0
            for token in tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                idf = math.log(1 + (count - self._df[token] + 0.5) / (self._df[token] + 0.5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / self._average_length))
            scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [{**chunk.as_dict(), "score": round(score, 8), "method": "keyword"} for score, chunk in scored[:top_k]]


class VectorRetriever:
    """BGE Chinese CPU embeddings stored in a corpus/model-specific Chroma index.

    ``embedder`` is an explicitly labelled offline-test seam; application code must
    omit it.  A fake is never reported as a real BGE embedding run.
    """

    def __init__(self, chunks: list[Chunk], persist_dir: Path, model_name: str = BGE_MODEL, model_path: str | None = None, *, embedder: Any | None = None):
        if not chunks:
            raise ValueError("chunks 不能为空。")
        if model_name != BGE_MODEL and embedder is None:
            raise ValueError(f"首版固定使用 {BGE_MODEL}；本地镜像请使用 model_path。")
        self.chunks = list(chunks)
        self.persist_dir = Path(persist_dir)
        self.model_name = model_name
        self.model_path = model_path
        self._embedder = embedder
        self._injected = embedder is not None
        self._client: Any | None = None
        self._collection: Any | None = None
        self._by_id = {chunk.chunk_id: chunk for chunk in chunks}
        if len(self._by_id) != len(chunks):
            raise ValueError("chunk_id 重复。")
        self.corpus_hash = corpus_fingerprint(self.chunks)
        self.model_identity = self._model_identity()
        self.fingerprint = _digest({"corpus": self.corpus_hash, "model": self.model_identity, "document_input": "title-section-text-v1", "query_instruction": QUERY_INSTRUCTION, "normalization": True, "metric": "cosine"})
        self.collection_name = f"docflow_{self.fingerprint[:32]}"

    def _model_identity(self) -> str:
        if self._injected:
            identity = getattr(self._embedder, "identity", None)
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("离线注入 embedder 必须提供非空 identity，避免不同假模型共用索引。")
            return f"injected_test_double:{identity}"
        if not self.model_path:
            return f"{self.model_name}@{BGE_REVISION}"
        path = Path(self.model_path)
        if not path.is_dir():
            raise RetrievalError("DOCFLOW_EMBEDDING_MODEL_PATH 不是可读的本地模型目录。")
        files = sorted(file for file in path.rglob("*") if file.is_file() and file.suffix in {".json", ".txt", ".safetensors", ".bin"})
        if not any(file.suffix in {".safetensors", ".bin"} for file in files):
            raise RetrievalError("本地模型目录缺少模型权重，请下载完整 BGE 模型。")
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.relative_to(path).as_posix().encode("utf-8"))
            with file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        return f"{self.model_name}:local-sha256:{digest.hexdigest()}"

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import chromadb
                from chromadb.config import Settings
            except ImportError as exc:
                raise RetrievalError("缺少 Chroma；请在项目环境执行 pip install -e .。") from exc
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir), settings=Settings(anonymized_telemetry=False))
        return self._client

    def _find_ready(self) -> Any | None:
        if not self.persist_dir.exists():
            return None
        try:
            collection = self._get_client().get_collection(name=self.collection_name, embedding_function=None)
        except Exception as exc:
            if type(exc).__name__ in {"NotFoundError", "InvalidCollectionException"} or isinstance(exc, ValueError):
                return None
            raise RetrievalError(f"无法读取 Chroma 索引（{type(exc).__name__}）；请检查目录权限后重建索引。") from exc
        metadata = collection.metadata or {}
        if metadata.get("status") != "ready" or metadata.get("fingerprint") != self.fingerprint or collection.count() != len(self.chunks):
            return None
        actual_ids = set(collection.get(include=[])["ids"])
        return collection if actual_ids == set(self._by_id) else None

    @property
    def ready(self) -> bool:
        """Check index identity/completeness without importing/loading the model."""
        self._collection = self._find_ready()
        return self._collection is not None

    def is_ready(self) -> bool:
        return self.ready

    def _load_model(self, *, allow_download: bool) -> Any:
        if self._embedder is not None:
            return self._embedder
        cache_key = (self.model_identity, "cpu")
        with _MODEL_LOCK:
            if cache_key not in _MODEL_CACHE:
                try:
                    from sentence_transformers import SentenceTransformer
                    import torch
                    torch.set_num_threads(4)
                    kwargs: dict[str, Any] = {"device": "cpu", "trust_remote_code": False, "local_files_only": not allow_download}
                    if not self.model_path:
                        kwargs["revision"] = BGE_REVISION
                    _MODEL_CACHE[cache_key] = SentenceTransformer(self.model_path or self.model_name, **kwargs)
                except Exception as exc:
                    raise RetrievalError(f"BGE 模型加载失败（{type(exc).__name__}）。先显式运行 build-index 下载模型，或设置 DOCFLOW_EMBEDDING_MODEL_PATH；搜索不会自动下载。") from exc
            self._embedder = _MODEL_CACHE[cache_key]
        return self._embedder

    def _encode(self, texts: list[str], *, allow_download: bool) -> list[list[float]]:
        model = self._load_model(allow_download=allow_download)
        tokenizer = getattr(model, "tokenizer", None)
        limit = min(int(getattr(model, "max_seq_length", 512)), 512)
        if tokenizer is not None:
            lengths = tokenizer(texts, add_special_tokens=True, truncation=False, padding=False)["input_ids"]
            if any(len(ids) > limit for ids in lengths):
                raise RetrievalError(f"片段或问题超过 BGE 的 {limit} token 上限；请缩短问题或切分片段，未执行静默截断。")
        elif not self._injected:
            raise RetrievalError("BGE tokenizer 不可用，无法保证片段未被截断。")
        with _MODEL_LOCK:
            result = model.encode(texts, batch_size=16, show_progress_bar=False, normalize_embeddings=True, convert_to_numpy=True)
        values = result.tolist() if hasattr(result, "tolist") else result
        if len(values) != len(texts) or not values or any(not vector or any(not math.isfinite(float(value)) for value in vector) for vector in values):
            raise RetrievalError("向量模型返回空向量、非有限值或数量不匹配。")
        return [[float(value) for value in vector] for vector in values]

    def build(self) -> dict[str, Any]:
        """Explicit opt-in to model download/build; incomplete indexes stay unusable."""
        if self.ready:
            return self.info(reused=True)
        # Keep short fragments intelligible even when they start with "this case".
        # Titles/sections remain provenance too; the tokenizer checks the FULL input.
        vectors = self._encode([f"{chunk.title}\n{chunk.section}\n{chunk.text}" for chunk in self.chunks], allow_download=True)
        client = self._get_client()
        try:
            client.delete_collection(name=self.collection_name)
        except Exception as exc:
            if type(exc).__name__ not in {"NotFoundError", "InvalidCollectionException"} and not isinstance(exc, ValueError):
                raise RetrievalError(f"无法替换不完整索引（{type(exc).__name__}）。") from exc
        metadata = {"status": "building", "fingerprint": self.fingerprint, "corpus_hash": self.corpus_hash, "model_identity": self.model_identity, "schema": INDEX_SCHEMA, "hnsw:space": "cosine"}
        collection = client.create_collection(name=self.collection_name, metadata=metadata, embedding_function=None)
        # Small batches also work with lower SQLite variable-count builds.
        for start in range(0, len(self.chunks), 64):
            batch = self.chunks[start : start + 64]
            collection.add(ids=[chunk.chunk_id for chunk in batch], embeddings=vectors[start : start + 64], metadatas=[{key: value for key, value in chunk.as_dict().items() if key not in {"chunk_id", "text"}} for chunk in batch])
        # Chroma forbids even repeating hnsw:space in modify(); the immutable
        # distance configuration was already persisted when creating the collection.
        ready_metadata = {key: value for key, value in metadata.items() if not key.startswith("hnsw:")}
        collection.modify(metadata={**ready_metadata, "status": "ready"})
        self._collection = self._find_ready()
        if self._collection is None:
            raise RetrievalError("Chroma 入库校验失败；索引没有被标记为可用。")
        return self.info(reused=False)

    def info(self, *, reused: bool = False) -> dict[str, Any]:
        return {"collection": self.collection_name, "fingerprint": self.fingerprint, "corpus_hash": self.corpus_hash, "chunks": len(self.chunks), "model": self.model_name, "model_identity": self.model_identity, "embedding_mode": "injected_test_double" if self._injected else "real_bge_cpu", "metric": "cosine", "reused": reused}

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        query = _check_search(query, top_k)
        if self._collection is None and not self.ready:
            raise RetrievalError("向量索引尚未构建，或知识库/模型已变化。请先显式运行 build-index；不会回退到关键词检索。")
        vector = self._encode([QUERY_INSTRUCTION + query], allow_download=False)[0]
        result = self._collection.query(query_embeddings=[vector], n_results=min(top_k, len(self.chunks)), include=["distances"])
        values = []
        for chunk_id, distance in zip(result["ids"][0], result["distances"][0], strict=True):
            if chunk_id not in self._by_id:
                raise RetrievalError("Chroma 返回未知片段；请重建索引。")
            values.append({**self._by_id[chunk_id].as_dict(), "score": round(1.0 - float(distance), 8), "method": "vector"})
        return values
