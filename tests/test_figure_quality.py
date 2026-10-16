import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from utils.figure_quality import filter_candidates, prose_lines, text_band_image
from utils.pdf_figure_extractor import Figure


class Page:
    width, height = 600, 800
    def __init__(self, words=()):
        self.words = words
    def extract_words(self):
        return self.words


class Pdf:
    def __init__(self, page):
        self.pages = [page]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class FigureQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf = self.root / 'source.pdf'
        self.pdf.write_bytes(b'PDF source identity')
        self.image = self.root / 'figure.png'
        image = Image.new('RGB', (400, 400), 'white')
        ImageDraw.Draw(image).ellipse((30, 30, 370, 370), fill='blue')
        image.save(self.image)
        self.figure = Figure('1', False, '', 1, str(self.image), 400, 400, (50, 50, 450, 450))
    def tearDown(self):
        self.temp.cleanup()
    def check(self, figures=None, words=()):
        with patch('pdfplumber.open', return_value=Pdf(Page(words))):
            return filter_candidates(self.pdf, figures or [self.figure], 'caption')
    def test_valid_candidate_keeps_geometry_and_source_identity(self):
        good, rejected = self.check()
        self.assertEqual(rejected, [])
        self.assertEqual(good[0].source_sha256, hashlib.sha256(self.pdf.read_bytes()).hexdigest())
    def test_missing_geometry_stale_cache_rejected(self):
        self.figure.bbox = None
        self.assertEqual(self.check()[1][0]['reason'], 'missing_crop_provenance')
    def test_same_number_from_multiple_regions_is_not_first_wins(self):
        other = Figure('1', False, '', 1, str(self.image), 400, 400, (100, 100, 500, 500))
        good, rejected = self.check([self.figure, other])
        self.assertEqual(good, [])
        self.assertTrue(all(r['reason'] == 'ambiguous_figure_number_across_regions' for r in rejected))
    def test_source_change_rejected(self):
        self.figure.source_sha256 = 'other'
        self.assertEqual(self.check()[1][0]['reason'], 'source_pdf_changed')
    def test_same_bytes_for_different_figure_numbers_rejected(self):
        other = Figure('2', False, '', 1, str(self.image), 400, 400, (50, 50, 450, 450))
        good, rejected = self.check([self.figure, other])
        self.assertEqual(good, [])
        self.assertEqual(len(rejected), 2)
    def test_dense_legend_words_rejected_but_short_axis_labels_not(self):
        words = [{'text': 'continuous-caption-' * 7, 'x0': 60, 'x1': 440,
                  'top': 70 + i*15, 'bottom': 80 + i*15} for i in range(8)]
        self.assertEqual(self.check(words=words)[1][0]['reason'], 'caption_or_body_text_in_crop')
        for word in words:
            word['text'] = 'AAV control'
        self.assertEqual(self.check(words=words)[1], [])
    def test_raster_text_lines_rejected_without_ocr_dependency(self):
        image = Image.new('RGB', (400, 300), 'white')
        draw = ImageDraw.Draw(image)
        for y in range(5, 280, 16):
            draw.text((5, y), 'Caption words and more words repeated across a line',
                      font=ImageFont.load_default(size=12), fill='black')
        self.assertTrue(text_band_image(image))
        image.save(self.image)
        self.assertEqual(self.check()[1][0]['reason'], 'raster_text_or_caption_only')


if __name__ == '__main__':
    unittest.main()
