"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { ticket: null, config: null, busy: false, evidence: [] };
  const statusLabels = { awaiting_review: "等待人工审核", rejected: "草稿已驳回", approved: "已批准 · 等待提交", submitted: "已提交", submission_failed: "提交中断 · 可重试", generation_failed: "生成失败 · 可重试" };
  const eventLabels = { created: "创建工单", retrieved: "检索证据", generated: "生成草稿", reviewed: "审核草稿", approved: "批准草稿", rejected: "驳回草稿", submitted: "提交完成", submission_failed: "提交中断", generation_failed: "生成失败", retried: "重试任务", resumed: "恢复工作流", model_call: "模型调用", draft_created: "草稿已保存", draft_generated: "草稿生成完成", local_reply_written: "本地系统已写入回复", idempotent_replay: "幂等重试 · 沿用已有回复", regenerated: "创建新草稿版本", review: "人工审核", submit: "本地提交", retrieve: "检索证据", generate: "生成草稿" };
  const methods = { keyword: "关键词 · BM25", vector: "向量 · BGE / Chroma" };

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = text(content);
    return node;
  }

  function message(content, kind = "info") {
    const node = byId("global-message");
    node.textContent = content;
    node.className = `message message-${kind}`;
    node.hidden = !content;
  }

  async function api(path, body, method = "POST") {
    const options = { method, headers: { Accept: "application/json" }, cache: "no-store" };
    if (method !== "GET") {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-DocFlow-Local"] = "1";
      options.body = JSON.stringify(body || {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (_) {
      throw new Error("无法连接本地服务。请确认 DocFlow 仍在运行，再载入工单继续。");
    }
    let data;
    try {
      data = await response.json();
    } catch (_) {
      throw new Error(`服务返回了无法读取的结果（HTTP ${response.status}）。`);
    }
    if (!response.ok) {
      const error = new Error(text(data.detail || data.error || `请求失败（HTTP ${response.status}）。`));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function selection() {
    return { question: byId("question").value.trim(), method: document.querySelector('input[name="method"]:checked').value, live: byId("live-mode").checked };
  }

  function setBusy(busy, trigger) {
    state.busy = busy;
    document.querySelectorAll("button").forEach((node) => { node.disabled = busy; });
    byId("question-form").setAttribute("aria-busy", String(busy));
    byId("ticket-panel").setAttribute("aria-busy", String(busy));
    document.querySelectorAll(".is-loading").forEach((node) => node.classList.remove("is-loading"));
    if (busy && trigger) trigger.classList.add("is-loading");
    if (!busy) applyPermissions();
  }

  async function operation(trigger, action) {
    if (state.busy) return;
    message("");
    setBusy(true, trigger);
    try {
      await action();
    } catch (error) {
      message(error.message || "操作失败，请稍后重试。", "error");
    } finally {
      setBusy(false);
      await loadConfig(false);
    }
  }

  function updateModelHint() {
    const live = byId("live-mode").checked;
    byId("live-mode").closest(".model-selection").classList.toggle("live-active", live);
    byId("model-hint").textContent = live
      ? `将调用 ${state.config?.model || "DeepSeek"}，消耗真实请求预算；问题和召回的片段会发送给模型。`
      : "当前使用离线模拟模型，不消耗请求预算。模拟输出用于验证流程。";
  }

  async function loadConfig(showError = true) {
    try {
      const config = await api("/api/config", null, "GET");
      state.config = config;
      const budget = config.budget || {};
      const remaining = Number(budget.remaining ?? 40);
      const limit = Number(budget.limit ?? 40);
      byId("budget-remaining").textContent = String(remaining);
      byId("budget-limit").textContent = String(limit);
      byId("budget-progress").style.width = `${limit > 0 ? Math.min(100, Math.max(0, remaining / limit * 100)) : 0}%`;
      byId("connection-status").textContent = "服务已连接";
      byId("connection-status").classList.remove("offline");
      byId("runtime-status").textContent = `${config.model || "DeepSeek"} · ${config.live_enabled ? "真实调用已配置" : "离线模拟可用"} · ${config.vector_ready ? "向量索引就绪" : "向量索引未就绪"}`;
      byId("vector-hint").textContent = config.vector_ready ? "向量索引已就绪；可切换检索方式，对比召回的 Top 5。" : "关键词检索可直接使用；请先在终端建立向量索引，再体验向量检索。";
      const liveControl = byId("live-mode");
      liveControl.disabled = !config.live_enabled || remaining <= 0;
      if (liveControl.disabled) liveControl.checked = false;
      liveControl.title = !config.live_enabled ? "服务端未开启真实调用或未配置密钥" : remaining <= 0 ? "真实请求预算已用完" : "发送问题和召回片段至 DeepSeek";
      updateModelHint();
    } catch (error) {
      byId("connection-status").textContent = "服务未连接";
      byId("connection-status").classList.add("offline");
      byId("runtime-status").textContent = "请先启动本地 DocFlow 服务";
      if (showError) message(error.message, "error");
    }
  }

  function renderFlow(ticket, hasEvidence) {
    const nodes = ["flow-retrieve", "flow-draft", "flow-review", "flow-submit"].map(byId);
    let active = 0;
    if (ticket?.draft) active = 2;
    if (ticket?.status === "approved" || ticket?.status === "submission_failed") active = 3;
    if (ticket?.status === "submitted") active = 4;
    if (!ticket && hasEvidence) active = 1;
    nodes.forEach((node, index) => {
      node.classList.toggle("done", index < active);
      node.classList.toggle("current", index === Math.min(active, 3));
      node.querySelector("span").textContent = index < active ? "✓" : String(index + 1);
    });
  }

  function focusEvidence(chunkId) {
    const evidence = Array.from(byId("evidence-list").children).find((node) => node.dataset.chunkId === chunkId);
    if (!evidence) return;
    document.querySelectorAll(".evidence-item.highlight").forEach((node) => node.classList.remove("highlight"));
    evidence.open = true;
    evidence.classList.add("highlight");
    evidence.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    evidence.querySelector("summary").focus({ preventScroll: true });
  }

  function renderEvidence(results, method) {
    state.evidence = Array.isArray(results) ? results : [];
    byId("evidence-panel").hidden = false;
    byId("evidence-count").textContent = String(state.evidence.length);
    byId("evidence-method").textContent = methods[method] || text(method);
    const list = byId("evidence-list");
    list.replaceChildren();
    if (!state.evidence.length) {
      list.append(element("p", "evidence-empty", "没有召回可用片段。可补充错误码、接口路径或具体操作后重试。"));
      return;
    }
    state.evidence.forEach((item, index) => {
      const details = element("details", "evidence-item");
      details.dataset.chunkId = item.chunk_id;
      const summary = element("summary");
      summary.append(element("span", "evidence-rank", String(index + 1).padStart(2, "0")));
      const heading = element("div", "evidence-summary");
      heading.append(element("h3", "", item.title || item.document_id), element("p", "", item.section || "正文"));
      summary.append(heading);
      if (typeof item.score === "number") summary.append(element("span", "evidence-score", `score ${item.score.toFixed(3)}`));
      summary.append(element("span", "evidence-chevron", "⌄"));
      const body = element("div", "evidence-body");
      body.append(element("pre", "", item.text), element("p", "evidence-source", `来源：${text(item.source)} · 版本：${text(item.version)}\n片段：${text(item.chunk_id)}`));
      details.append(summary, body);
      if (index === 0) details.open = true;
      list.append(details);
    });
  }

  function renderEvents(events) {
    const list = byId("event-list");
    list.replaceChildren();
    byId("events-panel").hidden = false;
    if (!Array.isArray(events) || !events.length) {
      list.append(element("li", "", "暂无处理记录。"));
      return;
    }
    events.forEach((event) => {
      const item = element("li");
      const date = new Date(event.created_at);
      const validDate = !Number.isNaN(date.getTime());
      const time = element("time", "", validDate ? date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : text(event.created_at));
      if (validDate) time.dateTime = date.toISOString();
      const detail = element("div");
      detail.append(element("h3", "", eventLabels[event.kind] || event.kind), element("p", "", event.detail));
      item.append(time, detail);
      list.append(item);
    });
  }

  function renderTicket(ticket, { scroll = false } = {}) {
    state.ticket = ticket;
    byId("empty-state").hidden = true;
    byId("ticket-panel").hidden = false;
    byId("ticket-id").textContent = text(ticket.id);
    byId("ticket-id-input").value = text(ticket.id);
    byId("ticket-question").textContent = text(ticket.question);
    byId("question").value = text(ticket.question);
    byId("draft-version").textContent = `v${ticket.version ?? 0}`;
    byId("ticket-status").textContent = statusLabels[ticket.status] || ticket.status;
    byId("ticket-status").className = `status-tag${ticket.status === "submitted" || ticket.status === "approved" ? " success" : ["submission_failed", "generation_failed", "rejected"].includes(ticket.status) ? " error" : ""}`;
    byId("ticket-mode").textContent = ticket.mode === "live" ? "真实模型 · DeepSeek" : "离线模拟 · 非真实模型结果";
    byId("ticket-mode").classList.toggle("live", ticket.mode === "live");
    byId("ticket-method").textContent = methods[ticket.method] || ticket.method;
    byId("ticket-error").textContent = text(ticket.error);
    byId("ticket-error").hidden = !ticket.error;
    const draft = ticket.draft || {};
    byId("draft-decision").textContent = { answer: "基于证据的回复建议", needs_info: "需要补充信息", out_of_scope: "知识库未覆盖" }[draft.decision] || "等待生成草稿";
    byId("mock-note").textContent = ticket.mode === "live" ? "请人工检查事实与引用" : "模拟模型仅验证工程流程";
    byId("draft-answer").textContent = text(draft.answer || "当前没有可审核的草稿，请查看失败记录并重试。");
    const citations = byId("draft-citations");
    citations.replaceChildren();
    (draft.citations || []).forEach((citation, index) => {
      const chunkId = typeof citation === "string" ? citation : citation.chunk_id;
      const evidence = (ticket.evidence || []).find((item) => item.chunk_id === chunkId);
      const button = element("button", "citation-button", `[${index + 1}] ${evidence ? `${evidence.title} · ${evidence.section}` : text(chunkId)}`);
      button.type = "button";
      button.title = evidence ? `查看原文片段 ${chunkId}` : `未找到此引用片段：${chunkId}`;
      button.disabled = !evidence;
      button.addEventListener("click", () => focusEvidence(chunkId));
      citations.append(button);
    });
    const approval = ticket.approval;
    byId("approval-description").textContent = approval?.approved && Number(approval.version) === Number(ticket.version)
      ? `v${ticket.version} 已通过人工审核。`
      : approval && !approval.approved ? "当前草稿已驳回。可以修改问题并生成新版本，再重新审核。" : "核对引用和建议后，再批准当前版本。";
    const reply = ticket.reply;
    byId("reply-panel").hidden = !reply;
    if (reply) {
      byId("reply-id").textContent = `回复编号：${text(reply.id)} · 草稿 v${reply.version}`;
      byId("reply-body").textContent = text(reply.body);
      byId("reply-note").textContent = ticket.status === "submitted" ? "工作流已确认提交完成。重复操作不会新增相同回复。" : "本地系统已有回复。请点击“重试失败步骤”确认结果，幂等提交不会重复写入。";
    }
    renderEvidence(ticket.evidence || [], ticket.method);
    renderEvents(ticket.events);
    renderFlow(ticket, true);
    applyPermissions();
    try {
      localStorage.setItem("docflow.lastTicket", text(ticket.id));
    } catch (_) { /* A blocked browser store does not affect the server workflow. */ }
    if (scroll) byId("ticket-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function applyPermissions() {
    if (state.busy) return;
    const ticket = state.ticket;
    const reviewed = ticket?.approval?.approved && Number(ticket.approval.version) === Number(ticket.version);
    const reviewable = Boolean(ticket?.draft && ticket.status === "awaiting_review" && !ticket.approval);
    byId("approve-button").disabled = !reviewable || Boolean(reviewed);
    byId("reject-button").disabled = !reviewable || ticket?.status === "rejected";
    byId("submit-button").disabled = !(ticket && reviewed && ticket.status === "approved");
    byId("retry-button").hidden = !ticket || !["submission_failed", "generation_failed"].includes(ticket.status);
    byId("retry-button").disabled = byId("retry-button").hidden;
    byId("regenerate-button").disabled = !ticket || ticket.status === "submitted" || Boolean(ticket.reply);
    byId("regenerate-button").title = ticket?.reply ? "本地系统已保存回复；提交中断时请重试确认，不再生成新版本。" : "生成新版本后，需要重新审核。";
    byId("refresh-button").disabled = !ticket;
    byId("copy-id-button").disabled = !ticket;
    byId("simulate-timeout").disabled = !ticket || ticket.status === "submitted";
    document.querySelectorAll(".citation-button").forEach((button) => { button.disabled = button.title.startsWith("未找到"); });
  }

  async function refreshTicket() {
    if (!state.ticket) return;
    renderTicket(await api(`/api/tickets/${encodeURIComponent(state.ticket.id)}`, null, "GET"));
  }

  async function mutateTicket(action, body) {
    const id = state.ticket.id;
    try {
      const ticket = await api(`/api/tickets/${encodeURIComponent(id)}/${action}`, body);
      renderTicket(ticket);
      return ticket;
    } catch (error) {
      // A timed-out response can follow a successful local write. Always re-read.
      try { await refreshTicket(); } catch (_) { /* Keep the original operation error. */ }
      throw error;
    }
  }

  byId("question-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!byId("question-form").reportValidity()) return;
    operation(byId("create-button"), async () => {
      const input = selection();
      if (!input.question) throw new Error("请先输入一个具体问题。");
      const ticket = await api("/api/tickets", input);
      renderTicket(ticket, { scroll: window.innerWidth < 801 });
      if (ticket.status === "generation_failed") {
        message("工单已保存，但草稿生成失败。请查看错误记录，修复后点击“重试失败步骤”。", "error");
      } else {
        message(input.live ? "真实模型草稿已生成。请核查证据，再审核当前版本。" : "离线模拟草稿已生成；它用于验证检索、审核和恢复流程，不代表真实模型效果。", "info");
      }
    });
  });

  byId("search-button").addEventListener("click", () => operation(byId("search-button"), async () => {
    const input = selection();
    if (!input.question) { byId("question").focus(); throw new Error("请先输入要检索的问题。"); }
    const result = await api("/api/search", { query: input.question, method: input.method });
    // Standalone search must not visually replace evidence bound to an existing draft.
    state.ticket = null;
    byId("ticket-panel").hidden = true;
    byId("events-panel").hidden = true;
    byId("empty-state").hidden = true;
    renderEvidence(result.results || [], input.method);
    renderFlow(null, true);
    message("仅展示本次检索结果，尚未创建工单。生成草稿后，会保存与该版本绑定的引用证据。", "info");
  }));

  byId("load-form").addEventListener("submit", (event) => {
    event.preventDefault();
    operation(byId("load-button"), async () => {
      const id = byId("ticket-id-input").value.trim();
      if (!id) throw new Error("请输入工单编号。");
      renderTicket(await api(`/api/tickets/${encodeURIComponent(id)}`, null, "GET"), { scroll: window.innerWidth < 801 });
      message("已载入服务端保存的工单状态。", "info");
    });
  });

  byId("approve-button").addEventListener("click", () => operation(byId("approve-button"), async () => {
    await mutateTicket("review", { version: state.ticket.version, approved: true });
    message("当前版本已批准。请单独点击“提交到本地工单”完成提交。", "info");
  }));
  byId("reject-button").addEventListener("click", () => operation(byId("reject-button"), async () => {
    await mutateTicket("review", { version: state.ticket.version, approved: false });
    message("草稿已驳回，可以修改问题并生成新版本。新版本需要重新审核。", "info");
  }));
  byId("submit-button").addEventListener("click", () => operation(byId("submit-button"), async () => {
    await mutateTicket("submit", { version: state.ticket.version, simulate_timeout: byId("simulate-timeout").checked });
    message("已提交到本地模拟工单系统。", "info");
  }));
  byId("retry-button").addEventListener("click", () => operation(byId("retry-button"), async () => {
    const ticket = await mutateTicket("retry", {});
    message(["generation_failed", "submission_failed"].includes(ticket.status) ? "重试仍未完成，请查看错误记录后处理。" : "已重试失败步骤，请查看工单当前状态与处理记录。", ["generation_failed", "submission_failed"].includes(ticket.status) ? "error" : "info");
  }));
  byId("regenerate-button").addEventListener("click", () => operation(byId("regenerate-button"), async () => {
    const input = selection();
    if (!input.question) throw new Error("请先输入问题，再生成新版本。");
    const ticket = await mutateTicket("draft", input);
    message(ticket.status === "generation_failed" ? "新版本已创建，但生成失败。请查看错误记录并重试。" : "新草稿已保存。旧版本的审批不能用于当前版本，请重新审核。", ticket.status === "generation_failed" ? "error" : "info");
  }));
  byId("refresh-button").addEventListener("click", () => operation(byId("refresh-button"), refreshTicket));
  byId("copy-id-button").addEventListener("click", async () => {
    if (!state.ticket) return;
    try {
      await navigator.clipboard.writeText(state.ticket.id);
      message("工单编号已复制。", "info");
    } catch (_) {
      byId("ticket-id-input").focus();
      byId("ticket-id-input").select();
      message("浏览器未开放剪贴板，已选中下方工单编号，可按 Ctrl+C 复制。", "info");
    }
  });
  document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => {
    byId("question").value = button.dataset.example;
    byId("question").focus();
  }));
  byId("live-mode").addEventListener("change", updateModelHint);
  try {
    const saved = localStorage.getItem("docflow.lastTicket");
    if (saved) byId("ticket-id-input").value = saved;
  } catch (_) { /* Server persistence remains available without localStorage. */ }
  applyPermissions();
  loadConfig();
})();
