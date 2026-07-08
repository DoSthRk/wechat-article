"use strict";
const $ = (s) => document.querySelector(s);
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let BUSY = false;
let pollTimer = null;

// ---------- 内容源 ----------
async function loadSources() {
  const box = $("#sources");
  try {
    const data = await (await fetch("/api/sources")).json();
    box.innerHTML = "";
    if (!data.lines || !data.lines.length) { box.innerHTML = '<p class="muted">没有内容线</p>'; return; }
    for (const line of data.lines) box.appendChild(renderLine(line));
    applyBusy();
  } catch (e) { box.innerHTML = `<p class="err">加载失败：${esc(e.message)}</p>`; }
}

function renderLine(line) {
  const wrap = el("div", "line-block");
  const head = el("div", "line-head");
  const counts = line.counts || {
    total: line.pdfs.length,
    pending: line.pdfs.filter((f) => !f.has_article).length,
    processed: line.pdfs.filter((f) => f.has_article).length,
    published: line.pdfs.filter((f) => f.published).length,
    blocked: line.pdfs.filter((f) => f.blocked).length,
  };
  head.innerHTML =
    `<b>${esc(line.name)}</b> <span class="tag">${esc(line.line_id)} → ${esc(line.account) || "?"}</span>` +
    `<span class="muted">inputs/pdfs/${esc(line.folder)}/ · 共 ${counts.total} 篇</span>` +
    `<span class="line-summary">未处理 <b>${counts.pending}</b> · 已处理 <b>${counts.processed}</b> · 已投 <b>${counts.published}</b>${counts.blocked ? ` · 质量闸 <b>${counts.blocked}</b>` : ""}</span>`;
  const acts = el("div", "line-acts");
  const fileInput = el("input"); fileInput.type = "file";
  fileInput.accept = "application/pdf,.pdf"; fileInput.multiple = true; fileInput.style.display = "none";
  fileInput.onchange = () => { uploadPdfs(line.line_id, fileInput.files); fileInput.value = ""; };
  const upBtn = el("button", "btn ghost", "⬆ 上传 PDF");
  upBtn.onclick = () => fileInput.click();
  const runSel = el("button", "btn run", "▶ 运行所选");
  const runNew = el("button", "btn run ghost", "▶ 运行全部未处理");
  acts.append(fileInput, upBtn, runSel, runNew);
  head.appendChild(acts);
  wrap.appendChild(head);

  // 拖拽上传：把 PDF 拖到该线区块即可（空文件夹也能接收第一篇）
  wrap.addEventListener("dragover", (e) => { e.preventDefault(); wrap.classList.add("drag"); });
  wrap.addEventListener("dragleave", (e) => { if (e.target === wrap) wrap.classList.remove("drag"); });
  wrap.addEventListener("drop", (e) => {
    e.preventDefault(); wrap.classList.remove("drag");
    const pdfs = [...(e.dataTransfer.files || [])].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    if (pdfs.length) uploadPdfs(line.line_id, pdfs);
    else alert("只接受 .pdf 文件");
  });

  if (!line.pdfs.length) { wrap.appendChild(el("p", "muted", "（该文件夹无 PDF · 上传或拖入 PDF）")); return wrap; }

  const pending = line.pdfs.filter((f) => !f.has_article);
  const processed = line.pdfs.filter((f) => f.has_article);
  if (pending.length) {
    wrap.appendChild(el("div", "src-section-title", "未处理 PDF（默认勾选，可直接运行）"));
    wrap.appendChild(renderPdfTable(pending, true));
  } else {
    wrap.appendChild(el("p", "muted", "（暂无未处理 PDF · 可上传新 PDF，或展开历史已处理文件重跑）"));
  }
  if (processed.length) {
    const details = el("details", "processed-box");
    const summary = el("summary");
    summary.innerHTML = `历史已处理 ${processed.length} 篇（默认收起，避免页面堆叠）`;
    details.appendChild(summary);
    details.appendChild(renderPdfTable(processed, false));
    wrap.appendChild(details);
  }

  runSel.onclick = () => {
    const picked = [...wrap.querySelectorAll(".pick:checked")];
    startRun(line.line_id, picked.map((c) => c.value), picked.filter((c) => c.dataset.processed === "1").length);
  };
  runNew.onclick = () => startRun(line.line_id, pending.map((f) => f.pdf), 0);
  return wrap;
}

