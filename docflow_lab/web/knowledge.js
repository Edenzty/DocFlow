"use strict";
(() => {
  const host = document.getElementById("view-knowledge");
  if (!host) return;
  const state = { current: null, staged: null, expected: null, formVersion: 0 };
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = String(text); if (cls) e.className = cls; return e; };
  const panel = node("section", undefined, "panel kb-workspace");
  // Authored static markup only. Every document, question and result is rendered with textContent.
  panel.innerHTML = `<h2>文档导入与发布</h2><p>选择 UTF-8 Markdown / TXT，或粘贴正文。预览后发布才会进入检索。每个草稿保留生成时的知识库版本。</p>
    <p id="kb-active" class="kb-code"></p><p id="kb-status" role="status"></p>
    <form id="kb-import-form" class="kb-form"><label>文档 ID（更新时复用）<input id="kb-doc-id" required maxlength="80" pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" value="my-manual"></label>
    <label>标题<input id="kb-title" required maxlength="160" value="我的技术手册"></label><label>来源<input id="kb-source" required maxlength="300" value="本地原创文档"></label>
    <label>本地文件<input id="kb-file" type="file" accept=".md,.txt,text/plain,text/markdown"></label>
    <label class="kb-wide">正文<textarea id="kb-content" rows="7" required maxlength="200000" placeholder="# 接口排查手册&#10;在这里粘贴技术说明，或选择文件。"></textarea></label>
    <div class="kb-actions kb-wide"><button type="submit" class="button button-primary">预览切分</button><button id="kb-publish" type="button" class="button" disabled>发布此修订</button><button id="kb-index" type="button" class="button">构建当前版本向量索引</button></div></form>
    <p>向量构建仅使用本地 BGE 缓存；首次下载请在终端显式执行 <code>python -m docflow_lab build-index</code>。关键词检索在发布后即可使用。</p>
    <div id="kb-preview"></div><h3>自定义文档与修订</h3><div id="kb-custom"></div><div id="kb-versions"></div>
    <h3>知识库快照</h3><p>回滚只影响新查询；已有草稿的证据保持原样。</p><div id="kb-snapshots"></div>`;
  host.insertBefore(panel, document.getElementById("knowledge-list"));
  const traces = node("section", undefined, "panel kb-workspace");
  traces.innerHTML = `<h2>检索追踪</h2><p>记录真实执行的查询、检索方法、快照、排名与耗时；不表示模型的内部推理。</p><div class="kb-actions"><input id="kb-ticket-filter" placeholder="工单 ID（留空查看最近记录）" aria-label="工单筛选"><button id="kb-traces-refresh" class="button" type="button">刷新检索记录</button></div><div id="kb-traces"></div>`;
  host.append(traces);
  const $ = id => document.getElementById(id);
  function status(text, error = false) { $("kb-status").textContent = text; $("kb-status").className = error ? "kb-error" : "kb-note"; }
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json", "X-DocFlow-Local":"1"}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) { const error = new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)); error.status = response.status; throw error; }
    return data;
  }
  function button(label, fn) { const b = node("button", label, "button"); b.type = "button"; b.addEventListener("click", () => act(b, fn)); return b; }
  async function act(b, fn) { b.disabled = true; try { await fn(); } catch (e) { status(e.message, true); if (e.status === 409) await refresh(); } finally { b.disabled = false; $("kb-publish").disabled = !state.staged; } }
  function showPreview(version) {
    const root = $("kb-preview"); root.replaceChildren();
    root.append(node("h3", `预览 ${version.title} · ${version.version} · ${version.chunk_count} 个片段`), node("p", `修订 ${version.version_id}；尚需明确发布。`, "kb-code"));
    for (const c of version.chunks.slice(0, 15)) { const d = node("details"); d.append(node("summary", `${c.section} · ${c.chunk_id}`), node("pre", c.text)); root.append(d); }
    if (version.chunks.length > 15) root.append(node("p", `共 ${version.chunks.length} 个片段，此处先展示前 15 个。`));
  }
  async function editVersion(id) { const v = await api(`/api/knowledge/versions/${encodeURIComponent(id)}`); $("kb-doc-id").value = v.document_id; $("kb-title").value = v.title; $("kb-source").value = v.source; $("kb-content").value = v.content; $("kb-file").value = ""; state.staged = null; $("kb-publish").disabled = true; status(`已载入 ${v.version}；修改后重新预览、发布。`); }
  async function loadVersions(id) { const data = await api(`/api/knowledge/documents/${encodeURIComponent(id)}/versions`); const target = $("kb-versions"); target.replaceChildren(node("h3", `${id} 的修订历史`)); for (const v of data.versions) { const row = node("div", undefined, "kb-row"); row.append(node("span", `${v.version} · ${v.version_id}`), button("载入此修订", () => editVersion(v.version_id))); target.append(row); } }
  async function refresh() {
    const previousSnapshot = state.current?.snapshot_id;
    const previousVectorReady = state.current?.vector_ready;
    state.current = await api("/api/knowledge");
    if (previousSnapshot && (previousSnapshot !== state.current.snapshot_id || previousVectorReady !== state.current.vector_ready)) document.dispatchEvent(new Event("docflow:knowledge-changed"));
    $("kb-active").textContent = `当前快照 ${state.current.snapshot_id} · ${state.current.documents.length} 篇 / ${state.current.chunks} 片段 · 向量索引${state.current.vector_ready ? "已就绪" : "未构建"}`;
    const custom = $("kb-custom"); custom.replaceChildren();
    const docs = state.current.documents.filter(d => !d.is_base);
    if (!docs.length) custom.append(node("p", "还没有发布自定义文档。基础文档受冻结评测保护。"));
    for (const d of docs) { const row = node("div", undefined, "kb-row"); row.append(node("span", `${d.title} · ${d.document_id} · ${d.version}`), button("修订", () => editVersion(d.version_id)), button("历史", () => loadVersions(d.document_id)), button("退役", async () => { await api("/api/knowledge/remove", {document_id:d.document_id, expected_snapshot_id:state.current.snapshot_id}); await refresh(); status("文档已退出新查询，历史修订和旧草稿仍保留。"); })); custom.append(row); }
    const data = await api("/api/knowledge/snapshots"); const snapshots = $("kb-snapshots"); snapshots.replaceChildren();
    for (const s of data.snapshots) { const row = node("div", undefined, "kb-row"); row.append(node("span", `${s.snapshot_id} · ${s.action} · ${s.documents.length} 篇`, "kb-code")); if (s.snapshot_id === state.current.snapshot_id) row.append(node("strong", "当前")); else row.append(button("切换到此快照", async () => { await api("/api/knowledge/activate", {snapshot_id:s.snapshot_id, expected_snapshot_id:state.current.snapshot_id}); await refresh(); status("已切换知识库快照，新查询立即使用该版本。"); })); snapshots.append(row); }
  }
  async function loadTraces() {
    const filter = $("kb-ticket-filter").value.trim(); const data = await api(`/api/retrieval-traces?limit=20${filter ? `&ticket_id=${encodeURIComponent(filter)}` : ""}`);
    const target = $("kb-traces"); target.replaceChildren();
    if (!data.traces.length) target.append(node("p", "还没有检索记录。请先在工作台查询或生成草稿。"));
    for (const t of data.traces) { const d = node("details"); d.append(node("summary", `${t.method} · ${t.elapsed_ms.toFixed(1)} ms · ${t.query}`), node("p", `快照 ${t.snapshot_id} · ${t.stage} · 工单 ${t.ticket_id || "独立查询"} / v${t.ticket_version || "—"}`, "kb-code")); if (t.error) d.append(node("p", t.error, "kb-error")); for (const r of t.results) d.append(node("h4", `#${r.rank} ${r.title} / ${r.section} · ${r.version}`), node("p", `片段 ${r.chunk_id} · 来源 ${r.source}`, "kb-code"), node("pre", r.text)); target.append(d); }
  }
  $("kb-file").addEventListener("change", async () => { const f = $("kb-file").files[0]; if (!f) return; const revision = ++state.formVersion; if (!/\.(md|txt)$/i.test(f.name) || f.size > 512 * 1024) { status("请选择不超过512KiB的Markdown/TXT文件。", true); return; } const content = await f.text(); if (revision !== state.formVersion) return; $("kb-content").value = content; state.staged = null; $("kb-publish").disabled = true; });
  $("kb-import-form").addEventListener("input", () => { state.formVersion += 1; state.staged = null; $("kb-publish").disabled = true; });
  $("kb-import-form").addEventListener("submit", e => { e.preventDefault(); const b = e.submitter; act(b, async () => { if (!state.current) await refresh(); const revision = state.formVersion; const expected = state.current.snapshot_id; const f = $("kb-file").files[0]; const v = await api("/api/knowledge/import", {document_id:$("kb-doc-id").value, title:$("kb-title").value, source:$("kb-source").value, filename:f ? f.name : "pasted.md", content:$("kb-content").value}); if (revision !== state.formVersion) { status("预览期间表单已改变，请重新预览当前内容。"); return; } state.staged = v; state.expected = expected; showPreview(v); status("已保存待发布修订；在线检索尚未改变。核对切分后点击发布。"); }); });
  $("kb-publish").addEventListener("click", () => act($("kb-publish"), async () => { if (!state.staged) return; await api("/api/knowledge/publish", {version_id:state.staged.version_id, expected_snapshot_id:state.expected}); state.staged = null; await refresh(); status("发布完成。新查询使用新快照；旧草稿的来源保持原样。"); }));
  $("kb-index").addEventListener("click", () => act($("kb-index"), async () => { if (!state.current) await refresh(); status("正在使用本地BGE缓存构建索引，请等待…"); await api("/api/knowledge/index", {expected_snapshot_id:state.current.snapshot_id}); await refresh(); status("当前版本向量索引已构建。"); }));
  $("kb-traces-refresh").addEventListener("click", () => act($("kb-traces-refresh"), loadTraces));
  document.querySelectorAll('[data-view="knowledge"]').forEach(b => b.addEventListener("click", () => { refresh().then(loadTraces).catch(e => status(e.message, true)); }));
  refresh().catch(e => status(e.message, true));
})();
