"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };
const esc = (v) => (v == null ? "" : String(v)).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let busy = false, pollTimer = null, sourceLines = [], pipelineArticles = [];
const fileName = (path) => String(path || "").split(/[\\/]/).pop() || "未命名 PDF";
const version = (a, lang) => (a.versions || []).find((v) => v.lang === lang);
const dist = (a, platform, lang) => (a.distributions || []).find((d) => d.platform === platform && d.lang === lang);
const label = (state, fallback) => ({ generated:"已生成", ready:"已就绪", pending:"待发布", publishing:"发布中", published:"已发布", failed:"失败", waiting_draft:"待入草稿箱", waiting_translation:"待翻译", translated:"已翻译", running:"生成中", blocked:"需处理" }[state] || fallback);
const tone = (state) => ["published","translated","ready","generated"].includes(state) ? "ok" : ["pending","running","publishing","waiting_translation"].includes(state) ? "info" : ["failed","blocked"].includes(state) ? "danger" : "muted";
const status = (state, fallback) => `<span class="stage-status"><i class="status-dot ${tone(state)}"></i>${esc(label(state, fallback))}</span>`;
const unique = (rows) => { const seen = new Set(); return (rows || []).filter((a) => a.job_id && !seen.has(a.job_id) && seen.add(a.job_id)); };

function updateStages() {
  const pdf = sourceLines.reduce((sum, line) => sum + ((line.counts || {}).pending || (line.pdfs || []).filter((f) => !f.has_article).length), 0);
  const drafts = pipelineArticles.filter((a) => dist(a, "wechat", "zh")?.publish_status === "published").length;
  const cms = pipelineArticles.map((a) => dist(a, "blog", "zh")).filter(Boolean);
  const done = cms.filter((d) => d.publish_status === "published").length;
  const wait = cms.filter((d) => ["pending", "failed"].includes(d.publish_status)).length;
  $("#stage-pdf").textContent = `${pdf} 篇待生成`;
  $("#stage-wechat").textContent = `${drafts} 篇已入草稿箱`;
  $("#stage-cms").textContent = wait ? `${wait} 篇待发布，${done} 篇已发布` : `${done} 篇已发布`;
}

function updateSelection() {
  const selected = [...document.querySelectorAll(".pipeline-pick:checked")];
  const available = document.querySelectorAll(".pipeline-pick:not(:disabled)").length;
  const translate = selected.filter((n) => n.dataset.translate === "1").length;
  const publish = selected.filter((n) => n.dataset.publish === "1").length;
  $("#pipeline-selection").textContent = selected.length ? `已选择 ${selected.length} 项，可翻译 ${translate} 项，可发布 ${publish} 项` : "未选择文章";
  $("#pick-all").checked = !!selected.length && selected.length === available;
}

function renderPipeline() {
  const body = $("#pipeline-rows"), rows = unique(pipelineArticles); body.innerHTML = "";
  if (!rows.length) body.innerHTML = '<tr><td colspan="7" class="muted empty-cell">暂无已生成文章</td></tr>';
  for (const article of rows) {
    const zh = version(article, "zh"), en = version(article, "en"), wx = dist(article, "wechat", "zh"), cmsZh = dist(article, "blog", "zh"), cmsEn = dist(article, "blog", "en");
    const genState = article.publish_blocked ? "blocked" : article.status;
    const wxState = wx?.publish_status === "published" ? "published" : (wx?.publish_status || "waiting_draft");
    const zhState = cmsZh?.publish_status || "pending", enState = en?.translation_status || "waiting_translation";
    const canTranslate = !!en && ["pending", "failed"].includes(enState);
    const canZh = !!cmsZh && ["pending", "failed"].includes(zhState);
    const canEn = !!cmsEn && enState === "translated" && ["pending", "failed"].includes(cmsEn.publish_status);
    const pick = el("input", "pipeline-pick"); pick.type = "checkbox"; pick.dataset.jobId = article.job_id; pick.dataset.translate = canTranslate ? "1" : "0"; pick.dataset.publish = (canZh || canEn) ? "1" : "0"; pick.disabled = !(canTranslate || canZh || canEn); pick.onchange = updateSelection;
    const title = article.title || fileName(article.pdf_path);
    const action = canZh ? '<button class="text-button" data-action="publish" data-lang="zh">发布中文</button>' : canTranslate ? '<button class="text-button" data-action="translate" data-lang="en">翻译英文</button>' : canEn ? '<button class="text-button" data-action="publish" data-lang="en">发布英文</button>' : "";
    const error = cmsZh?.publish_error || en?.translation_error || "";
    const row = el("tr");
    row.innerHTML = `<td class="check-col"></td><td><div class="article-title" title="${esc(title)}">${esc(title)}</div><div class="article-file">${esc(fileName(article.pdf_path))}</div></td><td>${status(genState,"待生成")}</td><td>${status(wxState,"待入草稿箱")}</td><td>${status(zhState,"待发布")}</td><td>${status(enState,"待翻译")}</td><td><div class="row-actions">${action}${article.title ? `<a class="link" target="_blank" href="/preview/${encodeURIComponent(article.job_id)}?wechat=1">预览</a>` : ""}${error ? `<span class="err" title="${esc(error)}">失败详情</span>` : ""}</div></td>`;
    row.firstElementChild.appendChild(pick); body.appendChild(row);
  }
  updateSelection(); updateStages();
}

