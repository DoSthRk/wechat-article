"use strict";

const $ = (selector) => document.querySelector(selector);
const jobId = document.body.dataset.jobId;
const api = `/api/image-review/${encodeURIComponent(jobId)}`;
const workflowKey = `image-review-workflow:${jobId}`;
const reviewableStates = new Set(["needs_review", "revoked", "approved"]);
const statusLabels = {missing: "图片尚未准备好", building: "正在准备图片", needs_review: "等待确认图片", approved: "图片已确认", blocked: "图片需要负责人处理", stale: "图片已变化，需要重新处理", superseded: "图片已有新版本", revoked: "等待重新确认图片"};
let currentPackage = null, busy = false, pollTimer = null, activeWorkflow = null, pollEpoch = 0;
let expectedImages = new Set(), loadedImages = new Set(), submittedBundle = "";
const acceptedMessage = "图片已确认，已提交后台发布。您可以关闭此页面，无需等待或重复点击；公众号草稿及各语言 Blog 将继续自动处理，全部完成后会发送飞书通知。";

function message(text, tone = "") {
  const node = $("#review-message");
  node.textContent = text; node.className = `review-message ${tone}`;
}

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  try {
    const response = await fetch(path, {...options, signal: controller.signal});
    let data;
    try { data = await response.json(); }
    catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error(`暂时无法核对结果（HTTP ${response.status}），请刷新页面查看。`);
    }
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || `请求失败（HTTP ${response.status}），请稍后重试。`);
      error.status = response.status; throw error;
    }
    return data;
  } catch (error) {
    if (controller.signal.aborted) throw new Error("请求超时，后台任务可能仍在运行，请刷新页面核对结果。");
    throw error;
  } finally { clearTimeout(timeout); }
}

function updateButton() {
  const button = $("#confirm-publish");
  const submitted = (currentPackage?.bundle_id && submittedBundle === currentPackage.bundle_id)
    || (activeWorkflow?.stage === "review_publish" && (!currentPackage || activeWorkflow.bundleId === currentPackage.bundle_id));
  const imagesReady = expectedImages.size > 0 && [...expectedImages].every((url) => loadedImages.has(url));
  button.disabled = busy || submitted || !currentPackage?.can_approve || !currentPackage?.figures?.length || !reviewableStates.has(currentPackage.status) || !imagesReady;
  button.textContent = submitted ? "已提交后台发布" : busy ? "正在提交…" : "图片正确，确认并发布";
}

function imagePreview(item, title) {
  const figure = document.createElement("figure"), caption = document.createElement("figcaption");
  caption.textContent = title; figure.appendChild(caption);
  if (!item?.url) {
    const warning = document.createElement("p");
    warning.textContent = "缺少原页，请告知负责人处理后再确认。"; warning.className = "asset-error";
    figure.appendChild(warning); expectedImages.add(`missing:${title}`); return figure;
  }
  const link = document.createElement("a"), image = document.createElement("img");
  link.href = item.url; link.target = "_blank"; link.rel = "noopener";
  image.alt = title; image.loading = "lazy";
  if (item.width > 0 && item.height > 0) { image.width = item.width; image.height = item.height; }
  expectedImages.add(item.url);
  image.addEventListener("load", () => { loadedImages.add(item.url); updateButton(); });
  image.addEventListener("error", () => {
    loadedImages.delete(item.url); updateButton();
    message("有图片未加载成功，请刷新页面；如仍无法查看，请告知负责人。", "error");
  });
  image.src = item.url; link.appendChild(image); figure.appendChild(link); return figure;
}

function renderPackage(data) {
  currentPackage = data; expectedImages = new Set(); loadedImages = new Set();
  $("#package-status").textContent = statusLabels[data.status] || data.status;
  $("#review-content").hidden = !data.bundle_id || !data.figures.length;
  $("#source-pdf").href = data.source_pdf_url || "#"; $("#source-pdf").hidden = !data.source_pdf_url;
  const list = $("#figure-list"); list.replaceChildren();
  data.figures.forEach((item) => {
    const card = document.createElement("article"), heading = document.createElement("h3"), pair = document.createElement("div");
    card.className = "figure-review"; pair.className = "figure-pair";
    const label = `${item.is_extended ? "Extended Data " : ""}Figure ${item.label}`;
    heading.textContent = `${label} · 原 PDF 第 ${item.page} 页`;
    const page = data.pages.find((source) => source.page === item.page);
    pair.append(imagePreview(page, `原 PDF 完整第 ${item.page} 页`), imagePreview(item, `${label} 配图`));
    card.append(heading, pair); list.appendChild(card);
  });
  const cover = $("#cover-image"); cover.onload = null; cover.onerror = null;
  if (data.cover?.url) {
    expectedImages.add(data.cover.url); $("#cover-link").href = data.cover.url;
    cover.onload = () => { loadedImages.add(data.cover.url); updateButton(); };
    cover.onerror = () => { loadedImages.delete(data.cover.url); updateButton(); message("封面加载失败，请刷新页面后再确认。", "error"); };
    cover.src = data.cover.url; cover.hidden = false;
  } else { cover.removeAttribute("src"); cover.hidden = true; expectedImages.add("missing-cover"); }
  $("#approval-record").textContent = data.status === "approved" ? `图片已确认，确认人：${data.approval?.reviewer || "已记录"}。` : "";
  updateButton();
}

