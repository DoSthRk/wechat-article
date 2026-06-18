"""_distribute_one：基准正文 → 公众号 distribution；首次 create，重投 PATCH。

用 fake WeChatClient + 临时 sqlite，不触网。
"""
import argparse
import tempfile
import unittest
from pathlib import Path

from db.database import DatabaseManager, JobStatus
from utils.job_loader import Job
from utils.wechat_client import WeChatAPIError
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


class FakeWeChatStaleDraft:
    """update_draft 报 40007（草稿已删/失效），create_draft 成功 —— 验证重建回退。"""

    def __init__(self):
        self.created = []

    def update_draft(self, media_id, index, article):
        raise WeChatAPIError("invalid media_id", errcode=40007)

    def create_draft(self, articles):
        self.created.append(articles)
        return "media-REBUILT"


def _args():
    return argparse.Namespace(placeholder_author="TarMart", placeholder_thumb_media="thumb-1")


def _get(client):
    """把单个 client 包成 get_client(account) 形式（_distribute_one 现在收 getter）。"""
    return lambda _account: client


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
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))
        self.assertEqual(len(fake.created), 1)        # 首次 create
        self.assertEqual(len(fake.updated), 0)
        dist = self.db.get_distribution(self.job_pk, "wechat", account="default", lang="zh")
        self.assertEqual(dist.wechat_media_id, "media-NEW")
        self.assertEqual(dist.publish_status, "published")
        self.assertEqual(dist.assembled_dir, str(Path(self._tmp.name) / "out"))

        # 重投放：同 distribution 已有 media_id → PATCH，不再 create
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(len(fake.updated), 1)
        self.assertEqual(fake.updated[0][0], "media-NEW")

    def test_stale_media_id_recreates(self):
        # 已有 media_id 的 distribution，但微信侧草稿已被删 → update 报 40007 → 回退新建
        self.db.upsert_distribution(
            self.job_pk, "wechat", account="default", lang="zh",
            wechat_media_id="media-OLD", publish_status="published",
        )
        fake = FakeWeChatStaleDraft()
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))
        self.assertEqual(len(fake.created), 1)  # 回退到 create_draft
        dist = self.db.get_distribution(self.job_pk, "wechat", account="default", lang="zh")
        self.assertEqual(dist.wechat_media_id, "media-REBUILT")
        self.assertEqual(dist.publish_status, "published")

    def test_product_module_appended_to_draft(self):
        # 产品模块（line×platform）应拼到草稿正文尾
        orig = bp._load_product_module
        bp._load_product_module = lambda line, platform: "<section>PMOD</section>"
        try:
            fake = FakeWeChat()
            self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))
            content = fake.created[0][0]["content"]
            self.assertIn("<section>PMOD</section>", content)
            self.assertTrue(content.rstrip().endswith("</section>"))  # 在正文之后
        finally:
            bp._load_product_module = orig

    def test_missing_article_fails(self):
        task = self.db.get_or_create_task("t")
        empty_pk = self.db.upsert_job(task.id, "j2", pdf_path="p", template_id="t", product_id="pr").id
        job2 = Job(job_id="j2", pdf="p", template="t", product="pr")
        self.assertFalse(bp._distribute_one(self.db, empty_pk, job2, _get(FakeWeChat()), _args()))

    def test_blocked_article_skips_distribute(self):
        # 质量闸拦下的稿：跳过投放（不算失败、不建 distribution、不调微信）
        self.db.upsert_article(self.job_pk, publish_blocked=True, block_reason="markdown_unhealthy:0")
        fake = FakeWeChat()
        self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))
        self.assertEqual(len(fake.created), 0)
        self.assertIsNone(self.db.get_distribution(self.job_pk, "wechat", account="default", lang="zh"))


class TestLoadProductModule(unittest.TestCase):
    """_load_product_module：读 inputs/product_modules/{line}-{platform}.html，无则空串。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        (base / "inputs" / "product_modules").mkdir(parents=True)
        (base / "inputs" / "product_modules" / "solidex-wechat.html").write_text(
            "<section>SOLIDEX MODULE</section>", encoding="utf-8")
        self._saved = bp.PROJECT_ROOT
        bp.PROJECT_ROOT = base

    def tearDown(self):
        bp.PROJECT_ROOT = self._saved
        self._tmp.cleanup()

    def test_loads_existing_module(self):
        self.assertEqual(bp._load_product_module("solidex", "wechat"), "<section>SOLIDEX MODULE</section>")

    def test_missing_or_no_line_returns_empty(self):
        self.assertEqual(bp._load_product_module("aav", "wechat"), "")   # 文件不存在
        self.assertEqual(bp._load_product_module(None, "wechat"), "")    # 无 line


class TestResolveAuthor(unittest.TestCase):
    """作者署名 = 公众号名：WECHAT_{ACCOUNT}_AUTHOR > --placeholder-author > DEFAULT_AUTHOR。"""

    def setUp(self):
        import os
        self._saved = {k: os.environ.get(k) for k in ("WECHAT_IMMUNE_AUTHOR", "DEFAULT_AUTHOR")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_account_author_wins(self):
        import os
        os.environ["WECHAT_IMMUNE_AUTHOR"] = "免疫客"
        args = argparse.Namespace(placeholder_author="TarMart")
        self.assertEqual(bp._resolve_author("immune", args), "免疫客")

    def test_falls_back_to_placeholder_then_default(self):
        import os
        args = argparse.Namespace(placeholder_author="TarMart")
        self.assertEqual(bp._resolve_author("immune", args), "TarMart")
        args2 = argparse.Namespace(placeholder_author="")
        os.environ["DEFAULT_AUTHOR"] = "默认号"
        self.assertEqual(bp._resolve_author("immune", args2), "默认号")


if __name__ == "__main__":
    unittest.main()
