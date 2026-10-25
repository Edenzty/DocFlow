"""Complete offline Agent + durable human review + local idempotent submission."""
import json
from pathlib import Path

import pytest

from docflow_lab.config import Settings
from docflow_lab.demo_v2 import run_demo
from docflow_lab.storage import RequestBudget


def test_complete_v2_demo_is_offline_and_preserves_user_state(tmp_path):
    source = Path(__file__).resolve().parents[1] / "knowledge"
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    for path in source.glob("*.md"):
        (knowledge / path.name).write_bytes(path.read_bytes())
    user_state = tmp_path / "user-state"
    user_state.mkdir()
    marker = user_state / "unchanged.txt"
    marker.write_text("用户工作台状态", encoding="utf-8")
    settings = Settings(root=tmp_path, data_dir=user_state, allow_live=True)
    output = tmp_path / "demo"
    report = run_demo(settings, output)
    assert report["status"] == "passed"
    assert report["passed"] == report["total"] >= 20
    assert report["model_requests"] == 0
    assert RequestBudget(settings.budget_path).summary()["used"] == 0
    assert marker.read_text(encoding="utf-8") == "用户工作台状态"
    assert not (user_state / "tickets.sqlite3").exists()
    assert (output / "state" / "checkpoints.sqlite3").is_file()
    assert json.loads((output / "demo.json").read_text(encoding="utf-8"))["snapshots"]["final"]["status"] == "submitted"
    assert "真实模型API请求0次" in (output / "report.md").read_text(encoding="utf-8")


def test_demo_refuses_existing_output_before_touching_state(tmp_path):
    with pytest.raises(ValueError, match="已存在"):
        run_demo(Settings(root=tmp_path), tmp_path)
