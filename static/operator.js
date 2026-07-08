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
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(el("p", "empty", "加载失败"));
  }
}

function renderLine(line) {
  const counts = line.counts || {};
  const pendingFiles = (line.pdfs || []).filter(needsAction);
  const card = el("section", "line");
  const head = el("div", "line-head");
  const title = el("div");
  title.appendChild(el("h2", "", LINE_TITLES[line.line_id] || line.name || line.line_id));
  title.appendChild(el("div", "meta", `待处理 ${pendingFiles.length} 篇 · 草稿 ${counts.published || 0} 篇`));

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
  if (pendingFiles.length) {
    card.appendChild(renderPendingFiles(line.line_id, pendingFiles, card, run));
    updateRunButton(card, run);
  } else {
    card.appendChild(el("div", "empty", "暂无待处理 PDF"));
  }
  return card;
}

function renderPendingFiles(lineId, files, card, runButton) {
  const wrap = el("div", "pending");
  wrap.appendChild(el("div", "pending-title", "待处理文件"));
  const list = el("div", "pending-list");
  for (const file of files) {
    const row = el("div", "pending-row");
    const pick = el("input", "pending-pick");
    pick.type = "checkbox";
    pick.value = file.pdf;
    pick.checked = true;
    pick.onchange = () => updateRunButton(card, runButton);
    row.appendChild(pick);
    const name = el("span", "pending-name", file.name);
    if (file.already_generated) {
      name.appendChild(el("span", "generated-mark", "已生成过"));
    }
    row.appendChild(name);
    const del = el("button", "delete", "删除");
    del.onclick = () => deletePdf(lineId, file.pdf, file.name, del);
    row.appendChild(del);
    list.appendChild(row);
  }
  wrap.appendChild(list);
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
  setStatus("启动中", "running");
  let body;
  try {
    body = await (await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_id: lineId, pdfs }),
    })).json();
  } catch {
    setStatus("启动失败", "failed");
    return;
  }
  if (!body.ok) {
    setStatus(body.error || "启动失败", "failed");
    return;
  }
  busy = true;
  applyBusy();
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
  if (last) {
    const cls = last.status === "done" ? "done" : (last.status === "failed" ? "failed" : "");
    setStatus((last.summary && last.summary.message) || "空闲", cls);
  } else {
    setStatus("空闲", "");
  }
  loadSources();
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
