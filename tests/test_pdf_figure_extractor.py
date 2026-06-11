"""pdf_figure_extractor 的纯函数测试（图号解析）。

extract_figures 依赖真实 PDF，不在单测覆盖；这里测可纯函数化的 figure_number。
"""
import unittest

from utils.pdf_figure_extractor import Figure, figure_number, match_figure


def _fig(label, ext):
    return Figure(label=label, is_extended=ext, caption="", page=1,
                  image_path=f"{label}.png", width=10, height=10)


class TestMatchFigure(unittest.TestCase):
    def test_main_ref_does_not_fall_back_to_extended(self):
        # 只有附录图1时，主图占位符不该被附录顶替
        self.assertIsNone(match_figure([_fig("1", True)], "Figure 1 示意图"))

    def test_main_ref_matches_main(self):
        m = match_figure([_fig("1", False), _fig("1", True)], "Figure 1 x")
        self.assertIsNotNone(m)
        self.assertFalse(m.is_extended)

    def test_extended_ref_matches_extended(self):
        m = match_figure([_fig("2", True)], "Extended Data Figure 2")
        self.assertIsNotNone(m)
        self.assertTrue(m.is_extended)

    def test_no_number_returns_none(self):
        self.assertIsNone(match_figure([_fig("1", False)], "见正文"))


class TestFigureNumber(unittest.TestCase):
    def test_figure_forms(self):
        self.assertEqual(figure_number("Figure 1 转录串扰示意图"), "1")
        self.assertEqual(figure_number("Fig. 3"), "3")
        self.assertEqual(figure_number("Figure 1e panel"), "1")   # 取图号，忽略面板字母
        self.assertEqual(figure_number("fig 6"), "6")

    def test_extended_data_number(self):
        # figure_number 只取图号；主图 / 附录的区分由 is_extended 承担
        self.assertEqual(figure_number("Extended Data Fig. 2"), "2")

    def test_non_figure(self):
        self.assertEqual(figure_number("see text"), "")
        self.assertEqual(figure_number("结尾段落"), "")
        self.assertEqual(figure_number(""), "")


if __name__ == "__main__":
    unittest.main()
