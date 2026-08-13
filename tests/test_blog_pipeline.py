import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db.database import DatabaseManager, JobStatus
from utils.blog_pipeline import BlogPipelineError, BlogWorkflow, _product_series, run_batch
from utils.translator import TranslationResult


class _FakeBlogClient:
    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, payload, *, publish=False):
        self.created.append((payload, publish))
        return {"uuid": "00000000-0000-0000-0000-000000000123", "node_id": "123", "public_url": "https://blog.example/article"}

    def update(self, uuid, payload, *, publish=None):
        self.updated.append((uuid, payload, publish))
        return {"uuid": uuid, "node_id": "123", "public_url": "https://blog.example/article"}


class BlogPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = DatabaseManager(database_url=f"sqlite:///{(base / 'test.db').as_posix()}")
        task = self.db.get_or_create_task("blog-test")
        self.job_pk = self.db.upsert_job(
            task.id, "paper-1", pdf_path=str(base / "paper.pdf"), template_id="aav", product_id="",
            status=JobStatus.GENERATED,
        ).id
        self.content_dir = base / "article"
        self.content_dir.mkdir()
        (self.content_dir / "article.md").write_text("# 中文标题\n\n中文正文。", encoding="utf-8")
        self.db.upsert_article(self.job_pk, title="中文标题", content_dir=str(self.content_dir), publish_blocked=False)
        self.db.ensure_blog_versions(self.job_pk, str(self.content_dir))
        self.client = _FakeBlogClient()
        self.old_lang = os.environ.get("GENEMEDI_BLOG_CHINESE_LANGCODE")
        os.environ["GENEMEDI_BLOG_CHINESE_LANGCODE"] = "zh-hans"

    def tearDown(self):
        if self.old_lang is None:
            os.environ.pop("GENEMEDI_BLOG_CHINESE_LANGCODE", None)
        else:
            os.environ["GENEMEDI_BLOG_CHINESE_LANGCODE"] = self.old_lang
        self.db.engine.dispose()
        self.tmp.cleanup()

    def _workflow(self, translated="# English title\n\nEnglish body."):
        def translator(_source, lang):
            return TranslationResult(True, lang, translated, model="fake", total_tokens=42)
        return BlogWorkflow(self.db, translator=translator, blog_client_factory=lambda: self.client)

    def test_product_series_defaults_to_business_line(self):
        with patch.dict(os.environ, {
            "GENEMEDI_BLOG_PRODUCT_SERIES_AAV": "",
            "GENEMEDI_BLOG_PRODUCT_SERIES_SOLIDEX": "",
        }):
            self.assertEqual(_product_series("purprox_aaveasy_spin_columns"), "AAV")
            self.assertEqual(_product_series("solidex_pan_t_cell_iso_kit"), "Solidex")

    def test_generation_initializes_multilingual_blog_rows(self):
        zh = self.db.get_article_version(self.job_pk, "zh")
        self.assertEqual(zh.translation_status, "ready")
        self.assertEqual(self.db.get_distribution(self.job_pk, "blog", "genemedi", "zh").publish_status, "pending")
        for lang in ("en", "ja", "ko", "ru"):
            self.assertEqual(self.db.get_article_version(self.job_pk, lang).translation_status, "pending")
            self.assertEqual(
                self.db.get_distribution(self.job_pk, "blog", "genemedi", lang).publish_status,
                "waiting_translation",
            )

    def test_translate_then_publish_english(self):
        workflow = self._workflow()
        result = workflow.translate("paper-1")
        self.assertEqual(result["status"], "translated")
        self.assertTrue((self.content_dir / "article.en.md").is_file())
        self.assertEqual(self.db.get_distribution(self.job_pk, "blog", "genemedi", "en").publish_status, "pending")

        published = workflow.publish("paper-1", "en")
        self.assertEqual(published["status"], "published")
        self.assertEqual(len(self.client.created), 1)
        payload, public = self.client.created[0]
        self.assertTrue(public)
        self.assertEqual(payload["langcode"], "en")
        dist = self.db.get_distribution(self.job_pk, "blog", "genemedi", "en")
        self.assertEqual(dist.publish_status, "published")
        self.assertEqual(dist.external_id, "00000000-0000-0000-0000-000000000123")

    def test_translate_then_publish_other_supported_languages(self):
        workflow = self._workflow("# Translated title\n\nTranslated body.")
        for lang in ("ja", "ko", "ru"):
            self.assertEqual(workflow.translate("paper-1", lang)["status"], "translated")
            self.assertEqual(workflow.publish("paper-1", lang)["status"], "published")
            payload, public = self.client.created[-1]
            self.assertTrue(public)
            self.assertEqual(payload["langcode"], lang)

    def test_published_article_is_not_overwritten(self):
        workflow = self._workflow()
        workflow.publish("paper-1", "zh")
        again = workflow.publish("paper-1", "zh")
        self.assertEqual(again["status"], "already_published")
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.updated), 0)

    def test_legacy_hub_publication_is_recreated_in_blog_cms(self):
        self.db.upsert_distribution(
            self.job_pk,
            "blog",
            account="genemedi",
            lang="zh",
            publish_status="published",
            external_id="old-hub-uuid",
            external_url="https://hub.genemedi.net/zh-hans/node/7",
        )

        result = self._workflow().publish("paper-1", "zh")

        self.assertEqual(result["status"], "published")
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.updated), 0)
        distribution = self.db.get_distribution(
            self.job_pk, "blog", "genemedi", "zh"
        )
        self.assertEqual(
            distribution.external_id,
            "00000000-0000-0000-0000-000000000123",
        )

    def test_unprefixed_chinese_blog_url_is_refreshed(self):
        self.db.upsert_distribution(
            self.job_pk,
            "blog",
            account="genemedi",
            lang="zh",
            publish_status="published",
            external_id="blog-uuid",
            external_url="https://blog.genemedi.com/node/93",
        )

        result = self._workflow().publish("paper-1", "zh")

        self.assertEqual(result["status"], "published")
        self.assertEqual(len(self.client.created), 0)
        self.assertEqual(len(self.client.updated), 1)

    def test_failed_translation_is_recorded_and_batch_continues(self):
        def failing(_source, lang):
            return TranslationResult(False, lang, error="fake provider unavailable")
        workflow = BlogWorkflow(self.db, translator=failing, blog_client_factory=lambda: self.client)
        batch = run_batch(workflow, [{"job_id": "paper-1", "lang": "en"}, {"job_id": "missing", "lang": "en"}], "translate")
        self.assertFalse(batch["ok"])
        self.assertEqual(len(batch["results"]), 2)
        version = self.db.get_article_version(self.job_pk, "en")
        self.assertEqual(version.translation_status, "failed")
        self.assertIn("unavailable", version.translation_error)

    def test_translation_exception_is_recorded(self):
        def broken(_source, _lang):
            raise RuntimeError("missing translation key")
        workflow = BlogWorkflow(self.db, translator=broken, blog_client_factory=lambda: self.client)
        with self.assertRaisesRegex(BlogPipelineError, "missing translation key"):
            workflow.translate("paper-1")
        self.assertEqual(self.db.get_article_version(self.job_pk, "en").translation_status, "failed")

    def test_english_publish_before_translation_is_rejected(self):
        with self.assertRaises(BlogPipelineError):
            self._workflow().publish("paper-1", "en")

    def test_blog_html_uses_original_figure_as_body_and_cover(self):
        image = self.content_dir / "figure.png"
        image.write_bytes(b"not-a-real-image")

        class _Store:
            def upload(self, path):
                self.path = path
                return "https://img.example/article-assets/figure.png"

        store = _Store()
        workflow = BlogWorkflow(self.db, blog_client_factory=lambda: self.client, asset_store_factory=lambda: store)
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch("batch_processor._resolve_job_figures", return_value=([], self.content_dir)), patch(
            "batch_processor._resolve_figure_path", return_value=str(image)
        ):
            html, cover = workflow._render_with_images("# 标题\n\n[图片:Figure 1 示例图]", source_job)
        self.assertIn('src="https://img.example/article-assets/figure.png"', html)
        self.assertEqual(cover, "https://img.example/article-assets/figure.png")


if __name__ == "__main__":
    unittest.main()
