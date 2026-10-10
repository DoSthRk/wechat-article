import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import wechat_draft_repair as repair
from utils.wechat_client import WeChatAPIError


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
            product_id="p", image_pool=None, title_hint="T")
        self.db.get_article.return_value = SimpleNamespace(content_dir=str(self.root), publish_blocked=False)
        self.db.latest_wechat_draft.return_value = {"account": "aav", "media_id": "existing"}
        self.original = {"title": "保留编辑过的标题", "author": "作者", "digest": "摘要",
            "thumb_media_id": "original-cover", "content_source_url": "https://genemedi.cn/blog/paper-zh",
            "need_open_comment": 1, "only_fans_can_comment": 0,
            "content": '<p>手工修改的正文</p>' + "".join(
                f'<p style="color:red"><span leaf="">图片:{desc}</span></p>' for desc in self.descriptions)
                + '<section>原产品模块<img data-src="https://mmbiz.qpic.cn/footer/640"></section>'
                + '<img data-src="https://mmbiz.qpic.cn/guide/640">'}
        self.current = dict(self.original)
        self.client = Mock()
        self.client.get_draft.side_effect = lambda _id: {"news_item": [dict(self.current)]}
        self.client.update_draft.side_effect = self.update
        self.factory = Mock(return_value=self.client)
        self.patches = [
            patch.object(repair, "BACKUP_DIR", self.root / "backups"),
            patch("utils.blog_task_lock.LOCK_DIR", self.root / "locks"),
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


if __name__ == '__main__':
    unittest.main()
