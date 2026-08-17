"""PDF 抽图策略：快速信号判断与覆盖率优先的回退顺序。"""
import unittest

from utils.figure_strategy import FigureSignals, _DETACHED_LEGEND_CAPTION_RE, planned_figure_strategies


class TestFigureStrategy(unittest.TestCase):
    def test_line_numbered_colon_legend_is_detectable(self):
        match = _DETACHED_LEGEND_CAPTION_RE.match(
            "1093 Figure 1: Integration site density."
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(2), "1")

    def test_caption_pdf_skips_legend_and_uses_caption_first(self):
        signals = FigureSignals(
            caption_keys=frozenset({("1", False), ("2", False)}),
            has_legend_section=False,
            page_count=12,
        )

        self.assertEqual(
            planned_figure_strategies(signals, vision_available=True),
            ("caption", "vision", "heuristic"),
        )

    def test_legend_pdf_skips_caption_and_starts_with_legend(self):
        signals = FigureSignals(
            caption_keys=frozenset(),
            has_legend_section=True,
            page_count=12,
        )

        self.assertEqual(
            planned_figure_strategies(signals, vision_available=False),
            ("legend", "heuristic"),
        )

    def test_unreadable_signal_keeps_all_deterministic_fallbacks(self):
        signals = FigureSignals(
            caption_keys=frozenset(),
            has_legend_section=False,
            page_count=0,
            scan_error="text extraction failed",
        )

        self.assertEqual(
            planned_figure_strategies(signals, vision_available=True),
            ("caption", "legend", "vision", "heuristic"),
        )
