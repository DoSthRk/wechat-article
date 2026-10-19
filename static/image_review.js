"use strict";

const $ = (selector) => document.querySelector(selector);
const jobId = document.body.dataset.jobId;
const api = `/api/image-review/${encodeURIComponent(jobId)}`;
let currentPackage = null, busy = false, pollTimer = null;
let expectedImages = new Set(), loadedImages = new Set();
const candidateStates = new Set(["needs_review", "revoked"]);
const statusLabels = {missing: "尚无图片候选", building: "正在生成图片候选", needs_review: "等待首次人工审核", approved: "图片包已审核", blocked: "图片候选被拦下", stale: "来源已变化，需要重新审核", superseded: "图片包已有新版本", revoked: "审核已撤回"};
const workflowKey = `image-review-workflow:${jobId}`;

function message(text, tone = "", focus = false) {
  const node = $("#review-message");
  node.textContent = text; node.className = `review-message ${tone}`;
  if (focus) node.focus();
}

async function request(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch (_) { throw new Error(`请求未完成（HTTP ${response.status}）。请刷新状态核对，不要重复提交。`); }
  if (!response.ok || data.ok === false) {
    const error = new Error(data.error || `请求失败（HTTP ${response.status}），请重试。`);
    error.field = data.field; throw error;
  }
  return data;
}

function draftKey() { return `image-review:${jobId}:${currentPackage?.bundle_id || "none"}`; }
function formValues() {
  const confirmations = {}, figures = {};
  document.querySelectorAll(".confirmations input").forEach((node) => confirmations[node.name] = node.checked);
  document.querySelectorAll(".figure-confirm input").forEach((node) => figures[node.dataset.key] = node.checked);
  return {title: $("#paper-title").value, doi: $("#paper-doi").value, pages: $("#paper-pages").value,
    evidence: $("#paper-evidence").value, note: $("#review-note").value, confirmations, figures};
}
function saveDraft() { try { sessionStorage.setItem(draftKey(), JSON.stringify(formValues())); } catch (_) {} }
function restoreDraft() {
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(draftKey()) || "{}"); } catch (_) {}
  const scope = currentPackage.paper_scope || {};
  $("#paper-title").value = saved.title ?? scope.title ?? "";
  $("#paper-doi").value = saved.doi ?? scope.doi ?? "";
  $("#paper-pages").value = saved.pages ?? (scope.pages || []).join(", ");
  $("#paper-evidence").value = saved.evidence ?? scope.evidence ?? "";
  $("#review-note").value = saved.note ?? "";
  document.querySelectorAll(".confirmations input").forEach((node) => node.checked = saved.confirmations?.[node.name] === true);
  document.querySelectorAll(".figure-confirm input").forEach((node) => node.checked = saved.figures?.[node.dataset.key] === true);
}

function updateButtons() {
  const approved = currentPackage?.status === "approved";
  const canReview = currentPackage?.can_approve && candidateStates.has(currentPackage.status);
  const imagesReady = expectedImages.size > 0 && [...expectedImages].every((url) => loadedImages.has(url));
  $("#approve-continue").hidden = approved; $("#approve-only").hidden = approved;
  $("#continue-publish").hidden = !approved;
  $("#approve-continue").disabled = busy || !canReview || !imagesReady;
  $("#approve-only").disabled = busy || !canReview || !imagesReady;
  $("#continue-publish").disabled = busy || !currentPackage?.can_approve;
  $("#prepare-package").disabled = busy;
  $("#revoke-package").disabled = busy || !approved || !currentPackage?.can_approve;
  $("#revoke-section").hidden = !approved;
}