async function loadArticles() {
  try { const data = await (await fetch("/api/articles")).json(); pipelineArticles = data.articles || []; const s = data.stats || {}; $("#stats").innerHTML = `<span>文章 <b>${s.total || 0}</b></span><span>已生成 <b>${s.generated || 0}</b></span><span>公众号草稿 <b>${s.published || 0}</b></span><span>需处理 <b>${s.blocked || 0}</b></span>`; renderPipeline(); }
  catch (e) { $("#pipeline-rows").innerHTML = `<tr><td colspan="7" class="err empty-cell">加载失败：${esc(e.message)}</td></tr>`; }
}

function sourceTable(files, checked) {
  const table = el("table", "src-grid"); table.innerHTML = "<thead><tr><th></th><th>PDF 文件</th><th>关联文章</th><th>状态</th><th></th></tr></thead>"; const body = el("tbody");
  files.forEach((file) => { const row = el("tr"), pick = el("input", "pick"); pick.type = "checkbox"; pick.value = file.pdf; pick.checked = checked; pick.dataset.processed = file.has_article ? "1" : "0"; const bound = file.bound ? `<span class="mono">${esc(file.job_id)}</span>${file.title ? `<div class="bind-title">${esc(file.title)}</div>` : ""}` : '<span class="muted">未生成</span>'; const state = file.published ? status("published","已入草稿箱") : file.blocked ? status("blocked","需处理") : file.has_article ? status("generated","已生成") : status("pending","待处理"); row.innerHTML = `<td></td><td class="src-name">${esc(file.name)}</td><td class="src-bind">${bound}</td><td>${state}</td><td class="src-act"></td>`; row.firstElementChild.appendChild(pick); const action = row.lastElementChild; if (file.has_article) { action.innerHTML = `<a class="link" target="_blank" href="/preview/${encodeURIComponent(file.job_id)}?wechat=1">预览</a>`; } else { const remove = el("button","text-button","删除"); remove.onclick = () => deletePdf(file.line_id, file.pdf); action.appendChild(remove); } body.appendChild(row); }); table.appendChild(body); return table;
}

