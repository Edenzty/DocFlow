"""Versioned local knowledge: staging, atomic publication, immutable snapshots.

LangChain Documents carry section metadata. Frozen baseline files are never
edited; imported text and snapshots live only in the local SQLite workspace.
"""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from langchain_core.documents import Document

from .retrieval import Chunk, _sections, _windows, corpus_fingerprint
from .storage import Conflict, NotFound, now


MAX_CHUNKS = 5000
MAX_DOCUMENTS = 100


class KnowledgeStore:
    def __init__(self, path: Path, base_chunks: list[Chunk]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.base_chunks = list(base_chunks)
        self.base_ids = {x.document_id for x in base_chunks}
        fingerprint = corpus_fingerprint(base_chunks)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kb_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kb_versions(
                    version_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    title TEXT NOT NULL,source TEXT NOT NULL,filename TEXT NOT NULL,
                    content TEXT NOT NULL,content_sha256 TEXT NOT NULL,chunks_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,UNIQUE(document_id,revision));
                CREATE TABLE IF NOT EXISTS kb_snapshots(
                    snapshot_id TEXT PRIMARY KEY,parent_id TEXT,action TEXT NOT NULL,
                    created_at TEXT NOT NULL,corpus_sha256 TEXT NOT NULL,
                    chunks_json TEXT NOT NULL,members_json TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT value FROM kb_meta WHERE name='baseline'").fetchone()
            if stored and stored[0] != fingerprint:
                raise Conflict("基础文档已变化；请使用新的数据目录，不得把旧工单改绑到新基础语料")
            if not stored:
                sid = 'kb-' + fingerprint[:24]
                db.execute("INSERT INTO kb_snapshots VALUES(?,?,?,?,?,?,?)",
                           (sid, None, 'bootstrap', now(), fingerprint,
                            json.dumps([asdict(x) for x in base_chunks], ensure_ascii=False), '{}'))
                db.executemany("INSERT INTO kb_meta VALUES(?,?)", [('baseline', fingerprint), ('active', sid)])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _snapshot(self, db, snapshot_id=None):
        if snapshot_id is None:
            snapshot_id = db.execute("SELECT value FROM kb_meta WHERE name='active'").fetchone()[0]
        row = db.execute("SELECT * FROM kb_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise NotFound("知识库快照不存在")
        return dict(row)

    def _summary(self, row, *, active_id=None):
        chunks = json.loads(row['chunks_json'])
        members = json.loads(row['members_json'])
        documents = {}
        for c in chunks:
            d = documents.setdefault(c['document_id'], {
                'document_id': c['document_id'], 'title': c['title'], 'source': c['source'],
                'version': c['version'], 'version_id': members.get(c['document_id']),
                'is_base': c['document_id'] in self.base_ids, 'base': c['document_id'] in self.base_ids,
                'chunks': 0, 'sections': set()})
            d['chunks'] += 1
            d['sections'].add(c['section'])
        return {'snapshot_id': row['snapshot_id'], 'parent_id': row['parent_id'],
                'action': row['action'], 'created_at': row['created_at'],
                'corpus_sha256': row['corpus_sha256'], 'chunks': len(chunks),
                'documents': [{**d, 'sections': len(d['sections'])} for d in documents.values()],
                'active': row['snapshot_id'] == active_id if active_id else True}

    def active(self):
        with self.connect() as db:
            return self._summary(self._snapshot(db))

    def snapshots(self):
        with self.connect() as db:
            active_id = self._snapshot(db)['snapshot_id']
            return [self._summary(dict(row), active_id=active_id) for row in db.execute(
                'SELECT * FROM kb_snapshots ORDER BY created_at DESC,snapshot_id DESC').fetchall()]

    def chunks(self, snapshot_id=None):
        with self.connect() as db:
            return [Chunk(**c) for c in json.loads(self._snapshot(db, snapshot_id)['chunks_json'])]

    @staticmethod
    def _validate(document_id, title, source, filename, content):
        if not isinstance(document_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', document_id):
            raise ValueError('document_id须为字母、数字、点、下划线或短横线')
        for label, value, limit in [('title', title, 160), ('source', source, 300), ('filename', filename, 200)]:
            if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f'{label}为空、过长或包含控制字符')
        if any(c in filename for c in ('/', '\\', ':')) or Path(filename).suffix.lower() not in {'.md', '.txt'}:
            raise ValueError('仅接收无目录的Markdown或TXT文件名')
        if not isinstance(content, str) or not content.strip() or len(content) > 200000 or len(content.encode('utf-8')) > 512 * 1024:
            raise ValueError('正文为空或超过200000字符/512KiB限制')
        if any(ord(c) < 32 and c not in '\n\r\t' for c in content):
            raise ValueError('正文含二进制或不支持的控制字符')

    def import_document(self, document_id, title, source, filename, content):
        self._validate(document_id, title, source, filename, content)
        if document_id in self.base_ids:
            raise Conflict('基础文档受冻结评测保护，请使用新的自定义document_id')
        content = content.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        title, source = title.strip(), source.strip()
        content_hash = sha256(content.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM kb_versions WHERE document_id=? ORDER BY revision DESC LIMIT 1', (document_id,)).fetchone()
            if previous and all(previous[k] == v for k, v in [('title', title), ('source', source), ('filename', filename), ('content_sha256', content_hash)]):
                return self._version(dict(previous))
            if not previous and db.execute('SELECT COUNT(DISTINCT document_id) FROM kb_versions').fetchone()[0] >= MAX_DOCUMENTS:
                raise Conflict('自定义文档达到100篇上限，请使用独立数据目录')
            revision = previous['revision'] + 1 if previous else 1
            if revision > 100:
                raise Conflict('单篇文档修订达到100版上限')
            version_id = 'dv-' + uuid4().hex
            sections = _sections(content, title) if Path(filename).suffix.lower() == '.md' else [(title, content.strip())]
            sections = [Document(page_content=text, metadata={'section': section, 'document_id': document_id,
                         'version_id': version_id, 'source': source}) for section, text in sections]
            chunks = []
            for section in sections:
                for window in _windows(section.page_content):
                    identifier = 'user-' + sha256(f'{version_id}:{len(chunks)}'.encode()).hexdigest()[:32]
                    chunks.append(Chunk(identifier, document_id, title, section.metadata['section'], source, f'r{revision}', window))
            if not chunks or len(chunks) > 1500:
                raise ValueError('切分结果为空或单篇超过1500片段')
            db.execute('INSERT INTO kb_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (version_id, document_id, revision, title, source, filename, content, content_hash,
                        json.dumps([asdict(c) for c in chunks], ensure_ascii=False), now()))
            return self._version(dict(db.execute('SELECT * FROM kb_versions WHERE version_id=?', (version_id,)).fetchone()))

    @staticmethod
    def _version(row):
        chunks = json.loads(row.pop('chunks_json'))
        return {**row, 'version': f"r{row['revision']}", 'chunks': chunks, 'chunk_count': len(chunks), 'status': 'staged_or_published'}

    def get_version(self, version_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM kb_versions WHERE version_id=?', (version_id,)).fetchone()
            if row is None:
                raise NotFound('文档修订不存在')
            return self._version(dict(row))

    def document_versions(self, document_id):
        with self.connect() as db:
            return [self._version(dict(row)) for row in db.execute(
                'SELECT * FROM kb_versions WHERE document_id=? ORDER BY revision DESC', (document_id,)).fetchall()]

    def _check_expected(self, current, expected):
        if current['snapshot_id'] != expected:
            raise Conflict('知识库已有新版本，请刷新后重新核对并发布')

    def _publish_snapshot(self, db, current, members, chunks, action):
        if len(chunks) > MAX_CHUNKS:
            raise Conflict('当前知识库超过5000片段上限')
        fingerprint = corpus_fingerprint(chunks)
        snapshot_id = 'kb-' + uuid4().hex
        db.execute('INSERT INTO kb_snapshots VALUES(?,?,?,?,?,?,?)',
                   (snapshot_id, current['snapshot_id'], action, now(), fingerprint,
                    json.dumps([asdict(c) for c in chunks], ensure_ascii=False), json.dumps(members)))
        db.execute("UPDATE kb_meta SET value=? WHERE name='active'", (snapshot_id,))
        return self._summary(self._snapshot(db, snapshot_id))

    def publish(self, version_id, expected_snapshot_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._snapshot(db)
            self._check_expected(current, expected_snapshot_id)
            row = db.execute('SELECT * FROM kb_versions WHERE version_id=?', (version_id,)).fetchone()
            if row is None:
                raise NotFound('文档修订不存在')
            members = json.loads(current['members_json'])
            if members.get(row['document_id']) == version_id:
                return self._summary(current)
            members[row['document_id']] = version_id
            chunks = [Chunk(**c) for c in json.loads(current['chunks_json']) if c['document_id'] != row['document_id']]
            chunks += [Chunk(**c) for c in json.loads(row['chunks_json'])]
            return self._publish_snapshot(db, current, members, chunks, 'publish:' + row['document_id'])

    def remove(self, document_id, expected_snapshot_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._snapshot(db)
            self._check_expected(current, expected_snapshot_id)
            if document_id in self.base_ids:
                raise Conflict('不能退役冻结的基础文档')
            members = json.loads(current['members_json'])
            if document_id not in members:
                raise NotFound('当前快照没有此自定义文档')
            del members[document_id]
            chunks = [Chunk(**c) for c in json.loads(current['chunks_json']) if c['document_id'] != document_id]
            return self._publish_snapshot(db, current, members, chunks, 'retire:' + document_id)

    def activate(self, snapshot_id, expected_snapshot_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._snapshot(db)
            self._check_expected(current, expected_snapshot_id)
            target = self._snapshot(db, snapshot_id)
            db.execute("UPDATE kb_meta SET value=? WHERE name='active'", (snapshot_id,))
            return self._summary(target)
