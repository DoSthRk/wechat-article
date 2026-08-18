import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import wechat_template_assets as assets


class FakeClient:
    def __init__(self):
        self.uploads = []

    def upload_image(self, path):
        self.uploads.append(path)
        return "http://mmbiz.qpic.cn/test/source-pdf-guide.png"


class TestWeChatTemplateAssets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.asset = base / "guide.png"
        self.asset.write_bytes(b"guide-v1")
        self.cache = base / "cache.json"
        self._patches = [
            patch.object(assets, "SOURCE_PDF_GUIDE_PATH", self.asset),
            patch.object(assets, "DEFAULT_CACHE_PATH", self.cache),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def test_upload_is_cached_by_account_and_asset_hash(self):
        client = FakeClient()
        first = assets.upload_source_pdf_guide(client, "immune")
        second = assets.upload_source_pdf_guide(client, "immune")
        self.assertEqual(first, second)
        self.assertEqual(first, "http://mmbiz.qpic.cn/test/source-pdf-guide.png")
        self.assertEqual(len(client.uploads), 1)
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(len(cache), 1)

        assets.upload_source_pdf_guide(client, "aav")
        self.assertEqual(len(client.uploads), 2)

    def test_append_is_idempotent_and_builds_full_width_png(self):
        client = FakeClient()
        html, url = assets.append_source_pdf_guide("<p>正文</p>", client, "immune")
        self.assertTrue(html.endswith("</section>"))
        self.assertIn('data-source-pdf-guide="1"', html)
        self.assertIn('data-w="1080"', html)
        self.assertIn(url, html)

        repeated, _ = assets.append_source_pdf_guide(html, client, "immune")
        self.assertEqual(repeated, html)
        self.assertEqual(len(client.uploads), 1)

        rewritten = html.replace("https://", "http://").replace(
            "source-pdf-guide.png", "source-pdf-guide.png?wx_fmt=png",
        )
        repeated, _ = assets.append_source_pdf_guide(rewritten, client, "immune")
        self.assertEqual(repeated, rewritten)
        self.assertTrue(assets.contains_source_pdf_guide(rewritten, url))


if __name__ == "__main__":
    unittest.main()