function renderPdfTable(files, checkedByDefault) {
  const tbl = el("table", "src-grid");
  tbl.innerHTML = "<thead><tr><th></th><th>PDF 源文件</th><th>绑定文章</th><th>状态</th><th></th></tr></thead>";
  const tb = el("tbody");
  for (const f of files) {
    const tr = el("tr");
    const cb = el("input"); cb.type = "checkbox"; cb.value = f.pdf; cb.className = "pick";
    cb.checked = checkedByDefault;
    cb.dataset.processed = f.has_article ? "1" : "0";
    const c0 = el("td"); c0.appendChild(cb);
    const c1 = el("td", "src-name"); c1.textContent = f.name;
    const cBind = el("td", "src-bind");
    if (f.bound) {
      cBind.innerHTML = `<span class="arrow">↳</span><span class="mono">${esc(f.job_id)}</span>` +
        (f.title ? `<div class="bind-title">${esc(f.title)}</div>` : "");
    } else {
      cBind.innerHTML = '<span class="muted">未绑定</span>';
    }
    const c2 = el("td"); c2.appendChild(statusBadge(f));
    const c3 = el("td", "src-act");
    if (f.has_article) {
      const a = el("a", "link", "预览"); a.href = `/preview/${encodeURIComponent(f.job_id)}?wechat=1`; a.target = "_blank";
      c3.appendChild(a);
    }
    tr.append(c0, c1, cBind, c2, c3);
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  return tbl;
}

async function uploadPdfs(lineId, fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  const fd = new FormData();
  fd.append("line_id", lineId);
  for (const f of files) fd.append("file", f);
  let resp;
  try {
    resp = await fetch("/api/upload", { method: "POST", body: fd });
  } catch (e) { alert("上传失败（网络）：" + esc(e.message)); return; }
  let r;
  try { r = await resp.json(); }
  catch {
    const hint = resp.status === 404 ? "服务端无 /api/upload，面板可能是旧版，请重启" : "请重试或换更小的 PDF";
    alert(`上传失败：服务端返回 HTTP ${resp.status}（非 JSON）。${hint}`);
    return;
  }
  const results = r.results || [];
  const failed = results.filter((x) => !x.ok);
  const over = results.filter((x) => x.ok && x.overwrite).map((x) => x.name);
  if (!resp.ok || (r.ok === false && !results.length)) {
    alert("上传失败：" + (r.error || `HTTP ${resp.status}`));
  } else if (failed.length) {
    alert("部分上传失败：\n" + failed.map((x) => `${x.name || ""}：${x.error}`).join("\n"));
  } else if (over.length) {
    alert("已上传（覆盖同名）：" + over.join("、"));
  }
  loadSources();
}

function statusBadge(f) {
  if (f.published) return el("span", "badge ok", "已投草稿");
  if (f.blocked) return el("span", "badge warn", "质量闸拦");
  if (f.has_article) return el("span", "badge gen", "已生成");
  return el("span", "badge new", "未处理");
}

// ---------- 运行 ----------
async function startRun(lineId, pdfs, processedCount = 0) {
  if (BUSY) { alert("已有任务在跑，等它结束"); return; }
  if (!pdfs || !pdfs.length) { alert("没选 PDF"); return; }
  const rerunHint = processedCount
    ? `\n\n其中 ${processedCount} 篇来自“历史已处理”。当前面板会新起任务重新生成草稿，不会直接修改公众号后台已有草稿。`
    : "";
  if (!confirm(`要跑 ${pdfs.length} 篇（${lineId}）吗？会调用 LLM/VLM 并投到公众号草稿箱。${rerunHint}`)) return;
  const r = await (await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ line_id: lineId, pdfs }),
  })).json();
  if (!r.ok) { alert("启动失败：" + (r.error || "?")); return; }
  BUSY = true; applyBusy(); poll();
}

async function cancelRun() {
  if (!BUSY) return;
  if (!confirm("要停止当前任务吗？已生成的中间结果会保留，未完成的文章需要之后重跑。")) return;
  let r;
  try {
    r = await (await fetch("/api/run/cancel", { method: "POST" })).json();
  } catch (e) { alert("停止失败（网络）：" + esc(e.message)); return; }
  if (!r.ok) { alert("停止失败：" + (r.error || "?")); return; }
  BUSY = false; applyBusy(); poll();
}

