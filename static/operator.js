"use strict";

const $ = (selector) => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};
const esc = (value) => (value == null ? "" : String(value)).replace(/[&<>"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
}[char]));

const LINE_TITLES = { aav: "AAV", solidex: "Solidex" };
const TRANSLATION_LANGS = ["en", "ja", "ko", "ru"];
const LANG_LABELS = { en: "EN", ja: "JA", ko: "KO", ru: "RU" };

let generationBusy = false;
let generationState = {};
let workflowState = {};
let preflight = {};
let sourceLines = [];
let pollTimer = null;
const selectedKeys = new Set();
const expandedLanguageJobs = new Set();
let lineNotice = { text: "", state: "" };

function needsAction(file) {
  return file.needs_action != null ? !!file.needs_action : (!file.has_article || file.operator_pending);
}

function articleVersion(file, lang) {
  return (file.versions || []).find((item) => item.lang === lang);
}

function distribution(file, platform, lang) {
  return (file.distributions || []).find((item) => item.platform === platform && item.lang === lang);
}

function wechatState(file) {
  const state = distribution(file, "wechat", "zh")?.publish_status;
  if (state === "published") return "done";
  if (state === "publishing") return "running";
  if (["failed", "blocked"].includes(state)) return "failed";
  return file.has_article ? "pending" : "waiting";
}

function translationState(file, lang) {
  const state = articleVersion(file, lang)?.translation_status;
  if (state === "translated") return "done";
  if (state === "translating") return "running";
  if (state === "failed") return "failed";
  return "pending";
}

function cmsState(file, lang) {
  const state = distribution(file, "blog", lang)?.publish_status;
  if (state === "published") return "done";
  if (state === "publishing") return "running";
  if (["failed", "blocked"].includes(state)) return "failed";
  return "pending";
}

function coreComplete(file) {
  return file.has_article && cmsState(file, "zh") === "done" && wechatState(file) === "done";
}

function requiresCoreAction(file) {
  return needsAction(file) || !coreComplete(file);
}

function stateBadge(state, text) {
  return `<span class="state-badge ${esc(state)}"><i></i>${esc(text)}</span>`;
}

function coreStatus(file) {
  if ((generationState.current?.jobs || []).includes(file.job_id)) {
    return ["running", "中文内容处理中"];
  }
  const recentRun = (generationState.history || []).find((run) => (run.jobs || []).includes(file.job_id));
  if (recentRun?.status === "failed" && requiresCoreAction(file)) {
    return ["failed", recentRun.summary?.message || "最近一次生成失败，请重试"];
  }
  if (recentRun?.status === "cancelled" && requiresCoreAction(file)) {
    return ["failed", "最近一次生成已取消"];
  }
  if (file.blocked) return ["failed", "生成结果需要检查"];
  if (!file.has_article) return ["pending", "待生成"];
  if (cmsState(file, "zh") === "failed") return ["failed", "中文 Blog 发布失败"];
  if (cmsState(file, "zh") !== "done") return ["running", "等待中文 Blog"];
  if (wechatState(file) === "failed") return ["failed", "公众号草稿提交失败"];
  if (wechatState(file) !== "done") return ["running", "等待公众号草稿"];
  return ["done", "中文流程已完成"];
}

function allFiles() {
  return sourceLines.flatMap((line) => (line.pdfs || []).map((file) => ({ ...file, line_id: line.line_id })));
}

function selectedFiles(lineId = "") {
  return allFiles().filter((file) => {
    const key = `${file.line_id}|${file.pdf}`;
    return selectedKeys.has(key) && (!lineId || file.line_id === lineId);
  });
}

function updateGenerateButton(card) {
  const lineId = card.dataset.lineId;
  const count = selectedFiles(lineId).filter(requiresCoreAction).length;
  const button = card.querySelector(".generate-line");
  const countNode = card.querySelector(".selected-count");
  if (countNode) countNode.textContent = String(count);
  if (!button) return;
  button.disabled = generationBusy || !count;
  button.textContent = count ? `生成并提交草稿（${count}）` : "生成并提交草稿";
}

function updateSelection() {
  document.querySelectorAll(".line").forEach(updateGenerateButton);
}

