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
