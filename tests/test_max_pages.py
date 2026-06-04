"""_resolve_max_pages：job.extra.max_pages > env PDF_MAX_PAGES > 不限。"""
import os
import unittest

from core.main import ArticleAnalyzer
from utils.job_loader import Job


def _job(extra: dict) -> Job:
    return Job(job_id="j", pdf="x.pdf", template="t", product="p", extra=extra)


class TestResolveMaxPages(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PDF_MAX_PAGES", None)

    def tearDown(self):
        os.environ.pop("PDF_MAX_PAGES", None)

    def test_from_job_extra(self):
        self.assertEqual(ArticleAnalyzer._resolve_max_pages(_job({"max_pages": 25})), 25)

    def test_absent_returns_none(self):
        self.assertIsNone(ArticleAnalyzer._resolve_max_pages(_job({})))

    def test_invalid_returns_none(self):
        self.assertIsNone(ArticleAnalyzer._resolve_max_pages(_job({"max_pages": "abc"})))

    def test_zero_or_negative_returns_none(self):
        self.assertIsNone(ArticleAnalyzer._resolve_max_pages(_job({"max_pages": 0})))
        self.assertIsNone(ArticleAnalyzer._resolve_max_pages(_job({"max_pages": -3})))

    def test_env_fallback_when_no_job_value(self):
        os.environ["PDF_MAX_PAGES"] = "10"
        self.assertEqual(ArticleAnalyzer._resolve_max_pages(_job({})), 10)

    def test_job_extra_overrides_env(self):
        os.environ["PDF_MAX_PAGES"] = "10"
        self.assertEqual(ArticleAnalyzer._resolve_max_pages(_job({"max_pages": 30})), 30)


if __name__ == "__main__":
    unittest.main()
