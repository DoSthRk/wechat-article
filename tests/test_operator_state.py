"""用 Node 执行业务页状态函数，验证重新上传与已完成队列互斥。"""
from pathlib import Path
import subprocess


def test_reuploaded_pdf_is_pending_until_cancelled_or_processed():
    root = Path(__file__).resolve().parent.parent
    subprocess.run(["node", "-e", r'''
const vm = require("node:vm");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const context = vm.createContext({
  document: {querySelector: () => ({addEventListener() {}})},
  fetch: () => new Promise(() => {}),
  clearTimeout() {}, setTimeout() {}, Event: class Event {}, assert,
});
vm.runInContext(fs.readFileSync("static/operator.js", "utf8"), context);
vm.runInContext(`
  const completed = {has_article: true, needs_action: false, distributions: [
    {platform: "blog", lang: "zh", publish_status: "published"},
    {platform: "wechat", lang: "zh", publish_status: "published"},
  ]};
  const uploaded = {...completed, operator_pending: true, needs_action: true};
  assert.equal(requiresCoreAction(uploaded), true);
  assert.equal(isCompleted(uploaded), false);
  assert.equal(coreStatus(uploaded)[1], "PDF 已重新上传，待重新处理");
  assert.equal(isCompleted(completed), true);
  assert.equal(requiresCoreAction(completed), false);
  const failed = {...completed, distributions: []};
  assert.equal(isCompleted(failed), false);
  assert.equal(requiresCoreAction(failed), true);
  generationState = {current: {jobs: ["active"]}};
  assert.equal(coreStatus({...uploaded, job_id: "active"})[0], "running");
`, context);
'''], cwd=root, check=True, text=True, timeout=15)
