import copy
import hashlib
import tempfile
import unittest
import io
from unittest.mock import patch
from pathlib import Path

from scripts.migrate_article_assets import Cms, changed_fields, freeze, patch_articles


class CmsFake:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def articles(self):
        return self.rows

    def path(self, article):
        return article["uuid"]

    def request(self, url, payload=None):
        if payload:
            self.writes.append(payload)
            self.rows[0]["attributes"].update(payload["data"]["attributes"])
        return {"data": copy.deepcopy(self.rows[0])}


class AssetMigrationTests(unittest.TestCase):
    def test_pagination_never_sends_credentials_over_http_or_to_another_host(self):
        cms = object.__new__(Cms)
        cms.base = "https://hub.genemedi.net"
        cms.headers = {"Authorization": "Basic test-only"}
        with patch("scripts.migrate_article_assets.cms_urlopen", return_value=io.BytesIO(b'{"data": []}')) as request:
            cms.request("http://hub.genemedi.net/jsonapi/node/article?page=2")
            self.assertEqual(request.call_args.args[0].full_url,
                             "https://hub.genemedi.net/jsonapi/node/article?page=2")
        with patch("scripts.migrate_article_assets.cms_urlopen") as request:
            with self.assertRaises(ValueError):
                cms.request("https://unexpected.example/jsonapi/node/article")
            request.assert_not_called()

    def test_plan_preserves_prose_and_snapshots_only_asset_field_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = b"%PDF-1.7\noriginal\n%%EOF\n"
            digest = hashlib.sha256(raw).hexdigest()
            path = Path(directory) / digest[:2] / (digest + ".pdf")
            path.parent.mkdir()
            path.write_bytes(raw)
            old = "https://www.genemedi.net/uploads/papers/" + path.relative_to(directory).as_posix()
            attributes = {"title": "Keep title", "langcode": "en", "status": True, "changed": "2026-10-28",
                          "body": {"value": "<p>Keep prose</p><a href=\"" + old + "\">PDF</a>", "format": "full_html"},
                          "field_source_pdf_url": old,
                          "field_cover_url": "https://img.genemedi.cn/article-assets/blog/" + "a" * 64 + ".jpg"}
            cms = CmsFake([{"id": "article-1", "attributes": copy.deepcopy(attributes)}])
            plan = freeze(directory, cms, "https://img.genemedi.cn")
            self.assertEqual(len(plan["assets"]), 1)
            self.assertEqual(len(plan["articles"]), 1)
            self.assertEqual(plan["existing_cdn_urls"], [attributes["field_cover_url"]])
            self.assertEqual(plan["articles"][0]["original"], attributes)
            patch = plan["articles"][0]["patch"]
            self.assertEqual(set(patch), {"body", "field_source_pdf_url"})
            self.assertIn("<p>Keep prose</p>", patch["body"]["value"])
            journal = Path(directory) / "journal.json"
            patch_articles(plan, cms, journal)
            self.assertEqual(cms.rows[0]["attributes"]["title"], attributes["title"])
            self.assertEqual(cms.rows[0]["attributes"]["status"], True)
            patch_articles(plan, cms, journal, rollback=True)
            self.assertEqual(cms.rows[0]["attributes"]["body"], attributes["body"])

    def test_missing_source_or_concurrent_edit_blocks_before_write(self):
        old = "https://www.genemedi.net/uploads/papers/ab/" + "a" * 64 + ".pdf"
        with self.assertRaisesRegex(ValueError, "missing"):
            changed_fields({"field_source_pdf_url": old}, {})
        original = {"langcode": "en", "body": {"value": "original", "format": "full_html"}}
        plan = {"articles": [{"uuid": "one", "original": original,
                              "patch": {"body": {"value": "migration", "format": "full_html"}}}]}
        edited = copy.deepcopy(original)
        edited["body"]["value"] = "human edit"
        cms = CmsFake([{"id": "one", "attributes": edited}])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "concurrently"):
                patch_articles(plan, cms, Path(directory) / "journal.json")
        self.assertEqual(cms.writes, [])


if __name__ == "__main__":
    unittest.main()