function renderLine(line) {
  const wrap = el("div", "line-block"), pdfs = line.pdfs || [], pending = pdfs.filter((f) => !f.has_article), processed = pdfs.filter((f) => f.has_article), counts = line.counts || {}; const head = el("div", "line-head");
  head.innerHTML = `<b>${esc(line.name)}</b><span class="tag">${esc(line.account || line.line_id)}</span><span class="line-summary">待处理 <b>${counts.pending ?? pending.length}</b> · 已生成 <b>${counts.processed ?? processed.length}</b> · 草稿箱 <b>${counts.published || 0}</b></span>`;
  const acts = el("div", "line-acts"), input = el("input"); input.type = "file"; input.accept = "application/pdf,.pdf"; input.multiple = true; input.hidden = true; input.onchange = () => { uploadPdfs(line.line_id,input.files); input.value=""; }; const upload = el("button","btn ghost","上传 PDF"), selected = el("button","btn run","生成选中文件"), all = el("button","btn run ghost","生成待处理"); upload.onclick = () => input.click(); selected.onclick = () => { const picked=[...wrap.querySelectorAll(".pick:checked")]; startRun(line.line_id,picked.map((n)=>n.value),picked.filter((n)=>n.dataset.processed==="1").length); }; all.onclick=()=>startRun(line.line_id,pending.map((f)=>f.pdf),0); acts.append(input,upload,selected,all); head.appendChild(acts); wrap.appendChild(head);
  wrap.ondragover=(e)=>{e.preventDefault();wrap.classList.add("drag");}; wrap.ondragleave=()=>wrap.classList.remove("drag"); wrap.ondrop=(e)=>{e.preventDefault();wrap.classList.remove("drag");const files=[...(e.dataTransfer.files||[])].filter((f)=>f.name.toLowerCase().endsWith(".pdf"));if(files.length)uploadPdfs(line.line_id,files);};
  if(pending.length){wrap.append(el("p","src-section-title","待处理 PDF"),sourceTable(pending,true));}else wrap.appendChild(el("p","src-section-title muted","暂无待处理 PDF")); if(processed.length){const details=el("details","processed-box");details.appendChild(el("summary","",`已生成文件 ${processed.length} 篇`));details.appendChild(sourceTable(processed,false));wrap.appendChild(details);} return wrap;
}

