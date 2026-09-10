"""Shared geometry cleanup for detached journal headers and footers."""
import unittest

from utils.figure_crop_geometry import trim_detached_edge_bands


def _el(x0, top, x1, bottom):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom}


class TestTrimDetachedEdgeBands(unittest.TestCase):
    W, H = 600.0, 800.0

    def test_trims_detached_top_journal_band(self):
        elements = [
            _el(0, 42, 126, 76),       # CellPress / OPEN ACCESS
            _el(500, 52, 598, 82),     # journal / article label
            _el(48, 104, 550, 501),    # scientific figure
        ]

        box = trim_detached_edge_bands(
            elements, (0, 42, 598, 501), self.W, self.H,
        )

        self.assertEqual(box, (0, 104.0, 598, 501))

    def test_trims_detached_bottom_journal_band(self):
        elements = [
            _el(48, 180, 550, 650),    # scientific figure
            _el(20, 724, 180, 750),    # detached publisher footer
            _el(520, 724, 580, 750),   # page/article label
        ]

        box = trim_detached_edge_bands(
            elements, (20, 180, 580, 750), self.W, self.H,
        )

        self.assertEqual(box, (20, 180, 580, 650.0))

    def test_keeps_top_figure_without_detached_header(self):
        elements = [
            _el(50, 50, 550, 220),
            _el(50, 220, 550, 500),
        ]

        box = trim_detached_edge_bands(
            elements, (50, 50, 550, 500), self.W, self.H,
        )

        self.assertEqual(box, (50, 50, 550, 500))

    def test_keeps_internal_panel_gap(self):
        elements = [
            _el(50, 220, 550, 270),
            _el(50, 310, 550, 650),
        ]

        box = trim_detached_edge_bands(
            elements, (50, 220, 550, 650), self.W, self.H,
        )

        self.assertEqual(box, (50, 220, 550, 650))

    def test_keeps_small_bottom_panel_outside_footer_margin(self):
        elements = [
            _el(50, 180, 550, 620),
            _el(50, 660, 550, 705),
        ]

        box = trim_detached_edge_bands(
            elements, (50, 180, 550, 705), self.W, self.H,
        )

        self.assertEqual(box, (50, 180, 550, 705))


if __name__ == "__main__":
    unittest.main()


class TestPublisherAwareCrop(unittest.TestCase):
    def test_real_cellpress_stacked_citation_and_logo(self):
        import json
        from pathlib import Path
        from utils.figure_crop_geometry import sanitize_figure_region
        data = json.loads((Path(__file__).parent / 'fixtures/cellpress_stacked_header.json').read_text())
        box = sanitize_figure_region(data['elements'], data['words'], data['original_crop'], *data['size'])
        scientific_image = next(e for e in data['elements'] if e['object_type'] == 'image')
        self.assertGreater(box[1], 95)
        self.assertLess(box[0], scientific_image['x0'])
        self.assertGreater(box[2], scientific_image['x1'])
        self.assertLessEqual(box[1], scientific_image['top'])
        self.assertGreaterEqual(box[3], scientific_image['bottom'])
        self.assertLess(box[2] - box[0], 420)

    def test_small_top_scientific_panel_is_not_a_publisher_header(self):
        from utils.figure_crop_geometry import sanitize_figure_region
        elements = [_el(60, 54, 530, 75), _el(60, 114, 530, 700)]
        original = (60, 54, 530, 700)
        self.assertEqual(sanitize_figure_region(elements, [], original, 600, 800), original)

    def test_keeps_panel_letters_above_graphics_and_y_axis_label(self):
        from utils.figure_crop_geometry import sanitize_figure_region
        words = [dict(_el(50, 65, 140, 78), text='OPEN ACCESS'),
                 dict(_el(70, 94, 80, 104), text='A'),
                 dict(_el(77, 140, 90, 180), text='Expression')]
        elements = [_el(50, 50, 140, 70), _el(100, 105, 530, 700)]
        box = sanitize_figure_region(elements, words, (50, 50, 530, 700), 600, 800)
        self.assertLessEqual(box[0], 70)
        self.assertLessEqual(box[1], 94)
        self.assertGreater(box[1], 78)
        self.assertGreaterEqual(box[3], 700)

    def test_does_not_slice_a_raster_containing_header_and_science(self):
        from utils.figure_crop_geometry import sanitize_figure_region, UnsafeFigureCrop
        words = [dict(_el(50, 65, 140, 78), text='OPEN ACCESS')]
        raster = dict(_el(0, 0, 600, 800), object_type='image')
        with self.assertRaises(UnsafeFigureCrop):
            sanitize_figure_region([raster], words, (0, 0, 600, 800), 600, 800)

    def test_open_access_inside_scientific_diagram_is_not_header(self):
        from utils.figure_crop_geometry import sanitize_figure_region
        words = [dict(_el(70, 210, 150, 230), text='OPEN ACCESS')]
        elements = [_el(60, 50, 530, 700)]
        original = (60, 50, 530, 700)
        self.assertEqual(sanitize_figure_region(elements, words, original, 600, 800), original)

    def test_publisher_words_in_sentence_do_not_match_article_label(self):
        from utils.figure_crop_geometry import publisher_header_bottom
        self.assertEqual(publisher_header_bottom([dict(_el(50, 60, 200, 70), text='Article selection workflow')], 800), 0)


class TestOutlinedPublisherBanners(unittest.TestCase):
    def make_page(self, top=26):
        from types import SimpleNamespace
        return SimpleNamespace(
            width=600, height=800,
            rects=[_el(75, top, 535, top + 15)],
            curves=[_el(250 + i * 7, top + 3, 255 + i * 7, top + 12) for i in range(12)],
            extract_words=lambda: [],
        )

    def test_requires_three_repeated_outlined_banners(self):
        from types import SimpleNamespace
        from utils.figure_crop_geometry import figure_crop_words, publisher_header_bottom
        pages = [self.make_page() for _ in range(3)]
        pdf = SimpleNamespace(pages=pages)
        for p in pages:
            p.pdf = pdf
        self.assertEqual(publisher_header_bottom(figure_crop_words(pages[0]), 800), 41)
        self.assertEqual(publisher_header_bottom(figure_crop_words(pages[1]), 800), 41)
        fewer = pages[:2]
        pdf2 = SimpleNamespace(pages=fewer)
        for p in fewer:
            p.pdf = pdf2
        self.assertEqual(figure_crop_words(fewer[0]), [])

    def test_does_not_treat_scientific_legend_as_margin_banner(self):
        from types import SimpleNamespace
        from utils.figure_crop_geometry import figure_crop_words
        pages = [self.make_page(54) for _ in range(3)]
        for p in pages:
            p.pdf = SimpleNamespace(pages=pages)
        self.assertEqual(figure_crop_words(pages[0]), [])
