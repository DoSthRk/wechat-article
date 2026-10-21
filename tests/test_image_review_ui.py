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
      ? {approved:true,continuation:{ok:true,run_id:'mine'}} : {run_id:'mine',stage:'review_publish',status:'running'}};};
    await confirmAndPublish();await Promise.resolve();
    assert.deepEqual(JSON.stringify(calls[0].body),JSON.stringify({package_id:bundle,images_correct:true}));
    assert.ok(calls[0].path.endsWith('/confirm'));
    assert.equal(busy,true);assert.equal(activeWorkflow.bundleId,bundle);
    const count=calls.length;await confirmAndPublish();assert.equal(calls.length,count);
    assert.equal($('#confirm-publish').disabled,true);
    const approved={bundle_id:bundle,status:'approved',can_approve:true,figures:[],pages:[],approval:{reviewer:'human'}};
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'done'} : approved});
    await pollWorkflow('review_publish','mine');
    assert.equal(submittedBundle,bundle);assert.equal($('#confirm-publish').textContent,'已提交发布');
    assert.equal($('#confirm-publish').disabled,true);
    assert.match($('#review-message').textContent,/发布步骤已完成/);
    submittedBundle='';activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'failed',errors:['failed publish']} : approved});
    await pollWorkflow('review_publish','mine');assert.equal(busy,false);
    assert.match($('#review-message').textContent,/failed publish/);
    activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async(path)=>({ok:true,json:async()=>path.includes('/workflow/')
      ? {run_id:'mine',stage:'review_publish',status:'done'} : {...approved,bundle_id:'b'.repeat(64)}});
    await pollWorkflow('review_publish','mine');assert.equal(submittedBundle,'');
    assert.match($('#review-message').textContent,/配图状态已变化/);
    activeWorkflow={stage:'review_publish',runId:'mine',bundleId:bundle};busy=true;
    fetch=async()=>{throw new Error('offline');};
    await pollWorkflow('review_publish','mine');assert.equal(busy,true);
    assert.match($('#review-message').textContent,/offline/);
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
