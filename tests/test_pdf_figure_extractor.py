"""pdf_figure_extractor 的纯函数测试（图号解析）。

extract_figures 依赖真实 PDF，不在单测覆盖；这里测可纯函数化的 figure_number。
"""
import tempfile
import unittest
from pathlib import Path

from utils import pdf_figure_extractor as pe
from utils.pdf_figure_extractor import Figure, figure_number, match_figure


def _el(x0, top, x1, bottom):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom}


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


class TestLegendFigurePairing(unittest.TestCase):
    def test_dense_vector_figure_page_can_contain_many_chart_labels(self):
        page = type("Page", (), {"width": 612, "height": 792, "images": []})()
        self.assertTrue(
            pe._detached_figure_page(page, words=[{}] * 269, gfx=[{}] * 1553)
        )
        self.assertTrue(
            pe._detached_figure_page(page, words=[{}] * 750, gfx=[{}] * 1276)
        )

    def test_figure_legends_pair_with_following_figure_pages(self):
        caps = pe._legend_caption_numbers([
            "256 FIGURE LEGENDS",
            "257 Figure 1. Differences in AAV-derived FVIII activity.",
            "284 Figure 2. AAV-derived FVIIIa-SQ demonstrates expected function.",
        ])
        self.assertEqual(caps, [(False, "1"), (False, "2")])

        prof = [
            {"legend_caps": caps, "is_fig": False},
            {"legend_caps": [], "is_fig": True},
            {"legend_caps": [], "is_fig": True},
            {"legend_caps": [], "is_fig": False},
        ]
        self.assertEqual(
            pe._legend_page_pairs(prof, 0, used_pages=set()),
            [((False, "1"), 1), ((False, "2"), 2)],
        )

    def test_figure_legends_without_heading_accept_line_numbers_and_colons(self):
        caps = pe._legend_caption_numbers([
            "1093 Figure 1: Integration site density.",
            "1101 Figure 2: Properties of human integration site CIS.",
        ])
        self.assertEqual(caps, [(False, "1"), (False, "2")])

    def test_document_pairing_allows_multi_page_legends_and_intervening_tables(self):
        prof = [
            {"legend_caps": [], "has_legend_heading": False, "is_detached_fig": False}
            for _ in range(10)
        ]
        prof[5].update({"legend_caps": [(False, "1"), (False, "2")], "has_legend_heading": True})
        prof[6]["legend_caps"] = [(False, "3")]
        prof[7]["is_detached_fig"] = False  # 表格页
        prof[8]["is_detached_fig"] = True
        prof[9]["is_detached_fig"] = True
        # 只有两张图页时先配 Figure 1/2；覆盖不足由上层质量闸拦下。
        self.assertEqual(
            pe._legend_document_pairs(prof),
            [((False, "1"), 8), ((False, "2"), 9)],
        )

    def test_legend_page_region_uses_full_graphics_union(self):
        # 文末整页图没有题注边界，按密度带裁剪会把下方稀疏面板截掉；应取整页图形并集。
        region = pe._legend_page_region([
            _el(100, 80, 300, 220),
            _el(120, 650, 280, 760),
        ])
        self.assertEqual(region, (100, 80, 300, 760))

    def test_legend_page_region_trims_detached_journal_header(self):
        region = pe._legend_page_region([
            _el(0, 42, 126, 76),
            _el(500, 52, 598, 82),
            _el(48, 104, 550, 650),
        ], page_w=600, page_h=800)
        self.assertEqual(region, (0, 104.0, 598, 650))

    def test_legend_content_region_includes_axis_text_but_not_page_header(self):
        region = pe._legend_page_content_region(
            [
                _el(0, 42, 126, 76),
                _el(500, 52, 598, 82),
                _el(48, 104, 550, 650),
            ],
            [
                _el(20, 48, 180, 62),   # 独立页眉：应排除
                _el(26, 96, 140, 110),  # 主图上方标签：应保留
                _el(18, 660, 590, 676), # 底部图例：应保留
                dict(_el(588, 210, 594, 790), upright=False), # 右侧下载水印：应排除
            ],
            page_w=600,
            page_h=800,
        )
        self.assertEqual(region, (0.0, 90.0, 600, 682.0))


class TestCropCacheVersion(unittest.TestCase):
    def test_missing_or_stale_version_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "legend_figures_manifest.json"
            manifest.write_text("[]", encoding="utf-8")

            self.assertFalse(pe._manifest_version_is_current(manifest))
            pe._write_manifest_version(manifest)
            self.assertTrue(pe._manifest_version_is_current(manifest))

            pe._manifest_version_path(manifest).write_text("0", encoding="utf-8")
            self.assertFalse(pe._manifest_version_is_current(manifest))


if __name__ == "__main__":
    unittest.main()
