"""Durable graph: retrieve -> generate -> review interrupt -> submit interrupt -> submit.

The review interrupt contains no side effect before it. The submit node may run
again after failure; the transactional store makes that replay idempotent.
"""
from pathlib import Path
import sqlite3
from threading import RLock
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import Settings
from .model import DeepSeekModel, MockModel, ModelError, validate_draft
from .retrieval import KeywordRetriever, VectorRetriever, load_chunks
from .storage import Conflict, RequestBudget, Store


class FlowState(TypedDict, total=False):
    ticket_id: str
    version: int
    question: str
    method: str
    live: bool
    evidence: list[dict]
    draft: dict
    approved: bool
    submit_requested: bool
    simulate_timeout: bool
    reply: dict


class Engine:
    def __init__(self, settings: Settings, *, model=None, retrievers=None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "tickets.sqlite3")
        self.budget = RequestBudget(settings.budget_path)
        self.lock = RLock()  # Deliberately single-process; serialize graph mutations.
        self.chunks = load_chunks(settings.knowledge_dir)
        self.retrievers = retrievers or {
            "keyword": KeywordRetriever(self.chunks),
            "vector": VectorRetriever(self.chunks, settings.vector_dir, model_path=settings.model_path),
        }
        self.injected_model = model
        self.connection = sqlite3.connect(str(settings.data_dir / "checkpoints.sqlite3"), check_same_thread=False)
        self.checkpointer = SqliteSaver(self.connection)
        graph = StateGraph(FlowState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("generate", self._generate)
        graph.add_node("human_review", self._review)
        graph.add_node("await_submit", self._await_submit)
        graph.add_node("submit_local_reply", self._submit)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "generate")
        graph.add_edge("generate", "human_review")
        graph.add_conditional_edges("human_review", lambda state: "await_submit" if state["approved"] else END)
        graph.add_edge("await_submit", "submit_local_reply")
        graph.add_edge("submit_local_reply", END)
        self.graph = graph.compile(checkpointer=self.checkpointer)

    def close(self):
        self.connection.close()

    def _config(self, ticket: dict):
        return {"configurable": {"thread_id": f"{ticket['id']}:v{ticket['version']}"}}

    def search(self, question: str, method: str) -> list[dict]:
        if method not in self.retrievers:
            raise Conflict("仅支持keyword或vector检索")
        return self.retrievers[method].search(question, top_k=5)

    def _retrieve(self, state):
        evidence = self.search(state["question"], state["method"])
        self.store.event(state["ticket_id"], "retrieved", f"v{state['version']} 使用{state['method']}，返回{len(evidence)}个片段")
        return {"evidence": evidence}

    def _generate(self, state):
        ticket = self.store.get(state["ticket_id"])
        if ticket["version"] != state["version"]:
            raise Conflict("旧草稿工作流已失效")
        if ticket["draft"]:
            return {"draft": ticket["draft"], "evidence": ticket["evidence"]}
        model = self.injected_model or (DeepSeekModel(self.budget, self.settings.model) if state["live"] else MockModel())
        try:
            draft = validate_draft(model.generate(state["question"], state["evidence"]), state["evidence"])
        finally:
            if isinstance(model, DeepSeekModel) and model.last_call_id:
                call = next(c for c in self.budget.records() if c["id"] == model.last_call_id)
                self.store.event(state["ticket_id"], "model_call", f"DeepSeek请求{call['id']}；{call['status']}；{call['elapsed_ms']:.0f}ms；输入token={call['prompt_tokens']}；输出token={call['completion_tokens']}")
            elif isinstance(model, MockModel):
                self.store.event(state["ticket_id"], "model_call", "离线摘录模拟：真实模型请求0次")
        self.store.save_draft(state["ticket_id"], state["version"], draft, state["evidence"])
        return {"draft": draft}

    def _review(self, state):
        persisted = self.store.get(state["ticket_id"])
        if persisted["version"] != state["version"]:
            raise Conflict("旧版本审核节点已失效")
        if persisted["approval"]:
            # Recover a crash between the approval transaction and graph checkpoint.
            return {"approved": persisted["approval"]["approved"]}
        review = interrupt({"action": "human_review", "version": state["version"], "draft": state["draft"]})
        if not isinstance(review, dict) or review.get("version") != state["version"] or type(review.get("approved")) is not bool:
            raise Conflict("无效审批或草稿版本不匹配")
        self.store.approve(state["ticket_id"], state["version"], review["approved"])
        return {"approved": review["approved"]}

    def _await_submit(self, state):
        request = interrupt({"action": "explicit_submit", "version": state["version"]})
        if not isinstance(request, dict) or request.get("version") != state["version"] or request.get("submit") is not True:
            raise Conflict("必须明确提交当前已审核版本")
        return {"submit_requested": True, "simulate_timeout": request.get("simulate_timeout") is True}

    def _submit(self, state):
        reply = self.store.submit(state["ticket_id"], state["version"], state.get("simulate_timeout", False))
        return {"reply": reply}

    def _check_live(self, live):
        if live and not self.settings.allow_live:
            raise Conflict("服务未显式开启真实模型；请用serve --live启动并确认请求预算")

    def create(self, question: str, method="keyword", live=False):
        with self.lock:
            self._check_live(live)
            tid = self.store.create(question, method, live)
            return self._run_new(self.store.get(tid))

    def _run_new(self, ticket):
        state = {"ticket_id": ticket["id"], "version": ticket["version"], "question": ticket["question"],
                 "method": ticket["method"], "live": ticket["mode"] == "live"}
        try:
            self.graph.invoke(state, self._config(ticket))
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ModelError, Conflict, RuntimeError)) else "生成失败，请查看本地依赖与检索索引"
            self.store.error(ticket["id"], "generation", message)
        return self.store.get(ticket["id"])

    def regenerate(self, tid, question=None, method=None, live=False):
        with self.lock:
            self._check_live(live)
            old = self.store.get(tid)
            ticket = self.store.regenerate(tid, question or old["question"], method or old["method"], live)
            return self._run_new(ticket)

    def review(self, tid, version, approved):
        with self.lock:
            ticket = self.store.get(tid)
            if ticket["version"] != version:
                raise Conflict("旧版本审批不能用于当前草稿")
            if ticket["approval"]:
                if ticket["approval"]["approved"] == approved:
                    if "human_review" in self.graph.get_state(self._config(ticket)).next:
                        self.graph.invoke(None, self._config(ticket))
                    return ticket
                raise Conflict("该版本已审核，请生成新版本")
            if ticket["status"] != "awaiting_review":
                raise Conflict("当前没有待审核草稿")
            self.graph.invoke(Command(resume={"version": version, "approved": approved}), self._config(ticket))
            return self.store.get(tid)

    def submit(self, tid, version, simulate_timeout=False):
        with self.lock:
            ticket = self.store.get(tid)
            if ticket["version"] != version or not ticket["approval"] or not ticket["approval"]["approved"]:
                raise Conflict("当前版本尚未通过人工审核")
            if ticket["status"] == "submitted":
                self.store.submit(tid, version)
                return self.store.get(tid)
            if ticket["status"] == "submission_failed":
                return self.retry(tid)
            if "human_review" in self.graph.get_state(self._config(ticket)).next:
                self.graph.invoke(None, self._config(ticket))
            self.graph.invoke(Command(resume={"version": version, "submit": True, "simulate_timeout": simulate_timeout}), self._config(ticket))
            return self.store.get(tid)

    def retry(self, tid):
        with self.lock:
            ticket = self.store.get(tid)
            if ticket["status"] == "submitted":
                return ticket
            if ticket["status"] not in ("generation_failed", "submission_failed"):
                raise Conflict("仅失败的生成或提交可以重试；待审核任务请先审核")
            if ticket["mode"] == "live" and ticket["status"] == "generation_failed":
                self._check_live(True)
            try:
                self.graph.invoke(None, self._config(ticket))
            except (ModelError, RuntimeError, Conflict) as exc:
                self.store.error(tid, "generation" if ticket["status"] == "generation_failed" else "submission", str(exc))
            return self.store.get(tid)