function imagePreview(item, title) {
  const figure = document.createElement("figure"), caption = document.createElement("figcaption");
  caption.textContent = title; figure.appendChild(caption);
  if (!item?.url) { const warning = document.createElement("p"); warning.textContent = "缺少原页，不能确认本图。请重新生成候选。"; warning.className = "asset-error"; figure.appendChild(warning); expectedImages.add(`missing:${title}`); return figure; }
  const link = document.createElement("a"), image = document.createElement("img");
  link.href = item.url; link.target = "_blank"; link.rel = "noopener";
  image.alt = title; image.loading = "lazy";
  if (item.width > 0 && item.height > 0) { image.width = item.width; image.height = item.height; }
  expectedImages.add(item.url);
  image.addEventListener("load", () => { loadedImages.add(item.url); updateButtons(); });
  image.addEventListener("error", () => { loadedImages.delete(item.url); updateButtons(); message("有图片未完整加载，请刷新状态重试。图片全部可见后才能提交审核。", "error"); });
  image.src = item.url; link.appendChild(image); figure.appendChild(link); return figure;
}

function renderPackage(data) {
  currentPackage = data; expectedImages = new Set(); loadedImages = new Set();
  $("#package-status").textContent = statusLabels[data.status] || data.status;
  $("#package-id").textContent = data.bundle_id ? `图片包 ${data.bundle_id}` : "候选生成不会发布文章";
  $("#review-form").hidden = !data.bundle_id || !data.figures.length;
  $("#source-binding").textContent = data.source_binding === "verified" ? "源 PDF 哈希已与正文生成记录绑定；仍需人工核对论文身份。" : "旧稿缺少生成时来源绑定。请重点核对论文标题、DOI 与页面内容，不能仅凭文件名确认。";
  $("#source-hash").textContent = data.source_sha256 ? `PDF SHA-256：${data.source_sha256}` : "";
  $("#source-pdf").href = data.source_pdf_url || "#"; $("#source-pdf").hidden = !data.source_pdf_url;
  const list = $("#figure-list"); list.replaceChildren();
  data.figures.forEach((item) => {
    const card = document.createElement("article"), heading = document.createElement("h3"), pair = document.createElement("div");
    card.className = "figure-review"; pair.className = "figure-pair";
    const label = `${item.is_extended ? "Extended Data " : ""}Figure ${item.label}`;
    heading.textContent = `${label} · PDF 第 ${item.page} 页`;
    const page = data.pages.find((source) => source.page === item.page);
    pair.append(imagePreview(page, `原 PDF 完整第 ${item.page} 页`), imagePreview(item, `${label} 完整裁图`));
    const confirm = document.createElement("label"), input = document.createElement("input"), text = document.createElement("span");
    confirm.className = "figure-confirm"; input.type = "checkbox"; input.dataset.key = item.key;
    input.disabled = !page;
    text.textContent = `已对照原页确认 ${label}：图号正确，所有子图、图例、坐标与刻度完整。`;
    confirm.append(input, text); card.append(heading, pair, confirm); list.appendChild(card);
  });
  const cover = $("#cover-image");
  cover.onload = null; cover.onerror = null;
  if (data.cover?.url) {
    expectedImages.add(data.cover.url); $("#cover-link").href = data.cover.url;
    cover.onload = () => { loadedImages.add(data.cover.url); updateButtons(); };
    cover.onerror = () => { loadedImages.delete(data.cover.url); updateButtons(); message("封面加载失败，请刷新后重新核对。", "error"); };
    cover.src = data.cover.url; cover.hidden = false;
  } else { cover.removeAttribute("src"); cover.hidden = true; expectedImages.add("missing-cover"); }
  restoreDraft();
  const approval = data.approval || {};
  $("#approval-record").textContent = data.status === "approved" ? `审核已保存。审核人：${approval.reviewer || "已记录"}。继续发布只使用本包，不重新生成正文。` : "此处不会默认为历史图片批准；每次新包必须重新核对。";
  updateButtons();
}

