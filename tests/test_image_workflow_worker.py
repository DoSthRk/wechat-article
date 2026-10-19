"""Human-approved continuation uses one bundle and preserves existing content."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.image_packages import ImagePackageError
from utils.blog_pipeline import BlogPipelineError
from utils.workflow_runner import _normalize
from workflow_worker import run_image_stage


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bundle_id = 'b' * 64
        self.item = {'job_id': 'paper', 'lang': 'zh', 'expected_package_id': self.bundle_id}
        self.db = Mock()
        self.db.find_job_pk.return_value = 9
        self.db.get_article.return_value = SimpleNamespace(content_dir='fixture', publish_blocked=False)
        self.db.get_distribution.return_value = None
        self.db.latest_wechat_draft.return_value = None
        self.job = SimpleNamespace(job_id='paper', line='aav')
        self.patches = [
            patch('utils.blog_task_lock.LOCK_DIR', Path(self.temp.name)),
            patch('utils.blog_pipeline._build_source_job', return_value=self.job),
            patch('utils.auto_blog.enqueue'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_continue_requires_full_fixed_bundle_id(self):
        for value in (None, '', 'latest', 'a' * 63, 'A' * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _normalize('review_publish', [{**self.item, 'expected_package_id': value}])
        self.assertEqual(_normalize('review_publish', [self.item]), [self.item])
        self.assertEqual(_normalize('images', [self.item]), [{'job_id': 'paper', 'lang': 'zh'}])
        self.assertEqual(_normalize('images', [{**self.item, 'lang': 'en'}]), [])

    def test_prepare_does_not_approve_or_publish(self):
        with patch('utils.image_packages.build_package', return_value={'bundle_id': self.bundle_id, 'status': 'needs_review'}) as build, \
             patch('utils.image_packages.approve_package') as approve, \
             patch('batch_processor._publish_chinese_blog') as publish:
            self.assertEqual(run_image_stage(self.db, 'images', self.item)['status'], 'needs_review')
        build.assert_called_once_with(self.job, job_pk=9)
        approve.assert_not_called()
        publish.assert_not_called()

    def test_continue_unapproved_cannot_rebuild_or_write(self):
        with patch('utils.image_packages.require_approved_package', side_effect=ImagePackageError('needs_review')), \
             patch('utils.image_packages.build_package') as build, \
             patch('batch_processor._publish_chinese_blog') as publish, \
             patch('batch_processor._distribute_one') as distribute:
            with self.assertRaises(ImagePackageError):
                run_image_stage(self.db, 'review_publish', self.item)
        build.assert_not_called()
        publish.assert_not_called()
        distribute.assert_not_called()

    def test_new_article_passes_exact_bundle_to_both_consumers(self):
        self.db.get_distribution.side_effect = [None, SimpleNamespace(wechat_media_id='draft-id',
            publish_status='published', image_package_id=self.bundle_id, image_verification_status='verified')]
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('batch_processor._publish_chinese_blog', return_value=True) as publish, \
             patch('batch_processor._distribute_one', return_value=True) as distribute, \
             patch('batch_processor._make_client_getter', return_value=Mock()):
            self.assertEqual(run_image_stage(self.db, 'review_publish', self.item)['status'], 'draft_verified')
        publish.assert_called_once_with(self.db, 9, self.job, expected_package_id=self.bundle_id)
        self.assertEqual(distribute.call_args.kwargs, {'expected_package_id': self.bundle_id})

    def test_queue_failure_is_reported_without_recreating_verified_draft(self):
        self.db.get_distribution.side_effect = [None, SimpleNamespace(wechat_media_id='draft-id',
            publish_status='published', image_package_id=self.bundle_id, image_verification_status='verified')]
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('batch_processor._publish_chinese_blog', return_value=True), \
             patch('batch_processor._distribute_one', return_value=True) as distribute, \
             patch('batch_processor._make_client_getter', return_value=Mock()), \
             patch('utils.auto_blog.enqueue', side_effect=BlogPipelineError('queue unavailable')):
            with self.assertRaisesRegex(BlogPipelineError, 'queue unavailable'):
                run_image_stage(self.db, 'review_publish', self.item)
        distribute.assert_called_once()

    def test_distribute_skipped_or_missing_receipt_is_not_success(self):
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('batch_processor._publish_chinese_blog', return_value=True), \
             patch('batch_processor._distribute_one', return_value=True), \
             patch('batch_processor._make_client_getter', return_value=Mock()):
            with self.assertRaisesRegex(BlogPipelineError, '回执'):
                run_image_stage(self.db, 'review_publish', self.item)

    def test_quality_block_cannot_be_overridden_by_image_approval(self):
        self.db.get_article.return_value.publish_blocked = True
        with patch('utils.image_packages.require_approved_package') as approve, \
             patch('batch_processor._publish_chinese_blog') as publish:
            with self.assertRaisesRegex(BlogPipelineError, '内容质量'):
                run_image_stage(self.db, 'review_publish', self.item)
        approve.assert_not_called()
        publish.assert_not_called()

    def test_existing_article_repairs_only_images_then_enqueues(self):
        self.db.get_distribution.return_value = SimpleNamespace(publish_status='published', external_id='cms-id',
            external_url='https://genemedi.cn/blog/paper-zh', image_package_id='old', image_verification_status=None)
        self.db.latest_wechat_draft.return_value = SimpleNamespace(wechat_media_id='draft-id')
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('utils.blog_image_repair.repair_images', return_value={'verified': True}) as blog_repair, \
             patch('utils.wechat_draft_repair.repair_existing_images', return_value={'verified': True}) as wx_repair, \
             patch('utils.auto_blog.enqueue') as enqueue, \
             patch('batch_processor._publish_chinese_blog') as publish, \
             patch('batch_processor._distribute_one') as distribute:
            self.assertEqual(run_image_stage(self.db, 'review_publish', self.item)['status'], 'draft_images_verified')
        self.assertEqual(blog_repair.call_args.kwargs['expected_package_id'], self.bundle_id)
        wx_repair.assert_called_once_with(self.db, 'paper', repair_cover=True, expected_package_id=self.bundle_id)
        enqueue.assert_called_once_with(self.db, 9, 'paper', 'aav', image_package_id=self.bundle_id)
        publish.assert_not_called()
        distribute.assert_not_called()

    def test_failed_draft_readback_does_not_start_multilingual_publication(self):
        self.db.latest_wechat_draft.return_value = SimpleNamespace(wechat_media_id='draft-id')
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('batch_processor._publish_chinese_blog', return_value=True), \
             patch('utils.wechat_draft_repair.repair_existing_images', return_value={'verified': False}), \
             patch('utils.auto_blog.enqueue') as enqueue:
            with self.assertRaises(BlogPipelineError):
                run_image_stage(self.db, 'review_publish', self.item)
        enqueue.assert_not_called()

    def test_unknown_business_line_cannot_use_default_wechat_account(self):
        self.job.line = None
        with patch('utils.image_packages.require_approved_package', return_value=SimpleNamespace(bundle_id=self.bundle_id)), \
             patch('batch_processor._publish_chinese_blog') as publish:
            with self.assertRaises(ImagePackageError):
                run_image_stage(self.db, 'review_publish', self.item)
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
