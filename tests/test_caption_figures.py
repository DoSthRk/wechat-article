"""题注锚定抽图：题注正则 + 框计算（纯函数，无需真 PDF）。"""
import os
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_detached_panel_titles_and_axis_labels_fit_inside_caption_boundaries(self):
        graphics = [_el(80, 90, 520, 400)]
        words = [dict(_el(80, 65, 90, 75), text='a'),
                 dict(_el(300, 63, 500, 73), text='CAR clonal evolution'),
                 dict(_el(250, 410, 350, 420), text='Late CAR T signature'),
                 dict(_el(80, 430, 520, 440), text='Fig. 5 | caption must stay out')]
        box = cf._box_for_caption(430, 445, graphics, [430], [445], self.W, self.H, words=words)
        self.assertLessEqual(box[1], 63)
        self.assertGreaterEqual(box[3], 420)
        self.assertLess(box[3], 430)

    def test_figure_above_caption_excludes_rule(self):
        gfx = [_el(100, 50, 500, 350),          # 正图大框
               _el(40, 380, 560, 380.5)]        # 全宽细规则线（应剔除）
        box = cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H)
        self.assertEqual(box, (100, 50, 500, 350))

    def test_figure_below_caption_fallback(self):
        gfx = [_el(100, 450, 500, 750)]         # 图在题注下方
        box = cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H)
        self.assertEqual(box, (100, 450, 500, 750))

    def test_detached_journal_header_is_trimmed(self):
        gfx = [
            _el(0, 42, 126, 76),                 # CellPress / OPEN ACCESS
            _el(500, 52, 598, 82),               # Immunity / Article
            _el(48, 104, 550, 501),              # 主图
        ]
        box = cf._box_for_caption(540, 555, gfx, [540], [555], self.W, self.H)
        self.assertEqual(box, (0, 104.0, 598, 501))

    def test_prev_caption_bounds_region(self):
        # 上一题注 bottom=300：题注上方只取 300 以下的图，不吃上一张图（top=100 那块）
        gfx = [_el(100, 100, 500, 250),         # 上一张图（应被上一题注隔开）
               _el(100, 320, 500, 380)]         # 本图
        box = cf._box_for_caption(400, 415, gfx, [290, 400], [300, 415], self.W, self.H)
        self.assertEqual(box, (100, 320, 500, 380))

    def test_too_small_returns_none(self):
        gfx = [_el(100, 380, 110, 390)]         # 太小
        self.assertIsNone(cf._box_for_caption(400, 415, gfx, [400], [415], self.W, self.H))


class _FakePage:
    width = 600.0
    height = 800.0
    rects = []
    curves = []
    lines = []

    def __init__(self, words, images=None):
        self._words = words
        self.images = images or []

    def extract_words(self, **_kwargs):
        return self._words


