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
const CMS_LANGS = ["zh", ...TRANSLATION_LANGS];
const LANG_LABELS = { zh: "ZH", en: "EN", ja: "JA", ko: "KO", ru: "RU" };

let generationBusy = false;
let workflowState = {};
let preflight = {};
let sourceLines = [];
let pollTimer = null;

function needsAction(file) {
  return file.needs_action != null ? !!file.needs_action : (!file.has_article || file.operator_pending);
}

function articleVersion(file, lang) {
  return (file.versions || []).find((item) => item.lang === lang);
}

function distribution(file, platform, lang) {
  return (file.distributions || []).find((item) => item.platform === platform && item.lang === lang);
}

function generationState(file) {
  if (file.blocked) return "failed";
  if (file.has_article) return "done";
  return generationBusy ? "pending" : "pending";
}

function wechatState(file) {
  const item = distribution(file, "wechat", "zh");
  if (item?.publish_status === "published") return "done";
  if (item?.publish_status === "publishing") return "running";
  if (["failed", "blocked"].includes(item?.publish_status)) return "failed";
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

function stateText(state, labels) {
  const key = state === "waiting" ? "pending" : state;
  return `<span class="state-text"><i class="dot ${key}"></i>${esc(labels[state] || labels.pending)}</span>`;
}

function languageDots(langs, stateFor, stageLabel) {
  return `<span class="lang-dots">${langs.map((lang) => {
    const state = stateFor(lang);
    const label = LANG_LABELS[lang];
    const readable = { pending: "待处理", running: "处理中", done: "已完成", failed: "失败" }[state] || "待处理";
    return `<span class="lang-dot" title="${label} ${stageLabel}：${readable}"><i class="dot ${state}"></i><b>${label}</b></span>`;
  }).join("")}</span>`;
}

function allFiles() {
  return sourceLines.flatMap((line) => (line.pdfs || []).map((file) => ({ ...file, line_id: line.line_id })));
}

function selectedFiles() {
  const keys = new Set([...document.querySelectorAll(".row-pick:checked")].map((node) => node.dataset.key));
  return allFiles().filter((file) => keys.has(`${file.line_id}|${file.pdf}`));
}

function updateSelection() {
  const count = document.querySelectorAll(".row-pick:checked").length;
  $("#selected-count").textContent = count;
  $("#translate-selected").disabled = !count || workflowState.translate?.status === "running";
  $("#publish-selected").disabled = !count || workflowState.publish?.status === "running";
  document.querySelectorAll(".line").forEach((line) => updateGenerateButton(line));
}

function eligibleTranslations(file) {
  if (!file.has_article || file.blocked) return [];
  return TRANSLATION_LANGS.filter((lang) => ["pending", "failed"].includes(articleVersion(file, lang)?.translation_status || "pending"));
}

function eligiblePublications(file) {
  if (!file.has_article || file.blocked) return [];
  return CMS_LANGS.filter((lang) => {
    const queue = distribution(file, "blog", lang);
    if (!queue || !["pending", "failed"].includes(queue.publish_status)) return false;
    if (lang === "zh") return articleVersion(file, lang)?.translation_status === "ready";
    return articleVersion(file, lang)?.translation_status === "translated";
  });
}

function renderRow(lineId, file) {
  const row = el("tr");
  const key = `${lineId}|${file.pdf}`;
  const generated = file.title || file.name;
  const generation = generationState(file);
  const draft = wechatState(file);
  const canTranslate = eligibleTranslations(file).length > 0;
  const canPublish = eligiblePublications(file).length > 0;
  row.innerHTML = `
    <td class="pick-col"><input class="row-pick" type="checkbox" data-key="${esc(key)}" aria-label="选择 ${esc(file.name)}"></td>
    <td class="file-cell"><div class="file-title" title="${esc(generated)}">${esc(generated)}${file.already_generated && needsAction(file) ? '<span class="generated-mark">已生成过</span>' : ""}</div><div class="file-name">${esc(file.name)}</div></td>
    <td>${stateText(generation, { done: "已生成", failed: "需要处理", pending: "待生成" })}</td>
    <td>${stateText(draft, { done: "已入草稿箱", running: "提交中", failed: "失败", waiting: "等待生成", pending: "待入草稿箱" })}</td>
    <td>${languageDots(TRANSLATION_LANGS, (lang) => translationState(file, lang), "翻译")}</td>
    <td>${languageDots(CMS_LANGS, (lang) => cmsState(file, lang), "发布")}</td>
    <td><div class="row-actions"></div></td>`;
  row.querySelector(".row-pick").addEventListener("change", updateSelection);
  const actions = row.querySelector(".row-actions");
  if (needsAction(file)) {
    const remove = el("button", "text-action delete", "删除");
    remove.addEventListener("click", () => deletePdf(lineId, file.pdf, file.name, remove));
    actions.appendChild(remove);
  }
  if (canTranslate) {
    const translate = el("button", "text-action", "翻译");
    translate.addEventListener("click", () => runWorkflow("translate", [file]));
    actions.appendChild(translate);
  }
  if (canPublish) {
    const publish = el("button", "text-action", "发布");
    publish.addEventListener("click", () => runWorkflow("publish", [file]));
    actions.appendChild(publish);
  }
  if (file.has_article) {
    const preview = el("a", "text-action", "预览");
    preview.href = `/preview/${encodeURIComponent(file.job_id)}?wechat=1`;
    preview.target = "_blank";
    actions.appendChild(preview);
  }
  if (!actions.children.length) actions.appendChild(el("span", "line-meta", "—"));
  return row;
}

function renderLine(line) {
  const card = el("section", "line");
  card.dataset.lineId = line.line_id;
  const files = line.pdfs || [];
  const head = el("div", "line-head");
  const title = el("div");
  title.innerHTML = `<h2>${esc(LINE_TITLES[line.line_id] || line.name || line.line_id)}</h2><div class="line-meta">PDF ${files.length} 篇 · 待生成 ${(line.counts || {}).pending || 0} 篇 · 草稿箱 ${(line.counts || {}).published || 0} 篇</div>`;
  const actions = el("div", "line-actions");
  const input = el("input");
  input.type = "file";
  input.accept = "application/pdf,.pdf";
  input.multiple = true;
  input.hidden = true;
  input.addEventListener("change", () => { uploadPdfs(line.line_id, input.files); input.value = ""; });
  const upload = el("button", "btn secondary", "上传 PDF");
  upload.addEventListener("click", () => input.click());
  const generate = el("button", "btn primary generate-line", "生成选中文件");
  generate.addEventListener("click", () => {
    const selected = selectedFiles().filter((file) => file.line_id === line.line_id && needsAction(file));
    startGeneration(line.line_id, selected.map((file) => file.pdf));
  });
  actions.append(input, upload, generate);
  head.append(title, actions);
  card.appendChild(head);
  if (!files.length) {
    card.appendChild(el("div", "empty", "暂无 PDF，请先上传文件"));
    return card;
  }
  const wrap = el("div", "pipeline-wrap");
  const table = el("table", "pipeline-table");
  table.innerHTML = `<thead><tr><th class="pick-col"></th><th>PDF / 文章</th><th>生成</th><th>公众号草稿</th><th>翻译 <span class="line-meta">EN · JA · KO · RU</span></th><th>CMS 发布 <span class="line-meta">ZH · EN · JA · KO · RU</span></th><th>操作</th></tr></thead>`;
  const body = el("tbody");
  files.forEach((file) => body.appendChild(renderRow(line.line_id, file)));
  table.appendChild(body);
  wrap.appendChild(table);
  card.appendChild(wrap);
  return card;
}

function updateGenerateButton(card) {
  const lineId = card.dataset.lineId;
  const count = selectedFiles().filter((file) => file.line_id === lineId && needsAction(file)).length;
  const button = card.querySelector(".generate-line");
  if (!button) return;
  button.disabled = generationBusy || !count;
  button.textContent = count ? `生成选中文件 (${count})` : "生成选中文件";
}

async function loadSources() {
  try {
    const data = await (await fetch("/api/sources")).json();
    sourceLines = data.lines || [];
    const box = $("#lines");
    box.innerHTML = "";
    sourceLines.forEach((line) => box.appendChild(renderLine(line)));
    if (!sourceLines.length) box.appendChild(el("div", "empty", "暂无内容线"));
    updateSelection();
    updateStageSummaries();
  } catch (error) {
    $("#lines").innerHTML = '<div class="empty">流水线加载失败</div>';
    setStatus(`加载失败：${error.message}`, "failed");
  }
}

function updateStageSummaries() {
  const files = allFiles();
  const generated = files.filter((file) => file.has_article);
  const pending = files.filter(needsAction).length;
  const drafts = files.filter((file) => wechatState(file) === "done").length;
  const translated = generated.reduce((sum, file) => sum + TRANSLATION_LANGS.filter((lang) => translationState(file, lang) === "done").length, 0);
  const translationTotal = generated.length * TRANSLATION_LANGS.length;
  const published = generated.reduce((sum, file) => sum + CMS_LANGS.filter((lang) => cmsState(file, lang) === "done").length, 0);
  const publishTotal = generated.length * CMS_LANGS.length;
  $("#stage-generate").textContent = `${pending} 篇待生成 · ${drafts} 篇已入草稿箱`;
  $("#stage-translate").textContent = `${translated}/${translationTotal} 个语言版本已完成`;
  $("#stage-publish").textContent = `${published}/${publishTotal} 个语言版本已发布`;
}

function setPill(id, state, text) {
  const node = $(id);
  node.className = `stage-pill ${state}`;
  node.textContent = text;
}

function updateWorkflowPills() {
  setPill("#stage-generate-state", generationBusy ? "running" : "idle", generationBusy ? "运行中" : "空闲");
  for (const stage of ["translate", "publish"]) {
    const state = workflowState[stage] || { status: "idle" };
    const id = stage === "translate" ? "#stage-translate-state" : "#stage-publish-state";
    if (state.status === "running") setPill(id, "running", `${state.completed || 0}/${state.total || 0}`);
    else if (state.status === "failed") setPill(id, "failed", `失败 ${state.failed || 0}`);
    else if (state.status === "done") setPill(id, "done", "已完成");
    else if (preflight[stage === "translate" ? "translation" : "cms"]?.configured === false) setPill(id, "failed", "未配置");
    else setPill(id, "idle", "空闲");
  }
}

function setStatus(text, state = "") {
  const box = $("#status");
  box.textContent = text;
  box.className = `status ${state}`.trim();
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
    const repeated = (data.results || []).filter((item) => item.already_generated).length;
    await loadSources();
    setStatus(repeated ? `上传完成；${repeated} 个文件已生成过，可重新生成` : `上传完成，共 ${files.length} 个 PDF`, "done");
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
    await loadSources();
    setStatus(`已删除 ${data.name || name}`, "done");
  } catch (error) {
    button.disabled = false;
    button.textContent = "删除";
    setStatus(`删除失败：${error.message}`, "failed");
  }
}

