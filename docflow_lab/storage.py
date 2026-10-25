"""SQLite transactions are the authority for versions, approvals and local replies."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: dict) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, question TEXT NOT NULL, method TEXT NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                    draft TEXT, evidence TEXT NOT NULL DEFAULT '[]', draft_hash TEXT,
                    approval TEXT, error TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replies (
                    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL, draft_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
                    timeout_injected INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
            """)

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

    def _event(self, db, tid, kind, detail):
        db.execute("INSERT INTO events(ticket_id,kind,detail,created_at) VALUES(?,?,?,?)",
                   (tid, kind, detail, now()))

    def event(self, tid: str, kind: str, detail: str):
        with self.connect() as db:
            self._event(db, tid, kind, detail)

    def get(self, tid: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise NotFound("工单不存在")
            result = dict(row)
            for field in ("draft", "evidence", "approval"):
                result[field] = json.loads(result[field]) if result[field] else None
            reply = db.execute("SELECT * FROM replies WHERE ticket_id=?", (tid,)).fetchone()
            result["reply"] = dict(reply) if reply else None
            result["events"] = [dict(e) for e in db.execute(
                "SELECT kind,detail,created_at FROM events WHERE ticket_id=? ORDER BY id", (tid,))]
            return result

    def create(self, question: str, method: str, live: bool) -> str:
        tid = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO tickets(id,question,method,mode,status,version,created_at) VALUES(?,?,?,?,?,?,?)",
                       (tid, question, method, "live" if live else "mock", "generating", 1, now()))
            self._event(db, tid, "created", "创建本地模拟工单；尚未生成或提交回复")
        return tid

    def regenerate(self, tid: str, question: str, method: str, live: bool) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row:
                raise NotFound("工单不存在")
            if db.execute("SELECT 1 FROM replies WHERE ticket_id=?", (tid,)).fetchone():
                raise Conflict("已经写入本地回复，不能再生成新草稿")
            db.execute("UPDATE tickets SET question=?,method=?,mode=?,version=version+1,status='generating',draft=NULL,evidence='[]',draft_hash=NULL,approval=NULL,error=NULL WHERE id=?",
                       (question, method, "live" if live else "mock", tid))
            self._event(db, tid, "regenerated", "创建新草稿版本，旧审核立即失效")
        return self.get(tid)

    def save_draft(self, tid: str, version: int, draft: dict, evidence: list):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,draft_hash FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row or row["version"] != version:
                raise Conflict("草稿版本已改变")
            if row["draft_hash"]:
                return
            db.execute("UPDATE tickets SET draft=?,draft_hash=?,evidence=?,status='awaiting_review',error=NULL WHERE id=?",
                       (json.dumps(draft, ensure_ascii=False), digest(draft), json.dumps(evidence, ensure_ascii=False), tid))
            self._event(db, tid, "draft_generated", f"v{version} 已生成；引用已校验，等待人工审核")

    def approve(self, tid: str, version: int, approved: bool):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row or row["version"] != version or not row["draft_hash"]:
                raise Conflict("审批必须匹配当前已生成的草稿版本")
            approval = {"version": version, "draft_hash": row["draft_hash"], "approved": approved, "created_at": now()}
            if row["approval"]:
                old = json.loads(row["approval"])
                if old["approved"] != approved:
                    raise Conflict("已审核的版本不能修改审批，请创建新草稿")
                return
            db.execute("UPDATE tickets SET approval=?,status=?,error=NULL WHERE id=?",
                       (json.dumps(approval), "approved" if approved else "rejected", tid))
            self._event(db, tid, "approved" if approved else "rejected", f"人工审核 v{version}")

    def submit(self, tid: str, version: int, simulate_timeout: bool = False) -> dict:
        should_timeout = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row:
                raise NotFound("工单不存在")
            approval = json.loads(row["approval"]) if row["approval"] else {}
            if (row["version"] != version or not approval.get("approved")
                    or approval.get("version") != version or approval.get("draft_hash") != row["draft_hash"]):
                raise Conflict("未通过当前草稿版本的人工审核，禁止提交")
            key = f"{tid}:{version}:{row['draft_hash']}"
            reply = db.execute("SELECT * FROM replies WHERE ticket_id=?", (tid,)).fetchone()
            if reply and reply["idempotency_key"] != key:
                raise Conflict("该工单已有其他版本回复")
            if reply is None:
                reply_id = uuid4().hex
                body = json.loads(row["draft"])["answer"]
                should_timeout = simulate_timeout
                db.execute("INSERT INTO replies VALUES(?,?,?,?,?,?,?,?)",
                           (reply_id, tid, version, row["draft_hash"], key, body, int(should_timeout), now()))
                self._event(db, tid, "local_reply_written", f"本地模拟系统写入回复 {reply_id}（幂等键绑定版本与内容）")
            else:
                self._event(db, tid, "idempotent_replay", "重复提交返回同一条已持久化回复")
            db.execute("UPDATE tickets SET status=?,error=? WHERE id=?",
                       ("submission_failed" if should_timeout else "submitted",
                        "模拟：写入成功后响应超时，请重试确认同一条回复" if should_timeout else None, tid))
        if should_timeout:
            raise TimeoutError("模拟：提交已写入，但响应超时；可安全重试")
        return self.get(tid)["reply"]

    def error(self, tid: str, stage: str, message: str):
        with self.connect() as db:
            db.execute("UPDATE tickets SET status=?,error=? WHERE id=?", (stage + "_failed", message, tid))
            self._event(db, tid, stage + "_failed", message)

    def reply_count(self, tid: str) -> int:
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM replies WHERE ticket_id=?", (tid,)).fetchone()[0]


class RequestBudget:
    LIMIT = 40

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,status TEXT NOT NULL,elapsed_ms REAL,prompt_tokens INTEGER,completion_tokens INTEGER,error TEXT,purpose TEXT NOT NULL DEFAULT 'interactive')")
            if "purpose" not in {row[1] for row in db.execute("PRAGMA table_info(calls)")}:
                db.execute("ALTER TABLE calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'interactive'")
            if "total_tokens" not in {row[1] for row in db.execute("PRAGMA table_info(calls)")}:
                db.execute("ALTER TABLE calls ADD COLUMN total_tokens INTEGER")

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

    def summary(self) -> dict:
        with self.connect() as db:
            used = db.execute("SELECT count(*) FROM calls").fetchone()[0]
            return {"limit": self.LIMIT, "used": used, "remaining": max(0, self.LIMIT - used)}

    def reserve(self, purpose: str = "interactive") -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM calls").fetchone()[0] >= self.LIMIT:
                raise Conflict("真实模型总请求预算40次已用完（失败尝试也计入）")
            limit = {"development": 10, "holdout": 20}.get(purpose)
            if limit is not None and db.execute("SELECT count(*) FROM calls WHERE purpose=?", (purpose,)).fetchone()[0] >= limit:
                raise Conflict(f"{purpose}评测请求配额{limit}次已用完，失败尝试也计入")
            cid = uuid4().hex
            db.execute("INSERT INTO calls(id,created_at,status,purpose) VALUES(?,?,'reserved',?)", (cid, now(), purpose))
            return cid

    def finish(self, cid: str, elapsed_ms: float, usage: dict | None, error: str | None):
        usage = usage or {}
        with self.connect() as db:
            total = usage.get("total_tokens")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                total = None
            db.execute("UPDATE calls SET status=?,elapsed_ms=?,prompt_tokens=?,completion_tokens=?,total_tokens=?,error=? WHERE id=?",
                       ("failed" if error else "completed", elapsed_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"), total, error, cid))

    def records(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM calls ORDER BY created_at")]