async function loadPackage(quiet = false) {
  if (currentPackage) saveDraft();
  try {
    const data = await request(api); renderPackage(data);
    if (!quiet) message(data.reason || (data.status === "approved" ? "图片审核已保存，可继续发布。" : "请按下面顺序核对原页、裁图和封面。"), data.status === "approved" ? "success" : "");
  } catch (error) { message(error.message, "error"); }
}

function parsePages(text) {
  const pages = new Set();
  for (const token of text.trim().split(/[,，\s]+/)) {
    if (!/^\d+(?:-\d+)?$/.test(token)) throw new Error("请使用 PDF 页码，例如 1-5, 8。");
    const [start, end = start] = token.split("-").map(Number);
    if (start < 1 || end < start || end > 2000) throw new Error("页码范围须从 1 开始，结束页不能小于起始页。");
    for (let page = start; page <= end; page += 1) pages.add(page);
  }
  return [...pages].sort((a, b) => a - b);
}

function validateForm() {
  document.querySelectorAll(".field-error").forEach((node) => { node.hidden = true; node.textContent = ""; });
  document.querySelectorAll('[aria-invalid="true"]').forEach((node) => node.removeAttribute("aria-invalid"));
  const values = formValues(), errors = [];
  const targets = {"#pages-error": "#paper-pages", "#scope-error": "#paper-title", "#figures-error": "#figure-list input", "#confirmations-error": ".confirmations input"};
  function fail(selector, text) {
    const node = $(selector); node.hidden = false; node.textContent = text;
    const target = $(targets[selector]); if (target) target.setAttribute("aria-invalid", "true");
    errors.push({text, target});
  }
  let pages = [];
  try { pages = parsePages(values.pages); } catch (error) { fail("#pages-error", error.message); }
  if (!values.title.trim() && !values.doi.trim()) fail("#scope-error", "请填写论文原文标题或 DOI，不能仅凭文章中文标题确认来源。");
  if (!values.evidence.trim()) fail("#scope-error", "请填写你实际核对的论文身份依据，例如标题、DOI 或页内位置。");
  if (currentPackage.figures.some((figure) => !pages.includes(figure.page))) fail("#pages-error", "页范围必须包含下面全部图片所在的 PDF 页。");
  if (currentPackage.figures.some((figure) => values.figures[figure.key] !== true)) fail("#figures-error", "请逐张对照并勾选所有图片。");
  if (["source", "number", "completeness", "cover"].some((key) => values.confirmations[key] !== true)) fail("#confirmations-error", "请完成四项确认。");
  if (errors.length) {
    message("尚未提交，请检查以下项目：", "error", true);
    const list = document.createElement("ul");
    errors.forEach(({text, target}) => {
      const item = document.createElement("li"), link = document.createElement("a");
      link.textContent = text; link.href = "#review-form";
      link.addEventListener("click", (event) => { event.preventDefault(); target?.focus(); });
      item.appendChild(link); list.appendChild(item);
    });
    $("#review-message").appendChild(list); return null;
  }
  return {package_id: currentPackage.bundle_id, confirmations: values.confirmations,
    figure_confirmations: values.figures, paper_scope: {title: values.title.trim(), doi: values.doi.trim(), evidence: values.evidence.trim(), pages}, note: values.note};
}

async function pollWorkflow(stage, runId) {
  clearTimeout(pollTimer);
  try {
    const data = await request("/api/workflow/status"), state = data[stage];
    if (state?.run_id !== runId) {
      busy = false; updateButtons();
      message("该任务已不在当前状态窗口，不能确认本次结果。请返回工作台核对文章状态，不要重复启动。", "error");
      return;
    }
    if (state.status === "running") { pollTimer = setTimeout(() => pollWorkflow(stage, runId), 3000); return; }
    try { sessionStorage.removeItem(workflowKey); } catch (_) {}
    busy = false; await loadPackage(true); updateButtons();
    if (!state || !["done", "completed", "failed"].includes(state.status)) { message("后台任务结果暂未确认。请返回工作台查看状态，不要重复启动。", "error"); return; }
    message(state?.status === "failed" ? `后台处理未完成：${(state.errors || []).join("；")}。请核对状态后重试。` : stage === "images" ? "图片候选已生成，请逐张审核。" : "后台发布步骤已完成。多语言任务继续在工作台显示。", state?.status === "failed" ? "error" : "success");
  } catch (error) { busy = false; updateButtons(); message(`${error.message} 可返回工作台查看后台任务；不要重复启动。`, "error"); }
}

