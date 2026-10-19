"""Execute review-page state logic with a minimal DOM, without a browser or network."""
from pathlib import Path
import subprocess
import unittest


class ImageReviewUiTests(unittest.TestCase):
    def test_review_readiness_recovery_and_task_identity(self):
        subprocess.run(['node', '-e', r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
function node() { return {value: '', textContent: '', dataset: {}, children: [], checked: false,
  addEventListener() {}, appendChild(child) {this.children.push(child);}, append(...children) {this.children.push(...children);},
  replaceChildren() {this.children = [];}, setAttribute(k, v) {this[k] = v;}, removeAttribute(k) {delete this[k];}, focus() {this.focused = true;}}; }
const nodes = new Map(), confirmations = ['source', 'number', 'completeness', 'cover'].map(name => ({...node(), name}));
const figures = [{...node(), dataset: {key: 'main:1'}}], stored = new Map();
const document = {body: {dataset: {jobId: 'paper'}}, createElement: node,
  querySelector(selector) {if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector);},
  querySelectorAll(selector) {return selector === '.confirmations input' ? confirmations : selector === '.figure-confirm input' ? figures : [];} };
const context = vm.createContext({document, assert, nodes, confirmations, figures,
  sessionStorage: {getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)},
  fetch: () => new Promise(() => {}), clearTimeout() {}, setTimeout() {}, confirm: () => true});
vm.runInContext(fs.readFileSync('static/image_review.js', 'utf8'), context);
(async () => {
  await vm.runInContext(`(async () => {
    assert.deepEqual(JSON.stringify(parsePages('1-3, 8， 3')), '[1,2,3,8]');
    for (const bad of ['', '0', '3-1', '1-2001', 'page1']) assert.throws(() => parsePages(bad));
    currentPackage = {bundle_id: 'a'.repeat(64), status: 'needs_review', can_approve: true,
      figures: [{key: 'main:1', page: 2}]};
    expectedImages = new Set(['page', 'crop', 'cover']); loadedImages = new Set(['page', 'crop']);
    updateButtons(); assert.equal($('#approve-continue').disabled, true);
    loadedImages.add('cover'); updateButtons(); assert.equal($('#approve-continue').disabled, false);
    currentPackage.can_approve = false; updateButtons(); assert.equal($('#approve-continue').disabled, true);
    currentPackage.can_approve = true; currentPackage.status = 'stale'; updateButtons(); assert.equal($('#approve-continue').disabled, true);
    currentPackage.status = 'needs_review';
    $('#paper-title').value = 'Source title'; $('#paper-pages').value = '1-2'; $('#paper-evidence').value = 'DOI checked';
    confirmations.forEach(node => node.checked = true);
    assert.equal(validateForm(), null); assert.equal($('#review-message').focused, true);
    assert.equal($('#figures-error').hidden, false);
    figures[0].checked = true;
    const payload = validateForm(); assert.equal(payload.figure_confirmations['main:1'], true);
    assert.equal(payload.paper_scope.title, 'Source title');
    saveDraft(); $('#paper-title').value = ''; restoreDraft(); assert.equal($('#paper-title').value, 'Source title');
    imagePreview(null, 'Original page'); updateButtons(); assert.equal($('#approve-continue').disabled, true);
    currentPackage.status = 'approved'; updateButtons(); assert.equal($('#continue-publish').hidden, false);
    assert.equal($('#approve-continue').hidden, true);
    fetch = async () => ({ok: true, json: async () => ({images: {run_id: 'other', status: 'done'}})});
    await pollWorkflow('images', 'mine'); assert.match($('#review-message').textContent, /不能确认本次结果/);
    fetch = async () => ({status: 502, json: async () => {throw new Error('invalid JSON');}});
    await assert.rejects(() => request('/api'), /HTTP 502/);
  })()`, context);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''], cwd=Path(__file__).resolve().parent.parent, check=True, timeout=15)


if __name__ == '__main__':
    unittest.main()
