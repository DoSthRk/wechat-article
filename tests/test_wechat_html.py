"""wechat_html 单元测试 —— 重点验证标题分级内联样式（公众号层次）。"""
import re
import unittest

from utils.wechat_html import (
    extract_title_and_digest,
    find_image_placeholders,
    markdown_to_wechat_html,
    replace_image_placeholder,
)


class TestFormatRules(unittest.TestCase):
    """格式规则（用户拍板）：删大标题 / 正文 14px / 小标题 15px #ab1942 / 统一微软雅黑。"""

    def setUp(self):
        md = "# 大标题\n\n正文一段。\n\n## 小标题A\n\n正文二段。"
        self.html = markdown_to_wechat_html(md)

    def test_h1_big_title_removed(self):
        self.assertNotIn("<h1", self.html)
        self.assertNotIn("大标题", self.html)  # h1 文本一并删除

    def test_h2_subheading_color_and_size(self):
        m = re.search(r'<h2 style="([^"]+)">小标题A</h2>', self.html)
        self.assertIsNotNone(m)
        style = m.group(1)
        self.assertIn("#ab1942", style)
        self.assertIn("font-size:15px", style)
        self.assertIn("Microsoft YaHei", style)

    def test_body_paragraph_14px(self):
        m = re.search(r'<p style="([^"]+)">正文一段。</p>', self.html)
        self.assertIsNotNone(m)
        self.assertIn("font-size:14px", m.group(1))
        self.assertIn("Microsoft YaHei", m.group(1))

    def test_no_serif_or_blue_anywhere(self):
        # sans-serif（无衬线回退）允许；真正的衬线/宋体不允许
        self.assertNotIn("serif", self.html.replace("sans-serif", ""))
        self.assertNotIn("SimSun", self.html)
        self.assertNotIn("Songti", self.html)
        self.assertNotIn("宋体", self.html)
        self.assertNotIn("#2563eb", self.html)  # 旧的蓝色不再出现


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
