"""_distribute_one：基准正文 → 公众号 distribution；首次 create，重投 PATCH。

用 fake WeChatClient + 临时 sqlite，不触网。
"""
import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db.database import DatabaseManager, JobStatus
from utils.job_loader import Job
from utils.wechat_client import WeChatAPIError
from utils.source_pdf_store import SourcePdfError
from utils.blog_urls import BlogUrlError
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
        self._apply_figures = patch.object(
            bp,
            "_apply_figures",
            side_effect=lambda html, *_args, **_kwargs: (html + '<img src="https://img.test/1.png">', 1),
        )
        self._apply_figures.start()
        self._blog_url = patch.object(
            bp,
            "resolve_published_blog_url",
            return_value="https://genemedi.cn/blog/j1-zh",
        )
        self._blog_url.start()
        self._source_pdf = patch.object(
            bp,
            "_ensure_source_pdf_published",
            return_value="https://www.genemedi.net/uploads/papers/ab/source.pdf",
        )
        self.source_pdf_mock = self._source_pdf.start()
        self._source_pdf_guide = patch.object(
            bp,
            "append_source_pdf_guide",
            side_effect=lambda html, *_args, **_kwargs: (
                html + '<section data-source-pdf-guide="1">GUIDE</section>',
                "https://img.test/source-pdf-guide.png",
            ),
        )
        self._source_pdf_guide.start()

    def tearDown(self):
        self._source_pdf_guide.stop()
        self._source_pdf.stop()
        self._blog_url.stop()
        self._apply_figures.stop()
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
        self.assertEqual(
            fake.created[0][0]["content_source_url"],
            "https://genemedi.cn/blog/j1-zh",
        )

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
            self.assertLess(content.index("<section>PMOD</section>"), content.index("GUIDE"))
            self.assertTrue(content.rstrip().endswith("GUIDE</section>"))
        finally:
            bp._load_product_module = orig

    def test_guide_failure_does_not_block_draft_write(self):
        fake = FakeWeChat()
        with patch.object(
            bp, "append_source_pdf_guide", side_effect=bp.TemplateAssetError("上传失败"),
        ):
            self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))

        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.updated, [])
        self.assertNotIn("GUIDE", fake.created[0][0]["content"])

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

    def test_missing_required_figure_does_not_block_draft_write(self):
        content_dir = Path(self.db.get_article(self.job_pk).content_dir)
        (content_dir / "article.md").write_text(
            "# 标题\n\n正文。\n\n[图片:Figure 1 关键结果]", encoding="utf-8",
        )
        fake = FakeWeChat()
        with patch.object(bp, "_resolve_job_figures", return_value=([], content_dir / "figures")):
            self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))

        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.updated, [])
        job_row = self.db.get_job(self.job_pk)
        self.assertEqual(job_row.status, JobStatus.PUBLISHED)

    def test_zero_successful_body_images_does_not_block_draft_write(self):
        fake = FakeWeChat()
        with patch.object(bp, "_apply_figures", return_value=("<p>正文</p>", 0)):
            self.assertTrue(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))

        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.updated, [])
        job_row = self.db.get_job(self.job_pk)
        self.assertEqual(job_row.status, JobStatus.PUBLISHED)
        self.assertIn("GUIDE", fake.created[0][0]["content"])

    def test_source_pdf_failure_blocks_before_draft_write(self):
        fake = FakeWeChat()
        with patch.object(
            bp, "_ensure_source_pdf_published", side_effect=SourcePdfError("公网校验失败"),
        ):
            self.assertFalse(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))

        self.assertEqual(fake.created, [])
        self.assertEqual(fake.updated, [])
        job_row = self.db.get_job(self.job_pk)
        self.assertEqual(job_row.status, JobStatus.FAILED)
        self.assertIn("原文 PDF 上传失败", job_row.error_message or "")

    def test_missing_blog_blocks_before_pdf_or_draft_write(self):
        fake = FakeWeChat()
        with patch.object(
            bp, "resolve_published_blog_url", side_effect=BlogUrlError("zh Blog 尚未发布"),
        ):
            self.assertFalse(bp._distribute_one(self.db, self.job_pk, self.job, _get(fake), _args()))

        self.assertEqual(fake.created, [])
        self.assertEqual(fake.updated, [])
        self.source_pdf_mock.assert_not_called()
        self.assertIn("中文 Blog 未就绪", self.db.get_job(self.job_pk).error_message or "")

    def test_payload_shows_cover_in_article_body(self):
        payload = bp._build_article_payload(
            title="测试标题",
            digest="测试摘要",
            content_html="<p>正文</p>",
            author="免疫客",
            thumb_media_id="thumb-1",
        )
        self.assertEqual(payload["thumb_media_id"], "thumb-1")
        self.assertEqual(payload["show_cover_pic"], 1)
        self.assertEqual(payload["pic_crop_235_1"], "0_0_1_1")
        self.assertEqual(payload["pic_crop_1_1"], "0.287222_0_0.712778_1")
        self.assertEqual(payload["content_source_url"], "")

    def test_article_image_cover_precedes_configured_fallback(self):
        """正文有可用配图时，不应被账户固定占位封面覆盖。"""
        job = Job(
            job_id="j1", pdf="p", template="t", product="pr", line="solidex",
        )
        fake = FakeWeChat()
        with patch.dict(os.environ, {"WECHAT_IMMUNE_THUMB_MEDIA_ID": "fixed-thumb"}):
            with patch.object(bp, "_auto_cover_media_id", return_value="article-image-thumb") as auto_cover:
                self.assertTrue(bp._distribute_one(self.db, self.job_pk, job, _get(fake), _args()))

        self.assertEqual(fake.created[0][0]["thumb_media_id"], "article-image-thumb")
        auto_cover.assert_called_once()


class TestRunOneJobOrder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(
            database_url=f"sqlite:///{(Path(self._tmp.name) / 'order.db').as_posix()}"
        )
        self.task = self.db.get_or_create_task("order")
        self.job = Job(job_id="j1", pdf="paper.pdf", template="aav", product="purprox")
        self.db.upsert_job(
            self.task.id, self.job.job_id, pdf_path=self.job.pdf,
            template_id=self.job.template, product_id=self.job.product,
            status=JobStatus.GENERATED,
        )

    def tearDown(self):
        self.db.engine.dispose()
        self._tmp.cleanup()

    def test_chinese_blog_is_published_before_wechat(self):
        events = []
        with patch.object(
            bp, "_publish_chinese_blog",
            side_effect=lambda *_args: events.append("blog") or True,
        ), patch.object(
            bp, "_distribute_one",
            side_effect=lambda *_args: events.append("wechat") or True,
        ):
            ok = bp._run_one_job(
                self.db, self.task.id, self.job, None, lambda _account: FakeWeChat(),
                _args(), False, True,
            )
        self.assertTrue(ok)
        self.assertEqual(events, ["blog", "wechat"])

    def test_blog_failure_blocks_wechat(self):
        with patch.object(bp, "_publish_chinese_blog", return_value=False), patch.object(
            bp, "_distribute_one",
        ) as distribute:
            ok = bp._run_one_job(
                self.db, self.task.id, self.job, None, lambda _account: FakeWeChat(),
                _args(), False, True,
            )
        self.assertFalse(ok)
        distribute.assert_not_called()


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