function languageProgress(file, lang) {
  const translating = workflowState.translate;
  if (translating?.status === "running" && translating.current?.job_id === file.job_id && translating.current?.lang === lang) {
    return { state: "running", text: "翻译中", action: "" };
  }
  const publishing = workflowState.publish;
  if (publishing?.status === "running" && publishing.current?.job_id === file.job_id && publishing.current?.lang === lang) {
    return { state: "running", text: "发布中", action: "" };
  }
  const translated = translationState(file, lang);
  const published = cmsState(file, lang);
  if (published === "done") return { state: "done", text: "已发布", action: "" };
  if (published === "running") return { state: "running", text: "发布中", action: "" };
  if (translated === "running") return { state: "running", text: "翻译中", action: "" };
  if (translated === "done") return { state: published === "failed" ? "failed" : "ready", text: published === "failed" ? "发布失败" : "已翻译", action: "publish" };
  return { state: translated === "failed" ? "failed" : "pending", text: translated === "failed" ? "翻译失败" : "未翻译", action: "translate" };
}

function renderLanguagePanel(file) {
  const publishedCount = TRANSLATION_LANGS.filter((lang) => cmsState(file, lang) === "done").length;
  const details = el("details", "language-panel");
  details.open = expandedLanguageJobs.has(file.job_id);
  details.addEventListener("toggle", () => {
    if (details.open) expandedLanguageJobs.add(file.job_id);
    else expandedLanguageJobs.delete(file.job_id);
  });
  const summary = el("summary");
  summary.innerHTML = `<span>多语言发布 <em>可选</em></span><small>${publishedCount}/4 已发布</small>`;
  details.appendChild(summary);
  const list = el("div", "language-list");
  for (const lang of TRANSLATION_LANGS) {
    const progress = languageProgress(file, lang);
    const row = el("div", "language-row");
    row.innerHTML = `<b>${LANG_LABELS[lang]}</b>${stateBadge(progress.state, progress.text)}<span class="language-action"></span>`;
    const action = row.querySelector(".language-action");
    const published = distribution(file, "blog", lang);
    if (progress.state === "done" && published?.external_url) {
      const open = el("a", "text-action", "打开");
      open.href = published.external_url;
      open.target = "_blank";
      action.appendChild(open);
    } else if (progress.action) {
      const button = el("button", "text-action", progress.action === "translate" ? "翻译" : "发布");
      const busy = workflowState[progress.action]?.status === "running";
      const configured = progress.action === "translate" ? preflight.translation?.configured : preflight.cms?.configured;
      button.disabled = busy || configured === false;
      button.addEventListener("click", () => runWorkflow(progress.action, [{ job_id: file.job_id, lang }]));
      action.appendChild(button);
    }
    list.appendChild(row);
  }
  details.appendChild(list);
  return details;
}

function renderPendingRow(lineId, file) {
  const row = el("tr");
  const key = `${lineId}|${file.pdf}`;
  const [state, label] = coreStatus(file);
  row.innerHTML = `
    <td class="pick-col"><input class="row-pick" type="checkbox" data-key="${esc(key)}" aria-label="选择 ${esc(file.name)}" ${selectedKeys.has(key) ? "checked" : ""}></td>
    <td class="file-cell"><div class="file-title" title="${esc(file.title || file.name)}">${esc(file.title || file.name)}</div><div class="file-name">${esc(file.name)}</div></td>
    <td>${stateBadge(state, label)}</td>
    <td><div class="row-actions"></div></td>`;
  row.querySelector(".row-pick").addEventListener("change", (event) => {
    if (event.target.checked) selectedKeys.add(key); else selectedKeys.delete(key);
    updateSelection();
  });
  const actions = row.querySelector(".row-actions");
  if (file.has_article) {
    const preview = el("a", "text-action", "预览现有内容");
    preview.href = `/preview/${encodeURIComponent(file.job_id)}?wechat=1`;
    preview.target = "_blank";
    actions.appendChild(preview);
  }
  if (needsAction(file)) {
    const remove = el("button", "text-action delete", "删除 PDF");
    remove.addEventListener("click", () => deletePdf(lineId, file.pdf, file.name, remove));
    actions.appendChild(remove);
  }
  return row;
}

