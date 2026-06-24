"""题注锚定抽图：题注正则 + 框计算（纯函数，无需真 PDF）。"""
import os
import unittest

from utils import caption_figures as cf


def _el(x0, top, x1, bottom):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom}


class TestCaptionRegex(unittest.TestCase):
    def test_matches(self):
        for t, num in [
            ("Figure 1. Integrative single-cell analysis", "1"),
            ("Fig. 2 | Safety endpoints.", "2"),
            ("Extended Data Fig. 3 | PET images", "3"),
            ("图3 研究设计", "3"),
            ("Figure 10. The mechanism", "10"),
        ]:
            m = cf._CAPTION_RE.match(t)
            self.assertIsNotNone(m, t)
            self.assertEqual(m.group(2), num)

    def test_non_captions(self):
        for t in ["Significant findings were", "Configuration of the", "Figures show that", "Fig shows"]:
            self.assertIsNone(cf._CAPTION_RE.match(t), t)

    def test_extended_flag(self):
        import re
        m = cf._CAPTION_RE.match("Extended Data Fig. 1 | x")
        self.assertTrue(bool(re.search(r"extended|sup", m.group(1), re.I)))


class TestBoxForCaption(unittest.TestCase):
    W, H = 600.0, 800.0

    def test_figure_above_caption_excludes_rule(self):
        gfx = [_el(100, 50, 500, 350),          # 正图大框
               _el(40, 380, 560, 380.5)]        # 全宽细规则线（应剔除）
        box = cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H)
        self.assertEqual(box, (100, 50, 500, 350))

    def test_figure_below_caption_fallback(self):
        gfx = [_el(100, 450, 500, 750)]         # 图在题注下方
        box = cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H)
        self.assertEqual(box, (100, 450, 500, 750))

    def test_prev_caption_bounds_region(self):
        # 上一题注 bottom=300：题注上方只取 300 以下的图，不吃上一张图（top=100 那块）
        gfx = [_el(100, 100, 500, 250),         # 上一张图（应被上一题注隔开）
               _el(100, 320, 500, 380)]         # 本图
        box = cf._box_for_caption(400, 415, gfx, [290, 400], [300, 415], self.W, self.H)
        self.assertEqual(box, (100, 320, 500, 380))

    def test_too_small_returns_none(self):
        gfx = [_el(100, 380, 110, 390)]         # 太小
        self.assertIsNone(cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H))


class TestEnabled(unittest.TestCase):
    def test_default_on(self):
        os.environ.pop("CAPTION_FIGURES_ENABLED", None)
        self.assertTrue(cf.caption_enabled())

    def test_off(self):
        os.environ["CAPTION_FIGURES_ENABLED"] = "0"
        try:
            self.assertFalse(cf.caption_enabled())
        finally:
            os.environ.pop("CAPTION_FIGURES_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