def _words(text, top):
    words = []
    x = 50.0
    for token in text.split():
        width = max(12.0, len(token) * 5.0)
        words.append({
            "text": token, "x0": x, "x1": x + width,
            "top": top, "bottom": top + 10,
        })
        x += width + 4.0
    return words


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestSelectiveCaptionParsing(unittest.TestCase):
    def test_scan_keeps_all_requested_occurrences_and_closes_resources(self):
        from unittest.mock import MagicMock
        document = MagicMock()
        texts = ['Fig. 1 | first', 'Extended Data Fig. 1 | appendix',
                 'Left-column text    Fig. 1 | another article', 'Fig. 2 | last']
        pages = [MagicMock() for _ in texts]
        document.__len__.return_value = len(pages)
        document.__getitem__.side_effect = pages.__getitem__
        for page, text in zip(pages, texts):
            page.get_textpage.return_value.get_text_bounded.return_value = text
        with patch('pypdfium2.PdfDocument', return_value=document):
            self.assertEqual(cf._caption_page_indices('source.pdf', {('1', False), ('2', False)}), {0, 2, 3})
            self.assertIsNone(cf._caption_page_indices('source.pdf', {('5', False)}))
        self.assertEqual(document.close.call_count, 2)
        for page in pages:
            self.assertEqual(page.close.call_count, 2)
            self.assertEqual(page.get_textpage.return_value.close.call_count, 2)

    def test_layout_skips_unreferenced_pages_but_keeps_duplicate_regions(self):
        first = _FakePage(_words('Fig. 1 | first', 420), images=[_el(60, 60, 540, 390)])
        second = _FakePage(_words('Fig. 1 | different article', 420), images=[_el(60, 90, 540, 390)])
        unused = _FakePage([])
        unused.extract_words = lambda **kw: self.fail('unreferenced appendix must not be parsed')
        with patch('pdfplumber.open', return_value=_FakePdf([first, unused, second])), patch.object(
                cf, '_caption_page_indices', return_value={0, 2}):
            boxes = cf.find_figure_boxes('source.pdf', required_keys={('1', False)})
        self.assertEqual([box[2] for box in boxes], [0, 2])

    def test_incomplete_fast_scan_falls_back_to_layout_without_dropping_keys(self):
        page = _FakePage(_words('Fig. 2 | results', 420), images=[_el(60, 60, 540, 390)])
        with patch('pdfplumber.open', return_value=_FakePdf([page])), patch.object(
                cf, '_caption_page_indices', return_value=None):
            boxes = cf.find_figure_boxes('source.pdf', required_keys={('2', False)})
        self.assertEqual([box[0] for box in boxes], ['2'])

    def test_partial_cache_is_not_reused_as_complete_or_for_another_key(self):
        from utils.pdf_figure_extractor import Figure
        with TemporaryDirectory() as temp:
            out = Path(temp)
            cf._write_manifest(cf._manifest(out), [Figure('1', False, '', 1, '1.jpg', 600, 400)])
            (out / '.caption_required_keys.json').write_text(json.dumps([['1', False]]))
            with patch.object(cf, 'find_figure_boxes', return_value=[]) as find:
                self.assertEqual(len(cf.extract_figures_by_caption('source.pdf', temp, required_keys={('1', False)})), 1)
                find.assert_not_called()
                cf.extract_figures_by_caption('source.pdf', temp, required_keys={('2', False)})
                find.assert_called_once()
                find.reset_mock()
                cf.extract_figures_by_caption('source.pdf', temp)
                find.assert_called_once()

    def test_worker_forwards_requested_keys_without_approving_or_publishing(self):
        from utils import figure_extract_worker as worker
        with TemporaryDirectory() as temp, patch.object(worker, '_extract', return_value=[]) as extract:
            result = Path(temp) / 'result.json'
            self.assertEqual(worker.main(['caption', 'source.pdf', temp, '', str(result),
                                          json.dumps([['1', False], ['5', False]])]), 0)
            extract.assert_called_once_with('caption', 'source.pdf', temp, None,
                                            required_keys={('1', False), ('5', False)})