function renderCompletedCard(file) {
  const card = el("article", "completed-card");
  const body = el("div", "completed-main");
  body.innerHTML = `<div><span class="complete-mark">✓</span><div class="completed-copy"><h3>${esc(file.title || file.name)}</h3><p>中文 Blog 已发布 · 已进入公众号草稿箱</p></div></div><div class="row-actions"></div>`;
  const actions = body.querySelector(".row-actions");
  const blog = distribution(file, "blog", "zh");
  if (blog?.external_url) {
    const openBlog = el("a", "btn secondary compact", "打开 Blog");
    openBlog.href = blog.external_url;
    openBlog.target = "_blank";
    actions.appendChild(openBlog);
  }
  const preview = el("a", "btn secondary compact", "预览草稿");
  preview.href = `/preview/${encodeURIComponent(file.job_id)}?wechat=1`;
  preview.target = "_blank";
  actions.appendChild(preview);
  card.append(body, renderLanguagePanel(file));
  return card;
}

function renderLine(line) {
  const card = el("section", "line");
  card.dataset.lineId = line.line_id;
  const files = line.pdfs || [];
  const pending = files.filter(requiresCoreAction);
  const completed = files.filter(coreComplete);

  const head = el("div", "line-head");
  const title = el("div");
  title.innerHTML = `<h2>${esc(LINE_TITLES[line.line_id] || line.name || line.line_id)}</h2><div class="line-meta">待处理 ${pending.length} 篇 · 已完成 ${completed.length} 篇</div>`;
  const actions = el("div", "line-actions");
  const input = el("input");
  input.type = "file";
  input.accept = "application/pdf,.pdf";
  input.multiple = true;
  input.hidden = true;
  input.addEventListener("change", () => { uploadPdfs(line.line_id, input.files); input.value = ""; });
  const upload = el("button", "btn primary", "上传 PDF");
  upload.addEventListener("click", () => input.click());
  actions.append(input, upload);
  head.append(title, actions);
  card.appendChild(head);

  const notice = el("div", `line-notice ${lineNotice.state}`.trim(), lineNotice.text);
  notice.hidden = !lineNotice.text;
  card.appendChild(notice);

  const pendingSection = el("section", "work-section");
  const pendingHead = el("div", "section-head");
  pendingHead.innerHTML = `<div><h3>待处理 PDF</h3><p>选择后一次完成中文 Blog、原文 PDF 和公众号草稿</p></div><div class="selection-actions"><span>已选 <b class="selected-count">0</b> 篇</span></div>`;
  const generate = el("button", "btn primary generate-line", "生成并提交草稿");
  generate.disabled = true;
  generate.addEventListener("click", () => {
    const chosen = selectedFiles(line.line_id).filter(requiresCoreAction);
    startGeneration(line.line_id, chosen.map((file) => file.pdf));
  });
  pendingHead.querySelector(".selection-actions").appendChild(generate);
  pendingSection.appendChild(pendingHead);
  if (!pending.length) {
    pendingSection.appendChild(el("div", "empty success-empty", "没有待处理文件。上传 PDF 即可开始。"));
  } else {
    const wrap = el("div", "pipeline-wrap");
    const table = el("table", "pipeline-table");
    table.innerHTML = `<thead><tr><th class="pick-col"></th><th>PDF / 文章</th><th>当前状态</th><th>操作</th></tr></thead>`;
    const body = el("tbody");
    pending.forEach((file) => body.appendChild(renderPendingRow(line.line_id, file)));
    table.appendChild(body);
    wrap.appendChild(table);
    pendingSection.appendChild(wrap);
  }
  card.appendChild(pendingSection);

  const completedSection = el("section", "work-section completed-section");
  const completedHead = el("div", "section-head");
  completedHead.innerHTML = `<div><h3>已完成文章</h3><p>中文流程已结束；需要时可单独测试某一种语言</p></div>`;
  completedSection.appendChild(completedHead);
  if (!completed.length) completedSection.appendChild(el("div", "empty", "暂无已完成文章"));
  else completed.forEach((file) => completedSection.appendChild(renderCompletedCard(file)));
  card.appendChild(completedSection);
  return card;
}

function setStatus(text, state = "") {
  lineNotice = { text, state };
  document.querySelectorAll(".line-notice").forEach((box) => {
    box.textContent = text;
    box.className = `line-notice ${state}`.trim();
    box.hidden = !text;
  });
}

async function loadSources() {
  try {
    const response = await fetch("/api/sources");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    sourceLines = data.lines || [];
    const box = $("#lines");
    box.innerHTML = "";
    sourceLines.forEach((line) => box.appendChild(renderLine(line)));
    if (!sourceLines.length) box.appendChild(el("div", "empty", "当前用户没有配置业务线"));
    updateSelection();
  } catch (error) {
    $("#lines").innerHTML = '<div class="empty">工作台加载失败</div>';
    setStatus(`加载失败：${error.message}`, "failed");
  }
}

