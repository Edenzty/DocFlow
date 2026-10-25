"""Executable offline V2 workflow demo, with isolated durable SQLite state.

This exercises real BM25, LangGraph, SQLite and the bounded deterministic Agent
fixture.  It is a workflow acceptance demo, never a real LLM quality benchmark.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .config import Settings
from .storage import Conflict
from .workflow import Engine


QUESTION = "设备A的身份已经校验正确，为什么访问设备B的配置接口返回403？"
FOLLOWUP = "补充：我用key-a读取device-b配置，返回403，reason是device_forbidden。请结合归属规则解释。"


def run_demo(settings: Settings, output: Path) -> dict:
    """Run the complete local mock scenario and persist its acceptance evidence.

    ``output`` must be new.  Runtime databases stay under output/state and live
    generation is disabled regardless of the caller's settings or API key.
    No ticket from the user's normal working state is changed.
    """
    output = Path(output)
    if output.exists():
        raise ValueError("演示输出目录已存在，请使用新目录保留历史。")
    output.mkdir(parents=True)
    isolated = replace(settings, data_dir=output / "state", allow_live=False)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_agent_acceptance_demo",
        "components": "real BM25 / LangGraph / SQLite; deterministic offline Agent client",
        "note": "真实模型API请求0次；离线Agent夹具仅验证工具与工单流程，不代表LLM回答质量。",
        "checks": [], "snapshots": {}, "model_requests": None,
        "output_directory": str(output.resolve()),
    }
    engine = None
    initial_used = None

    def check(name, condition, detail):
        report["checks"].append({"case": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"V2演示验收失败：{name}；{detail}")

    def denied(operation):
        try:
            operation()
        except Conflict:
            return True
        return False

    def snapshot(ticket):
        return {key: ticket[key] for key in ("id", "version", "status", "question", "strategy", "mode",
                                             "draft", "draft_hash", "evidence", "quality", "trace",
                                             "approval", "reply", "messages", "versions", "events")}

    try:
        engine = Engine(isolated)
        initial_used = engine.budget.summary()["used"]
        job = engine.enqueue(QUESTION, method="keyword", live=False, strategy="agent")
        check("background_job_created", job["status"] in ("queued", "running"), "通过后台队列创建只读Agent工单。")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = engine.store.get_job(job["id"])
            if job["status"] in ("completed", "failed", "interrupted", "stale"):
                break
            time.sleep(0.025)
        report["job"] = job
        check("background_job_completed", job["status"] == "completed", "在10秒轮询窗口内完成后台离线生成。")
        ticket = engine.store.get(job["ticket_id"])
        tid = ticket["id"]
        report["ticket_id"] = tid
        report["snapshots"]["initial"] = snapshot(ticket)
        check("awaiting_human_review", ticket["status"] == "awaiting_review" and ticket["approval"] is None and ticket["reply"] is None,
              "Agent生成后停在人工审核，未自动审批或提交。")
        tools = [step.get("tool") for step in ticket["trace"] if step.get("kind") == "tool"]
        check("read_only_tool_trace", "get_ticket_context" in tools and "search_knowledge" in tools,
              "保存当前工单上下文读取与知识库检索的真实工具执行轨迹。")
        check("quote_validation_recorded", ticket["quality"]["validation"] == "quote_presence_only" and ticket["quality"]["checked_quotes"] > 0,
              "仅验证逐条引文存在，不将其当作语义支持证明。")
        evidence_ids = {item["chunk_id"] for item in ticket["evidence"]}
        check("citations_from_actual_evidence", bool(ticket["draft"]["citations"]) and set(ticket["draft"]["citations"]) <= evidence_ids,
              "草稿引用来自本次真实BM25召回的片段。")
        check("unreviewed_submit_denied", denied(lambda: engine.submit(tid, ticket["version"])), "未审核的草稿不能提交。")

        followed = engine.followup(tid, FOLLOWUP, method="keyword", live=False, strategy="agent")
        report["snapshots"]["followup"] = snapshot(followed)
        check("followup_creates_new_version", followed["version"] == 2 and followed["status"] == "awaiting_review",
              "用户补充信息产生v2，保留v1及原始问题。")
        user_messages = [item["content"] for item in followed["messages"] if item["role"] == "user"]
        check("conversation_history_preserved", len(user_messages) == 2 and user_messages[-1] == FOLLOWUP,
              "补充信息作为新消息保存，没有覆盖原问题。")
        approved = engine.review(tid, followed["version"], True)
        check("approval_binds_draft", approved["status"] == "approved" and approved["approval"]["draft_hash"] == approved["draft_hash"],
              "审批绑定当前版本及草稿、证据、质量记录的摘要。")
        edited = engine.edit(tid, approved["version"], approved["draft"]["answer"] + "\n人工补充：继续核对目标设备路径和密钥编号，勿提供密钥值。")
        report["snapshots"]["edited"] = snapshot(edited)
        check("manual_edit_invalidates_approval", edited["version"] == 3 and edited["approval"] is None and edited["status"] == "awaiting_review",
              "人工编辑产生v3，v2审批失效。")
        check("manual_edit_resets_quote_quality", edited["quality"]["validation"] == "manual_review_required",
              "人工编辑后不沿用旧的逐条引文校验结果。")
        check("old_version_review_denied", denied(lambda: engine.review(tid, 2, True)), "旧版本审批不能批准v3草稿。")
        check("edited_unreviewed_submit_denied", denied(lambda: engine.submit(tid, 3)), "v3必须重新审核后才能提交。")

        engine.close()
        engine = Engine(isolated)
        restored = engine.store.get(tid)
        check("restart_preserves_review", restored["status"] == "awaiting_review" and restored["draft"] == edited["draft"] and restored["draft_hash"] == edited["draft_hash"],
              "关闭并重建Engine后，SQLite检查点恢复v3审核，不重新生成。")
        engine.review(tid, 3, True)
        timed_out = False
        try:
            engine.submit(tid, 3, simulate_timeout=True)
        except TimeoutError:
            timed_out = True
        ambiguous = engine.store.get(tid)
        report["snapshots"]["committed_then_timeout"] = snapshot(ambiguous)
        check("committed_then_timeout", timed_out and ambiguous["status"] == "submission_failed" and ambiguous["reply"] is not None,
              "模拟本地回复已写入，但提交响应超时。")
        reply_id = ambiguous["reply"]["id"]

        engine.close()
        engine = Engine(isolated)
        recovered = engine.retry(tid)
        check("restart_retry_recovers", recovered["status"] == "submitted" and recovered["reply"]["id"] == reply_id,
              "再次重启后只重试失败提交，复用已经存在的同一回复。")
        repeated = engine.submit(tid, 3)
        check("repeated_submit_one_reply", repeated["reply"]["id"] == reply_id and engine.store.reply_count(tid) == 1,
              "重复提交仍只有一条本地模拟工单回复。")
        check("idempotency_event_recorded", any(item["kind"] == "idempotent_replay" for item in repeated["events"]),
              "幂等重放留有审计事件。")
        check("three_versions_preserved", {item["version"] for item in repeated["versions"]} == {1, 2, 3},
              "原始生成、补充生成、人工编辑三个版本均可追溯。")
        report["model_requests"] = engine.budget.summary()["used"] - initial_used
        check("zero_llm_requests", report["model_requests"] == 0, "所有生成明确使用离线Agent客户端，无模型API请求。")
        report["snapshots"]["final"] = snapshot(repeated)
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if engine is not None:
            if initial_used is not None:
                report["model_requests"] = engine.budget.summary()["used"] - initial_used
            engine.close()
        report["passed"] = sum(item["passed"] for item in report["checks"])
        report["total"] = len(report["checks"])
        (output / "demo.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# DocFlow V2 离线验收演示", "", report["note"], "",
                 f"状态：{report.get('status', 'failed')}；通过：{report['passed']}/{report['total']}。", "",
                 "| 检查 | 结果 | 说明 |", "|---|---|---|"]
        lines.extend(f"| {item['case']} | {'通过' if item['passed'] else '失败'} | {item['detail']} |" for item in report["checks"])
        lines += ["", "详细工具轨迹、草稿、引文校验、版本、审批和幂等重放事件见demo.json。",
                  "SQLite演示状态在state子目录，未修改正常工作台的工单。", ""]
        (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