class TestCrossPageCaption(unittest.TestCase):
    def test_following_figure_page_keeps_rotated_bottom_ticks_after_padding(self):
        page = _FakePage([*_words('Article', 20),
                          dict(_el(90, 690, 110, 707), text='−1,000')],
                         images=[_el(60, 60, 540, 688)])
        box = cf._following_page_figure_box(page)
        self.assertGreaterEqual(box[3], 707)

    def test_inline_figure_reference_in_right_column_is_not_a_caption(self):
        # Nature Immunology: Extended Data Fig. 5's legend contains
        # "clusters defined in Fig. 2 in the scRNAseq dataset" at x=447.
        words = [dict(word, x0=word['x0'] + 300, x1=word['x1'] + 300)
                 for word in _words('clusters defined in Fig. 2 in the scRNAseq dataset', 490)]
        page = _FakePage(words, images=[_el(300, 60, 540, 400)])
        self.assertFalse(any(cf._CAPTION_RE.match(line['text']) for line in cf._group_lines(page)))
        with patch('pdfplumber.open', return_value=_FakePdf([page])):
            self.assertEqual(cf.find_figure_boxes('paper.pdf'), [])

    def test_shaded_legend_is_not_a_figure_and_uses_previous_raster_page(self):
        previous = _FakePage(_words('A B C D', 50), images=[_el(60, 70, 535, 680)])
        caption = _FakePage(_words('Figure 1. Experimental results', 85))
        caption.rects = [_el(42, 75, 560, 275), _el(42, 270, 560, 285)]
        with patch('pdfplumber.open', return_value=_FakePdf([previous, caption])):
            boxes = cf.find_figure_boxes('paper.pdf')
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][2], 0)
        self.assertLess(boxes[0][3][1], 80)
        self.assertGreater(boxes[0][3][3], 670)

    def test_previous_article_legend_does_not_win_same_number(self):
        unrelated = _FakePage(_words('Figure 1 (facing page). Dengue trial', 65))
        unrelated.rects = [_el(40, 55, 400, 330)]
        # Left prose crosses the page midpoint, before a real right-column caption.
        caption_words = [dict(w, x0=w['x0']+280, x1=w['x1']+280)
                         for w in _words('Figure 1 (facing page). AAV', 205)]
        correct = _FakePage([{'text': 'for', 'x0': 300, 'x1': 320, 'top': 205, 'bottom': 215}] + caption_words)
        correct.rects = [_el(325, 195, 595, 680)]
        figure = _FakePage(_words('A B C', 65), images=[_el(60, 55, 535, 680)])
        with patch('pdfplumber.open', return_value=_FakePdf([unrelated, correct, figure])):
            boxes = cf.find_figure_boxes('paper.pdf')
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][2], 2)

    def test_caption_on_next_page_uses_previous_full_page_figure(self):
        previous = _FakePage(
            _words("(caption on next page)", 740),
            images=[_el(60, 55, 535, 680)],
        )
        caption = _FakePage(_words("Fig. 1. Transcriptomic analysis", 35))
        with patch("pdfplumber.open", return_value=_FakePdf([previous, caption])):
            boxes = cf.find_figure_boxes("paper.pdf")

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][0], "1")
        self.assertEqual(boxes[0][2], 0)
        self.assertEqual(boxes[0][3], (60.0, 55.0, 535.0, 680.0))

    def test_right_column_caption_sharing_a_row_with_prose(self):
        prose = _words("Previous findings continue", 220)
        caption = [dict(word, x0=word["x0"] + 300, x1=word["x1"] + 300)
                   for word in _words("Fig. 1 | Study flow", 220)]
        page = _FakePage(prose + caption, images=[_el(350, 60, 540, 190)])
        with patch("pdfplumber.open", return_value=_FakePdf([page])):
            boxes = cf.find_figure_boxes("paper.pdf")

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][0], "1")
        self.assertEqual(boxes[0][3], (350.0, 60.0, 540.0, 190.0))

    def test_right_column_legend_beside_full_height_left_figure(self):
        panel_label = _words("A", 100)
        caption = [dict(word, x0=word["x0"] + 340, x1=word["x1"] + 340)
                   for word in _words("Figure 4. Activated dendritic cells", 100)]
        page = _FakePage(panel_label + caption, images=[
            _el(60 + (i % 8), 110 + (i % 12), 370, 600)
            for i in range(250)
        ])
        with patch("pdfplumber.open", return_value=_FakePdf([page])):
            boxes = cf.find_figure_boxes("paper.pdf")

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][0], "4")
        self.assertLess(boxes[0][3][0], 100)
        self.assertGreater(boxes[0][3][2], 350)
        self.assertGreater(boxes[0][3][3], 590)

    def test_bottom_caption_uses_graphics_on_following_page(self):
        caption = _FakePage(_words("Fig. 4 | Immune-cell analysis", 700))
        following = _FakePage([], images=[
            _el(60 + (i % 10), 130 + (i % 20), 520, 680)
            for i in range(250)
        ])
        with patch("pdfplumber.open", return_value=_FakePdf([caption, following])):
            boxes = cf.find_figure_boxes("paper.pdf")

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][0], "4")
        self.assertEqual(boxes[0][2], 1)


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
