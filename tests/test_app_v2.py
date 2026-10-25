"""V2 HTTP integration: offline Agent and isolated unavailable vector path."""
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from docflow_lab.app import create_app
from docflow_lab.config import Settings
from docflow_lab.retrieval import KeywordRetriever, RetrievalError, load_chunks
from docflow_lab.storage import digest
from docflow_lab.workflow import Engine
from docflow_lab.agent import SupportAgent


HEADERS = {"X-DocFlow-Local": "1"}
QUESTION = "设备A访问设备B配置为什么返回403？"


class UnavailableVector:
    ready = False

    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, query, top_k=5):
        raise RetrievalError("测试：向量索引不可用，不隐式回退")


@pytest.fixture
def settings(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "guide.md").write_text(
        "document_id: api-guide\ntitle: 设备归属手册\nsource: original-api-fixture\nversion: 2.0\nkind: guide\n---\n"
        "## 403\n设备只能访问属于自己的配置。跨设备访问返回403，应检查归属。\n"
        "## 日志\n请记录接口路径与request_id，不要记录API密钥。\n", encoding="utf-8")
    (knowledge / "faq.md").write_text(
        "document_id: api-faq\ntitle: 常见问题\nsource: original-api-faq\nversion: 2.1\nkind: faq\n---\n"
        "## 审核\n草稿必须经人工审核，Agent不能直接提交。\n", encoding="utf-8")
    return Settings(root=tmp_path, data_dir=tmp_path / "state")


