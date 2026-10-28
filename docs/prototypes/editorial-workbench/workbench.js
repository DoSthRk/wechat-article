/* 本地交互原型：不访问生产接口，不做实际公开发布。 */
'use strict';
const $ = (id) => document.getElementById(id);
const STORE = 'gm-editorial-v2-prototype-1';
const initial = {title:$('article-title').value,digest:$('article-digest').value,body:$('article-body').innerHTML,caption:$('figure-caption').value,figure:'assets/figure-1.jpg',crop:{x:6,y:5.57,w:44.5,h:30.36}};
let model = {...initial,crop:{...initial.crop}}, pendingSave, toastTimer, releaseTimers = [], draftCrop;
let savedRange;

// 示例只接收有限的文本排版，浏览器存储同样视为不可信输入。
function cleanBody(html) {
  const doc = new DOMParser().parseFromString(String(html), 'text/html');
  const allowed = new Set(['P','DIV','BR','H2','H3','STRONG','B','EM','I','UL','OL','LI','BLOCKQUOTE']);
  function clean(node) {
    if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent);
    const fragment = document.createDocumentFragment();
    if (node.nodeType !== Node.ELEMENT_NODE) return fragment;
    if (['SCRIPT','STYLE','IFRAME','OBJECT','EMBED'].includes(node.tagName)) return fragment;
    const out = allowed.has(node.tagName) ? document.createElement(node.tagName.toLowerCase()) : fragment;
    for (const child of node.childNodes) out.append(clean(child));
    return out;
  }
  const wrapper = document.createElement('div');
  for (const node of doc.body.childNodes) wrapper.append(clean(node));
  return wrapper.innerHTML;
}
try {
  const saved = JSON.parse(localStorage.getItem(STORE) || 'null');
  if (saved && typeof saved === 'object') {
    for (const key of ['title','digest','caption']) if (typeof saved[key] === 'string') model[key] = saved[key];
    if (typeof saved.body === 'string') model.body = cleanBody(saved.body);
    if (typeof saved.figure === 'string' && /^data:image\/jpeg;base64,[a-zA-Z0-9+/=]+$/.test(saved.figure)) model.figure = saved.figure;
    if (validCrop(saved.crop)) model.crop = saved.crop;
    if (typeof saved.savedAt === 'string') model.savedAt = saved.savedAt;
  }
} catch (_) { /* 无存储权限时仍可编辑，保存时说明。 */ }

function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},4000); }
function renderModel() {
  savedRange=null;
  $('article-title').value=model.title; $('article-digest').value=model.digest; $('article-body').innerHTML=cleanBody(model.body); $('figure-caption').value=model.caption;
  for (const id of ['article-figure','cover-image','card-figure']) $(id).src=model.figure;
  $('card-title').textContent=model.title;
  $('save-state').textContent=model.savedAt ? `已保存在本机 · ${model.savedAt}` : '示例草稿';
  $('revision-label').textContent=model.savedAt ? `本机修订 · ${model.savedAt}` : '生成原稿 · 示例';
}
function collect() { model.title=$('article-title').value;model.digest=$('article-digest').value;model.body=cleanBody($('article-body').innerHTML);model.caption=$('figure-caption').value; }
function save() {
  clearTimeout(pendingSave);collect();model.savedAt=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  try {localStorage.setItem(STORE,JSON.stringify(model));$('save-state').textContent=`已保存在本机 · ${model.savedAt}`;$('revision-label').textContent=`本机修订 · ${model.savedAt}`;$('card-title').textContent=model.title;return true;}
  catch (_) {$('save-state').textContent='本机保存失败，请保留此页面';toast('浏览器存储不可用或已满，尚未保存。');return false;}
}
function queueSave(){ $('save-state').textContent='正在保存到本机…';clearTimeout(pendingSave);pendingSave=setTimeout(save,500); }
for(const id of ['article-title','article-digest','article-body','figure-caption']) $(id).addEventListener('input',queueSave);
$('article-body').addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData.getData('text/plain'));queueSave();});
document.addEventListener('selectionchange',()=>{const selection=window.getSelection();if(selection.rangeCount && $('article-body').contains(selection.anchorNode)) savedRange=selection.getRangeAt(0).cloneRange();});
document.querySelectorAll('[data-command]').forEach(button=>button.addEventListener('click',()=>{
  $('article-body').focus();if(savedRange){const s=window.getSelection();s.removeAllRanges();s.addRange(savedRange);}
  // 仅用于演示；正式版采用结构化富文本组件。
  document.execCommand(button.dataset.command,false,button.dataset.value || null);queueSave();
}));
function route(){const name=location.hash.slice(1)||'inbox';const valid=['inbox','generating','editor','records'].includes(name)?name:'inbox';document.querySelectorAll('.view').forEach(view=>{view.hidden=view.id!==`${valid}-view`;});document.querySelectorAll('[data-route]').forEach(a=>{const active=a.dataset.route===(valid==='editor'?'inbox':valid);a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});window.scrollTo(0,0);}
window.addEventListener('hashchange',route);
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
for(const id of ['upload-open','generate-upload']) $(id).addEventListener('click',()=>$('upload-dialog').showModal());
$('pdf-input').addEventListener('change',event=>{const file=event.target.files[0];if(!file)return;if(!file.name.toLowerCase().endsWith('.pdf')){toast('请选择 PDF 文件。');return;}$('demo-filename').textContent=file.name;$('generation-empty').hidden=true;$('generation-demo').hidden=false;$('generation-count').textContent='1';$('upload-dialog').close();location.hash='generating';event.target.value='';});
$('finish-demo').addEventListener('click',()=>{$('generation-count').textContent='0';$('generation-demo').hidden=true;$('generation-empty').hidden=false;location.hash='inbox';toast('已回到示例草稿。此原型不根据所选 PDF 生成正文。');});

