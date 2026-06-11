"""从 PDF 抽取插图候选 —— 渲染裁剪法（pypdfium2 渲染 + pdfplumber 定位）。

策略：pdfplumber 找页内较大的内嵌图（按像素边长过滤图标 / logo）和「Figure N」题注；
pypdfium2 把该页渲染成高 DPI 位图，按图的版面 bbox 裁剪成 PNG。产出
``figures_manifest.json``：每条 ``{label, caption, page, image_path, width, height}``，
供正文占位符 ``[图片:Figure N …]`` 按图号 label 匹配。

注：复杂 / 合订本 PDF 抽取不保证准确，manifest 可人工校正。渲染裁剪对位图 /
矢量 / 拼版图都通用（不依赖能否解出内嵌图字节）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber
import pypdfium2 as pdfium

from utils.logger import setup_logger

logger = setup_logger("pdf_figure_extractor")

_CAP_RE = re.compile(r"^(?:Fig(?:ure)?\.?\s*)(\d+)", re.I)
_MIN_DIM = 200      # 内嵌图最小像素边长（过滤图标 / logo / 修饰小图）
_DPI = 200


@dataclass
class Figure:
    label: str          # 图号，如 "1" / "3"（题注解析得；无题注则空）
    caption: str
    page: int           # 1-based
    image_path: str
    width: int
    height: int


def _caption_lines(page) -> List[Tuple[str, float]]:
    """返回 [(caption_text, top)]，只取以「Figure N」开头的行（题注，非正文引用）。"""
    out: List[Tuple[str, float]] = []
    try:
        for ln in page.extract_text_lines():
            text = (ln.get("text") or "").strip()
            if _CAP_RE.match(text):
                out.append((text, float(ln.get("top", 0))))
    except Exception:  # noqa: BLE001 - 题注解析失败不致命
        pass
    return out


def extract_figures(
    pdf_path: str, out_dir: str, *,
    max_pages: Optional[int] = None, min_dim: int = _MIN_DIM, dpi: int = _DPI,
) -> List[Figure]:
    """抽取候选图到 out_dir，返回 Figure 列表并写 figures_manifest.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    figures: List[Figure] = []

    doc = pdfium.PdfDocument(pdf_path)
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            limit = min(max_pages, total) if max_pages else total
            for i in range(limit):
                page = pdf.pages[i]
                big = [
                    im for im in (page.images or [])
                    if (im.get("width") or 0) >= min_dim and (im.get("height") or 0) >= min_dim
                ]
                if not big:
                    continue
                caps = _caption_lines(page)
                rendered = doc[i].render(scale=scale).to_pil()
                for k, im in enumerate(big):
                    x0, top, x1, bottom = float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"])
                    box = (int(x0 * scale), int(top * scale), int(x1 * scale), int(bottom * scale))
                    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
                        continue
                    crop = rendered.crop(box)
                    label, caption = _nearest_caption(caps, top, bottom)
                    fname = f"p{i + 1}_{k + 1}.png"
                    crop.save(str(out / fname))
                    figures.append(Figure(
                        label=label, caption=caption, page=i + 1,
                        image_path=str(out / fname), width=crop.width, height=crop.height,
                    ))
    finally:
        doc.close()

    (out / "figures_manifest.json").write_text(
        json.dumps([asdict(f) for f in figures], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("extracted %d figure(s) from %s -> %s", len(figures), Path(pdf_path).name, out)
    return figures


def _nearest_caption(caps: List[Tuple[str, float]], img_top: float, img_bottom: float) -> Tuple[str, str]:
    """题注通常在图下方：选 top 最接近图底边的题注；解析图号。"""
    if not caps:
        return "", ""
    below = [(t, ct) for (t, ct) in caps if ct >= img_top - 5]
    pool = below or caps
    cap_text, _ = min(pool, key=lambda x: abs(x[1] - img_bottom))
    m = _CAP_RE.match(cap_text)
    return (m.group(1) if m else ""), cap_text[:120]


def figure_number(text: str) -> str:
    """从占位符描述或题注里取图号（'Figure 1e' / 'Fig. 3' → '1' / '3'）。"""
    m = _CAP_RE.match((text or "").strip())
    return m.group(1) if m else ""