async function loadSources() { const box=$("#sources"); try { const data=await (await fetch("/api/sources")).json(); sourceLines=data.lines||[]; box.innerHTML=""; if(!sourceLines.length)box.innerHTML='<p class="muted source-loading">暂无内容线</p>';sourceLines.forEach((line)=>box.appendChild(renderLine(line)));applyBusy();updateStages();} catch(e){box.innerHTML=`<p class="err source-loading">加载失败：${esc(e.message)}</p>`;} }
async function uploadPdfs(lineId,files) { if(!files?.length)return;const form=new FormData();form.append("line_id",lineId);[...files].forEach((f)=>form.append("file",f));try{const response=await fetch("/api/upload",{method:"POST",body:form}),data=await response.json(),failed=(data.results||[]).filter((x)=>!x.ok);if(!response.ok||(!data.ok&&!data.results?.length))throw Error(data.error||`HTTP ${response.status}`);if(failed.length)alert(`有 ${failed.length} 个文件上传失败：${failed[0].error||failed[0].name}`);await loadSources();}catch(e){alert(`上传失败：${e.message}`);} }
async function deletePdf(lineId,pdf) { if(!confirm(`删除待处理文件 ${fileName(pdf)}？`))return;try{const response=await fetch("/api/pdf/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({line_id:lineId,pdf})}),data=await response.json();if(!response.ok||!data.ok)throw Error(data.error||`HTTP ${response.status}`);await loadSources();}catch(e){alert(`删除失败：${e.message}`);} }
async function startRun(lineId,pdfs,rerun) { if(busy)return alert("已有任务在运行，请等待完成后再启动。");if(!pdfs.length)return alert("请先选择 PDF。");if(!confirm(`生成 ${pdfs.length} 篇文章并提交至公众号草稿箱？${rerun?`\n其中 ${rerun} 篇会重新生成新的草稿。`:""}`))return;try{const response=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({line_id:lineId,pdfs})}),data=await response.json();if(!response.ok||!data.ok)throw Error(data.error||`HTTP ${response.status}`);busy=true;applyBusy();poll();}catch(e){alert(`启动失败：${e.message}`);} }
function applyBusy(){document.querySelectorAll(".btn.run").forEach((b)=>b.disabled=busy);$("#cancel-run").disabled=!busy;}
function summary(s){if(!s)return"";return `<div class="run-summary">${esc(s.message||"")}<span>生成 ${s.generated||0}/${s.total||0}</span><span>拦下 ${s.blocked||0}</span><span>失败 ${s.failed||0}</span>${s.last_problem?`<div class="run-problem">${esc(s.last_problem)}</div>`:""}</div>`;}
async function poll(){if(pollTimer)clearTimeout(pollTimer);try{const data=await (await fetch("/api/runs")).json();busy=!!data.busy;applyBusy();const badge=$("#run-badge"),current=$("#run-current"),log=$("#run-log"),history=$("#run-history");if(data.current){badge.textContent="运行中";badge.className="run-badge running";current.innerHTML=`<b>${esc(data.current.task)}</b> · ${esc(data.current.line_id)} · ${data.current.jobs.length} 篇${summary(data.current.summary)}`;log.textContent=(data.current.log||[]).join("\n")||"任务启动中";log.classList.remove("muted");}else{badge.textContent="空闲";badge.className="run-badge idle";current.innerHTML="";log.textContent="暂无运行任务";log.classList.add("muted");}history.innerHTML="";(data.history||[]).forEach((item)=>{const d=el("details","hist-item");d.append(el("summary","",`${item.status==="done"?"已完成":item.status==="cancelled"?"已停止":"失败"} · ${item.task} · ${item.jobs.length} 篇`),el("pre","run-log",(item.log||[]).join("\n")));history.appendChild(d);});if(busy)pollTimer=setTimeout(poll,2500);else await Promise.all([loadSources(),loadArticles()]);}catch(e){pollTimer=setTimeout(poll,3000);}}
function selected(kind){return [...document.querySelectorAll(".pipeline-pick:checked")].filter((n)=>n.dataset[kind]==="1").flatMap((n)=>{if(kind==="translate")return[{job_id:n.dataset.jobId,lang:"en"}];const a=pipelineArticles.find((x)=>x.job_id===n.dataset.jobId)||{},out=[],zh=dist(a,"blog","zh"),en=dist(a,"blog","en");if(zh&&["pending","failed"].includes(zh.publish_status))out.push({job_id:n.dataset.jobId,lang:"zh"});if(en&&version(a,"en")?.translation_status==="translated"&&["pending","failed"].includes(en.publish_status))out.push({job_id:n.dataset.jobId,lang:"en"});return out;});}
async function cmsAction(action, choices){const data=choices||selected(action==="translate"?"translate":"publish"),message=$("#pipeline-message");if(!data.length){message.className="pipeline-message err";message.textContent=action==="translate"?"请选择待翻译的英文版本。":"请选择可发布的中文或英文版本。";return;}if(!confirm(`确认${action==="translate"?"翻译":"发布到 GeneMedi CMS"} ${data.length} 个版本？`))return;message.className="pipeline-message muted";message.textContent="处理中...";try{const response=await fetch(`/api/blog/${action}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({selections:data})}),result=await response.json(),failed=(result.results||[]).filter((x)=>!x.ok);message.className=failed.length?"pipeline-message err":"pipeline-message ok";message.textContent=failed.length?`完成，但有 ${failed.length} 项失败：${failed[0].error||"请查看任务状态"}`:`完成，已处理 ${result.results.length} 项。`;await loadArticles();}catch(e){message.className="pipeline-message err";message.textContent=`操作失败：${e.message}`;}}
$("#refresh-src").onclick=loadSources;$("#refresh-pipeline").onclick=loadArticles;$("#blog-translate").onclick=()=>cmsAction("translate");$("#blog-publish").onclick=()=>cmsAction("publish");$("#cancel-run").onclick=async()=>{if(busy&&confirm("停止当前任务？已生成的中间结果会保留。")){await fetch("/api/run/cancel",{method:"POST"});poll();}};$("#pick-all").onchange=(e)=>{document.querySelectorAll(".pipeline-pick:not(:disabled)").forEach((n)=>n.checked=e.target.checked);updateSelection();};$("#pipeline-rows").onclick=(e)=>{const b=e.target.closest("[data-action]");if(!b)return;const pick=b.closest("tr").querySelector(".pipeline-pick");cmsAction(b.dataset.action,[{job_id:pick.dataset.jobId,lang:b.dataset.lang}]);};
loadSources();loadArticles();poll();
