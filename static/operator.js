"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

let busy = false;
let lastSources = [];
let pollTimer = null;

const LINE_TITLES = {
  aav: "AAV",
  solidex: "Solidex",
};

function needsAction(file) {
  return file.needs_action != null ? !!file.needs_action : (!file.has_article || file.operator_pending);
}

async function loadSources() {
  const box = $("#lines");
  try {
    const data = await (await fetch("/api/sources")).json();
    lastSources = data.lines || [];
    box.innerHTML = "";
    for (const line of lastSources) box.appendChild(renderLine(line));
    applyBusy();
    renderStages(lastSources);
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(el("p", "empty", "加载失败"));
  }
}

function renderIdleStatus(lines = lastSources) {
  const totals = (lines || []).reduce((acc, line) => {
    const counts = line.counts || {};
    acc.pending += counts.pending || 0;
    acc.drafts += counts.published || 0;
    acc.processed += counts.processed || 0;
    return acc;
  }, { pending: 0, drafts: 0, processed: 0 });
  if (totals.pending) return `待处理 ${totals.pending} 篇，请选择文件后生成`;
  if (totals.drafts) return `当前无待处理 PDF；公众号草稿 ${totals.drafts} 篇`;
  if (totals.processed) return `当前无待处理 PDF；已生成 ${totals.processed} 篇`;
  return "暂无待处理 PDF";
}

function renderStages(lines = lastSources) {
  const totals = (lines || []).reduce((acc, line) => {
    const counts = line.counts || {};
    acc.pending += counts.pending || 0;
    acc.drafts += counts.published || 0;
    return acc;
  }, { pending: 0, drafts: 0 });
  $("#stage-pdf").textContent = totals.pending ? `${totals.pending} 篇待处理` : "暂无待处理文件";
  $("#stage-drafts").textContent = `${totals.drafts} 篇已入草稿箱`;
}

function renderLine(line) {
  const counts = line.counts || {};
  const files = line.pdfs || [];
  const pendingFiles = files.filter(needsAction);
  const card = el("section", "line");
  const head = el("div", "line-head");
  const title = el("div");
  title.appendChild(el("h2", "", LINE_TITLES[line.line_id] || line.name || line.line_id));
  title.appendChild(el("div", "meta", `待生成 ${pendingFiles.length} 篇 · 已入草稿箱 ${counts.published || 0} 篇`));

  const actions = el("div", "actions");
  const input = el("input");
  input.type = "file";
  input.accept = "application/pdf,.pdf";
  input.multiple = true;
  input.style.display = "none";
  input.onchange = () => { uploadPdfs(line.line_id, input.files); input.value = ""; };

  const upload = el("button", "secondary", "上传 PDF");
  upload.onclick = () => input.click();

  const run = el("button", "primary run", "生成选中文件");
  run.dataset.hasPending = pendingFiles.length ? "1" : "0";
  run.disabled = !pendingFiles.length;
  run.onclick = () => startRun(line.line_id, selectedPdfs(card));

  actions.append(input, upload, run);
  head.append(title, actions);
  card.appendChild(head);
  if (files.length) card.appendChild(renderPipelineTable(line.line_id, files, card, run));
  else card.appendChild(el("div", "empty", "暂无 PDF"));
  updateRunButton(card, run);
  return card;
}

function pipelineStatus(kind, file) {
  if (kind === "generate") {
    if (file.blocked) return '<span class="flow-state failed"><i></i>需要处理</span>';
    if (file.has_article) return '<span class="flow-state done"><i></i>已生成</span>';
    return '<span class="flow-state waiting"><i></i>待生成</span>';
  }
  if (file.published) return '<span class="flow-state done"><i></i>已入草稿箱</span>';
  if (file.has_article) return '<span class="flow-state waiting"><i></i>待入草稿箱</span>';
  return '<span class="flow-state muted"><i></i>等待生成</span>';
}

function renderPipelineTable(lineId, files, card, runButton) {
  const wrap = el("div", "pipeline-wrap");
  const table = el("table", "file-pipeline");
  table.innerHTML = "<thead><tr><th class=\"pick-col\"></th><th>PDF 文件</th><th>生成</th><th>公众号草稿</th><th>操作</th></tr></thead>";
  const list = el("tbody");
  for (const file of files) {
    const row = el("tr");
    const pick = el("input", "pending-pick");
    pick.type = "checkbox";
    pick.value = file.pdf;
    pick.checked = needsAction(file);
    pick.disabled = !needsAction(file);
    pick.onchange = () => updateRunButton(card, runButton);
    const pickCell = el("td", "pick-col");
    pickCell.appendChild(pick);
    const name = el("div", "pending-name", file.name);
    if (file.already_generated) {
      name.appendChild(el("span", "generated-mark", "已生成过"));
    }
    const nameCell = el("td", "file-name");
    nameCell.appendChild(name);
    const operation = el("td", "file-operation");
    if (needsAction(file)) {
      const del = el("button", "delete", "删除");
      del.onclick = () => deletePdf(lineId, file.pdf, file.name, del);
      operation.appendChild(del);
    } else if (file.has_article) {
      const preview = el("a", "preview", "查看草稿");
      preview.href = `/preview/${encodeURIComponent(file.job_id)}?wechat=1`;
      preview.target = "_blank";
      operation.appendChild(preview);
    }
    row.append(pickCell, nameCell);
    const generateCell = el("td"); generateCell.innerHTML = pipelineStatus("generate", file);
    const draftCell = el("td"); draftCell.innerHTML = pipelineStatus("draft", file);
    row.append(generateCell, draftCell, operation);
    list.appendChild(row);
  }
  table.appendChild(list);
  wrap.appendChild(table);
  return wrap;
}

