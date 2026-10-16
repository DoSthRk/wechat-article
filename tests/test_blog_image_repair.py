import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils import blog_image_repair as repair
from utils.blog_pipeline import BlogPipelineError
from utils.blog_urls import public_blog_url


class BlogImageRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desc = [f'Figure {i} 结果' for i in range(1, 5)]
        (self.root / 'article.md').write_text('\n\n'.join(f'[图片:{d}]' for d in self.desc))
        self.paths = []
        for i in range(4):
            path = self.root / f'{i}.jpg'
            path.write_bytes(f'figure {i}'.encode())
            self.paths.append(str(path))
        self.workflow = Mock()
        self.workflow._resolve_job_pk.return_value = 1
        self.workflow._drupal_language.return_value = 'zh-hans'
        self.workflow.db.get_article.return_value = SimpleNamespace(content_dir=str(self.root), publish_blocked=False)
        self.workflow.db.get_job.return_value = SimpleNamespace(job_id='paper', pdf_path='p.pdf', template_id='t',
                                                               product_id='p', image_pool=None, title_hint='T')
        self.workflow.db.get_distribution.return_value = SimpleNamespace(publish_status='published', external_id='uuid',
                                                                         external_url=public_blog_url('paper', 'zh'))
        self.original = {'title': '保留手工标题', 'langcode': 'zh-hans', 'status': True,
                         'field_slug': 'paper-zh', 'field_cover_url': 'https://example.com/old/0.jpg',
                         'field_source_pdf_url': 'https://example.com/source.pdf', 'field_product_series': 'solidex',
                         'body': {'format': 'full_html', 'value': '<h2>保留手工正文</h2><p>&lt;实验&gt;</p>' + ''.join(
                             f'<p><img src="https://example.com/old/{i}.jpg" alt="{d}" style="max-width:100%"></p>'
                             for i, d in enumerate(self.desc)) + '<p>原产品模块</p>'}}
        self.current = copy.deepcopy(self.original)
        self.client = Mock()
        self.client.get.side_effect = lambda *a, **k: {'data': {'attributes': copy.deepcopy(self.current)}}
        self.client.update.side_effect = self.update
        self.workflow.blog_client_factory.return_value = self.client
        self.store = self.workflow.asset_store_factory.return_value
        self.store.upload.side_effect = [f'https://example.com/new/{i}.jpg' for i in range(4)]
        self.patches = [patch.object(repair, 'BACKUP_DIR', self.root / 'backups'),
                        patch('utils.blog_task_lock.LOCK_DIR', self.root / 'locks'),
                        patch('batch_processor._resolve_job_figures', return_value=([], self.root)),
                        patch('batch_processor._resolve_figure_path', side_effect=self.paths)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def update(self, uuid, payload):
        self.current['body'] = {'format': payload['body_format'], 'value': payload['body']}
        if 'cover_url' in payload:
            self.current['field_cover_url'] = payload['cover_url']

    def test_patches_only_four_urls_and_matching_cover_preserves_body_fields_and_backup(self):
        result = repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertTrue(result['verified'])
        self.client.create.assert_not_called()
        self.client.update.assert_called_once()
        payload = self.client.update.call_args.args[1]
        self.assertEqual(set(payload), {'body', 'body_format', 'langcode', 'cover_url'})
        expected = self.original['body']['value'].replace('/old/', '/new/')
        self.assertEqual(self.current['body']['value'], expected)
        for key in ('title', 'langcode', 'status', 'field_slug', 'field_product_series', 'field_source_pdf_url'):
            self.assertEqual(self.current[key], self.original[key])
        self.assertEqual(self.current['field_cover_url'], 'https://example.com/new/0.jpg')
        backup = self.root / 'backups' / (result['backup_id'] + '.json')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(backup.read_text())['status'], 'verified')

    def test_ambiguous_cms_figures_never_upload_or_patch(self):
        self.current['body']['value'] += '<img src="x" alt="Figure 1 duplicate">'
        with self.assertRaisesRegex(BlogPipelineError, '不能唯一对应'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_repeated_source_figure_updates_every_occurrence_and_uploads_once(self):
        descriptions = ['Figure 1 全图', 'Figure 1 再次解读', 'Figure 2 结果']
        (self.root / 'article.md').write_text('\n\n'.join(f'[图片:{d}]' for d in descriptions))
        self.current['body']['value'] = '<h2>保留正文</h2>' + ''.join(
            f'<p><img src="https://example.com/old/{i}.jpg" alt="{d}"></p>'
            for i, d in enumerate(descriptions)) + '<img src="product.jpg" alt="产品示意图">'
        original_body = self.current['body']['value']
        result = repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertTrue(result['verified'])
        self.assertEqual(self.store.upload.call_count, 2)
        self.assertEqual([call.args[0] for call in self.store.upload.call_args_list], self.paths[:2])
        expected = original_body.replace('/old/0.jpg', '/new/0.jpg').replace('/old/1.jpg', '/new/0.jpg').replace('/old/2.jpg', '/new/1.jpg')
        self.assertEqual(self.current['body']['value'], expected)
        self.assertEqual(self.current['field_cover_url'], 'https://example.com/new/0.jpg')
        self.assertEqual(result['images'], ['https://example.com/new/0.jpg', 'https://example.com/new/1.jpg'])

    def test_renderer_deduplicated_source_occurrence_is_accepted(self):
        (self.root / 'article.md').write_text('\n\n'.join(f'[图片:{d}]' for d in [self.desc[0], *self.desc]))
        result = repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertTrue(result['verified'])
        self.assertEqual(self.store.upload.call_count, 4)
        self.assertEqual(self.current['body']['value'], self.original['body']['value'].replace('/old/', '/new/'))

    def test_missing_unique_source_figure_never_uploads_or_patches(self):
        self.current['body']['value'] = self.current['body']['value'].replace(
            '<p><img src="https://example.com/old/3.jpg" alt="Figure 4 结果" style="max-width:100%"></p>', '')
        with self.assertRaisesRegex(BlogPipelineError, '次数或顺序'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_cms_figure_order_must_match_source(self):
        self.current['body']['value'] = self.current['body']['value'].replace('Figure 1', 'Figure temp').replace('Figure 2', 'Figure 1').replace('Figure temp', 'Figure 2')
        with self.assertRaisesRegex(BlogPipelineError, '次数或顺序'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_extra_known_description_occurrence_is_rejected(self):
        self.current['body']['value'] += '<img src="extra.jpg" alt="Figure 1 结果">'
        with self.assertRaisesRegex(BlogPipelineError, '次数或顺序'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_translated_cms_alts_are_matched_to_target_language_version(self):
        target_path = self.root / 'article.en.md'
        target_path.write_text('\n\n'.join(f'[图片:Figure {i} Results]' for i in range(1, 5)))
        self.workflow.db.get_article_version.return_value = SimpleNamespace(content_path=str(target_path))
        self.workflow._drupal_language.return_value = 'en'
        self.workflow.db.get_distribution.return_value.external_url = public_blog_url('paper', 'en')
        self.current['langcode'] = 'en'
        self.current['body']['value'] = self.current['body']['value'].replace('结果', 'Results')
        result = repair.repair_images(self.workflow, 'paper', 'en')
        self.assertTrue(result['verified'])
        self.assertEqual(self.store.upload.call_count, 4)
        self.workflow.db.get_article_version.assert_called_once_with(1, 'en')

    def test_missing_target_language_version_is_rejected(self):
        self.workflow.db.get_article_version.return_value = None
        self.workflow.db.get_distribution.return_value.external_url = public_blog_url('paper', 'en')
        with self.assertRaisesRegex(BlogPipelineError, '目标语言稿件缺失'):
            repair.repair_images(self.workflow, 'paper', 'en')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_target_language_figure_mismatch_is_rejected(self):
        target_path = self.root / 'article.en.md'
        target_path.write_text('[图片:Figure 2 Results]')
        self.workflow.db.get_article_version.return_value = SimpleNamespace(content_path=str(target_path))
        self.workflow.db.get_distribution.return_value.external_url = public_blog_url('paper', 'en')
        with self.assertRaisesRegex(BlogPipelineError, '目标语言稿件图号与源稿不匹配'):
            repair.repair_images(self.workflow, 'paper', 'en')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_duplicate_files_fail_before_upload(self):
        Path(self.paths[1]).write_bytes(Path(self.paths[0]).read_bytes())
        with self.assertRaisesRegex(BlogPipelineError, '同一张图片'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_distinct_figure_keys_cannot_resolve_to_same_path(self):
        with patch('batch_processor._resolve_figure_path', side_effect=[self.paths[0], self.paths[0], *self.paths[2:]]):
            with self.assertRaisesRegex(BlogPipelineError, '真实图片缺失或重复'):
                repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

    def test_distinct_figure_keys_cannot_upload_to_same_url(self):
        self.store.upload.side_effect = ['https://example.com/new/duplicate.jpg'] * 4
        with self.assertRaisesRegex(BlogPipelineError, '上传图片没有唯一对应'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.client.update.assert_not_called()

    def test_custom_cover_is_preserved_by_default(self):
        self.current['field_cover_url'] = 'https://example.com/custom-cover.jpg'
        repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertEqual(self.current['field_cover_url'], 'https://example.com/custom-cover.jpg')
        self.assertNotIn('cover_url', self.client.update.call_args.args[1])

    def test_explicit_cover_repair_uses_first_validated_figure(self):
        self.current['field_cover_url'] = 'https://example.com/wrong-cover.jpg'
        repair.repair_images(self.workflow, 'paper', 'zh', repair_cover=True)
        self.assertEqual(self.current['field_cover_url'], 'https://example.com/new/0.jpg')

    def test_cover_only_repair_updates_and_backs_up_unchanged_body(self):
        self.current['field_cover_url'] = 'https://example.com/wrong-cover.jpg'
        original = copy.deepcopy(self.current)
        self.store.upload.side_effect = [f'https://example.com/old/{i}.jpg' for i in range(4)]
        result = repair.repair_images(self.workflow, 'paper', 'zh', repair_cover=True)
        self.assertEqual(result['status'], 'repaired')
        self.client.update.assert_called_once()
        self.assertEqual(self.current['body'], original['body'])
        self.assertEqual(self.current['field_cover_url'], 'https://example.com/old/0.jpg')
        record = json.loads((self.root / 'backups' / (result['backup_id'] + '.json')).read_text())
        self.assertEqual(record['original'], original)
        self.assertEqual(record['status'], 'verified')

    def test_no_changes_preserves_custom_cover_and_skips_patch(self):
        self.current['field_cover_url'] = 'https://example.com/custom-cover.jpg'
        self.store.upload.side_effect = [f'https://example.com/old/{i}.jpg' for i in range(4)]
        result = repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertEqual(result['status'], 'already_repaired')
        self.client.update.assert_not_called()
        self.assertFalse((self.root / 'backups').exists())

    def test_concurrent_cms_edit_not_overwritten(self):
        self.client.get.side_effect = [{'data': {'attributes': copy.deepcopy(self.original)}},
                                      {'data': {'attributes': {**self.original, 'title': '刚改的标题'}}}]
        with self.assertRaisesRegex(BlogPipelineError, '未覆盖新内容'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.client.update.assert_not_called()

    def test_failed_readback_has_backup_and_never_repeats_patch(self):
        def bad_update(uuid, payload):
            self.update(uuid, payload)
            self.current['body']['value'] += '意外改动'
        self.client.update.side_effect = bad_update
        with self.assertRaisesRegex(BlogPipelineError, '回读校验未通过'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.client.update.assert_called_once()
        self.assertEqual(json.loads(next((self.root / 'backups').glob('*.json')).read_text())['status'], 'needs_review')

    def test_transport_uncertainty_never_creates_replacement_or_repeats_patch(self):
        self.client.update.side_effect = OSError('remote-secret')
        with self.assertRaisesRegex(BlogPipelineError, '结果待核对') as error:
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.assertNotIn('remote-secret', str(error.exception))
        self.client.update.assert_called_once()
        self.client.create.assert_not_called()

    def test_wrong_language_is_not_patched(self):
        self.current['langcode'] = 'en'
        with self.assertRaisesRegex(BlogPipelineError, '不匹配'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()
