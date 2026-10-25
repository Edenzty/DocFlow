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
                CREATE TABLE IF NOT EXISTS versions (
                    ticket_id TEXT NOT NULL, version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(ticket_id, version)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, version INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tickets)")}
            for name, definition in {
                "strategy": "TEXT NOT NULL DEFAULT 'rag'", "quality": "TEXT",
                "trace": "TEXT NOT NULL DEFAULT '[]'", "updated_at": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
            db.execute("UPDATE tickets SET updated_at=created_at WHERE updated_at IS NULL")
            for row in db.execute("SELECT id,question,version,created_at FROM tickets").fetchall():
                if not db.execute("SELECT 1 FROM versions WHERE ticket_id=?", (row["id"],)).fetchone():
                    self._snapshot(db, row["id"], row["created_at"])
                if not db.execute("SELECT 1 FROM messages WHERE ticket_id=?", (row["id"],)).fetchone():
                    self._message(db, row["id"], "user", row["question"], row["version"])

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

    def _snapshot(self, db, tid: str, created_at=None):
        row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if row:
            db.execute("INSERT INTO versions(ticket_id,version,snapshot,created_at) VALUES(?,?,?,?) ON CONFLICT(ticket_id,version) DO UPDATE SET snapshot=excluded.snapshot",
                       (tid, row["version"], json.dumps(dict(row), ensure_ascii=False), created_at or now()))

    def _message(self, db, tid, role, content, version):
        db.execute("INSERT INTO messages(ticket_id,role,content,version,created_at) VALUES(?,?,?,?,?)",
                   (tid, role, content, version, now()))

    @staticmethod
    def _decode(row):
        result = dict(row)
        for field in ("draft", "evidence", "approval", "quality", "trace"):
            result[field] = json.loads(result[field]) if result.get(field) else ([] if field in ("evidence", "trace") else None)
        return result

    def get(self, tid: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise NotFound("工单不存在")
            result = self._decode(row)
            reply = db.execute("SELECT * FROM replies WHERE ticket_id=?", (tid,)).fetchone()
            result["reply"] = dict(reply) if reply else None
            result["events"] = [dict(e) for e in db.execute(
                "SELECT kind,detail,created_at FROM events WHERE ticket_id=? ORDER BY id", (tid,))]
            result["messages"] = [dict(m) for m in db.execute("SELECT role,content,version,created_at FROM messages WHERE ticket_id=? ORDER BY id", (tid,))]
            result["versions"] = []
            for v in db.execute("SELECT snapshot,created_at FROM versions WHERE ticket_id=? ORDER BY version DESC", (tid,)):
                snapshot = self._decode(json.loads(v["snapshot"]))
                snapshot["created_at"] = v["created_at"]
                result["versions"].append(snapshot)
            job = db.execute("SELECT * FROM jobs WHERE ticket_id=? AND version=? ORDER BY created_at DESC LIMIT 1", (tid, row["version"])).fetchone()
            result["job"] = dict(job) if job else None
            return result

    def create(self, question: str, method: str, live: bool, strategy="rag", status="generating") -> str:
        tid = uuid4().hex
        with self.connect() as db:
            timestamp = now()
            db.execute("INSERT INTO tickets(id,question,method,mode,status,version,created_at,updated_at,strategy) VALUES(?,?,?,?,?,?,?,?,?)",
                       (tid, question, method, "live" if live else "mock", status, 1, timestamp, timestamp, strategy))
            self._event(db, tid, "created", "创建本地模拟工单；尚未生成或提交回复")
            self._message(db, tid, "user", question, 1)
            self._snapshot(db, tid)
        return tid

    def regenerate(self, tid: str, question: str, method: str, live: bool, strategy=None, message=None) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row:
                raise NotFound("工单不存在")
            if db.execute("SELECT 1 FROM replies WHERE ticket_id=?", (tid,)).fetchone():
                raise Conflict("已经写入本地回复，不能再生成新草稿")
            if row["status"] in ("queued", "generating"):
                raise Conflict("当前生成任务尚未结束，暂不能修改版本")
            self._snapshot(db, tid)
            db.execute("UPDATE tickets SET question=?,method=?,mode=?,strategy=?,version=version+1,status='generating',draft=NULL,evidence='[]',draft_hash=NULL,approval=NULL,error=NULL,quality=NULL,trace='[]',updated_at=? WHERE id=?",
                       (question, method, "live" if live else "mock", strategy or row["strategy"], now(), tid))
            self._event(db, tid, "regenerated", "创建新草稿版本，旧审核立即失效")
            if message:
                self._message(db, tid, "user", message, row["version"] + 1)
            elif question != row["question"]:
                self._message(db, tid, "user", question, row["version"] + 1)
            self._snapshot(db, tid)
        return self.get(tid)

    def save_draft(self, tid: str, version: int, draft: dict, evidence: list, quality=None, trace=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,draft_hash FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row or row["version"] != version:
                raise Conflict("草稿版本已改变")
            if row["draft_hash"]:
                return
            bound_hash = digest({"draft": draft, "evidence": evidence, "quality": quality}) if quality is not None else digest(draft)
            db.execute("UPDATE tickets SET draft=?,draft_hash=?,evidence=?,quality=?,trace=?,status='awaiting_review',error=NULL,updated_at=? WHERE id=?",
                       (json.dumps(draft, ensure_ascii=False), bound_hash, json.dumps(evidence, ensure_ascii=False),
                        json.dumps(quality, ensure_ascii=False) if quality is not None else None,
                        json.dumps(trace or [], ensure_ascii=False), now(), tid))
            self._event(db, tid, "draft_generated", f"v{version} 已生成；引用已校验，等待人工审核")
            self._message(db, tid, "assistant", draft["answer"], version)
            self._snapshot(db, tid)

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
            db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now(), tid))
            self._snapshot(db, tid)

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
            db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now(), tid))
            self._snapshot(db, tid)
        if should_timeout:
            raise TimeoutError("模拟：提交已写入，但响应超时；可安全重试")
        return self.get(tid)["reply"]

    def error(self, tid: str, stage: str, message: str, trace=None):
        with self.connect() as db:
            db.execute("UPDATE tickets SET status=?,error=?,updated_at=? WHERE id=?", (stage + "_failed", message, now(), tid))
            if trace is not None:
                db.execute("UPDATE tickets SET trace=? WHERE id=?", (json.dumps(trace, ensure_ascii=False), tid))
            self._event(db, tid, stage + "_failed", message)
            self._snapshot(db, tid)

    def list_tickets(self, status=None, q=None, limit=50):
        conditions, values = [], []
        if status:
            conditions.append("status=?")
            values.append(status)
        if q:
            conditions.append("(question LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            values += [pattern, pattern]
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM tickets" + where, values).fetchone()[0]
            rows = db.execute("SELECT id,question,status,version,method,mode,strategy,created_at,updated_at FROM tickets" + where + " ORDER BY updated_at DESC,id DESC LIMIT ?", [*values, min(max(limit, 1), 100)])
            return {"items": [dict(row) for row in rows], "total": total}

    def statistics(self):
        with self.connect() as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM tickets GROUP BY status")}
            return {"tickets": sum(counts.values()), "awaiting_review": counts.get("awaiting_review", 0),
                    "submitted": counts.get("submitted", 0), "failed": sum(v for k, v in counts.items() if k.endswith("_failed")),
                    "queued": counts.get("queued", 0), "running": counts.get("generating", 0),
                    "jobs": db.execute("SELECT count(*) FROM jobs").fetchone()[0],
                    "versions": db.execute("SELECT count(*) FROM versions").fetchone()[0]}

    def create_job(self, tid: str, version: int) -> dict:
        jid = uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise Conflict("后台任务队列已满，请等待现有任务完成")
            db.execute("INSERT INTO jobs(id,ticket_id,version,kind,status,created_at) VALUES(?,?,?,'generation','queued',?)", (jid, tid, version, now()))
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise NotFound("后台任务不存在")
            return dict(row)

    def set_job(self, jid: str, status: str, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=? WHERE id=?", (status, error, jid))
            if status == "running":
                db.execute("UPDATE jobs SET attempts=attempts+1,started_at=?,finished_at=NULL WHERE id=?", (now(), jid))
            elif status in ("completed", "failed", "interrupted", "stale"):
                db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (now(), jid))

    def queue_retry(self, jid: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not job:
                raise NotFound("后台任务不存在")
            ticket = db.execute("SELECT version FROM tickets WHERE id=?", (job["ticket_id"],)).fetchone()
            if not ticket or ticket["version"] != job["version"] or job["status"] not in ("failed", "interrupted"):
                raise Conflict("仅当前版本失败或中断的后台任务可以重试")
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise Conflict("后台任务队列已满，请稍后重试")
            db.execute("UPDATE jobs SET status='queued',error=NULL,finished_at=NULL WHERE id=?", (jid,))
            db.execute("UPDATE tickets SET status='queued',error=NULL,updated_at=? WHERE id=?", (now(), job["ticket_id"]))
            self._event(db, job["ticket_id"], "job_queued", "明确重试后台生成；不会自动提交")
            self._snapshot(db, job["ticket_id"])

    def set_status(self, tid: str, status: str):
        with self.connect() as db:
            db.execute("UPDATE tickets SET status=?,error=NULL,updated_at=? WHERE id=?", (status, now(), tid))
            self._snapshot(db, tid)

    def recover_jobs(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,ticket_id,version FROM jobs WHERE status IN ('queued','running')").fetchall()
            for row in rows:
                message = "服务中断，任务没有自动重试；请检查已有草稿后明确重试"
                db.execute("UPDATE jobs SET status='interrupted',error=?,finished_at=? WHERE id=?", (message, now(), row["id"]))
                current = db.execute("SELECT version,status FROM tickets WHERE id=?", (row["ticket_id"],)).fetchone()
                if current and current["version"] == row["version"] and current["status"] in ("queued", "generating"):
                    db.execute("UPDATE tickets SET status='generation_failed',error=?,updated_at=? WHERE id=?", (message, now(), row["ticket_id"]))
                    self._event(db, row["ticket_id"], "job_interrupted", message)
                    self._snapshot(db, row["ticket_id"])

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
