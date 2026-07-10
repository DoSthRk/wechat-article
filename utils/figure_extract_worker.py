"""Subprocess entrypoint for PDF figure extraction.

The panel runner invokes this module with a timeout so problematic PDFs cannot
hang the main generation/distribution process.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _parse_max_pages(raw: str) -> Optional[int]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract(kind: str, pdf_path: str, out_dir: str, max_pages: Optional[int]):
    if kind == "caption":
        from utils.caption_figures import extract_figures_by_caption

        return extract_figures_by_caption(pdf_path, out_dir, max_pages=max_pages)
    if kind == "legend":
        from utils.pdf_figure_extractor import extract_figures_from_legend_pages

        return extract_figures_from_legend_pages(pdf_path, out_dir, max_pages=max_pages)
    if kind == "vision":
        from utils.vision_figures import extract_figures_via_vision

        return extract_figures_via_vision(pdf_path, out_dir, max_pages=max_pages)
    if kind == "heuristic":
        from utils.pdf_figure_extractor import extract_figures

        return extract_figures(pdf_path, out_dir, max_pages=max_pages)
    raise ValueError(f"unknown extractor kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 5:
        print(
            "usage: python -m utils.figure_extract_worker "
            "<caption|legend|vision|heuristic> <pdf> <out_dir> <max_pages> <result_json>",
            file=sys.stderr,
        )
        return 2
    kind, pdf_path, out_dir, max_pages_raw, result_json = argv
    load_dotenv()
    figs = _extract(kind, pdf_path, out_dir, _parse_max_pages(max_pages_raw))
    result_path = Path(result_json)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps([asdict(fig) for fig in figs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