function selectedPdfs(card) {
  return [...card.querySelectorAll(".pending-pick:checked")].map((input) => input.value);
}

function updateRunButton(card, runButton) {
  const count = selectedPdfs(card).length;
  runButton.dataset.hasPending = count ? "1" : "0";
  runButton.disabled = busy || !count;
  runButton.textContent = count ? `生成选中文件（${count}）` : "生成选中文件";
}

async function uploadPdfs(lineId, filesLike) {
  const files = [...(filesLike || [])];
  if (!files.length) return;
  const fd = new FormData();
  fd.append("line_id", lineId);
  for (const f of files) fd.append("file", f);
  setStatus(`上传中：${files.length} 个文件`, "running");
  let resp, body;
  try {
    resp = await fetch("/api/upload", { method: "POST", body: fd });
    body = await resp.json();
  } catch {
    setStatus("上传失败", "failed");
    return;
  }
  if (!resp.ok || body.ok === false) {
    const failed = (body.results || []).find((r) => !r.ok);
    setStatus((failed && failed.error) || body.error || "上传失败", "failed");
    return;
  }
  await loadSources();
  setStatus(uploadMessage(body.results || files.map((f) => ({ ok: true, name: f.name }))), "done");
}

function uploadMessage(results) {
  const ok = results.filter((r) => r.ok);
  const overwritten = ok.filter((r) => r.overwrite);
  const generated = ok.filter((r) => r.already_generated);
  const fresh = ok.length - overwritten.length;
  if (generated.length) {
    return `此文件已生成过：${generated.map((r) => r.name).join("、")}`;
  }
  if (overwritten.length && !fresh) {
    return `已上传 ${ok.length} 个，覆盖同名 ${overwritten.length} 个；已生成过的文件会在列表提示`;
  }
  if (overwritten.length) {
    return `已上传 ${ok.length} 个，新增 ${fresh} 个，覆盖同名 ${overwritten.length} 个`;
  }
  return `已上传 ${ok.length} 个，可点击生成待处理内容`;
}

async function startRun(lineId, pdfs) {
  if (busy) return;
  if (!pdfs.length) return;
  busy = true;
  applyBusy();
  setStatus("启动中", "running");
  let body;
  try {
    body = await (await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_id: lineId, pdfs }),
    })).json();
  } catch {
    busy = false;
    applyBusy();
    setStatus("启动失败", "failed");
    return;
  }
  if (!body.ok) {
    busy = false;
    applyBusy();
    setStatus(body.error || "启动失败", "failed");
    return;
  }
  poll();
}

async function deletePdf(lineId, pdf, name, button) {
  if (busy) return;
  if (button) {
    button.dataset.deleting = "1";
    button.disabled = true;
    button.textContent = "删除中";
  }
  setStatus("删除中", "running");
  let body;
  try {
    body = await (await fetch("/api/pdf/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_id: lineId, pdf }),
    })).json();
  } catch {
    setStatus("删除失败", "failed");
    resetDeleteButton(button);
    return;
  }
  if (!body.ok) {
    setStatus(body.error || "删除失败", "failed");
    resetDeleteButton(button);
    return;
  }
  await loadSources();
  setStatus(`已删除 ${body.name || name}`, "done");
}

function resetDeleteButton(button) {
  if (!button) return;
  button.dataset.deleting = "0";
  button.disabled = busy;
  button.textContent = "删除";
}

async function poll() {
  if (pollTimer) clearTimeout(pollTimer);
  let data;
  try {
    data = await (await fetch("/api/runs")).json();
  } catch {
    pollTimer = setTimeout(poll, 3000);
    return;
  }
  busy = !!data.busy;
  applyBusy();
  if (data.current) {
    const s = data.current.summary || {};
    setStatus(s.message || "生成中", "running");
    pollTimer = setTimeout(poll, 2500);
    return;
  }
  const last = (data.history || [])[0];
  await loadSources();
  if (last && (last.status === "failed" || last.status === "cancelled")) {
    const cls = last.status === "failed" ? "failed" : "";
    setStatus((last.summary && last.summary.message) || "任务未完成", cls);
    return;
  }
  setStatus(renderIdleStatus(), "");
}

function setStatus(text, cls) {
  const box = $("#status");
  box.textContent = text;
  box.className = `status ${cls || ""}`.trim();
}

function applyBusy() {
  document.querySelectorAll("button.run").forEach((b) => { b.disabled = busy || b.dataset.hasPending !== "1"; });
  document.querySelectorAll("button.delete").forEach((b) => { b.disabled = busy || b.dataset.deleting === "1"; });
}

loadSources();
poll();
