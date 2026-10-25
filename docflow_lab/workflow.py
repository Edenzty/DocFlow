"""Durable graph: retrieve -> generate -> review interrupt -> submit interrupt -> submit.

The review interrupt contains no side effect before it. The submit node may run
again after failure; the transactional store makes that replay idempotent.
"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import RLock, Lock
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import Settings
from .agent import SupportAgent
from .model import DeepSeekModel, MockModel, ModelError, safe_model_text, validate_draft
from .retrieval import KeywordRetriever, VectorRetriever, load_chunks
from .retrieval_v2 import HybridRetriever, expand_evidence
from .storage import Conflict, RequestBudget, Store


class FlowState(TypedDict, total=False):
    ticket_id: str
    version: int
    question: str
    method: str
    live: bool
    strategy: str
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
        if "keyword" in self.retrievers and "vector" in self.retrievers:
            self.retrievers["hybrid"] = HybridRetriever(self.chunks, self.retrievers["keyword"], self.retrievers["vector"])
        self.injected_model = model
        self.job_lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docflow-generation")
        self.closed = False
        # Recovery is explicit: restarting never silently spends a paid request.
        self.store.recover_jobs()
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
        if not self.closed:
            self.closed = True
            self.executor.shutdown(wait=True)
            self.connection.close()

    def _config(self, ticket: dict):
        return {"configurable": {"thread_id": f"{ticket['id']}:v{ticket['version']}"}}

    def search(self, question: str, method: str) -> list[dict]:
        if method not in self.retrievers:
            raise Conflict("检索方法不可用；请选择keyword、vector或hybrid")
        return self.retrievers[method].search(question, top_k=5)

    def _options(self, method, strategy, live):
        self._check_live(live)
        if method not in self.retrievers or strategy not in ("rag", "agent"):
            raise Conflict("检索方法或生成策略不支持")
        if self.closed:
            raise Conflict("服务已关闭")

    @staticmethod
    def _question(ticket):
        followups = [m["content"] for m in ticket["messages"] if m["role"] == "user"][1:]
        return (ticket["question"] + ("\n补充信息：\n" + "\n".join(followups[-5:]) if followups else ""))[:6000]

    def _retrieve(self, state):
        ticket = self.store.get(state["ticket_id"])
        if ticket["version"] != state["version"]:
            raise Conflict("旧版本检索节点已失效")
        if ticket["draft"]:
            return {"evidence": ticket["evidence"]}
        query = state["question"]
        if state.get("strategy") == "agent" or sum(m["role"] == "user" for m in ticket["messages"]) > 1:
            latest = [m["content"] for m in ticket["messages"] if m["role"] == "user"][-1]
            query = (ticket["question"][:150] + " " + latest[-149:])[:300]
        evidence = self.search(query, state["method"])
        self.store.event(state["ticket_id"], "retrieved", f"v{state['version']} 使用{state['method']}，返回{len(evidence)}个片段")
        return {"evidence": evidence}

    def _generate(self, state):
        ticket = self.store.get(state["ticket_id"])
        if ticket["version"] != state["version"]:
            raise Conflict("旧草稿工作流已失效")
        if ticket["draft"]:
            return {"draft": ticket["draft"], "evidence": ticket["evidence"]}
        if state.get("strategy") == "agent":
            agent = SupportAgent(self.budget, self.settings.model, live=state["live"])
            try:
                result = agent.run(state["question"], state["evidence"],
                                   search=self.search,
                                   expand=lambda seeds, max_chunks=15: expand_evidence(self.chunks, seeds, max_chunks=max_chunks),
                                   context={"ticket_id": ticket["id"], "version": ticket["version"],
                                            "question": ticket["question"], "messages": ticket["messages"][-8:],
                                            "versions": [{"version": v["version"], "status": v["status"]} for v in ticket["versions"][:5]]})
                draft = validate_draft(result["draft"], result["evidence"])
                self.store.save_draft(ticket["id"], ticket["version"], draft, result["evidence"], result["quality"], result["trace"])
                return {"draft": draft, "evidence": result["evidence"]}
            except Exception:
                self.store.error(ticket["id"], "generation", "Agent生成失败；未自动重试", trace=agent.last_trace)
                raise
            finally:
                self.store.event(ticket["id"], "agent_run", f"只读Agent；真实模型请求{len(agent.last_call_ids)}次；无审核或提交权限")
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

    def create(self, question: str, method="keyword", live=False, strategy="rag"):
        with self.lock:
            self._options(method, strategy, live)
            tid = self.store.create(question, method, live, strategy)
            return self._run_new(self.store.get(tid))

    def _run_new(self, ticket):
        state = {"ticket_id": ticket["id"], "version": ticket["version"], "question": self._question(ticket),
                 "method": ticket["method"], "live": ticket["mode"] == "live", "strategy": ticket["strategy"]}
        try:
            self.graph.invoke(state, self._config(ticket))
        except Exception as exc:
            message = safe_model_text(str(exc)) if isinstance(exc, (ModelError, Conflict, RuntimeError)) else "生成失败，请查看本地依赖与检索索引"
            self.store.error(ticket["id"], "generation", message)
        return self.store.get(ticket["id"])

    def regenerate(self, tid, question=None, method=None, live=False, strategy=None):
        with self.lock:
            old = self.store.get(tid)
            self._options(method or old["method"], strategy or old["strategy"], live)
            ticket = self.store.regenerate(tid, question or old["question"], method or old["method"], live, strategy)
            return self._run_new(ticket)

    def followup(self, tid, message, method=None, live=False, strategy=None):
        with self.lock:
            old = self.store.get(tid)
            self._options(method or old["method"], strategy or old["strategy"], live)
            ticket = self.store.regenerate(tid, old["question"], method or old["method"], live, strategy, message=message)
            return self._run_new(ticket)

    def edit(self, tid, version, answer):
        with self.lock:
            old = self.store.get(tid)
            if old["version"] != version or not old["draft"]:
                raise Conflict("编辑必须匹配当前已有草稿版本")
            draft = validate_draft({**old["draft"], "answer": answer}, old["evidence"])
            ticket = self.store.regenerate(tid, old["question"], old["method"], old["mode"] == "live")
            quality = {"status": "manual_review_required", "validation": "manual_review_required", "claims": [],
                       "checked_quotes": 0, "warnings": ["人工编辑后原来的逐条引文校验不再适用，必须重新核对全文与证据。"]}
            self.store.save_draft(tid, ticket["version"], draft, old["evidence"], quality,
                                  [{"kind": "manual_edit", "status": "completed", "detail": f"基于v{version}人工编辑；原审核失效"}])
            return self._run_new(self.store.get(tid))

    def enqueue(self, question, method="keyword", live=False, strategy="agent"):
        # A separate short lock lets job creation/reads proceed during a slow LLM request.
        with self.job_lock:
            self._options(method, strategy, live)
            tid = self.store.create(question, method, live, strategy, status="queued")
            try:
                job = self.store.create_job(tid, 1)
            except Conflict as exc:
                self.store.error(tid, "generation", str(exc))
                raise
            self.executor.submit(self._work_job, job["id"])
            return job

    def _work_job(self, jid):
        with self.lock:
            job = self.store.get_job(jid)
            ticket = self.store.get(job["ticket_id"])
            if job["version"] != ticket["version"]:
                self.store.set_job(jid, "stale", "工单已有新版本，旧任务不再执行")
                return
            if job["status"] != "queued":
                return
            self.store.set_job(jid, "running")
            self.store.set_status(ticket["id"], "awaiting_review" if ticket["draft"] else "generating")
            try:
                checkpoint = self.graph.get_state(self._config(ticket))
                if checkpoint.values:
                    self.graph.invoke(None, self._config(ticket))
                else:
                    self._run_new(ticket)
                current = self.store.get(ticket["id"])
                self.store.set_job(jid, "completed" if current["status"] in ("awaiting_review", "approved", "rejected", "submitted") else "failed", current["error"])
            except Exception as exc:
                message = safe_model_text(str(exc)) if isinstance(exc, (ModelError, Conflict, RuntimeError)) else "后台生成失败，请检查本地依赖"
                self.store.error(ticket["id"], "generation", message)
                self.store.set_job(jid, "failed", message)

    def retry_job(self, jid):
        with self.job_lock:
            job = self.store.get_job(jid)
            ticket = self.store.get(job["ticket_id"])
            self._options(ticket["method"], ticket["strategy"], ticket["mode"] == "live")
            if job["version"] != ticket["version"]:
                raise Conflict("旧版本后台任务不能重试")
            if job["status"] not in ("failed", "interrupted"):
                raise Conflict("仅失败或中断的后台任务可以明确重试")
            self.store.queue_retry(jid)
            self.executor.submit(self._work_job, jid)
            return self.store.get_job(jid)

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
                if self.graph.get_state(self._config(ticket)).values:
                    self.graph.invoke(None, self._config(ticket))
                else:
                    return self._run_new(ticket)
            except (ModelError, RuntimeError, Conflict) as exc:
                self.store.error(tid, "generation" if ticket["status"] == "generation_failed" else "submission", safe_model_text(str(exc)))
            return self.store.get(tid)
