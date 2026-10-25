from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from docflow_lab.app import create_app
from docflow_lab.config import Settings


HEADERS = {"X-DocFlow-Local": "1"}


@pytest.fixture
def client(tmp_path):
    root = Settings().root
    settings = Settings(root=root, data_dir=tmp_path / "state")
    # Budget resides under project root for real runs; tests use a copied small corpus.
    import shutil
    shutil.copytree(root / "knowledge", tmp_path / "knowledge")
    settings = Settings(root=tmp_path, data_dir=tmp_path / "state")
    with TestClient(create_app(settings)) as client:
        yield client


def create(client):
    response = client.post("/api/tickets", json={"question": "设备A访问设备B配置接口为什么返回403？"}, headers=HEADERS)
    assert response.status_code == 201
    return response.json()


def test_page_api_config_and_search(client):
    response = client.get("/")
    assert response.status_code == 200 and "DocFlow" in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200
    docs = client.get("/docs")
    assert docs.status_code == 200 and "完整 OpenAPI JSON" in docs.text
    assert "<script" not in docs.text
    assert client.get("/openapi.json").json()["info"]["title"].startswith("DocFlow")
    config = client.get("/api/config").json()
    assert config["knowledge_documents"] == 12
    assert config["budget"] == {"limit": 40, "used": 0, "remaining": 40}
    assert not config["live_enabled"] and not config["vector_ready"]
    response = client.post("/api/search", json={"query": "设备归属403"}, headers=HEADERS)
    results = response.json()["results"]
    assert len(results) == 5
    assert all(result["source"] and result["version"] for result in results)


def test_write_boundary_and_validation(client):
    assert client.post("/api/tickets", json={"question": "接口问题"}).status_code == 403
    assert client.post("/api/tickets", json={"question": "接口问题"}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400
    for payload in [{"question": "   "}, {"question": "接口问题", "live": "true"}, {"question": "接口问题", "tools": ["submit"]}, {"question": "x" * 2001}]:
        assert client.post("/api/tickets", json=payload, headers=HEADERS).status_code == 422
    assert client.post("/api/tickets", json={"question": "接口问题", "live": True}, headers=HEADERS).status_code == 409
    assert client.get("/api/tickets/missing").status_code == 404


def test_review_then_timeout_retry_does_not_duplicate(client):
    ticket = create(client)
    tid = ticket["id"]
    assert ticket["status"] == "awaiting_review" and ticket["mode"] == "mock"
    assert client.post(f"/api/tickets/{tid}/submit", json={"version": 1}, headers=HEADERS).status_code == 409
    assert client.post(f"/api/tickets/{tid}/review", json={"version": 1, "approved": True}, headers=HEADERS).status_code == 200
    response = client.post(f"/api/tickets/{tid}/submit", json={"version": 1, "simulate_timeout": True}, headers=HEADERS)
    assert response.status_code == 504
    saved = client.get(f"/api/tickets/{tid}").json()
    assert saved["status"] == "submission_failed" and saved["reply"]
    again = client.post(f"/api/tickets/{tid}/retry", json={}, headers=HEADERS).json()
    assert again["status"] == "submitted" and again["reply"]["id"] == saved["reply"]["id"]
    replay = client.post(f"/api/tickets/{tid}/submit", json={"version": 1}, headers=HEADERS).json()
    assert replay["reply"]["id"] == again["reply"]["id"]
    assert client.post(f"/api/tickets/{tid}/draft", json={}, headers=HEADERS).status_code == 409


def test_new_version_invalidates_review(client):
    ticket = create(client)
    tid = ticket["id"]
    client.post(f"/api/tickets/{tid}/review", json={"version": 1, "approved": True}, headers=HEADERS)
    ticket = client.post(f"/api/tickets/{tid}/draft", json={}, headers=HEADERS).json()
    assert ticket["version"] == 2 and ticket["approval"] is None
    assert client.post(f"/api/tickets/{tid}/review", json={"version": 1, "approved": True}, headers=HEADERS).status_code == 409
    assert client.post(f"/api/tickets/{tid}/submit", json={"version": 2}, headers=HEADERS).status_code == 409


def test_parallel_submit_returns_same_reply(client):
    tid = create(client)["id"]
    client.post(f"/api/tickets/{tid}/review", json={"version": 1, "approved": True}, headers=HEADERS)
    def submit(_):
        return client.post(f"/api/tickets/{tid}/submit", json={"version": 1}, headers=HEADERS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["reply"]["id"] for r in results}) == 1
