"""北京时间输入不受浏览器/系统时区影响；不访问网络或生产数据。"""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


class ScheduleUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node unavailable; schedule UI tests NOT run')
    def test_beijing_input_and_validation_in_non_chinese_timezone(self):
        module=(Path(__file__).resolve().parents[1]/'static/editorial-schedule.mjs').as_uri()
        code="""
const assert=require('node:assert/strict');
(async()=>{
const {beijingInput,scheduledISO,beijingTime}=await import(MODULE);
const now=Date.parse('2030-01-01T00:00:00Z');
assert.equal(beijingInput(now),'2030-01-01T08:00');
assert.equal(scheduledISO('2030-01-01T10:00',now),'2030-01-01T02:00:00.000Z');
assert.equal(scheduledISO('2030-01-02T00:30',now),'2030-01-01T16:30:00.000Z');
assert.match(beijingTime('2030-01-01T02:00:00Z'),/10:00.*北京时间/);
for(const invalid of ['','2030-02-30T10:00','2030-01-01T08:00','2030-01-01T08:00Z'])
  assert.throws(()=>scheduledISO(invalid,now));
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace('MODULE',repr(module))
        subprocess.run(['node','-e',code],check=True,
                       env={**os.environ,'TZ':'America/Los_Angeles'},capture_output=True,text=True)
