"""Shared PDF geometry cleanup for scientific figure crop regions."""
from __future__ import annotations

from typing import Iterable, Mapping, Tuple

Box = Tuple[float, float, float, float]

_BIN_PT = 4.0
_MAX_TOP_BAND_PT = 60.0
_MAX_BOTTOM_BAND_PT = 40.0
_MIN_DETACHED_GAP_PT = 18.0
_TOP_PAGE_FRAC = 0.20
_BOTTOM_PAGE_FRAC = 0.12
_RULE_SPAN_FRAC = 0.70
_RULE_THICK_PT = 3.0


def is_rule_line(element: Mapping, page_w: float, page_h: float) -> bool:
    """Return whether an element is a long, thin page rule."""
    width = float(element["x1"]) - float(element["x0"])
    height = float(element["bottom"]) - float(element["top"])
    horizontal = width >= _RULE_SPAN_FRAC * page_w and height <= _RULE_THICK_PT
    vertical = height >= _RULE_SPAN_FRAC * page_h and width <= _RULE_THICK_PT
    return horizontal or vertical


def trim_detached_edge_bands(
    elements: Iterable[Mapping],
    region: Box,
    page_w: float,
    page_h: float,
) -> Box:
    """Trim short detached publisher bands from the outer page edges.

    A band is removed only when it is short, lies in the outer 20% of the
    page, and is separated from the remaining figure graphics by at least
    18 points of whitespace.
    """
    x0, top, x1, bottom = region
    clean = []
    for element in elements:
        element_top = float(element.get("top", 0))
        element_bottom = float(element.get("bottom", 0))
        element_x0 = float(element.get("x0", 0))
        element_x1 = float(element.get("x1", 0))
        if element_bottom <= top or element_top >= bottom:
            continue
        if element_x1 <= x0 or element_x0 >= x1:
            continue
        if is_rule_line(element, page_w, page_h):
            continue
        clean.append((
            max(top, element_top),
            min(bottom, element_bottom),
        ))
    if not clean:
        return region

    bin_count = int(page_h // _BIN_PT) + 2
    occupied = [False] * bin_count
    for element_top, element_bottom in clean:
        start = max(0, int(element_top // _BIN_PT))
        end = min(bin_count - 1, int(element_bottom // _BIN_PT))
        for index in range(start, end + 1):
            occupied[index] = True

    start_bin = max(0, int(top // _BIN_PT))
    end_bin = min(bin_count - 1, int(bottom // _BIN_PT))
    runs = []
    index = start_bin
    while index <= end_bin:
        while index <= end_bin and not occupied[index]:
            index += 1
        if index > end_bin:
            break
        run_start = index
        while index <= end_bin and occupied[index]:
            index += 1
        runs.append((run_start, index))

    if len(runs) < 2:
        return region

    first_start, first_end = runs[0]
    second_start, _ = runs[1]
    first_height = (first_end - first_start) * _BIN_PT
    top_gap = (second_start - first_end) * _BIN_PT
    if (
        first_start * _BIN_PT < _TOP_PAGE_FRAC * page_h
        and first_height < _MAX_TOP_BAND_PT
        and top_gap >= _MIN_DETACHED_GAP_PT
    ):
        next_run_top = second_start * _BIN_PT
        top = min(
            element_top for element_top, element_bottom in clean
            if element_bottom >= next_run_top
        )

    previous_start, previous_end = runs[-2]
    last_start, last_end = runs[-1]
    last_height = (last_end - last_start) * _BIN_PT
    bottom_gap = (last_start - previous_end) * _BIN_PT
    if (
        last_start * _BIN_PT > (1.0 - _BOTTOM_PAGE_FRAC) * page_h
        and last_height < _MAX_BOTTOM_BAND_PT
        and bottom_gap >= _MIN_DETACHED_GAP_PT
    ):
        previous_run_bottom = previous_end * _BIN_PT
        bottom = max(
            element_bottom for element_top, element_bottom in clean
            if element_top < previous_run_bottom and element_bottom <= previous_run_bottom
        )

    return x0, top, x1, bottom


class UnsafeFigureCrop(ValueError):
    """Publisher material cannot be separated without cutting figure content."""


def publisher_header_bottom(words: Iterable[Mapping], page_h: float) -> float:
    """Locate explicit publisher furniture, not arbitrary short scientific panels.

    PDF logos often consist of vector outlines, so their name is not searchable.
    The adjacent publication labels and citation notice remain real PDF text.
    Limit these anchors to the page margin and require whole lines for generic
    labels (an occurrence of 'Article' inside a figure is not sufficient).
    """
    import re

    lines = []
    for word in sorted(words, key=lambda w: (float(w['top']), float(w['x0']))):
        if float(word['bottom']) > min(120.0, page_h * 0.2):
            continue
        if not lines or abs(float(word['top']) - float(lines[-1][0]['top'])) > 3:
            lines.append([])
        lines[-1].append(word)
    end = 0.0
    for line in lines:
        text = ' '.join(str(w.get('text', '')) for w in sorted(line, key=lambda w: float(w['x0'])))
        if (re.search(r'\b(?:open\s+access|cell\s*press|journal\s+pre-proof|please\s+cite\s+this\s+article|J\s+ALLERGY\s+CLIN\s+IMMUNOL)\b', text, re.I)
                or re.match(r'^(?:Letter|Article)\s+https?://doi\.org/', text, re.I)
                or re.fullmatch(r'(?:ll\s+)?(?:article|correction|letter)(?:\s+open\s+access)?', text.strip(), re.I)):
            end = max(end, max(float(w['bottom']) for w in line))
    return end


def figure_crop_words(page) -> list:
    """Text plus evidence for repeated vector-only publication banners.

    Pre-proof PDFs outline even the banner letters. Recognize these by an
    identical, wide, short margin rectangle AND identical enclosed outlines on
    three pages. Never infer a banner from whitespace or its height alone.
    Sampling stops after three matches and is memoized for the open document.
    """
    words = page.extract_words() or []
    pdf = getattr(page, 'pdf', None)
    if pdf is None:
        return words

    def signatures(pg):
        def white(color):
            if isinstance(color, (int, float)):
                return color >= .98
            if not isinstance(color, (tuple, list)):
                return False
            if len(color) == 4:  # CMYK
                return all(float(c) <= .02 for c in color)
            return bool(color) and all(float(c) >= .98 for c in color)

        found = {}
        for rect in pg.rects or []:
            x0, t, x1, b = (float(rect[k]) for k in ('x0', 'top', 'x1', 'bottom'))
            if not (0 <= t < 45 and 3 < b - t < 25 and x1 - x0 > float(pg.width) * .65):
                continue
            outlines = [e for e in (pg.curves or [])
                        if e['x0'] >= x0 and e['x1'] <= x1 and e['top'] >= t and e['bottom'] <= b]
            if len(outlines) < 8:
                continue
            # White outlined letters on a filled banner are publisher chrome.
            # A repeated experimental key with dark text is still figure data.
            if not rect.get('fill') or not all(white(e.get('non_stroking_color')) for e in outlines):
                continue
            signature = tuple((round(float(e[k]), 2) for e in [rect] + outlines
                               for k in ('x0', 'top', 'x1', 'bottom')))
            found[signature] = rect
        return found

    candidates = signatures(page)
    if not candidates:
        return words
    cache = getattr(pdf, '_figure_banner_evidence', None)
    if cache is None:
        cache = {}
        pdf._figure_banner_evidence = cache
    for signature, rect in candidates.items():
        if not cache.get(signature):
            matches = 0
            index = max(0, int(getattr(page, 'page_number', 1)) - 1)
            # Figure appendices can use a different banner than the manuscript.
            nearby = pdf.pages[max(0, index - 4):index + 5]
            samples = list({id(p): p for p in nearby + pdf.pages[:10]}.values())
            for other in samples:
                if signature in signatures(other):
                    matches += 1
                if matches >= 3:
                    break
            cache[signature] = matches >= 3
        if cache[signature]:
            words = words + [dict(rect, text='Journal Pre-proof', crop_evidence='repeated_vector_banner')]
    return words


def sanitize_figure_region(elements: Iterable[Mapping], words: Iterable[Mapping],
                           region: Box, page_w: float, page_h: float) -> Box:
    """Remove identified page headers as whole objects, then refit all four edges.

    Unlike the old first-gap heuristic this handles stacked citation/logo bands.
    Never slice an object crossing the header boundary; an inseparable raster
    must be reviewed instead. Include nearby PDF text to protect panel letters,
    axes and legends. No pixels are erased or synthesized.
    """
    elements, words = list(elements), list(words)
    header = publisher_header_bottom(words, page_h)
    x0, top, x1, bottom = region
    if not header:
        return region

    # Include outlines/boxes surrounding the identified text. A scientific image
    # is deliberately excluded from this expansion, as are tall figure objects.
    for _ in range(3):
        expanded = max([header] + [float(e['bottom']) for e in elements
            if float(e['top']) < header and float(e['bottom']) <= header + 12
            and float(e['bottom']) - float(e['top']) <= 60
            and e.get('object_type') != 'image'])
        if expanded == header:
            break
        header = expanded

    kept = []
    for e in elements:
        ex0, et, ex1, eb = (float(e[k]) for k in ('x0', 'top', 'x1', 'bottom'))
        if eb <= header or et >= bottom or ex1 < x0 or ex0 > x1:
            continue
        if is_rule_line(e, page_w, page_h):
            continue
        if et < header < eb:
            raise UnsafeFigureCrop('Publisher header overlaps figure graphics; manual crop review required')
        if eb > top:
            kept.append(e)
    if not kept:
        raise UnsafeFigureCrop('No scientific graphics remain below publisher header')
    new_top = min(float(e['top']) for e in kept)
    # Text outside the graphics union can be the panel label or y-axis title.
    # Only include the figure band; caption text below the input box stays out.
    content = kept + [w for w in words
        if float(w['top']) > header + 1
        and float(w['bottom']) >= new_top - 12
        and float(w['top']) < bottom and float(w['bottom']) <= bottom + 2
        and float(w['x1']) >= x0 - 24 and float(w['x0']) <= x1 + 24]
    pad = 2.0
    result = (max(0.0, min(float(e['x0']) for e in content) - pad),
              max(header + 1, min(float(e['top']) for e in content) - pad),
              min(page_w, max(float(e['x1']) for e in content) + pad),
              min(page_h, bottom + pad, max(float(e['bottom']) for e in content) + pad))
    if result[3] - result[1] < 40 or result[2] - result[0] < 40:
        raise UnsafeFigureCrop('Scientific crop too small after publisher header removal')
    return result
