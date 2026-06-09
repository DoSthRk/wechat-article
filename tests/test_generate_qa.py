"""_generate_one 的质量闸：健康/调性落库 + 坏稿标 publish_blocked。

用 fake analyzer（不调 LLM）+ 临时库 + 临时 outputs 目录。
"""
import tempfile
import unittest
from pathlib import Path

from core.main import AnalysisResult
from db.database import DatabaseManager, JobStatus
from utils.job_loader import Job
import batch_processor as bp

_GOOD = (
    "# 标题\n\n"
    + "中立、克制的科普正文段落。" * 60
    + "\n\n## 第一节\n内容\n\n## 第二节\n内容\n\n## 第三节\n内容\n"
)
_BAD = "<div>这不是 markdown</div>"


class _FakeAnalyzer:
    def __init__(self, markdown: str):
        self._md = markdown

    def analyze(self, job: Job) -> AnalysisResult:
        return AnalysisResult(job_id=job.job_id, success=True, markdown=self._md, model="fake")


class TestGenerateQA(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.db = DatabaseManager(database_url=f"sqlite:///{(base / 't.db').as_posix()}")
        self._orig_dir = bp.ARTICLE_CONTENT_DIR
        bp.ARTICLE_CONTENT_DIR = str(base / "out")
        task = self.db.get_or_create_task("t")
        # product_id 空 → 跳过正文夹带扫描，专测健康/调性
        self.job_pk = self.db.upsert_job(task.id, "j1", pdf_path="p", template_id="t", product_id="").id
        self.job = Job(job_id="j1", pdf="p", template="t", product="")

    def tearDown(self):
        bp.ARTICLE_CONTENT_DIR = self._orig_dir
        self.db.engine.dispose()
        self._tmp.cleanup()

    def test_healthy_article_not_blocked(self):
        self.assertTrue(bp._generate_one(self.db, self.job_pk, self.job, _FakeAnalyzer(_GOOD)))
        a = self.db.get_article(self.job_pk)
        self.assertGreaterEqual(a.markdown_health_score, 80)
        self.assertFalse(a.publish_blocked)

    def test_unhealthy_article_blocked(self):
        self.assertTrue(bp._generate_one(self.db, self.job_pk, self.job, _FakeAnalyzer(_BAD)))
        a = self.db.get_article(self.job_pk)
        self.assertTrue(a.publish_blocked)
        self.assertIn("markdown_unhealthy", a.block_reason or "")


if __name__ == "__main__":
    unittest.main()
