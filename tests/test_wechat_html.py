"""wechat_html 单元测试 —— 重点验证标题分级内联样式（公众号层次）。"""
import re
import unittest

from utils.wechat_html import (
    extract_title_and_digest,
    find_image_placeholders,
    markdown_to_wechat_html,
    replace_image_placeholder,
)


class TestHeadingStyles(unittest.TestCase):
    def setUp(self):
        md = "# 大标题\n\n正文一。\n\n## 小标题A\n\n正文二。\n\n### 子标题\n\n正文三。"
        self.html = markdown_to_wechat_html(md)

    def test_each_level_has_inline_style(self):
        self.assertRegex(self.html, r'<h1 style="[^"]+">大标题</h1>')
        self.assertRegex(self.html, r'<h2 style="[^"]+">小标题A</h2>')
        self.assertRegex(self.html, r'<h3 style="[^"]+">子标题</h3>')

    def test_levels_are_visually_distinct(self):
        h1 = re.search(r'<h1 style="([^"]+)"', self.html).group(1)
        h2 = re.search(r'<h2 style="([^"]+)"', self.html).group(1)
        h3 = re.search(r'<h3 style="([^"]+)"', self.html).group(1)
        # 三级样式互不相同（字体 / 字号 / 颜色至少有别）
        self.assertNotEqual(h1, h2)
        self.assertNotEqual(h2, h3)
        # h1 衬线、h2 品牌蓝
        self.assertIn("serif", h1)
        self.assertIn("#2563eb", h2)


class TestPlaceholderAndSafety(unittest.TestCase):
    def test_image_placeholder_preserved(self):
        html = markdown_to_wechat_html("# T\n\n[图片:Figure 1 示意图]\n\n正文")
        self.assertIn("[图片:Figure 1 示意图]", html)

    def test_find_and_replace_placeholder(self):
        self.assertEqual(find_image_placeholders("[图片:a] x [图片:b]"), ["a", "b"])
        out = replace_image_placeholder("x [图片:a] y", "a", "https://mmbiz.qpic.cn/p.png")
        self.assertIn('<img src="https://mmbiz.qpic.cn/p.png"', out)
        self.assertNotIn("[图片:a]", out)

    def test_strips_dangerous_tags(self):
        html = markdown_to_wechat_html("# T\n\n<script>alert(1)</script>\n\n正文段落内容。")
        self.assertNotIn("<script", html)

    def test_empty(self):
        self.assertEqual(markdown_to_wechat_html(""), "")


class TestTitleDigest(unittest.TestCase):
    def test_extract(self):
        t, d = extract_title_and_digest("# 标题\n\n首段摘要内容。\n\n## 节")
        self.assertEqual(t, "标题")
        self.assertIn("首段摘要", d)


if __name__ == "__main__":
    unittest.main()
