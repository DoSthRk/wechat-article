"""PDF 配图策略的轻量信号扫描与覆盖率工具。

这里不做图像裁剪。它只用 PDFium 的文本层快速判断哪类抽图器值得启动，
避免对同一份 PDF 无差别地重复执行四次完整解析。
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple

from utils.pdf_figure_extractor import Figure, figure_number

FigureKey = Tuple[str, bool]
_CAPTION_RE = re.compile(
    r"^(extended\s+data\s+fig(?:ure)?|sup(?:plementary|pl)?\.?\s+fig(?:ure)?|fig(?:ure)?|图)"
    r"\.?\s*([0-9]+)(?=[\s\.\:\|、。]|$)",
    re.IGNORECASE,
)
_LEGEND_HEADING_RE = re.compile(r"\bFIGURE\s+LEGENDS?\b", re.IGNORECASE)
_DETACHED_LEGEND_CAPTION_RE = re.compile(
    r"^(?:\d+\s+)?(extended\s+data\s+)?fig(?:ure)?\.?\s*([0-9]+)"
    r"(?=[\s\.\:\|、。：]|$)",
    re.IGNORECASE,
)
_EXTENDED_RE = re.compile(r"extended\s*data|supplementary|supp\.?|附录|扩展数据", re.IGNORECASE)


@dataclass(frozen=True)
class FigureSignals:
    caption_keys: frozenset[FigureKey]
    has_legend_section: bool
    page_count: int
    scan_error: str = ""


def figure_key_from_description(text: str) -> FigureKey | None:
    """把文章占位符的 Figure 描述标准化为 ``(图号, 是否扩展图)``。"""
    number = figure_number(text)
    if not number:
        return None
    return number, bool(_EXTENDED_RE.search(text or ""))


def figure_key_from_figure(figure: Figure) -> FigureKey:
    return str(figure.label), bool(figure.is_extended)


def missing_figure_keys(required: Iterable[FigureKey], figures: Iterable[Figure]) -> set[FigureKey]:
    return set(required) - {figure_key_from_figure(figure) for figure in figures if figure.label}


def merge_preferred_figures(preferred: Iterable[Figure], fallback: Iterable[Figure]) -> list[Figure]:
    """合并两组图，同一图号保持前一策略结果，后续策略只补缺失图。"""
    merged: list[Figure] = []
    seen: set[FigureKey] = set()
    for figure in [*preferred, *fallback]:
        if not figure.label:
            continue
        key = figure_key_from_figure(figure)
        if key in seen:
            continue
        merged.append(figure)
        seen.add(key)
    return merged


def planned_figure_strategies(signals: FigureSignals, vision_available: bool) -> tuple[str, ...]:
    """根据快速文本信号返回值得尝试的抽图器顺序。"""
    if signals.scan_error:
        deterministic = ["caption", "legend"]
    else:
        deterministic = []
        if signals.caption_keys:
            deterministic.append("caption")
        if signals.has_legend_section:
            deterministic.append("legend")

    strategies = list(deterministic)
    if vision_available:
        strategies.append("vision")
    if deterministic or signals.scan_error:
        strategies.append("heuristic")
    return tuple(strategies)


def inspect_pdf_figure_signals(pdf_path: str, max_pages: int | None = None) -> FigureSignals:
    """快速扫描 PDF 文本层，提取题注/文末图例信号而不解析图形元素。"""
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(Path(pdf_path)))
        try:
            limit = min(len(document), max_pages) if max_pages else len(document)
            caption_keys: set[FigureKey] = set()
            has_legend_section = False
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for index in range(limit):
                    text = document[index].get_textpage().get_text_bounded() or ""
                    if _LEGEND_HEADING_RE.search(text):
                        has_legend_section = True
                    for line in text.splitlines():
                        stripped = line.strip()
                        match = _CAPTION_RE.match(stripped)
                        if match:
                            caption_keys.add((match.group(2), bool(_EXTENDED_RE.search(match.group(1)))))
                        if _DETACHED_LEGEND_CAPTION_RE.match(stripped):
                            has_legend_section = True
            return FigureSignals(frozenset(caption_keys), has_legend_section, limit)
        finally:
            document.close()
    except Exception as exc:  # noqa: BLE001 - 未知 PDF 格式时交由完整回退链处理
        return FigureSignals(frozenset(), False, 0, str(exc))
