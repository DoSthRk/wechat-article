"""_distribute_one：基准正文 → 公众号 distribution；首次 create，重投 PATCH。

用 fake WeChatClient + 临时 sqlite，不触网。
"""
import argparse
import tempfile
import unittest
from pathlib import Path

from db.database import DatabaseManager, JobStatus
from utils.job_loader import Job
import batch_processor as bp


class FakeWeChat:
    def __init__(self):
        self.created = []
        self.updated = []

    def create_draft(self, articles):
        self.created.append(articles)
        return "media-NEW"

    def update_draft(self, media_id, index, article):
        self.updated.append((media_id, index, article))


def _args():
    return argparse.Namespace(placeholder_author="TarMart", placeholder_thumb_media="thumb-1")


class TestDistributeOne(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.db = DatabaseManager(database_url=f"sqlite:///{(base / 't.db').as_posix()}")
        task = self.db.get_or_create_task("t")
        self.job_pk = self.db.upsert_job(
            task.id, "j1", pdf_path="p", template_id="t", product_id="pr",
            status=JobStatus.GENERATED,
        ).id
        content_dir = base / "out"
        content_dir.mkdir()
        # distribute 从 article.md 实时渲染，所以这里写 markdown
        (content_dir / "article.md").write_text("# 标题\n\n正文段落。", encoding="utf-8")
        self.db.upsert_article(self.job_pk, title="测试标题", digest="测试摘要", content_dir=str(content_dir))
        self.job = Job(job_id="j1", pdf="p", template="t", product="pr", line=None)

    def tearDown(self):
        self.db.engine.dispose()
        self._tmp.cleanup()

    def test_first_creates_then_repeat_patches(self):
        fake = FakeWeChat()
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, fake, _args()))
        self.assertEqual(len(fake.created), 1)        # 首次 create
        self.assertEqual(len(fake.updated), 0)
        dist = self.db.get_distribution(self.job_pk, "wechat", account="default", lang="zh")
        self.assertEqual(dist.wechat_media_id, "media-NEW")
        self.assertEqual(dist.publish_status, "published")
        self.assertEqual(dist.assembled_dir, str(Path(self._tmp.name) / "out"))

        # 重投放：同 distribution 已有 media_id → PATCH，不再 create
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, fake, _args()))
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(len(fake.updated), 1)
        self.assertEqual(fake.updated[0][0], "media-NEW")

    def test_missing_article_fails(self):
        task = self.db.get_or_create_task("t")
        empty_pk = self.db.upsert_job(task.id, "j2", pdf_path="p", template_id="t", product_id="pr").id
        job2 = Job(job_id="j2", pdf="p", template="t", product="pr")
        self.assertFalse(bp._distribute_one(self.db, empty_pk, job2, FakeWeChat(), _args()))

    def test_blocked_article_skips_distribute(self):
        # 质量闸拦下的稿：跳过投放（不算失败、不建 distribution、不调微信）
        self.db.upsert_article(self.job_pk, publish_blocked=True, block_reason="markdown_unhealthy:0")
        fake = FakeWeChat()
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, fake, _args()))
        self.assertEqual(len(fake.created), 0)
        self.assertIsNone(self.db.get_distribution(self.job_pk, "wechat", account="default", lang="zh"))


if __name__ == "__main__":
    unittest.main()
