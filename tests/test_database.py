"""distributions 表 + Manager 方法测试（1 article : N distribution）。

用临时 sqlite，验证 upsert / 唯一性 / 一对多 / 按目标查询。
"""
import tempfile
import unittest
from pathlib import Path

from db.database import DatabaseManager, JobStatus


class TestDistributions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "t.db"
        self.db = DatabaseManager(database_url=f"sqlite:///{db_path.as_posix()}")
        task = self.db.get_or_create_task("t1")
        job = self.db.upsert_job(
            task.id, "job1",
            pdf_path="p.pdf", template_id="t", product_id="pr",
            status=JobStatus.PENDING,
        )
        self.job_pk = job.id

    def tearDown(self):
        self.db.engine.dispose()   # Windows：释放 sqlite 文件句柄，否则临时目录删不掉
        self._tmp.cleanup()

    def test_upsert_creates_then_updates_same_row(self):
        d = self.db.upsert_distribution(self.job_pk, "wechat", account="aav")
        self.assertEqual(d.publish_status, "pending")
        self.assertEqual(d.lang, "zh")
        d2 = self.db.upsert_distribution(
            self.job_pk, "wechat", account="aav",
            wechat_media_id="m1", publish_status="published",
        )
        self.assertEqual(d2.id, d.id)               # upsert：同一行
        self.assertEqual(d2.wechat_media_id, "m1")
        self.assertEqual(d2.publish_status, "published")

    def test_one_article_many_distributions(self):
        self.db.upsert_distribution(self.job_pk, "wechat", account="aav")
        self.db.upsert_distribution(self.job_pk, "blog", account="genemedi", lang="en")
        self.db.upsert_distribution(self.job_pk, "linkedin")
        rows = self.db.list_distributions(self.job_pk)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r.platform for r in rows}, {"wechat", "blog", "linkedin"})

    def test_get_distribution_by_target(self):
        self.db.upsert_distribution(self.job_pk, "wechat", account="aav", lang="zh")
        self.assertIsNotNone(
            self.db.get_distribution(self.job_pk, "wechat", account="aav", lang="zh")
        )
        # 不同 account / lang → 不命中（platform×account×lang 才唯一）
        self.assertIsNone(self.db.get_distribution(self.job_pk, "wechat", account="immune"))
        self.assertIsNone(self.db.get_distribution(self.job_pk, "wechat", account="aav", lang="en"))

    def test_source_pdf_identity_is_persisted_and_exposed(self):
        self.db.update_job_source_pdf(
            self.job_pk,
            url="https://www.genemedi.net/uploads/papers/ab/hash.pdf",
            sha256="a" * 64,
            size=12345,
        )
        job = self.db.get_job(self.job_pk)
        self.assertEqual(job.source_pdf_sha256, "a" * 64)
        self.assertEqual(job.source_pdf_size, 12345)
        rows, _ = self.db.list_article_overview()
        self.assertEqual(rows[0]["source_pdf_url"], job.source_pdf_url)


if __name__ == "__main__":
    unittest.main()