async function poll() {
  if (pollTimer) clearTimeout(pollTimer);
  let data;
  try { data = await (await fetch("/api/runs")).json(); } catch (e) { pollTimer = setTimeout(poll, 3000); return; }
  BUSY = !!data.busy; applyBusy();
  const badge = $("#run-badge"), cur = $("#run-current"), log = $("#run-log"), hist = $("#run-history");
  if (data.current) {
    badge.textContent = "运行中"; badge.className = "run-badge running";
    cur.innerHTML = `<b>${esc(data.current.task)}</b> · ${esc(data.current.line_id)} · ${data.current.jobs.length} 篇 · ${esc(data.current.started)}` +
      renderRunSummary(data.current.summary);
    log.textContent = (data.current.log || []).join("\n") || "（启动中…）";
    log.classList.remove("muted");
  } else {
    badge.textContent = "空闲"; badge.className = "run-badge idle";
    cur.innerHTML = "";
    if (!data.history.length) { log.textContent = "（无运行）"; log.classList.add("muted"); }
  }
  hist.innerHTML = "";
  for (const h of data.history) {
    const d = el("details", "hist-item");
    const sm = el("summary");
    const statusText = h.status === "done" ? "完成" : (h.status === "cancelled" ? "已停止" : "失败");
    sm.innerHTML = `<span class="badge ${h.status === "done" ? "ok" : "warn"}">${statusText}</span> ${esc(h.task)} · ${h.jobs.length} 篇`;
    d.appendChild(sm);
    const summaryBox = el("div", "hist-summary");
    summaryBox.innerHTML = renderRunSummary(h.summary);
    d.appendChild(summaryBox);
    d.appendChild(el("pre", "run-log", (h.log || []).join("\n")));
    hist.appendChild(d);
  }
  if (data.busy) { pollTimer = setTimeout(poll, 2500); }
  else { loadSources(); loadArticles(); } // 跑完刷新源与结果
}

function applyBusy() {
  document.querySelectorAll(".btn.run").forEach((b) => { b.disabled = BUSY; });
  const cancel = $("#cancel-run");
  if (cancel) cancel.disabled = !BUSY;
}

function renderRunSummary(s) {
  if (!s) return "";
  const problem = s.last_problem ? `<div class="run-problem">${esc(s.last_problem)}</div>` : "";
  return `<div class="run-summary">${esc(s.message || "")}` +
    ` <span>生成 ${s.generated || 0}/${s.total || 0}</span>` +
    ` <span>拦下 ${s.blocked || 0}</span>` +
    ` <span>失败 ${s.failed || 0}</span>${problem}</div>`;
}

// ---------- 文章 × 投放 ----------
async function loadArticles() {
  try {
    const data = await (await fetch("/api/articles")).json();
    const s = data.stats || {};
    const r = (data.rates || {})["deepseek-chat"];
    const rateNote = r ? `按 deepseek-chat 估算：输入 ¥${r.input_per_1m}/M、输出 ¥${r.output_per_1m}/M（环境变量可改）。配图题注锚定不耗 token。` : "";
    $("#stats").innerHTML =
      `<span>共 <b>${s.total || 0}</b></span><span>已生成 <b>${s.generated || 0}</b></span>` +
      `<span>已投草稿 <b>${s.published || 0}</b></span><span class="warn">质量闸 <b>${s.blocked || 0}</b></span>` +
      `<span title="${esc(rateNote)}">成本 <b>¥${(s.cost_cny || 0).toFixed(2)}</b> ⓘ</span>`;
    const tb = $("#rows"); tb.innerHTML = "";
    if (!data.articles.length) { tb.innerHTML = '<tr><td colspan="9" class="muted">暂无</td></tr>'; return; }
    for (const a of data.articles) tb.appendChild(renderArticle(a));
  } catch (e) { $("#rows").innerHTML = `<tr><td colspan="9" class="err">加载失败：${esc(e.message)}</td></tr>`; }
}

function renderArticle(a) {
  const tr = el("tr");
  const dist = (a.distributions || []).map((d) =>
    `<span class="badge ${d.publish_status === "published" ? "ok" : "gen"}">${esc(d.account || d.platform)}${d.publish_status === "published" ? "·已投" : ""}</span>`).join(" ") || '<span class="muted">—</span>';
  const gate = a.publish_blocked ? `<span class="badge warn">拦:${esc(a.block_reason || "")}</span>` : '<span class="badge ok">通过</span>';
  const costTip = esc(`${esc(a.model || "?")} · 输入 ${a.prompt_tokens || 0} / 输出 ${a.completion_tokens || 0} tok`);
  const cost = a.cost_cny ? `<span title="${costTip}">¥${a.cost_cny.toFixed(4)}</span>` : '<span class="muted">—</span>';
  tr.innerHTML =
    `<td class="mono">${esc(a.job_id)}</td>` +
    `<td>${esc(a.title) || '<span class="muted">—</span>'}</td>` +
    `<td>${esc(a.status) || "—"}</td>` +
    `<td class="num">${a.markdown_health_score ?? "—"}</td>` +
    `<td class="num">${a.tonal_score ?? "—"}</td>` +
    `<td class="num">${cost}</td>` +
    `<td>${gate}</td><td>${dist}</td>` +
    `<td>${a.title ? `<a class="link" target="_blank" href="/preview/${encodeURIComponent(a.job_id)}?wechat=1">预览</a>` : ""}</td>`;
  return tr;
}

// ---------- init ----------
$("#refresh-src").onclick = loadSources;
$("#refresh-art").onclick = loadArticles;
$("#cancel-run").onclick = cancelRun;
loadSources(); loadArticles(); poll();
