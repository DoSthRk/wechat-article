"""pdf_figure_extractor 的纯函数测试（图号解析）。

extract_figures 依赖真实 PDF，不在单测覆盖；这里测可纯函数化的 figure_number。
"""
import unittest

from utils.pdf_figure_extractor import figure_number


class TestFigureNumber(unittest.TestCase):
    def test_figure_forms(self):
        self.assertEqual(figure_number("Figure 1 转录串扰示意图"), "1")
        self.assertEqual(figure_number("Fig. 3"), "3")
        self.assertEqual(figure_number("Figure 1e panel"), "1")   # 取图号，忽略面板字母
        self.assertEqual(figure_number("fig 6"), "6")

    def test_non_figure(self):
        self.assertEqual(figure_number("Extended Data Fig. 2"), "")  # 不以 Fig 开头 → 不当主图
        self.assertEqual(figure_number("see text"), "")
        self.assertEqual(figure_number(""), "")


if __name__ == "__main__":
    unittest.main()