async function loadPackage(quiet = false) {
  try {
    const data = await request(api); renderPackage(data);
    if (!quiet) message(reviewableStates.has(data.status) ? "请查看配图和封面，图片正确即可点击下方按钮发布。" : `${data.reason || statusLabels[data.status]}。图片有误或无法查看，请告知负责人。`, reviewableStates.has(data.status) ? "" : "error");
    return data;
  } catch (error) { message(error.message, "error"); return null; }
}

function clearWorkflow() {
  clearTimeout(pollTimer); pollEpoch += 1; activeWorkflow = null; busy = false;
  try { sessionStorage.removeItem(workflowKey); } catch (_) {}
}

async function pollWorkflow(stage, runId) {
  clearTimeout(pollTimer);
  const epoch = ++pollEpoch;
  try {
    const state = await request(`${api}/workflow/${encodeURIComponent(runId)}`);
    if (epoch !== pollEpoch) return;
    if (state?.run_id !== runId || state?.stage !== stage) {
      clearWorkflow(); updateButton(); message("暂时无法核对本次任务，请返回工作台查看。", "error"); return;
    }
    const expectedBundle = activeWorkflow?.bundleId;
    if (stage === "review_publish" && (state.bundle_id !== expectedBundle
        || (currentPackage && currentPackage.bundle_id !== expectedBundle))) {
      clearWorkflow(); submittedBundle = ""; updateButton();
      message("后台任务与当前配图版本不一致，请返回工作台核对或告知负责人。", "error"); return;
    }
    if (state.status === "running") {
      busy = stage === "images"; updateButton();
      message(stage === "images" ? "正在后台准备图片，完成后自动显示。" : acceptedMessage, stage === "images" ? "" : "success");
      pollTimer = setTimeout(() => pollWorkflow(stage, runId), 3000); return;
    }
    if (!["done", "completed", "failed"].includes(state.status)) {
      message("后台任务结果尚未确认，请刷新页面核对。", "error"); return;
    }
    clearWorkflow();
    if (state.status === "failed" || state.failed > 0) {
      submittedBundle = ""; updateButton();
      const prefix = stage === "review_publish" ? "图片确认已保存，但后台发布未完成" : "后台图片准备未完成";
      message(`${prefix}：${(state.errors || []).join("；")}。请重试或告知负责人。`, "error");
      if (!currentPackage) await loadPackage(true);
      return;
    }
    if (stage === "review_publish") {
      // Do not reread the full package here: the multilingual worker may hold
      // its image lock for minutes. This exact run/bundle receipt is sufficient
      // to show submission progress, not a claim that every Blog is published.
      submittedBundle = expectedBundle;
      if (!currentPackage) currentPackage = {bundle_id: expectedBundle};
      $("#package-status").textContent = "公众号草稿已就绪"; updateButton();
      message("中文 Blog 已核验，公众号草稿已就绪。各语言 Blog 继续后台处理，全部完成后会发送飞书通知；您可以关闭此页面。", "success");
    } else {
      const data = await loadPackage(true); updateButton();
      if (data) message("图片已准备好，请查看后确认。", "success");
    }
  } catch (error) {
    if (epoch !== pollEpoch) return;
    if (error.status === 404) clearWorkflow();
    updateButton();
    message(stage === "review_publish" ? `已提交后台发布，无需重复点击。暂时无法更新进度：${error.message} 您可以关闭页面，稍后在工作台核对。` : error.message, "error");
    if (activeWorkflow && error.status !== 404) pollTimer = setTimeout(() => pollWorkflow(stage, runId), 10000);
  }
}

function trackWorkflow(result) {
  if (!result?.run_id) {
    busy = false; updateButton(); message("图片确认已保存，发布任务尚未确认，请返回工作台核对。", "error"); return;
  }
  activeWorkflow = {stage: "review_publish", runId: result.run_id, bundleId: currentPackage.bundle_id};
  try { sessionStorage.setItem(workflowKey, JSON.stringify(activeWorkflow)); } catch (_) {}
  submittedBundle = currentPackage.bundle_id; busy = false;
  $("#package-status").textContent = "已提交后台发布";
  updateButton(); message(acceptedMessage, "success"); pollWorkflow(activeWorkflow.stage, activeWorkflow.runId);
}

async function confirmAndPublish() {
  if ($("#confirm-publish").disabled) return;
  busy = true; updateButton(); message("正在确认图片并提交发布…");
  try {
    const result = await request(`${api}/confirm`, {package_id: currentPackage.bundle_id, images_correct: true});
    if (result.continuation?.ok) { trackWorkflow(result.continuation); return; }
    busy = false; await loadPackage(true); updateButton();
    message(result.continuation?.error || "图片确认已保存，发布未启动，请点击同一按钮重试。", "error");
  } catch (error) {
    busy = false; updateButton(); message(`提交结果尚未确认：${error.message}`, "error");
  }
}

$("#confirm-publish").addEventListener("click", confirmAndPublish);

async function initializeReview() {
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem(workflowKey) || "null"); } catch (_) {}
  if (pending?.runId && ["images", "review_publish"].includes(pending.stage)) {
    activeWorkflow = pending; busy = pending.stage === "images"; updateButton();
    if (pending.stage === "review_publish") $("#package-status").textContent = "已提交后台发布";
    message(pending.stage === "review_publish" ? acceptedMessage : "正在核对上次提交的任务…", pending.stage === "review_publish" ? "success" : "");
    await pollWorkflow(pending.stage, pending.runId);
  } else { await loadPackage(); }
}
initializeReview();
