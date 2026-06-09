"""markdown_health_score 单元测试。"""
import unittest

from utils.health_check import is_markdown_healthy, markdown_health_score


def _healthy_md() -> str:
    return (
        "# 标题\n\n"
        + "这是一段足够长的正文内容。" * 60
        + "\n\n## 第一节\n内容\n\n## 第二节\n内容\n\n## 第三节\n内容\n"
    )


class TestHealthCheck(unittest.TestCase):
    def test_healthy_article_high_score(self):
        self.assertEqual(markdown_health_score(_healthy_md()), 100)
        self.assertTrue(is_markdown_healthy(_healthy_md()))

    def test_empty_is_zero(self):
        self.assertEqual(markdown_health_score(""), 0)
        self.assertEqual(markdown_health_score("   "), 0)

    def test_html_start_heavily_penalized(self):
        md = "<div>" + "x" * 600 + "</div>"
        self.assertLessEqual(markdown_health_score(md), 20)
        self.assertFalse(is_markdown_healthy(md))  # 以 < 开头不健康

    def test_too_short_penalized(self):
        self.assertLess(markdown_health_score("# 标题\n\n太短了。"), 50)

    def test_no_h1_penalized(self):
        with_h1 = markdown_health_score(_healthy_md())
        no_h1 = markdown_health_score(_healthy_md().replace("# 标题", "标题（无井号）", 1))
        self.assertLess(no_h1, with_h1)

    def test_part_marker_penalized(self):
        md = _healthy_md() + "\n\n===PART_2==="
        self.assertLess(markdown_health_score(md), 30)


if __name__ == "__main__":
    unittest.main()
