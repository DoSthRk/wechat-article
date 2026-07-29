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
