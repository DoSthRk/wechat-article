import os
import hashlib
import io
from email.message import Message
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from PIL import Image

from db.database import DatabaseManager, JobStatus
from utils.blog_pipeline import (
    BlogPipelineError,
    BlogWorkflow,
    SshImageStore,
    _product_series,
    _build_source_job,
    run_batch,
    verify_public_image,
    verify_public_page_images,
)
from utils.image_packages import ImagePackageError
from utils.translator import TranslationResult


class _FakeBlogClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self.payload = None

    def create(self, payload, *, publish=False):
        self.created.append((payload, publish))
        self.payload = payload
        return {"uuid": "00000000-0000-0000-0000-000000000123", "node_id": "123", "public_url": "https://blog.example/article"}

    def update(self, uuid, payload, *, publish=None):
        self.updated.append((uuid, payload, publish))
        self.payload = payload
        return {"uuid": uuid, "node_id": "123", "public_url": "https://blog.example/article"}

    def get(self, uuid, *, langcode):
        payload = self.payload
        return {'data': {'attributes': {'title': payload['title'], 'langcode': langcode, 'status': True,
            'body': {'value': payload['body'], 'format': payload['body_format']},
            'field_slug': payload['slug'], 'field_source_pdf_url': payload['source_pdf_url'],
            'field_cover_url': payload.get('cover_url', '')}}}


class BlogPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = DatabaseManager(database_url=f"sqlite:///{(base / 'test.db').as_posix()}")
        task = self.db.get_or_create_task("blog-test")
        self.job_pk = self.db.upsert_job(
            task.id, "paper-1", pdf_path=str(base / "paper.pdf"), template_id="aav", product_id="", line_id='aav',
            status=JobStatus.GENERATED,
        ).id
        self.content_dir = base / "article"
        self.content_dir.mkdir()
        (self.content_dir / "article.md").write_text("# 中文标题\n\n中文正文。", encoding="utf-8")
        self.db.upsert_article(self.job_pk, title="中文标题", content_dir=str(self.content_dir), publish_blocked=False)
        self.db.ensure_blog_versions(self.job_pk, str(self.content_dir))
        self.client = _FakeBlogClient()
        cover = self.content_dir / 'approved-cover.png'
        Image.new('RGB', (900, 383), 'blue').save(cover)
        self.package = SimpleNamespace(bundle_id='a' * 64, figures=[], directory=self.content_dir,
            cover_path=str(cover), source_markdown_sha256='', manifest={})
        def approved(*args, **kwargs):
            self.package.source_markdown_sha256 = hashlib.sha256((self.content_dir / 'article.md').read_bytes()).hexdigest()
            return self.package
        self.approval = patch('utils.blog_pipeline.require_approved_package', side_effect=approved)
        self.approval_mock = self.approval.start()
        self.public_image = patch('utils.blog_pipeline.verify_public_image', side_effect=lambda url, **kwargs: url)
        self.public_image.start()
        self.public_page = patch('utils.blog_pipeline.verify_public_page_images', side_effect=lambda url, *args, **kwargs: url)
        self.public_page.start()
        self.locks = patch('utils.blog_task_lock.LOCK_DIR', base / 'locks')
        self.locks.start()
        self.old_lang = os.environ.get("GENEMEDI_BLOG_CHINESE_LANGCODE")
        os.environ["GENEMEDI_BLOG_CHINESE_LANGCODE"] = "zh-hans"

    def tearDown(self):
        if self.old_lang is None:
            os.environ.pop("GENEMEDI_BLOG_CHINESE_LANGCODE", None)
        else:
            os.environ["GENEMEDI_BLOG_CHINESE_LANGCODE"] = self.old_lang
        self.db.engine.dispose()
        self.locks.stop()
        self.public_image.stop()
        self.public_page.stop()
        self.approval.stop()
        self.tmp.cleanup()

    def _workflow(self, translated="# English title\n\nEnglish body."):
        def translator(_source, lang):
            return TranslationResult(True, lang, translated, model="fake", total_tokens=42)
        return BlogWorkflow(
            self.db,
            translator=translator,
            blog_client_factory=lambda: self.client,
            blog_url_verifier=lambda url: url,
            source_pdf_publisher=lambda _db, _job_pk, _job: "https://www.genemedi.net/uploads/papers/ab/paper.pdf",
            asset_store_factory=lambda: SimpleNamespace(upload=lambda path: 'https://img.example/' + Path(path).name),
        )

    def test_product_series_defaults_to_business_line(self):
        with patch.dict(os.environ, {
            "GENEMEDI_BLOG_PRODUCT_SERIES_AAV": "",
            "GENEMEDI_BLOG_PRODUCT_SERIES_SOLIDEX": "",
        }):
            self.assertEqual(_product_series("purprox_aaveasy_spin_columns"), "AAV")
            self.assertEqual(_product_series("solidex_pan_t_cell_iso_kit"), "Solidex")

    def test_saved_or_exact_legacy_business_line_is_restored_without_default_account(self):
        job = SimpleNamespace(job_id='paper', pdf_path='source.pdf', template_id='t', product_id='unknown',
                              image_pool=None, title_hint='', line_id='solidex')
        self.assertEqual(_build_source_job(job).line, 'solidex')
        job.line_id = None
        job.product_id = 'purprox_aaveasy_spin_columns'
        self.assertEqual(_build_source_job(job).line, 'aav')
        for line, product in (('deleted-line', 'purprox_aaveasy_spin_columns'), (None, 'similar_aav_product')):
            with self.subTest(line=line, product=product):
                job.line_id, job.product_id = line, product
                with self.assertRaisesRegex(BlogPipelineError, '禁止回退'):
                    _build_source_job(job)

    def test_ssh_image_store_uploads_and_verifies_public_url(self):
        image = Path(self.tmp.name) / "figure.jpg"
        image.write_bytes(b"figure-bytes")

        class _SourceStore:
            private_key = "/tmp/key"
            known_hosts = "/tmp/known_hosts"
            host = "example.test"
            user = "uploader"
            port = 22
            timeout = 30
            min_free_bytes = 1

            def _run_ssh(self, command):
                if command.startswith("df "):
                    return "1000"
                if command.startswith("if test -f"):
                    return "no"
                return ""

        store = SshImageStore(
            _SourceStore(),
            "/srv/uploads/blog-images",
            "https://example.test/uploads/blog-images",
        )
        with patch("utils.blog_pipeline.subprocess.run") as run, patch(
            "utils.blog_pipeline.verify_public_image",
            side_effect=lambda url, **kwargs: url,
        ) as verify:
            run.return_value.returncode = 0
            url = store.upload(str(image))

        self.assertTrue(url.startswith("https://example.test/uploads/blog-images/"))
        self.assertTrue(url.endswith(".jpg"))
        self.assertEqual(run.call_args.args[0][0], "scp")
        verify.assert_called_once_with(url, timeout=30.0, expected_path=image)

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
        self.assertEqual(
            payload["source_pdf_url"],
            "https://www.genemedi.net/uploads/papers/ab/paper.pdf",
        )
        dist = self.db.get_distribution(self.job_pk, "blog", "genemedi", "en")
        self.assertEqual(dist.publish_status, "published")
        self.assertEqual(dist.external_id, "00000000-0000-0000-0000-000000000123")
        self.assertEqual(dist.external_url, "https://genemedi.net/blog/paper-1-en")

    def test_source_pdf_upload_reads_package_snapshot_not_live_pdf(self):
        workflow = self._workflow()
        publisher = Mock(return_value='https://example.test/frozen-source.pdf')
        workflow.source_pdf_publisher = publisher
        workflow.publish('paper-1', 'zh')
        source_job = publisher.call_args.args[-1]
        self.assertEqual(Path(source_job.pdf), self.package.directory / 'source.pdf')
        self.assertEqual(source_job.job_id, 'paper-1')
        self.assertEqual(source_job.line, 'aav')

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

    def test_force_publish_updates_existing_article(self):
        workflow = self._workflow()
        workflow.publish("paper-1", "zh")

        result = workflow.publish("paper-1", "zh", force=True)

        self.assertEqual(result["status"], "published")
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.updated), 1)

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

    def test_retired_blog_cms_publication_is_recreated_in_hub(self):
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
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.updated), 0)

    def test_failed_translation_is_recorded_and_batch_continues(self):
        def failing(_source, lang):
            return TranslationResult(False, lang, error="fake provider unavailable")
        workflow = BlogWorkflow(
            self.db, translator=failing, blog_client_factory=lambda: self.client,
            blog_url_verifier=lambda url: url,
            source_pdf_publisher=lambda _db, _job_pk, _job: "https://example.test/paper.pdf",
        )
        batch = run_batch(workflow, [{"job_id": "paper-1", "lang": "en"}, {"job_id": "missing", "lang": "en"}], "translate")
        self.assertFalse(batch["ok"])
        self.assertEqual(len(batch["results"]), 2)
        version = self.db.get_article_version(self.job_pk, "en")
        self.assertEqual(version.translation_status, "failed")
        self.assertIn("unavailable", version.translation_error)

    def test_translation_exception_is_recorded(self):
        def broken(_source, _lang):
            raise RuntimeError("missing translation key")
        workflow = BlogWorkflow(
            self.db, translator=broken, blog_client_factory=lambda: self.client,
            blog_url_verifier=lambda url: url,
            source_pdf_publisher=lambda _db, _job_pk, _job: "https://example.test/paper.pdf",
        )
        with self.assertRaisesRegex(BlogPipelineError, "missing translation key"):
            workflow.translate("paper-1")
        self.assertEqual(self.db.get_article_version(self.job_pk, "en").translation_status, "failed")

    def test_english_publish_before_translation_is_rejected(self):
        with self.assertRaises(BlogPipelineError):
            self._workflow().publish("paper-1", "en")

    def test_translated_publish_rejects_lost_figures_before_external_writes(self):
        (self.content_dir / "article.md").write_text("# 标题\n[图片:Figure 1 图注]", encoding="utf-8")
        workflow = self._workflow()
        workflow.translate("paper-1", "ja")
        with patch.object(workflow, "source_pdf_publisher") as publish_pdf:
            with self.assertRaisesRegex(BlogPipelineError, "image_placeholder_mismatch"):
                workflow.publish("paper-1", "ja")
        publish_pdf.assert_not_called()
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.client.updated, [])
        self.assertEqual(self.db.get_distribution(self.job_pk, "blog", "genemedi", "ja").publish_status, "failed")

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
            for prefix in ["图片", "画像", "이미지"]:
                with self.subTest(prefix=prefix):
                    html, cover = workflow._render_with_images(f"# 标题\n\n[{prefix}:Figure 1 示例图]", source_job)
                    self.assertIn('src="https://img.example/article-assets/figure.png"', html)
                    self.assertEqual(cover, "https://img.example/article-assets/figure.png")
                    self.assertNotIn(f"[{prefix}:", html)

    def test_blog_resolves_all_required_figures_before_rendering(self):
        image = self.content_dir / "figure.png"
        image.write_bytes(b"figure-bytes")

        class _Store:
            def upload(self, _path):
                return "https://img.example/figure.png"

        workflow = BlogWorkflow(self.db, asset_store_factory=lambda: _Store())
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch("batch_processor._resolve_job_figures", return_value=([], self.content_dir)) as resolve, patch(
            "batch_processor._resolve_figure_path", return_value=str(image)
        ):
            with self.assertRaisesRegex(BlogPipelineError, '同一张图片'):
                workflow._render_with_images(
                    "# 标题\n\n[图片:Figure 1 一]\n\n[图片:Figure 4 二]", source_job,
                )
        resolve.assert_not_called()

    def test_linked_placeholder_renders_as_image_instead_of_link(self):
        image = self.content_dir / "figure.png"
        image.write_bytes(b"figure-bytes")

        class _Store:
            def upload(self, _path):
                return "https://img.example/figure.png"

        workflow = BlogWorkflow(self.db, asset_store_factory=lambda: _Store())
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch("batch_processor._resolve_job_figures", return_value=([], self.content_dir)), patch(
            "batch_processor._resolve_figure_path", return_value=str(image)
        ):
            html, _cover = workflow._render_with_images(
                "# 标题\n\n[图片:Figure 1 示意图](Figure 1)", source_job,
            )
        self.assertIn('<img src="https://img.example/figure.png"', html)
        self.assertNotIn('href="Figure 1"', html)

    def test_missing_blog_figure_blocks_publish(self):
        class _Store:
            def upload(self, _path):
                raise AssertionError("missing figures must not be uploaded")

        workflow = BlogWorkflow(
            self.db,
            blog_client_factory=lambda: self.client,
            asset_store_factory=lambda: _Store(),
        )
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch("batch_processor._resolve_job_figures", return_value=([], self.content_dir)), patch(
            "batch_processor._resolve_figure_path", return_value=None
        ):
            with self.assertRaisesRegex(BlogPipelineError, "正文图片不可用"):
                workflow._render_with_images(
                    "# 标题\n\n正文。\n\n[图片:Figure 1 缺失图]",
                    source_job,
                )

    def test_unsafe_crop_blocks_publish_instead_of_omitting_figures(self):
        from utils.figure_crop_geometry import UnsafeFigureCrop
        workflow = self._workflow()
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch('utils.blog_pipeline.require_approved_package', side_effect=ImagePackageError('unsafe crop')):
            with self.assertRaisesRegex(BlogPipelineError, "已阻止发布"):
                workflow._render_with_images("[图片:Figure 1]", source_job)
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.client.updated, [])

    def test_blog_figure_upload_failure_blocks_publish(self):
        image = self.content_dir / "figure.png"
        image.write_bytes(b"not-a-real-image")

        class _Store:
            def upload(self, _path):
                raise RuntimeError("temporary image store failure")

        workflow = BlogWorkflow(
            self.db,
            blog_client_factory=lambda: self.client,
            asset_store_factory=lambda: _Store(),
        )
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch("batch_processor._resolve_job_figures", return_value=([], self.content_dir)), patch(
            "batch_processor._resolve_figure_path", return_value=str(image)
        ):
            with self.assertRaisesRegex(BlogPipelineError, "正文图片不可用"):
                workflow._render_with_images(
                    "# 标题\n\n[图片:Figure 1 上传失败图]",
                    source_job,
                )

    def test_blog_figure_extraction_failure_blocks_publish(self):
        workflow = BlogWorkflow(
            self.db,
            blog_client_factory=lambda: self.client,
            asset_store_factory=lambda: (_ for _ in ()).throw(
                AssertionError("image store should not affect extraction fallback")
            ),
        )
        source_job = type("Job", (), {"job_id": "paper-1"})()
        with patch('utils.blog_pipeline.require_approved_package', side_effect=ImagePackageError('missing approval')):
            with self.assertRaisesRegex(BlogPipelineError, "审批未通过"):
                workflow._render_with_images(
                    "# 标题\n\n[图片:Figure 1 解析失败图]",
                    source_job,
                )

    def test_cms_readback_failure_retains_created_id_without_verified_state(self):
        with patch.object(self.client, 'get', return_value={'data': {'attributes': {}}}):
            with self.assertRaisesRegex(BlogPipelineError, '回读不匹配'):
                self._workflow().publish('paper-1', 'zh')
        distribution = self.db.get_distribution(self.job_pk, 'blog', 'genemedi', 'zh')
        self.assertEqual(distribution.publish_status, 'failed')
        self.assertEqual(distribution.external_id, '00000000-0000-0000-0000-000000000123')
        self.assertNotEqual(distribution.image_verification_status, 'verified')

    def test_public_page_missing_images_does_not_mark_published(self):
        with patch('utils.blog_pipeline.verify_public_page_images', side_effect=BlogPipelineError('missing images')):
            with self.assertRaisesRegex(BlogPipelineError, 'missing images'):
                self._workflow().publish('paper-1', 'zh')
        distribution = self.db.get_distribution(self.job_pk, 'blog', 'genemedi', 'zh')
        self.assertEqual(distribution.publish_status, 'failed')
        self.assertEqual(distribution.image_verification_status, 'needs_review')
        self.assertTrue(distribution.external_id)

    def test_public_image_verification_failure_blocks_cms_write(self):
        with patch('utils.blog_pipeline.verify_public_image', side_effect=BlogPipelineError('incomplete image')):
            with self.assertRaisesRegex(BlogPipelineError, 'incomplete image'):
                self._workflow().publish('paper-1', 'zh')
        self.assertFalse(self.client.created)
        self.assertFalse(self.client.updated)


class PublicImageVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.expected = Path(self.tmp.name) / 'approved.png'
        Image.new('RGB', (200, 200), (90, 130, 170)).save(self.expected)

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, content, *, content_type='image/png', polished='', status=200):
        response = io.BytesIO(content)
        response.status = status
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        if polished:
            response.headers['CF-Polished'] = polished
        return response

    def jpeg(self, color=(90, 130, 170), size=(200, 200)):
        content = io.BytesIO()
        Image.new('RGB', size, color).save(content, format='JPEG', quality=90)
        return content.getvalue()

    def test_exact_approved_image_decodes_and_passes(self):
        with patch('utils.blog_pipeline.urllib_request.urlopen', return_value=self.response(self.expected.read_bytes())):
            self.assertEqual(verify_public_image('https://img.test/1', expected_path=self.expected), 'https://img.test/1')

    def test_partial_response_or_truncated_jpeg_cannot_pass(self):
        for content, status in ((self.jpeg()[:200], 200), (self.expected.read_bytes(), 206)):
            with self.subTest(status=status), patch('utils.blog_pipeline.urllib_request.urlopen',
                    return_value=self.response(content, status=status)):
                with self.assertRaises(BlogPipelineError):
                    verify_public_image('https://img.test/1', expected_path=self.expected)

    def test_same_dimensions_without_confirmed_polish_never_bypass_hash(self):
        for polished in ('', 'input_too_large', 'not_needed', 'ok'):
            with self.subTest(polished=polished), patch('utils.blog_pipeline.urllib_request.urlopen',
                    return_value=self.response(self.jpeg(), polished=polished)):
                with self.assertRaisesRegex(BlogPipelineError, '哈希不匹配'):
                    verify_public_image('https://img.test/1', expected_path=self.expected)

    def test_known_polish_reencoding_requires_small_pixel_difference_and_same_size(self):
        with patch('utils.blog_pipeline.urllib_request.urlopen',
                return_value=self.response(self.jpeg(), polished='ok, orig_size=700')):
            verify_public_image('https://img.test/1', expected_path=self.expected)
        for content in (self.jpeg(color='red'), self.jpeg(size=(201, 200))):
            with self.subTest(length=len(content)), patch('utils.blog_pipeline.urllib_request.urlopen',
                    return_value=self.response(content, polished='ok, orig_size=700')):
                with self.assertRaises(BlogPipelineError):
                    verify_public_image('https://img.test/1', expected_path=self.expected)

    def test_public_page_must_contain_actual_img_urls_including_cover(self):
        expected = {'https://img.test/figure', 'https://img.test/cover'}
        for html in ('https://img.test/figure https://img.test/cover',
                     '<source src="https://img.test/figure"><img src="https://img.test/cover">',
                     '<img src="https://img.test/figure">'):
            with self.subTest(html=html), patch('utils.blog_pipeline.urllib_request.urlopen',
                    side_effect=lambda *_a, **_k: self.response(html.encode(), content_type='text/html')):
                with self.assertRaises(BlogPipelineError):
                    verify_public_page_images('https://blog.test/article', expected)
        with patch('utils.blog_pipeline.urllib_request.urlopen', return_value=self.response(
                b'<img src="https://img.test/figure"><img data-src="https://img.test/cover">', content_type='text/html')):
            verify_public_page_images('https://blog.test/article', expected)


if __name__ == "__main__":
    unittest.main()
