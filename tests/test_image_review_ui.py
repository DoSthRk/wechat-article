"""Exercise the single-button review flow without network or real publication."""
from pathlib import Path
import subprocess
import unittest


class ImageReviewUiTests(unittest.TestCase):
    def test_single_decision_loading_retry_and_exact_task_result(self):
        subprocess.run(['node', '-e', r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
function node() {return {textContent:'', dataset:{}, children:[], disabled:false,
  addEventListener() {}, appendChild(child) {this.children.push(child);}, append(...children) {this.children.push(...children);},
  replaceChildren() {this.children=[];}, removeAttribute(k) {delete this[k];}};}
const nodes=new Map(), stored=new Map(), timers=new Map(); let timerId=0;
const document={body:{dataset:{jobId:'paper'}}, createElement:node,
  querySelector(selector) {if (!nodes.has(selector)) nodes.set(selector,node()); return nodes.get(selector);}};
const context=vm.createContext({document, assert, nodes, timers, AbortController,
  sessionStorage:{getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
  fetch:()=>new Promise(()=>{}),clearTimeout(id){timers.delete(id);},
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;}});
vm.runInContext(fs.readFileSync('static/image_review.js','utf8'),context);
(async()=>{
  await vm.runInContext(`(async()=>{
    const bundle='a'.repeat(64);
    currentPackage={bundle_id:bundle,status:'needs_review',can_approve:true,figures:[{page:1}],pages:[]};
    expectedImages=new Set(['page','crop','cover']);loadedImages=new Set(['page','crop']);
    updateButton();assert.equal($('#confirm-publish').disabled,true);
    loadedImages.add('cover');updateButton();assert.equal($('#confirm-publish').disabled,false);
    currentPackage.can_approve=false;updateButton();assert.equal($('#confirm-publish').disabled,true);
    currentPackage.can_approve=true;currentPackage.status='stale';updateButton();assert.equal($('#confirm-publish').disabled,true);
    currentPackage.status='needs_review';updateButton();
    let calls=[];
    fetch=async(path,options)=>{calls.push({path,body:options.body?JSON.parse(options.body):null});return {ok:true,json:async()=>path.endsWith('/confirm')
      ? {approved:true,continuation:{ok:true,run_id:'mine'}} : {run_id:'mine',stage:'review_publish',status:'running',bundle_id:bundle}};};
    await confirmAndPublish();await Promise.resolve();
    assert.deepEqual(JSON.stringify(calls[0].body),JSON.stringify({package_id:bundle,images_correct:true}));
    assert.ok(calls[0].path.endsWith('/confirm'));
    assert.equal(busy,false);assert.equal(activeWorkflow.bundleId,bundle);
    assert.equal($('#confirm-publish').textContent,'已提交后台发布');
    assert.match($('#review-message').textContent,/可以关闭此页面/);
    const count=calls.length;await confirmAndPublish();assert.equal(calls.length,count);
    assert.equal($('#confirm-publish').disabled,true);
    const approved={bundle_id:bundle,status:'approved',can_approve:true,figures:[],pages:[],approval:{reviewer:'human'}};
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'done',bundle_id:bundle} : approved});
    await pollWorkflow('review_publish','mine');
    assert.equal(submittedBundle,bundle);assert.equal($('#confirm-publish').textContent,'已提交后台发布');
    assert.equal($('#confirm-publish').disabled,true);
    assert.match($('#review-message').textContent,/公众号草稿已就绪/);
    submittedBundle='';activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'failed',bundle_id:bundle,errors:['failed publish']} : approved});
    await pollWorkflow('review_publish','mine');assert.equal(busy,false);
    assert.match($('#review-message').textContent,/failed publish/);
    activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'done',bundle_id:'b'.repeat(64)} : approved});
    await pollWorkflow('review_publish','mine');assert.equal(submittedBundle,'');
    assert.match($('#review-message').textContent,/配图版本不一致/);
    activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async()=>{throw new Error('offline');};
    await pollWorkflow('review_publish','mine');assert.equal(busy,true);
    assert.match($('#review-message').textContent,/offline/);
    assert.match($('#review-message').textContent,/无需重复点击/);
    assert.ok([...timers.values()].some(t=>t.ms===10000));
    // The accepted response releases the human immediately, even if progress
    // requests never finish. Completion must not reread the locked image pack.
    currentPackage={...approved,figures:[{page:1}]};busy=false;submittedBundle='';clearWorkflow();
    fetch=async(path)=>{
      if(path.endsWith('/confirm'))return {ok:true,json:async()=>({continuation:{ok:true,run_id:'slow'}})};
      return new Promise(()=>{});
    };
    expectedImages=new Set(['ready']);loadedImages=new Set(['ready']);updateButton();
    await confirmAndPublish();assert.equal(busy,false);
    assert.match($('#review-message').textContent,/可以关闭此页面/);
    assert.equal($('#confirm-publish').disabled,true);
    fetch=async(path)=>{
      assert.ok(path.includes('/workflow/'),'must not block on full package GET');
      return {ok:true,json:async()=>({run_id:'slow',stage:'review_publish',status:'done',bundle_id:bundle})};
    };
    await pollWorkflow('review_publish','slow');
    assert.match($('#review-message').textContent,/各语言 Blog 继续后台处理/);
    // Reloading a pending submission restores the accepted UI, not a spinner.
    currentPackage=null;sessionStorage.setItem(workflowKey,JSON.stringify({stage:'review_publish',runId:'slow',bundleId:bundle}));
    fetch=async()=>({ok:true,json:async()=>({run_id:'slow',stage:'review_publish',status:'running',bundle_id:bundle})});
    await initializeReview();assert.equal(busy,false);assert.equal($('#confirm-publish').textContent,'已提交后台发布');
    assert.match($('#review-message').textContent,/无需等待/);
    clearWorkflow();timers.clear();
    fetch=(_path,options)=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
    const timed=request('/timeout');[...timers.values()].find(t=>t.ms===15000).fn();
    await assert.rejects(()=>timed,/请求超时/);assert.equal(timers.size,0);
    fetch=async()=>({status:502,json:async()=>{throw new Error('bad JSON');}});
    await assert.rejects(()=>request('/api'),/HTTP 502/);
  })()`,context);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''], cwd=Path(__file__).resolve().parent.parent, check=True, timeout=15)


if __name__ == '__main__':
    unittest.main()