async function startGeneration(lineId, pdfs) {
  if (generationBusy || !pdfs.length) return;
  generationBusy = true;
  updateSelection();
  setStatus(`正在启动 ${pdfs.length} 篇内容生成`, "running");
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
    setStatus(`生成启动失败：${error.message}`, "failed");
  }
}

function workflowSelections(stage, files) {
  const selections = [];
  for (const file of files) {
    const langs = stage === "translate" ? eligibleTranslations(file) : eligiblePublications(file);
    langs.forEach((lang) => selections.push({ job_id: file.job_id, lang }));
  }
  return selections;
}

async function runWorkflow(stage, files) {
  const selections = workflowSelections(stage, files);
  if (!selections.length) {
    setStatus(stage === "translate" ? "所选内容没有待翻译版本" : "所选内容没有可发布版本", "failed");
    return;
  }
  const label = stage === "translate" ? "翻译" : "CMS 发布";
  setStatus(`正在提交 ${selections.length} 个${label}任务`, "running");
  try {
    const response = await fetch(`/api/workflow/${stage}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selections }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setStatus(`${label}任务已启动，共 ${data.total} 个语言版本`, "running");
    poll();
  } catch (error) {
    setStatus(`${label}启动失败：${error.message}`, "failed");
  }
}

function describeCurrent(generation, workflows) {
  if (generation.current) {
    const summary = generation.current.summary || {};
    return [summary.message || "内容生成中", "running"];
  }
  for (const stage of ["translate", "publish"]) {
    const state = workflows[stage];
    if (state?.status === "running") {
      const current = state.current ? `：${state.current.job_id} / ${LANG_LABELS[state.current.lang]}` : "";
      return [`${stage === "translate" ? "多语言翻译" : "CMS 发布"} ${state.completed || 0}/${state.total || 0}${current}`, "running"];
    }
  }
  const failed = [workflows.translate, workflows.publish].find((state) => state?.status === "failed" && state.failed);
  if (failed) return [`最近任务有 ${failed.failed} 个版本失败：${failed.errors?.[0] || "请重试失败项"}`, "failed"];
  const missing = [];
  if (preflight.translation?.configured === false) missing.push("翻译服务");
  if (preflight.cms?.configured === false) missing.push("CMS/图片服务");
  if (missing.length) return [`${missing.join("、")}尚未配置，生成公众号草稿不受影响`, "failed"];
  return ["流水线空闲，可选择文章执行下一阶段", ""];
}

async function poll() {
  if (pollTimer) clearTimeout(pollTimer);
  try {
    const [generation, workflows] = await Promise.all([
      fetch("/api/runs").then((response) => response.json()),
      fetch("/api/workflow/status").then((response) => response.json()),
    ]);
    generationBusy = !!generation.busy;
    workflowState = workflows || {};
    updateWorkflowPills();
    const [message, state] = describeCurrent(generation, workflowState);
    setStatus(message, state);
    await loadSources();
  } catch (error) {
    setStatus(`状态刷新失败：${error.message}`, "failed");
  }
  const active = generationBusy || workflowState.translate?.status === "running" || workflowState.publish?.status === "running";
  pollTimer = setTimeout(poll, active ? 2500 : 8000);
}

async function loadPreflight() {
  try {
    preflight = await fetch("/api/workflow/preflight").then((response) => response.json());
    updateWorkflowPills();
    if (preflight.translation?.configured === false || preflight.cms?.configured === false) {
      const missing = [];
      if (preflight.translation?.configured === false) missing.push("翻译服务");
      if (preflight.cms?.configured === false) missing.push("CMS/图片服务");
      setStatus(`${missing.join("、")}尚未配置，生成公众号草稿不受影响`, "failed");
    }
  } catch (_error) {
    /* 预检失败不阻断已存在的流水线状态。 */
  }
}

$("#refresh").addEventListener("click", poll);
$("#translate-selected").addEventListener("click", () => runWorkflow("translate", selectedFiles()));
$("#publish-selected").addEventListener("click", () => runWorkflow("publish", selectedFiles()));
loadPreflight();
poll();
