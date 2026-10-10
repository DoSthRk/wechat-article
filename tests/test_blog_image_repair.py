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

    def test_duplicate_files_fail_before_upload(self):
        Path(self.paths[1]).write_bytes(Path(self.paths[0]).read_bytes())
        with self.assertRaisesRegex(BlogPipelineError, '同一张图片'):
            repair.repair_images(self.workflow, 'paper', 'zh')
        self.store.upload.assert_not_called()
        self.client.update.assert_not_called()

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
