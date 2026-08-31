import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db.database import DatabaseManager
from utils.blog_urls import BlogUrlError, blog_slug, public_blog_url, resolve_published_blog_url


class BlogUrlTests(unittest.TestCase):
    def test_chinese_job_id_becomes_stable_public_slug(self):
        self.assertEqual(blog_slug("免疫客文章-3-9", "zh"), "3-9-zh")
        self.assertEqual(
            public_blog_url("免疫客文章-3-9", "zh", {}),
            "https://genemedi.cn/blog/3-9-zh",
        )

    def test_language_public_origins(self):
        self.assertEqual(
            public_blog_url("paper-1", "ja", {}),
            "https://ja.genemedi.com/blog/paper-1-ja",
        )

    def test_non_https_override_is_rejected(self):
        with self.assertRaisesRegex(BlogUrlError, "HTTPS origin"):
            public_blog_url(
                "paper-1", "zh",
                {"GENEMEDI_BLOG_PUBLIC_BASE_URL_ZH": "http://example.test"},
            )

    def test_distribution_must_match_canonical_public_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(database_url=f"sqlite:///{(Path(tmp) / 't.db').as_posix()}")
            task = db.get_or_create_task("t")
            job_pk = db.upsert_job(
                task.id, "paper-1", pdf_path="paper.pdf",
                template_id="aav", product_id="purprox",
            ).id
            db.upsert_distribution(
                job_pk, "blog", account="genemedi", lang="zh",
                publish_status="published",
                external_url="https://blog.genemedi.com/zh-hans/node/1",
            )
            with self.assertRaisesRegex(BlogUrlError, "不是当前官网地址"):
                resolve_published_blog_url(db, job_pk, "paper-1", verify=False)
            db.engine.dispose()

    def test_verified_canonical_distribution_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(database_url=f"sqlite:///{(Path(tmp) / 't.db').as_posix()}")
            task = db.get_or_create_task("t")
            job_pk = db.upsert_job(
                task.id, "paper-1", pdf_path="paper.pdf",
                template_id="aav", product_id="purprox",
            ).id
            expected = "https://genemedi.cn/blog/paper-1-zh"
            db.upsert_distribution(
                job_pk, "blog", account="genemedi", lang="zh",
                publish_status="published", external_url=expected,
            )
            with patch("utils.blog_urls.verify_public_blog_url", return_value=expected) as verify:
                self.assertEqual(resolve_published_blog_url(db, job_pk, "paper-1"), expected)
            verify.assert_called_once_with(expected)
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