async function uploadPdfs(lineId, filesLike) {
  const files = [...(filesLike || [])];
  if (!files.length) return;
  const form = new FormData();
  form.append("line_id", lineId);
  files.forEach((file) => form.append("file", file));
  setStatus(`正在上传 ${files.length} 个 PDF`, "running");
  try {
    const response = await fetch("/api/upload", { method: "POST", body: form });
    const data = await response.json();
    const failures = (data.results || []).filter((item) => !item.ok);
    if (!response.ok || failures.length) throw new Error(failures[0]?.error || data.error || `HTTP ${response.status}`);
    const uploadedKeys = (data.results || []).filter((item) => item.ok).map((item) => `${lineId}|${item.pdf}`);
    uploadedKeys.forEach((key) => selectedKeys.add(key));
    await loadSources();
    setStatus(`上传完成，已自动选中 ${files.length} 个 PDF；点击“生成并提交草稿”继续`, "done");
  } catch (error) {
    setStatus(`上传失败：${error.message}`, "failed");
  }
}

async function deletePdf(lineId, pdf, name, button) {
  if (generationBusy) return;
  button.disabled = true;
  button.textContent = "删除中";
  try {
    const response = await fetch("/api/pdf/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_id: lineId, pdf }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    selectedKeys.delete(`${lineId}|${pdf}`);
    await loadSources();
    setStatus(`已删除 ${data.name || name}`, "done");
  } catch (error) {
    button.disabled = false;
    button.textContent = "删除 PDF";
    setStatus(`删除失败：${error.message}`, "failed");
  }
}

async function startGeneration(lineId, pdfs) {
  if (generationBusy || !pdfs.length) return;
  generationBusy = true;
  updateSelection();
  setStatus(`正在启动 ${pdfs.length} 篇中文内容流程`, "running");
  try {
    const response = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_id: lineId, pdfs }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    poll();
  } catch (error) {
    generationBusy = false;
    updateSelection();
    setStatus(`启动失败：${error.message}`, "failed");
  }
}

async function runWorkflow(stage, selections) {
  if (!selections.length) return;
  const label = stage === "translate" ? "翻译" : "CMS 发布";
  setStatus(`正在启动 ${LANG_LABELS[selections[0].lang]} ${label}`, "running");
  try {
    const response = await fetch(`/api/workflow/${stage}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selections }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setStatus(`${LANG_LABELS[selections[0].lang]} ${label}任务已启动`, "running");
    poll();
  } catch (error) {
    setStatus(`${label}启动失败：${error.message}`, "failed");
  }
}

function workflowIsActive(states = workflowState) {
  return states.translate?.status === "running" || states.publish?.status === "running";
}

async function poll(forceSources = false) {
  if (forceSources instanceof Event) forceSources = true;
  if (pollTimer) clearTimeout(pollTimer);
  const wasActive = generationBusy || workflowIsActive();
  try {
    const [generation, workflows] = await Promise.all([
      fetch("/api/runs").then((response) => response.json()),
      fetch("/api/workflow/status").then((response) => response.json()),
    ]);
    generationBusy = !!generation.busy;
    generationState = generation;
    workflowState = workflows || {};
    const active = generationBusy || workflowIsActive();
    if (forceSources || active || wasActive || !sourceLines.length) await loadSources();

    const otherWorkflow = [workflowState.translate, workflowState.publish].some((state) => state?.other_line_running);
    if (generation.busy_other_line) setStatus("另一业务线正在处理任务，请稍后再试", "running");
    else if (otherWorkflow) setStatus("另一业务线正在执行多语言任务，请稍后再试", "running");
    else if (active || lineNotice.state === "running") setStatus("", "");
  } catch (error) {
    setStatus(`状态刷新失败：${error.message}`, "failed");
  }
  const active = generationBusy || workflowIsActive();
  pollTimer = setTimeout(poll, active ? 2500 : 10000);
}

async function loadPreflight() {
  try {
    preflight = await fetch("/api/workflow/preflight").then((response) => response.json());
  } catch (_error) {
    /* 多语言是可选能力，预检失败不影响中文主流程。 */
  }
}

$("#refresh").addEventListener("click", () => poll(true));
loadPreflight();
poll(true);