@pytest.fixture
def api(settings, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret-never-call")
    chunks = load_chunks(settings.knowledge_dir)
    engine = Engine(settings, retrievers={"keyword": KeywordRetriever(chunks), "vector": UnavailableVector(chunks)})
    try:
        with TestClient(create_app(engine=engine)) as client:
            yield client, engine
    finally:
        engine.close()


def create(client, question=QUESTION):
    response = client.post("/api/tickets", headers=HEADERS,
                           json={"question": question, "method": "keyword", "strategy": "agent"})
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert ticket["status"] == "awaiting_review", ticket
    return ticket


def await_job(client, jid, expected="completed"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{jid}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == expected, job
            return job
        time.sleep(0.02)
    pytest.fail("job did not finish within five seconds")


def test_compare_keeps_keyword_results_when_vector_and_hybrid_unavailable(api):
    client, engine = api
    response = client.post("/api/search/compare", json={"query": "跨设备访问403"}, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["results"]["keyword"]
    assert body["results"]["vector"] == body["results"]["hybrid"] == []
    assert set(body["errors"]) == {"vector", "hybrid"}
    assert all(item["method"] == "keyword" for item in body["results"]["keyword"])
    response = client.post("/api/search", json={"query": "403", "method": "vector"}, headers=HEADERS)
    assert response.status_code == 503
    assert engine.budget.summary()["used"] == 0


def test_knowledge_and_config_expose_provenance_and_statistics_without_secrets(api):
    client, _ = api
    response = client.get("/api/knowledge")
    assert response.status_code == 200
    data = response.json()
    assert {document["document_id"] for document in data["documents"]} == {"api-guide", "api-faq"}
    assert all(document["source"] and document["version"] and document["sections"] >= 1 for document in data["documents"])
    assert data["chunks"] == sum(document["chunks"] for document in data["documents"])
    config = client.get("/api/config")
    assert "fixture-secret-never-call" not in config.text
    assert config.json()["strategies"] == ["rag", "agent"]
    assert not config.json()["vector_ready"]
    assert config.json()["statistics"]["tickets"] == 0


def test_ticket_filter_status_search_literal_wildcards_and_sql_input(api):
    client, _ = api
    first = create(client, "设备A访问设备B为什么403？字面标记100%_abc")
    second = create(client, "设备配置返回403，怎么检查归属？")
    client.post(f"/api/tickets/{first['id']}/review", json={"version": 1, "approved": False}, headers=HEADERS)
    rejected = client.get("/api/tickets", params={"status": "rejected"}).json()
    assert rejected["total"] == 1 and rejected["items"][0]["id"] == first["id"]
    limited = client.get("/api/tickets", params={"limit": 1}).json()
    assert limited["total"] == 2 and len(limited["items"]) == 1
    literal = client.get("/api/tickets", params={"q": "%_"}).json()
    assert literal["total"] == 1 and literal["items"][0]["id"] == first["id"]
    assert client.get("/api/tickets", params={"q": "' OR 1=1 --"}).json()["total"] == 0
    assert client.get("/api/tickets", params={"q": second["id"]}).json()["total"] == 1


def test_background_job_api_generates_without_submitting_and_cannot_retry_completed_job(api):
    client, engine = api
    response = client.post("/api/jobs", json={"question": QUESTION, "strategy": "agent"}, headers=HEADERS)
    assert response.status_code == 202
    job = await_job(client, response.json()["id"])
    ticket = client.get(f"/api/tickets/{job['ticket_id']}").json()
    assert ticket["strategy"] == "agent" and ticket["status"] == "awaiting_review"
    assert ticket["quality"]["validation"] == "quote_presence_only"
    assert ticket["trace"] and ticket["reply"] is None
    assert client.post(f"/api/jobs/{job['id']}/retry", headers=HEADERS).status_code == 409
    assert client.get("/api/jobs/missing").status_code == 404
    assert engine.budget.summary()["used"] == 0


def test_failed_background_job_api_requires_explicit_retry(api, monkeypatch):
    client, engine = api
    original_search = engine.retrievers["keyword"].search

    def fail(query, top_k=5):
        raise RetrievalError("fixture unavailable")

    monkeypatch.setattr(engine.retrievers["keyword"], "search", fail)
    created = client.post("/api/jobs", json={"question": QUESTION, "strategy": "agent"}, headers=HEADERS).json()
    failed = await_job(client, created["id"], expected="failed")
    assert failed["error"]
    monkeypatch.setattr(engine.retrievers["keyword"], "search", original_search)
    response = client.post(f"/api/jobs/{created['id']}/retry", headers=HEADERS)
    assert response.status_code == 200
    completed = await_job(client, response.json()["id"])
    assert completed["attempts"] == 2
    assert client.get(f"/api/tickets/{created['ticket_id']}").json()["status"] == "awaiting_review"


def test_job_retry_cannot_exceed_20_pending_jobs_and_keeps_failed_state(api):
    client, engine = api
    tid = engine.store.create(QUESTION, "keyword", False, strategy="agent", status="generation_failed")
    failed = engine.store.create_job(tid, 1)
    engine.store.set_job(failed["id"], "failed", "fixture failure")
    for number in range(20):
        queued_tid = engine.store.create(f"排队问题{number}", "keyword", False, strategy="agent", status="queued")
        engine.store.create_job(queued_tid, 1)
    response = client.post(f"/api/jobs/{failed['id']}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert engine.store.get_job(failed["id"])["status"] == "failed"
    assert engine.store.get(tid)["status"] == "generation_failed"
    assert engine.budget.summary()["used"] == 0


def test_agent_draft_saved_before_checkpoint_crash_recovers_after_restart_without_regeneration(api, monkeypatch):
    client, engine = api
    original_save, original_run = engine.store.save_draft, SupportAgent.run
    runs = []

    def counted_run(self, *args, **kwargs):
        runs.append(1)
        return original_run(self, *args, **kwargs)

    def save_then_crash(*args, **kwargs):
        original_save(*args, **kwargs)
        raise RuntimeError("fixture crash after saving Agent draft before graph checkpoint")

    monkeypatch.setattr(SupportAgent, "run", counted_run)
    monkeypatch.setattr(engine.store, "save_draft", save_then_crash)
    created = client.post("/api/jobs", json={"question": QUESTION, "strategy": "agent"}, headers=HEADERS).json()
    await_job(client, created["id"], expected="failed")
    saved = engine.store.get(created["ticket_id"])
    assert saved["draft"] and saved["quality"] and len(runs) == 1
    engine.close()
    restarted = Engine(engine.settings, retrievers=dict(engine.retrievers))
    try:
        with TestClient(create_app(engine=restarted)) as second:
            response = second.post(f"/api/jobs/{created['id']}/retry", headers=HEADERS)
            assert response.status_code == 200
            await_job(second, created["id"])
            restored = second.get(f"/api/tickets/{created['ticket_id']}").json()
            assert restored["status"] == "awaiting_review" and restored["error"] is None
            assert restored["draft"] == saved["draft"] and restored["draft_hash"] == saved["draft_hash"]
            assert len(runs) == 1 and restarted.budget.summary()["used"] == 0
            assert second.post(f"/api/tickets/{created['ticket_id']}/review", headers=HEADERS,
                               json={"version": 1, "approved": True}).status_code == 200
    finally:
        restarted.close()


def test_followup_and_edit_api_preserve_history_and_invalidate_previous_review(api):
    client, _ = api
    original = create(client)
    tid = original["id"]
    followup = client.post(f"/api/tickets/{tid}/followups", headers=HEADERS,
                           json={"message": "补充：身份device-a，访问device-b配置。"})
    assert followup.status_code == 200
    v2 = followup.json()
    assert v2["version"] == 2 and v2["question"] == QUESTION
    assert len([m for m in v2["messages"] if m["role"] == "user"]) == 2
    assert client.post(f"/api/tickets/{tid}/review", json={"version": 2, "approved": True}, headers=HEADERS).status_code == 200
    edited = client.post(f"/api/tickets/{tid}/edit", headers=HEADERS,
                         json={"version": 2, "answer": "【人工编辑】请核对请求身份与设备归属，记录request_id。"})
    assert edited.status_code == 200
    v3 = edited.json()
    assert v3["version"] == 3 and v3["approval"] is None
    assert v3["quality"]["status"] == "manual_review_required"
    assert client.post(f"/api/tickets/{tid}/submit", json={"version": 3}, headers=HEADERS).status_code == 409
    assert client.post(f"/api/tickets/{tid}/review", json={"version": 2, "approved": True}, headers=HEADERS).status_code == 409
    assert client.post(f"/api/tickets/{tid}/review", json={"version": 3, "approved": True}, headers=HEADERS).status_code == 200
    submitted = client.post(f"/api/tickets/{tid}/submit", json={"version": 3}, headers=HEADERS)
    assert submitted.status_code == 200
    assert submitted.json()["reply"]["body"] == v3["draft"]["answer"]


@pytest.mark.parametrize("path,payload", [
    ("/api/jobs", {"question": QUESTION, "live": "true"}),
    ("/api/jobs", {"question": QUESTION, "strategy": "multiagent"}),
    ("/api/jobs", {"question": QUESTION, "tools": ["submit_ticket"]}),
    ("/api/search/compare", {"query": "403", "submit": True}),
    ("/api/search/compare", {"query": "   "}),
    ("/api/tickets/missing/followups", {"message": "   "}),
    ("/api/tickets/missing/followups", {"message": "补充", "approved": True}),
    ("/api/tickets/missing/edit", {"version": "1", "answer": "回复"}),
    ("/api/tickets/missing/edit", {"version": 1, "answer": " ", "approved": True}),
])
def test_v2_payloads_forbid_type_coercion_extra_actions_and_blank_input(api, path, payload):
    client, _ = api
    assert client.post(path, json=payload, headers=HEADERS).status_code == 422


def test_new_mutations_enforce_local_origin_and_live_explicit_enable(api):
    client, engine = api
    body = {"question": QUESTION, "strategy": "agent"}
    assert client.post("/api/jobs", json=body).status_code == 403
    assert client.post("/api/jobs", json=body, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/jobs", json={**body, "live": True}, headers=HEADERS).status_code == 409
    assert client.get("/api/knowledge", headers={"Host": "evil.example"}).status_code == 400
    assert engine.store.statistics()["tickets"] == 0


def test_explicit_ticket_retry_does_not_expose_sensitive_backend_error(api, monkeypatch):
    client, engine = api

    def fail(query, top_k=5):
        raise RuntimeError("backend fixture failure: Bearer fixture-secret-never-call")

    monkeypatch.setattr(engine.retrievers["keyword"], "search", fail)
    created = client.post("/api/tickets", json={"question": QUESTION, "strategy": "agent"}, headers=HEADERS)
    assert created.status_code == 201 and created.json()["status"] == "generation_failed"
    assert "fixture-secret-never-call" not in created.text
    retried = client.post(f"/api/tickets/{created.json()['id']}/retry", headers=HEADERS)
    assert retried.status_code == 200 and retried.json()["status"] == "generation_failed"
    assert "fixture-secret-never-call" not in retried.text
    assert engine.budget.summary()["used"] == 0


def test_legacy_v1_database_migrates_without_rewriting_draft_or_approval(settings):
    settings.data_dir.mkdir(parents=True)
    chunks = load_chunks(settings.knowledge_dir)
    draft = {"decision": "answer", "answer": "v1草稿，访问其他设备会返回403。", "citations": [chunks[0].chunk_id]}
    approval = {"version": 1, "draft_hash": digest(draft), "approved": True, "created_at": "2026-10-24T00:00:00Z"}
    with sqlite3.connect(settings.data_dir / "tickets.sqlite3") as db:
        db.execute("""CREATE TABLE tickets (
            id TEXT PRIMARY KEY, question TEXT NOT NULL, method TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
            draft TEXT, evidence TEXT NOT NULL DEFAULT '[]', draft_hash TEXT,
            approval TEXT, error TEXT, created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy", QUESTION, "keyword", "mock", "approved", 1, json.dumps(draft),
            json.dumps([chunks[0].as_dict()]), digest(draft), json.dumps(approval), None, "2026-10-24T00:00:00Z"))
    engine = Engine(settings, retrievers={"keyword": KeywordRetriever(chunks)})
    try:
        with TestClient(create_app(engine=engine)) as client:
            response = client.get("/api/tickets/legacy")
            assert response.status_code == 200
            ticket = response.json()
            assert ticket["draft"] == draft and ticket["approval"] == approval
            assert ticket["draft_hash"] == digest(draft)
            assert ticket["strategy"] == "rag" and ticket["quality"] is None and ticket["trace"] == []
            assert ticket["messages"][0]["content"] == QUESTION
            assert ticket["versions"][0]["version"] == 1
            assert ticket["updated_at"] == ticket["created_at"]
    finally:
        engine.close()