function validCrop(c){return c && ['x','y','w','h'].every(k=>typeof c[k]==='number'&&Number.isFinite(c[k]))&&c.x>=0&&c.y>=0&&c.w>=1&&c.h>=1&&c.x+c.w<=100.01&&c.y+c.h<=100.01;}
function updateCrop(fromFields=false){
  if(fromFields)draftCrop=Object.fromEntries(['x','y','w','h'].map(k=>[k,Number($(`crop-${k}`).value)]));
  const valid=validCrop(draftCrop);$('crop-apply').disabled=!valid;$('crop-error').textContent=valid?'':'裁框须在原页内，宽高至少为原页的 1%。';if(!valid)return;
  if(!fromFields)for(const key of ['x','y','w','h'])$(`crop-${key}`).value=draftCrop[key].toFixed(1);
  Object.assign($('crop-box').style,{left:`${draftCrop.x}%`,top:`${draftCrop.y}%`,width:`${draftCrop.w}%`,height:`${draftCrop.h}%`});
  const image=$('source-image');if(!image.complete||!image.naturalWidth){$('crop-apply').disabled=true;return;}
  const {x,y,w,h}=draftCrop;const canvas=$('crop-preview');canvas.width=Math.max(1,Math.round(image.naturalWidth*w/100));canvas.height=Math.max(1,Math.round(image.naturalHeight*h/100));canvas.getContext('2d').drawImage(image,image.naturalWidth*x/100,image.naturalHeight*y/100,image.naturalWidth*w/100,image.naturalHeight*h/100,0,0,canvas.width,canvas.height);
}
function openCrop(){draftCrop={...model.crop};$('crop-dialog').showModal();updateCrop();}
for(const id of ['crop-open','source-open'])$(id).addEventListener('click',openCrop);
$('source-image').addEventListener('load',()=>{if(draftCrop)updateCrop();});
$('source-image').addEventListener('error',()=>{$('crop-error').textContent='原页加载失败，请保留当前图片。';$('crop-apply').disabled=true;});
for(const k of ['x','y','w','h'])$(`crop-${k}`).addEventListener('input',()=>updateCrop(true));
$('crop-reset').addEventListener('click',()=>{draftCrop={...initial.crop};updateCrop();});
let start=null;
function point(event){const r=$('crop-stage').getBoundingClientRect();return {x:Math.max(0,Math.min(100,(event.clientX-r.left)/r.width*100)),y:Math.max(0,Math.min(100,(event.clientY-r.top)/r.height*100))};}
$('crop-stage').addEventListener('pointerdown',event=>{if(event.button!==0)return;start=point(event);$('crop-stage').setPointerCapture(event.pointerId);event.preventDefault();});
$('crop-stage').addEventListener('pointermove',event=>{if(!start)return;const p=point(event);draftCrop={x:Math.min(start.x,p.x),y:Math.min(start.y,p.y),w:Math.abs(p.x-start.x),h:Math.abs(p.y-start.y)};updateCrop();});
for(const type of ['pointerup','pointercancel'])$('crop-stage').addEventListener(type,()=>{start=null;});
$('crop-apply').addEventListener('click',()=>{if(!validCrop(draftCrop))return;collect();model.figure=$('crop-preview').toDataURL('image/jpeg',.94);model.crop={...draftCrop};for(const id of ['article-figure','cover-image','card-figure'])$(id).src=model.figure;if(save()){$('crop-dialog').close();toast('裁剪已保存在本机，正文修改已保留。');}});