function trackWorkflow(stage, result) {
  if (!result?.run_id) { busy = false; updateButtons(); message("后台未返回任务编号，结果暂未确认。请返回工作台核对，不要重复启动。", "error"); return; }
  try { sessionStorage.setItem(workflowKey, JSON.stringify({stage, runId: result.run_id})); } catch (_) {}
  busy = true; updateButtons(); pollWorkflow(stage, result.run_id);
}

async function submitApproval(shouldContinue) {
  if (busy) return; saveDraft(); const body = validateForm(); if (!body) return;
  busy = true; updateButtons(); message("正在保存审核…");
  try {
    const result = await request(`${api}/approve`, {...body, continue: shouldContinue});
    await loadPackage(true);
    if (result.continuation?.ok) { message("审核已保存，已启动后台发布。不会重新生成正文。", "success"); trackWorkflow("review_publish", result.continuation); return; }
    message(result.continuation ? `审核已保存，但未启动发布：${result.continuation.error || "请点击继续发布重试。"}` : "审核已保存，尚未启动发布。", result.continuation ? "error" : "success");
  } catch (error) { message(`审核未确认：${error.message} 你的输入已保留，请刷新状态核对。`, "error", true); }
  busy = false; updateButtons();
}

$("#review-form").addEventListener("input", saveDraft);
$("#review-form").addEventListener("submit", (event) => { event.preventDefault(); submitApproval(true); });
$("#approve-only").addEventListener("click", () => submitApproval(false));
$("#refresh-package").addEventListener("click", () => loadPackage());
$("#prepare-package").addEventListener("click", async () => {
  if (busy) return;
  if (currentPackage?.status === "approved" && !confirm("重新生成候选后需重新审核，现有线上文章不会自动改动。继续吗？")) return;
  busy = true; updateButtons(); message("正在后台准备图片候选；不会发布文章。可保留此页等待。");
  try { const result = await request(`${api}/prepare`, {}); trackWorkflow("images", result); }
  catch (error) { busy = false; updateButtons(); message(error.message, "error"); }
});
$("#continue-publish").addEventListener("click", async () => {
  if (busy) return; busy = true; updateButtons();
  try { const result = await request(`${api}/continue`, {package_id: currentPackage.bundle_id}); message("已启动后台发布，使用当前已审核图片包。", "success"); trackWorkflow("review_publish", result); }
  catch (error) { busy = false; updateButtons(); message(error.message, "error"); }
});
$("#revoke-package").addEventListener("click", async () => {
  const reason = $("#revoke-reason").value.trim();
  if (!reason) { message("请填写撤回原因。", "error"); $("#revoke-reason").focus(); return; }
  if (!confirm("撤回后会阻止后续发布，但不会自动撤下已发布内容。确认撤回？")) return;
  busy = true; updateButtons();
  try { await request(`${api}/revoke`, {package_id: currentPackage.bundle_id, reason}); await loadPackage(true); message("审核已撤回。既有线上内容未改动。", "success"); }
  catch (error) { message(error.message, "error"); }
  busy = false; updateButtons();
});
async function initializeReview() {
  await loadPackage();
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem(workflowKey) || "null"); } catch (_) {}
  if (pending?.runId && ["images", "review_publish"].includes(pending.stage)) {
    busy = true; updateButtons(); message("正在核对上次启动的后台任务…");
    pollWorkflow(pending.stage, pending.runId);
  }
}
initializeReview();
