"""Production consumers exercise the real approval gate; only extraction/I/O are faked."""
import argparse
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import batch_processor as bp
from db.database import DatabaseManager, JobStatus
from utils import image_packages as ip
from utils.blog_image_repair import repair_images as repair_blog
from utils.blog_pipeline import BlogPipelineError, BlogWorkflow
from utils.job_loader import Job
from utils.pdf_figure_extractor import Figure
from utils.wechat_draft_repair import DraftImageRepairError, repair_existing_images


class ImagePackageConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.articles = self.root / 'articles'
        self.directory = self.articles / 'paper'
        self.directory.mkdir(parents=True)
        self.pdf = self.root / 'source.pdf'
        self.pdf.write_bytes(b'%PDF immutable source identity')
        (self.directory / 'article.md').write_text('# Reviewed title\n\n[图片:Figure 1 Result]', encoding='utf-8')
        (self.directory / 'meta.json').write_text('{}')
        figure = self.root / 'figure.png'
        image = Image.new('RGB', (400, 400), 'white')
        ImageDraw.Draw(image).ellipse((20, 20, 380, 380), fill='blue')
        image.save(figure)
        self.figure = Figure('1', False, 'Figure 1 Result', 1, str(figure), 400, 400, (50, 50, 450, 450))
        self.patches = [patch.object(ip, 'ARTICLE_CONTENT_DIR', self.articles),
                        patch.object(bp, 'ARTICLE_CONTENT_DIR', self.articles),
                        patch('utils.blog_task_lock.LOCK_DIR', self.root / 'locks'),
                        patch.object(ip, '_render_pages', side_effect=self.render_pages),
                        patch.object(bp, '_resolve_job_figures', return_value=([self.figure], self.root)),
                        patch.dict('os.environ', {'GENEMEDI_BLOG_CHINESE_LANGCODE': 'zh-hans'})]
        for item in self.patches:
            item.start()
        self.db = DatabaseManager(database_url=f'sqlite:///{self.root / "test.db"}')
        task = self.db.get_or_create_task('test')
        self.pk = self.db.upsert_job(task.id, 'paper', pdf_path=str(self.pdf), template_id='aav',
                                    product_id='purprox_aaveasy_spin_columns', line_id='aav',
                                    status=JobStatus.GENERATED).id
        self.db.upsert_article(self.pk, content_dir=str(self.directory), title='Reviewed title', publish_blocked=False)
        self.db.ensure_blog_versions(self.pk, str(self.directory))
        self.job = Job('paper', str(self.pdf), 'aav', 'purprox_aaveasy_spin_columns', line='aav')
        self.args = argparse.Namespace(placeholder_author='Author', placeholder_thumb_media='unused')
        self.blog_client = Mock()
        self.store = Mock()
        self.source_publisher = Mock(return_value='https://example.test/source.pdf')
        self.blog_factory = Mock(return_value=self.blog_client)
        self.workflow = BlogWorkflow(self.db, blog_client_factory=self.blog_factory,
            asset_store_factory=lambda: self.store, source_pdf_publisher=self.source_publisher)
        self.wechat = Mock()
        self.wechat_factory = Mock(return_value=self.wechat)

    def tearDown(self):
        self.db.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def render_pages(self, source, directory):
        (directory / 'pages').mkdir()
        path = directory / 'pages/1.png'
        Image.new('RGB', (600, 800), 'white').save(path)
        return [{**ip._asset_record(path, directory, image=True), 'page': 1, 'pdf_width': 600, 'pdf_height': 800}]

    def approve(self):
        candidate = ip.build_package(self.job, job_pk=self.pk)
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'human-reviewer',
            dict.fromkeys(ip.CONFIRMATIONS, True),
            {'title': 'Source article', 'doi': '', 'pages': [1], 'evidence': 'Compared the complete source figure and caption'})
        return ip.require_approved_package(self.job, approved['bundle_id'])

    def assert_all_writes_blocked(self, expected_package_id=None):
        self.db.upsert_distribution(self.pk, 'blog', account='genemedi', lang='zh',
            publish_status='published', external_id='old-cms-id', external_url='https://genemedi.cn/blog/paper-zh')
        self.db.upsert_distribution(self.pk, 'wechat', account='aav', lang='zh',
            publish_status='published', wechat_media_id='old-draft-id')
        # force bypasses idempotence only; it must never bypass approval.
        with self.assertRaises(BlogPipelineError):
            self.workflow.publish('paper', 'zh', force=True, expected_package_id=expected_package_id)
        self.assertFalse(bp._distribute_one(self.db, self.pk, self.job, self.wechat_factory, self.args,
                                           expected_package_id=expected_package_id))
        with self.assertRaises(BlogPipelineError):
            repair_blog(self.workflow, 'paper', 'zh', repair_cover=True, expected_package_id=expected_package_id)
        with self.assertRaises(DraftImageRepairError):
            repair_existing_images(self.db, 'paper', repair_cover=True, client_factory=self.wechat_factory,
                                   expected_package_id=expected_package_id)
        self.blog_factory.assert_not_called()
        self.wechat_factory.assert_not_called()
        self.source_publisher.assert_not_called()
        self.store.upload.assert_not_called()

    def test_candidate_is_not_approval_for_any_consumer_even_with_force(self):
        candidate = ip.build_package(self.job, job_pk=self.pk)
        self.assertEqual(candidate['status'], 'needs_review')
        self.assert_all_writes_blocked(candidate['bundle_id'])

    def test_pdf_change_blocks_all_consumers_before_upload(self):
        package = self.approve()
        self.pdf.write_bytes(b'%PDF replaced paper')
        self.assert_all_writes_blocked(package.bundle_id)

    def test_markdown_change_blocks_all_consumers_before_upload(self):
        package = self.approve()
        (self.directory / 'article.md').write_text('# Changed article\n\n[图片:Figure 1 Result]')
        self.assert_all_writes_blocked(package.bundle_id)

    def test_asset_tampering_blocks_all_consumers_before_upload(self):
        package = self.approve()
        Path(package.figures[0].image_path).write_bytes(b'replaced image')
        self.assert_all_writes_blocked(package.bundle_id)

    def test_revoked_approval_blocks_all_consumers_before_upload(self):
        package = self.approve()
        ip.revoke_package(self.job, package.bundle_id, 'human-reviewer', 'incorrect panel')
        self.assert_all_writes_blocked(package.bundle_id)

    def test_a_different_approved_package_cannot_satisfy_a_pinned_run(self):
        self.approve()
        self.assert_all_writes_blocked('f' * 64)

    def test_same_job_id_in_new_task_cannot_reuse_old_revision_approval(self):
        package = self.approve()
        task = self.db.get_or_create_task('second-revision')
        self.pk = self.db.upsert_job(task.id, 'paper', pdf_path=str(self.pdf), template_id='aav',
            product_id='purprox_aaveasy_spin_columns', line_id='aav', status=JobStatus.GENERATED).id
        self.db.upsert_article(self.pk, content_dir=str(self.directory), title='Reviewed title', publish_blocked=False)
        self.db.ensure_blog_versions(self.pk, str(self.directory))
        self.assert_all_writes_blocked(package.bundle_id)

    def test_wechat_reads_approved_snapshot_and_enqueues_only_after_readback(self):
        package = self.approve()
        current = {}
        def create(payload):
            current.update(copy.deepcopy(payload[0]))
            return 'created-draft'
        self.wechat.create_draft.side_effect = create
        self.wechat.get_draft.side_effect = lambda _: {'news_item': [copy.deepcopy(current)]}
        with patch.object(bp, '_ensure_source_pdf_published', self.source_publisher), \
             patch.object(bp, 'resolve_published_blog_url', return_value='https://genemedi.cn/blog/paper-zh'), \
             patch.object(bp, '_upload_cached', return_value='https://mmbiz.qpic.cn/approved/640') as upload, \
             patch.object(bp, '_upload_cover_cached', return_value='approved-cover'), \
             patch.object(bp, '_load_product_module', return_value=''), \
             patch.object(bp, 'append_source_pdf_guide', side_effect=lambda html, *a: (html, '')), \
             patch.object(bp, 'enqueue_auto_blog', return_value=4) as enqueue, \
             patch.object(bp, '_resolve_job_figures', side_effect=AssertionError('must not extract while publishing')):
            self.assertTrue(bp._distribute_one(self.db, self.pk, self.job, self.wechat_factory, self.args,
                                              expected_package_id=package.bundle_id))
        self.assertEqual(Path(upload.call_args.args[-1]), Path(package.figures[0].image_path))
        uploaded_source_job = self.source_publisher.call_args.args[-1]
        self.assertEqual(uploaded_source_job.job_id, self.job.job_id)
        self.assertEqual(uploaded_source_job.line, self.job.line)
        self.assertEqual(Path(uploaded_source_job.pdf), package.directory / 'source.pdf')
        self.assertIn('approved', current['content'])
        enqueue.assert_called_once_with(self.db, self.pk, 'paper', owner_line='aav', image_package_id=package.bundle_id)
        distribution = self.db.get_distribution(self.pk, 'wechat', account='aav', lang='zh')
        self.assertEqual(distribution.image_verification_status, 'verified')
        self.assertEqual(distribution.image_package_id, package.bundle_id)


if __name__ == '__main__':
    unittest.main()