function renderPreview(mode='wechat'){
  collect();const root=$('preview-content');root.replaceChildren();root.className=`preview-content ${mode}`;
  const title=document.createElement('h1');title.textContent=model.title;root.append(title);
  const body=document.createElement('div');body.innerHTML=cleanBody(model.body);root.append(body);
  const image=document.createElement('img');image.src=model.figure;image.alt=model.caption;root.append(image);
  const caption=document.createElement('figcaption');caption.textContent=model.caption;root.append(caption);
  const footer=document.createElement('footer');footer.textContent=mode==='wechat'?'现有公众号产品模块与阅读原文引导 · 正式版完整套用':'SOLIDEX · Blog 产品模块 · 正式版完整套用';root.append(footer);
  document.querySelectorAll('[data-preview]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.preview===mode)));
}
$('preview-open').addEventListener('click',()=>{renderPreview();$('preview-dialog').showModal();});
document.querySelectorAll('[data-preview]').forEach(b=>b.addEventListener('click',()=>renderPreview(b.dataset.preview)));
$('release-open').addEventListener('click',()=>{if(!save())return;if(!model.title.trim()||!$('article-body').textContent.trim()){toast('请先补全文章标题和正文。');return;}$('confirm-title').textContent=model.title;$('release-dialog').showModal();});
const targets=['公众号 · 免疫客','中文 Blog','English Blog','日本語 Blog','한국어 Blog','Русский Blog'];
let targetStates=targets.map(()=> 'pending');
function renderTargets(){
  const list=$('target-list');list.replaceChildren();targets.forEach((name,i)=>{
    const row=document.createElement('div');row.className='target-row';const title=document.createElement('strong');title.textContent=name;row.append(title);
    const detail=document.createElement('div');const note=document.createElement('p');const state=targetStates[i];note.textContent=state==='failed'?'示例：翻译服务超时，已完成的渠道无需重发':state==='done'?'模拟已验证公开结果 · 正式版显示实际链接':'等待后台处理（演示）';detail.append(note);row.append(detail);
    const status=document.createElement('span');status.className=`target-state ${state==='failed'?'failed':state==='pending'?'pending':''}`;status.textContent=state==='done'?'模拟成功':state==='failed'?'模拟失败':'模拟处理中';
    if(state==='failed'){const retry=document.createElement('button');retry.textContent='模拟重试本语言';retry.addEventListener('click',()=>{targetStates[i]='done';renderTargets();toast('仅重试这一语言，其他渠道保留原结果。');});status.append(document.createElement('br'),retry);}row.append(status);list.append(row);
  });
  const all=targetStates.every(s=>s==='done'),some=targetStates.includes('failed');$('release-badge').textContent=all?'模拟全部完成':some?'模拟部分完成':'模拟发布中';$('release-description').textContent=all?'所有目标模拟完成。正式版会发送含公开链接的飞书通知。':some?'只有俄文版本需要处理，已成功渠道保留。':'已提交，可以关闭页面。正式版将在完成后发送飞书通知。';
}
$('publish-demo').addEventListener('click',()=>{releaseTimers.forEach(clearTimeout);releaseTimers=[];targetStates=targets.map(()=> 'pending');$('release-title').textContent=model.title;$('release-dialog').close();$('records-empty').hidden=true;$('records-content').hidden=false;$('record-count').textContent='1';renderTargets();location.hash='records';for(let i=0;i<targets.length;i++)releaseTimers.push(setTimeout(()=>{targetStates[i]=i===5?'failed':'done';renderTargets();},500+i*450));});
$('restore-open').addEventListener('click',()=>$('restore-dialog').showModal());
$('restore-confirm').addEventListener('click',()=>{model={...initial,crop:{...initial.crop}};renderModel();save();$('restore-dialog').close();toast('示例原稿已恢复。');});
window.addEventListener('beforeunload',()=>{clearTimeout(pendingSave);save();});
renderModel();route();
