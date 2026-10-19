import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import wechat_draft_repair as repair
from utils.wechat_client import WeChatAPIError
from utils.wechat_cover import approved_cover_geometry, full_width_cover_verified


class DraftImageRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.descriptions = [f"Figure {number} 结果" for number in (1, 5, 3, 6)]
        (self.root / "article.md").write_text("\n\n".join(
            f"[图片:{desc}](Figure {desc.split()[1]})" for desc in self.descriptions))
        self.paths = []
        for index in range(4):
            path = self.root / f"{index}.jpg"
            path.write_bytes(f"fake image {index}".encode())
            self.paths.append(str(path))
        self.db = Mock()
        self.db.find_job_pk.return_value = 1
        self.db.get_job.return_value = SimpleNamespace(job_id="paper", pdf_path="p.pdf", template_id="t",
            product_id="p", image_pool=None, title_hint="T", line_id='aav')
        self.db.get_article.return_value = SimpleNamespace(content_dir=str(self.root), publish_blocked=False)
        self.db.latest_wechat_draft.return_value = {"account": "aav", "media_id": "existing"}
        self.original = {"title": "保留编辑过的标题", "author": "作者", "digest": "摘要",
            **approved_cover_geometry(),
            "thumb_media_id": "original-cover", "content_source_url": "https://genemedi.cn/blog/paper-zh",
            "need_open_comment": 1, "only_fans_can_comment": 0,
            "content": '<p>手工修改的正文</p>' + "".join(
                f'<p style="color:red"><span leaf="">图片:{desc}</span></p>' for desc in self.descriptions)
                + '<section>原产品模块<img data-src="https://mmbiz.qpic.cn/footer/640"></section>'
                + '<img data-src="https://mmbiz.qpic.cn/guide/640">'}
        self.current = dict(self.original)
        self.package = SimpleNamespace(bundle_id='a' * 64, figures=[], directory=self.root,
                                      cover_path=self.paths[0], source_markdown_sha256='hash')
        self.db.get_distribution.side_effect = lambda *args, **kwargs: SimpleNamespace(
            image_package_id='a' * 64, image_verification_status='verified',
            image_receipt_json=json.dumps({'thumb_media_id': self.current['thumb_media_id'], 'wide_cover_crop_verified': True,
                'image_keys': [list(repair._image_key(url)) for url in repair._Body(self.current['content']).images]}))
        self.client = Mock()
        self.client.get_draft.side_effect = lambda _id: {"news_item": [dict(self.current)]}
        self.client.update_draft.side_effect = self.update
        self.factory = Mock(return_value=self.client)
        self.patches = [
            patch.object(repair, "BACKUP_DIR", self.root / "backups"),
            patch("utils.blog_task_lock.LOCK_DIR", self.root / "locks"),
            patch('utils.wechat_draft_repair.require_approved_package', return_value=self.package),
            patch("batch_processor._resolve_job_figures", return_value=([], self.root)),
            patch("batch_processor._resolve_figure_path", side_effect=self.paths),
            patch("batch_processor._upload_cached", side_effect=[f"https://mmbiz.qpic.cn/figure{i}/0?from=appmsg" for i in range(4)]),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.upload = self.mocks[-1]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def update(self, media_id, index, payload):
        self.current = dict(payload)
        self.current['content'] = re.sub(r'(?<!data-)src="', 'data-src="', self.current['content']).replace('/0?', '/640?')

    def run_repair(self):
        return repair.repair_images(self.db, "paper", client_factory=self.factory)

    def test_patches_four_distinct_figures_preserves_manual_edits_and_reads_back(self):
        result = self.run_repair()
        self.assertTrue(result["verified"])
        self.assertEqual(result["inserted_images"], 4)
        self.client.update_draft.assert_called_once()
        self.client.create_draft.assert_not_called()
        media, index, payload = self.client.update_draft.call_args.args
        self.assertEqual((media, index), ("existing", 0))
        self.assertEqual(payload['content'].count('<img '), 6)
        self.assertIn('手工修改的正文', payload['content'])
        self.assertIn('原产品模块', payload['content'])
        self.assertNotIn('图片:Figure', payload['content'])
        self.assertEqual({k: v for k, v in payload.items() if k != 'content'},
                         {k: v for k, v in self.original.items() if k != 'content'})
        backup = self.root / "backups" / (result['backup_id'] + '.json')
        saved = json.loads(backup.read_text())
        self.assertEqual(saved['original'], self.original)
        self.assertEqual(saved['status'], 'verified')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_missing_or_ambiguous_marker_never_uploads_or_writes(self):
        self.current['content'] += f'<p>图片:{self.descriptions[0]}</p>'
        with self.assertRaises(repair.DraftImageRepairError):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_missing_figure_never_writes(self):
        Path(self.paths[0]).unlink()
        with self.assertRaises(repair.DraftImageRepairError):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_identical_image_content_for_different_figures_is_rejected(self):
        Path(self.paths[1]).write_bytes(Path(self.paths[0]).read_bytes())
        with self.assertRaises(repair.DraftImageRepairError):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_concurrent_draft_edit_is_not_overwritten(self):
        self.client.get_draft.side_effect = [{"news_item": [dict(self.original)]},
            {"news_item": [{**self.original, "title": "刚改的标题"}]}]
        with self.assertRaises(repair.DraftImageRepairError):
            self.run_repair()
        self.client.update_draft.assert_not_called()

    def test_failed_readback_is_not_success_and_does_not_repeat_update(self):
        def lose_image(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['content'] = self.current['content'].replace('figure0', 'missing')
        self.client.update_draft.side_effect = lose_image
        with self.assertRaisesRegex(repair.DraftImageRepairError, '不要盲目重试'):
            self.run_repair()
        self.client.update_draft.assert_called_once()
        saved = json.loads(next((self.root / 'backups').glob('*.json')).read_text())
        self.assertEqual(saved['status'], 'needs_review')

    def test_stale_draft_never_creates_a_replacement(self):
        self.client.update_draft.side_effect = WeChatAPIError('stale', errcode=40007)
        with self.assertRaises(WeChatAPIError):
            self.run_repair()
        self.client.create_draft.assert_not_called()

    def formatting_content(self):
        return ('<p style=""><span leaf="">手工正文 &lt;实验&gt;</span></p>'
                '<h2 style=""><span leaf="">小标题一</span></h2>'
                '<p style=""><img data-src="https://mmbiz.qpic.cn/figure/640"></p>'
                '<h2 style=""><span leaf="">小标题二</span></h2>'
                '<p style="">【相关产品推荐】</p><p style="">保留产品模块</p>'
                '<img data-src="https://mmbiz.qpic.cn/footer/640">')

    def test_formatting_restores_headings_without_body_title_or_regeneration(self):
        self.current['content'] = self.formatting_content()
        original = dict(self.current)
        result = repair.repair_formatting(self.db, 'paper', client_factory=self.factory)
        self.assertEqual(result['styled_headings'], 2)
        self.assertFalse(result['body_h1'])
        self.assertTrue(result['verified'])
        content = self.current['content']
        self.assertNotIn('<h1', content)
        self.assertIn('font-size:15px;font-weight:700;color:#ab1942', content)
        self.assertIn('font-size:14px;line-height:1.75', content)
        self.assertIn('&lt;实验&gt;', content)
        self.assertEqual(repair._text(content), repair._text(original['content']))
        self.assertTrue(content.endswith('<p style="">【相关产品推荐】</p><p style="">保留产品模块</p>'
                                         '<img data-src="https://mmbiz.qpic.cn/footer/640">'))
        self.assertEqual({k: v for k, v in self.current.items() if k != 'content'},
                         {k: v for k, v in original.items() if k != 'content'})
        self.client.create_draft.assert_not_called()
        self.upload.assert_not_called()

    def test_formatting_readback_losing_styles_is_not_success(self):
        self.current['content'] = self.formatting_content()
        def lose_style(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['content'] = re.sub(r'<h2 style="[^"]*"', '<h2 style=""', self.current['content'])
        self.client.update_draft.side_effect = lose_style
        with self.assertRaisesRegex(repair.DraftImageRepairError, '格式回读校验未通过'):
            repair.repair_formatting(self.db, 'paper', client_factory=self.factory)
        self.client.update_draft.assert_called_once()
        self.assertEqual(json.loads(next((self.root / 'backups').glob('*.json')).read_text())['status'], 'needs_review')

    def test_image_repair_rejects_typography_loss(self):
        self.current['content'] = '<h2 style="font-size:15px;color:#ab1942;">现有标题</h2>' + self.current['content']
        def lose_style(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['content'] = re.sub(r'<h2 style="[^"]*"', '<h2 style=""', self.current['content'])
        self.client.update_draft.side_effect = lose_style
        with self.assertRaisesRegex(repair.DraftImageRepairError, '格式回读校验未通过'):
            self.run_repair()

    def test_formatting_concurrent_edits_are_not_overwritten(self):
        original = {**self.original, 'content': self.formatting_content()}
        self.current = dict(original)
        self.client.get_draft.side_effect = [{'news_item': [original]},
            {'news_item': [{**original, 'title': '刚改的标题'}]}]
        with self.assertRaisesRegex(repair.DraftImageRepairError, '未覆盖新内容'):
            repair.repair_formatting(self.db, 'paper', client_factory=self.factory)
        self.client.update_draft.assert_not_called()

    def test_css_readback_tolerates_wechat_serialization(self):
        self.assertTrue(repair._styles_preserved('<h2 style="font-size:15px;font-weight:700;color:#ab1942;">标题</h2>',
            '<h2 style="font-size: 15px; font-weight:bold; color:rgb(171, 25, 66);"><span leaf="">标题</span></h2>'))


class ExistingDraftImageRepairTests(unittest.TestCase):
    def setUp(self):
        DraftImageRepairTests.setUp(self)
        self.original['content'] = ('<h2 style="font-size:15px;font-weight:700;color:#ab1942;">保留小标题</h2>'
            '<p style="font-size:14px;line-height:1.75;">手工修改的正文 &lt;实验&gt;</p>' + ''.join(
                f'<p style="text-align:center"><img data-src="https://mmbiz.qpic.cn/oldfigure{i}/640" '
                f'alt="{desc}" style="max-width:100%"></p>' for i, desc in enumerate(self.descriptions))
            + '<section>原产品模块<img data-src="https://mmbiz.qpic.cn/footer/640"></section>'
            + '<img data-src="https://mmbiz.qpic.cn/guide/640" alt="阅读原文">')
        self.current = dict(self.original)
        self.cover = self.root / 'contained-cover.jpg'
        self.cover.write_bytes(b'contained full figure cover')
        self.package.cover_path = str(self.cover)
        self.cover_patches = [patch('batch_processor._fit_wechat_cover', return_value=str(self.cover)),
                              patch('batch_processor._upload_cover_cached', return_value='new-contained-cover')]
        self.fit_cover, self.upload_cover = [item.start() for item in self.cover_patches]
        self.patches.extend(self.cover_patches)

    tearDown = DraftImageRepairTests.tearDown
    update = DraftImageRepairTests.update

    def run_repair(self, **kwargs):
        return repair.repair_existing_images(self.db, 'paper', client_factory=self.factory, **kwargs)

    def test_replaces_data_src_only_preserving_text_styles_and_all_other_images(self):
        result = self.run_repair()
        self.assertTrue(result['verified'])
        self.assertEqual(result['replaced_images'], 4)
        self.assertEqual(result['unique_images'], 4)
        self.assertEqual(result['match_methods'], ['alt'] * 4)
        self.client.update_draft.assert_called_once()
        self.client.create_draft.assert_not_called()
        self.assertEqual(repair._text(self.current['content']), repair._text(self.original['content']))
        self.assertTrue(repair._styles_preserved(self.original['content'], self.current['content']))
        self.assertNotIn('oldfigure', self.current['content'])
        self.assertIn('https://mmbiz.qpic.cn/footer/640', self.current['content'])
        self.assertIn('https://mmbiz.qpic.cn/guide/640', self.current['content'])
        self.assertEqual({k: v for k, v in self.current.items() if k != 'content'},
                         {k: v for k, v in self.original.items() if k != 'content'})
        self.fit_cover.assert_not_called()
        self.upload_cover.assert_not_called()
        saved = json.loads((self.root / 'backups' / (result['backup_id'] + '.json')).read_text())
        self.assertEqual(saved['original'], self.original)
        self.assertEqual(saved['operation'], 'replace_images')
        self.assertEqual(saved['status'], 'verified')

    def test_explicit_cover_repair_contains_first_figure_and_only_overrides_thumb(self):
        self.run_repair(repair_cover=True)
        self.fit_cover.assert_not_called()
        self.upload_cover.assert_called_once_with(self.client, 'aav', str(self.cover))
        self.assertEqual(self.current['thumb_media_id'], 'new-contained-cover')
        self.assertEqual({k: v for k, v in self.current.items() if k not in {'content', 'thumb_media_id'}},
                         {k: v for k, v in self.original.items() if k not in {'content', 'thumb_media_id'}})

    def test_explicit_cover_repair_resets_old_crop_and_marks_square_unreviewed(self):
        self.current['pic_crop_235_1'] = '0.2_0.2_0.8_0.8'
        self.current['cover_info'] = {'crop_percent_list': [
            {'ratio': '2.35_1', 'x1': '0.2', 'y1': '0.2', 'x2': '0.8', 'y2': '0.8'}]}
        result = self.run_repair(repair_cover=True)
        self.assertTrue(full_width_cover_verified(self.current))
        self.assertEqual(self.current['cover_info'], approved_cover_geometry()['cover_info'])
        self.assertEqual(self.current['pic_crop_235_1'], '0_0_1_1')
        self.assertTrue(result['wide_cover_crop_verified'])
        self.assertEqual(result['square_preview_status'], 'not_reviewed')

    def test_bad_wide_crop_readback_prevents_verified_receipt(self):
        def keep_old_crop(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['cover_info']['crop_percent_list'][0]['x1'] = '0.2'
        self.client.update_draft.side_effect = keep_old_crop
        with self.assertRaisesRegex(repair.DraftImageRepairError, '回读校验未通过'):
            self.run_repair(repair_cover=True)
        self.assertNotIn('verified', [call.kwargs.get('image_verification_status')
                                     for call in self.db.upsert_distribution.call_args_list])

    def test_cover_false_preserves_cover_and_all_existing_crop_fields(self):
        self.current['pic_crop_1_1'] = '0.3_0_0.7_1'
        self.current['cover_info']['crop_percent_list'][1]['x1'] = '0.3'
        self.current['cover_info']['crop_percent_list'][1]['x2'] = '0.7'
        original_crop = json.loads(json.dumps({key: self.current[key] for key in approved_cover_geometry()}))
        self.run_repair(repair_cover=False)
        self.assertEqual({key: self.current[key] for key in original_crop}, original_crop)
        self.assertEqual(self.current['thumb_media_id'], 'original-cover')

    def test_cover_false_rejects_unverified_cover_before_any_upload(self):
        self.db.get_distribution.side_effect = None
        self.db.get_distribution.return_value = SimpleNamespace(image_package_id=self.package.bundle_id,
            image_verification_status='verified', image_receipt_json=json.dumps({'thumb_media_id': 'different-cover'}))
        with self.assertRaisesRegex(repair.DraftImageRepairError, '显式包含封面修复'):
            self.run_repair(repair_cover=False)
        self.upload.assert_not_called()
        self.upload_cover.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_empty_explicit_cover_upload_never_updates_draft(self):
        self.upload_cover.return_value = ''
        with self.assertRaisesRegex(repair.DraftImageRepairError, '封面上传失败'):
            self.run_repair(repair_cover=True)
        self.client.update_draft.assert_not_called()

    def test_dry_run_does_not_upload_patch_or_create_backup(self):
        result = self.run_repair(repair_cover=True, dry_run=True)
        self.assertEqual(result['status'], 'dry_run')
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['matched_images'], 4)
        self.assertEqual(len(result['figures']), 4)
        self.assertIn('cover_sha256', result)
        self.fit_cover.assert_not_called()
        self.upload.assert_not_called()
        self.upload_cover.assert_not_called()
        self.client.update_draft.assert_not_called()
        self.client.create_draft.assert_not_called()
        self.assertFalse((self.root / 'backups').exists())
        self.client.get_draft.assert_called_once_with('existing')

    def test_source_repeated_figure_may_be_deduplicated_in_existing_draft(self):
        (self.root / 'article.md').write_text('\n\n'.join(
            f'[图片:{desc}]' for desc in [self.descriptions[0], *self.descriptions]))
        result = self.run_repair()
        self.assertEqual(result['replaced_images'], 4)
        self.assertEqual(self.upload.call_count, 4)
        for index, description in enumerate(self.descriptions):
            self.assertIn(f'data-src="https://mmbiz.qpic.cn/figure{index}/640?from=appmsg" alt="{description}"',
                          self.current['content'])

    def test_repeated_existing_source_figure_reuses_one_upload(self):
        descriptions = [self.descriptions[0], self.descriptions[0], *self.descriptions[1:]]
        (self.root / 'article.md').write_text('\n\n'.join(f'[图片:{desc}]' for desc in descriptions))
        first_image = f'<img data-src="https://mmbiz.qpic.cn/oldrepeat/640" alt="{self.descriptions[0]}">'
        self.current['content'] = first_image + self.current['content']
        result = self.run_repair()
        self.assertEqual(result['replaced_images'], 5)
        self.assertEqual(self.upload.call_count, 4)
        self.assertEqual(repair._Body(self.current['content']).images.count('https://mmbiz.qpic.cn/figure0/640?from=appmsg'), 2)

    def test_missing_alt_does_not_fall_back_to_image_order(self):
        self.current['content'] = self.current['content'].replace(f' alt="{self.descriptions[0]}"', '')
        with self.assertRaisesRegex(repair.DraftImageRepairError, '身份与源稿不能唯一对应'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_saved_source_matched_alt_and_url_can_recover_stripped_alt(self):
        (self.root / 'article.html').write_text(self.original['content'])
        self.current['content'] = self.current['content'].replace(f' alt="{self.descriptions[0]}"', '')
        result = self.run_repair()
        self.assertEqual(result['match_methods'], ['saved_url', 'alt', 'alt', 'alt'])
        self.assertNotIn('oldfigure', self.current['content'])

    def test_ambiguous_saved_url_binding_is_rejected(self):
        (self.root / 'article.html').write_text(self.original['content'].replace('oldfigure1', 'oldfigure0'))
        self.current['content'] = self.current['content'].replace(f' alt="{self.descriptions[0]}"', '')
        with self.assertRaisesRegex(repair.DraftImageRepairError, '地址对应多个图号'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_unknown_description_for_known_figure_is_rejected(self):
        self.current['content'] = self.current['content'].replace(self.descriptions[0], 'Figure 1 人工新增未知图')
        with self.assertRaisesRegex(repair.DraftImageRepairError, '描述与源稿不能唯一对应'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_extra_matching_figure_is_rejected(self):
        self.current['content'] += f'<img data-src="https://mmbiz.qpic.cn/extra/640" alt="{self.descriptions[0]}">'
        with self.assertRaisesRegex(repair.DraftImageRepairError, '次数、顺序或身份'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_disagreeing_src_and_data_src_are_rejected(self):
        self.current['content'] = self.current['content'].replace('data-src="https://mmbiz.qpic.cn/oldfigure0/640"',
            'data-src="https://mmbiz.qpic.cn/oldfigure0/640" src="https://mmbiz.qpic.cn/different/640"')
        with self.assertRaisesRegex(repair.DraftImageRepairError, 'src 与 data-src 不一致'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_concurrent_edit_is_not_overwritten(self):
        self.client.get_draft.side_effect = [{'news_item': [dict(self.original)]},
            {'news_item': [{**self.original, 'title': '刚改的标题'}]}]
        with self.assertRaisesRegex(repair.DraftImageRepairError, '未覆盖新内容'):
            self.run_repair()
        self.client.update_draft.assert_not_called()

    def test_replacing_images_must_preserve_footer_and_sequence_on_readback(self):
        def lose_footer(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['content'] = self.current['content'].replace('https://mmbiz.qpic.cn/footer/640', 'https://mmbiz.qpic.cn/lost/640')
        self.client.update_draft.side_effect = lose_footer
        with self.assertRaisesRegex(repair.DraftImageRepairError, '不要盲目重试'):
            self.run_repair()
        self.client.update_draft.assert_called_once()
        saved = json.loads(next((self.root / 'backups').glob('*.json')).read_text())
        self.assertEqual(saved['status'], 'needs_review')

    def test_readback_image_reordering_is_rejected(self):
        def reorder_images(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['content'] = self.current['content'].replace('figure0/', 'swap/').replace(
                'figure1/', 'figure0/').replace('swap/', 'figure1/')
        self.client.update_draft.side_effect = reorder_images
        with self.assertRaisesRegex(repair.DraftImageRepairError, '不要盲目重试'):
            self.run_repair()
        self.client.update_draft.assert_called_once()

    def test_cover_readback_must_equal_intended_material(self):
        def lose_cover(media_id, index, payload):
            self.update(media_id, index, payload)
            self.current['thumb_media_id'] = 'old-cover'
        self.client.update_draft.side_effect = lose_cover
        with self.assertRaisesRegex(repair.DraftImageRepairError, '不要盲目重试'):
            self.run_repair(repair_cover=True)
        self.client.update_draft.assert_called_once()

    def test_stale_draft_failure_is_backed_up_without_recreation(self):
        self.client.update_draft.side_effect = WeChatAPIError('stale', errcode=40007)
        with self.assertRaises(WeChatAPIError):
            self.run_repair()
        self.client.create_draft.assert_not_called()
        self.assertEqual(json.loads(next((self.root / 'backups').glob('*.json')).read_text())['status'], 'needs_review')

    def test_duplicate_figure_bytes_are_rejected_before_uploads(self):
        Path(self.paths[1]).write_bytes(Path(self.paths[0]).read_bytes())
        with self.assertRaisesRegex(repair.DraftImageRepairError, '多个图号对应同一张图片'):
            self.run_repair()
        self.upload.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_duplicate_uploaded_images_are_rejected_before_patch(self):
        self.upload.side_effect = ['https://mmbiz.qpic.cn/samefigure/0'] * 4
        with self.assertRaisesRegex(repair.DraftImageRepairError, '没有对应到独立图片'):
            self.run_repair(repair_cover=True)
        self.upload_cover.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_cover_containment_failure_prevents_all_uploads(self):
        self.package.cover_path = str(self.root / 'missing-approved-cover.jpg')
        with self.assertRaisesRegex(repair.DraftImageRepairError, '完整图片封面适配失败'):
            self.run_repair(repair_cover=True)
        self.upload.assert_not_called()
        self.upload_cover.assert_not_called()
        self.client.update_draft.assert_not_called()

    def test_cover_only_repair_is_not_a_noop(self):
        self.upload.side_effect = [f'https://mmbiz.qpic.cn/oldfigure{i}/640' for i in range(4)]
        result = self.run_repair(repair_cover=True)
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(self.current['content'], self.original['content'])
        self.assertEqual(self.current['thumb_media_id'], 'new-contained-cover')
        self.client.update_draft.assert_called_once()

    def test_already_repaired_avoids_patch(self):
        self.upload.side_effect = [f'https://mmbiz.qpic.cn/oldfigure{i}/640' for i in range(4)]
        result = self.run_repair()
        self.assertEqual(result['status'], 'already_repaired')
        self.client.update_draft.assert_not_called()
        self.assertFalse((self.root / 'backups').exists())


if __name__ == '__main__':
    unittest.main()
