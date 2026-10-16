"""Shared, fail-closed acceptance of extracted scientific figures.

This is a conservative deterministic screen, not a promise of semantic OCR.
Keep the source page and crop coordinates so rejected/ambiguous results can be
reviewed instead of silently publishing a page or a legend as a figure.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

QUALITY_VERSION = 1


def prose_lines(words, bbox):
    """Count long continuous prose lines; retain short panel/axis labels."""
    x0, y0, x1, y1 = bbox
    rows = []
    for word in sorted(words, key=lambda w: (float(w['top']), float(w['x0']))):
        if not (word['x0'] >= x0 - 1 and word['x1'] <= x1 + 1
                and y0 <= word['top'] and word['bottom'] <= y1 + 1):
            continue
        if not rows or abs(rows[-1][0]['top'] - word['top']) > 3:
            rows.append([])
        rows[-1].append(word)
    count = 0
    for row in rows:
        chars = sum(len(w.get('text', '')) for w in row)
        span = max(w['x1'] for w in row) - min(w['x0'] for w in row)
        # Long sequences with wide gutters are chart panels, not prose lines.
        ordered = sorted(row, key=lambda w: w['x0'])
        max_gap = max((b['x0'] - a['x1'] for a, b in zip(ordered, ordered[1:])), default=0)
        if chars >= 45 and span >= (x1 - x0) * .70 and max_gap < 20:
            count += 1
    return count


def text_band_image(image):
    """Detect rasterized prose: many evenly spaced, short horizontal ink bands.

    Axis lines, plots, photos and flow diagrams normally connect bands vertically.
    Conservative rejection leaves unusual dense text-only diagrams for review.
    Uses Pillow only, including on production hosts without an OCR runtime.
    """
    small = image.convert('L')
    small.thumbnail((700, 1000))
    width, height = small.size
    if width < 100 or height < 80:
        return False
    pixels = small.load()
    ink = [sum(pixels[x, y] < 130 for x in range(width)) for y in range(height)]
    bands = []
    start = None
    for y, n in enumerate(ink + [0]):
        if n >= width * .025:
            if start is None:
                start = y
        elif start is not None:
            bands.append((start, y))
            start = None
    meaningful = [(a, b) for a, b in bands if b - a >= 2]
    if len(meaningful) < 8:
        return False
    heights = sorted(b - a for a, b in meaningful)
    median = heights[len(heights) // 2]
    regular = sum(.5 * median <= h <= 1.7 * median for h in heights)
    coverage = sum(heights) / height
    return (median <= 20 and max(heights) < median * 2.8
            and regular / len(heights) >= .85 and coverage >= .38)


def filter_candidates(pdf_path, figures, strategy):
    """Return accepted figures and explicit rejected reasons, without fallback."""
    import pdfplumber

    accepted, rejected = [], []
    source = Path(pdf_path)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with pdfplumber.open(source) as pdf:
        for figure in figures:
            reason = ''
            try:
                if figure.source_sha256 and figure.source_sha256 != source_hash:
                    raise ValueError('source_pdf_changed')
                with Image.open(figure.image_path) as image:
                    image.load()
                    if min(image.size) < 60:
                        raise ValueError('image_too_small')
                    if text_band_image(image):
                        raise ValueError('raster_text_or_caption_only')
                    figure.width, figure.height = image.size
                if strategy != 'pool':
                    if not figure.bbox or len(figure.bbox) != 4 or not 1 <= figure.page <= len(pdf.pages):
                        raise ValueError('missing_crop_provenance')
                    page = pdf.pages[figure.page - 1]
                    x0, y0, x1, y1 = map(float, figure.bbox)
                    if not (0 <= x0 < x1 <= float(page.width) + 1
                            and 0 <= y0 < y1 <= float(page.height) + 1):
                        raise ValueError('invalid_crop_bounds')
                    if (x1 - x0) * (y1 - y0) > page.width * page.height * .94:
                        raise ValueError('full_page_fallback')
                    if prose_lines(page.extract_words() or [], (x0, y0, x1, y1)) >= 5:
                        raise ValueError('caption_or_body_text_in_crop')
                    if abs(figure.width / figure.height - (x1 - x0) / (y1 - y0)) > .04:
                        raise ValueError('crop_geometry_mismatch')
                figure.source_sha256 = source_hash
                accepted.append(figure)
            except (OSError, ValueError, TypeError) as exc:
                reason = str(exc)
            if reason:
                rejected.append({'label': figure.label, 'extended': figure.is_extended,
                                 'page': figure.page, 'path': figure.image_path, 'reason': reason,
                                 'quality_version': QUALITY_VERSION})
    # Multiple real pages sharing Figure 1 can be different articles in one PDF.
    # Do not silently accept the first page/number encountered.
    by_key = {}
    for figure in accepted:
        by_key.setdefault((figure.label, figure.is_extended), []).append(figure)
    for group in by_key.values():
        if len({(f.page, tuple(f.bbox or ())) for f in group}) > 1:
            for figure in group:
                accepted.remove(figure)
                rejected.append({'label': figure.label, 'extended': figure.is_extended,
                                 'reason': 'ambiguous_figure_number_across_regions',
                                 'quality_version': QUALITY_VERSION})
    # Never count the same bytes as two independent scientific figures.
    hashes = {}
    for figure in accepted:
        digest = hashlib.sha256(Path(figure.image_path).read_bytes()).hexdigest()
        hashes.setdefault(digest, []).append(figure)
    for group in hashes.values():
        if len({(f.label, f.is_extended) for f in group}) > 1:
            for figure in group:
                accepted.remove(figure)
                rejected.append({'label': figure.label, 'reason': 'duplicate_image_for_different_figures',
                                 'quality_version': QUALITY_VERSION})
    return accepted, rejected
