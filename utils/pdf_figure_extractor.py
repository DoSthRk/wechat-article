"""从 PDF 抽取插图 —— 题注定位 + 渲染裁剪（pypdfium2 渲染 + pdfplumber 定位）。

策略（对矢量图 / 位图 / 拼版图通用）：
1. pdfplumber 找真题注：以「(Extended Data )Fig. N |」开头的行（竖线是 Nature 题注
   标记，正文引用没有竖线，可靠区分）。
2. 图区域 = 题注上方（回落下方）那一带里**图形元素（image/rect/curve/line）的并集 bbox**
   —— 排除正文文字，裁得紧。
3. pypdfium2 把该页渲染成高 DPI 位图，按 bbox 裁剪成 PNG。

产出 figures_manifest.json：每条 {label, is_extended, caption, page, image_path}。
正文占位符 [图片:Figure N …] 按图号 label 匹配（主图优先于 Extended Data）。

注：复杂论文不保证 100% 准确（接受尽力自动）。版权：复用已发表论文图需自行确认。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
import pypdfium2 as pdfium

from utils.logger import setup_logger

logger = setup_logger("pdf_figure_extractor")

# 题注：行首「(Extended Data )Fig(ure) N |」（竖线 | 或全角 ｜ 是真题注标记）
_CAP_RE = re.compile(r"^(Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)\s*[|｜]", re.I)
# 取图号（占位符 / 题注通用）："Figure 1e" → "1"
_NUM_RE = re.compile(r"(?:Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)", re.I)
_DPI = 170
_MARGIN = 28          # 页边距（点）
_MIN_REGION = 120     # 区域最小边长（点），太小视为没抓到


@dataclass
class Figure:
    label: str          # 图号 "1" / "3"
    is_extended: bool    # True = Extended Data 附录图
    caption: str
    page: int            # 1-based
    image_path: str
    width: int
    height: int


def figure_number(text: str) -> str:
    """从占位符描述或题注取图号（'Figure 1e …' / 'Fig. 3' → '1' / '3'）。"""
    m = _NUM_RE.match((text or "").strip())
    return m.group(1) if m else ""


def _graphics_union(page, band_top: float, band_bottom: float) -> Optional[Tuple[float, float, float, float]]:
    """题注带内图形元素（image/rect/curve/line，排除文字）的并集 bbox。"""
    xs0: List[float] = []
    ys0: List[float] = []
    xs1: List[float] = []
    ys1: List[float] = []
    for kind in ("images", "rects", "curves", "lines"):
        for e in (getattr(page, kind, None) or []):
            top, bottom = float(e.get("top", 0)), float(e.get("bottom", 0))
            if bottom <= band_top or top >= band_bottom:
                continue
            xs0.append(float(e.get("x0", 0)))
            ys0.append(max(top, band_top))
            xs1.append(float(e.get("x1", 0)))
            ys1.append(min(bottom, band_bottom))
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def extract_figures(
    pdf_path: str, out_dir: str, *,
    max_pages: Optional[int] = None, dpi: int = _DPI, use_cache: bool = True,
) -> List[Figure]:
    """抽图到 out_dir，写 figures_manifest.json。use_cache=True 时若 manifest 在则直接读。"""
    out = Path(out_dir)
    manifest_path = out / "figures_manifest.json"
    if use_cache and manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return [Figure(**d) for d in data]
        except Exception:  # noqa: BLE001 - 缓存坏了就重抽
            pass

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
                caps = []
                for ln in page.extract_text_lines():
                    m = _CAP_RE.match((ln.get("text") or "").strip())
                    if m:
                        caps.append((bool(m.group(1)), m.group(2),
                                     float(ln.get("top", 0)), float(ln.get("bottom", 0))))
                if not caps:
                    continue
                caps.sort(key=lambda c: c[2])
                rendered = doc[i].render(scale=scale).to_pil()
                pw, ph = float(page.width), float(page.height)
                words = page.extract_words() or []
                for idx, (is_ext, num, ctop, cbot) in enumerate(caps):
                    prev_bottom = caps[idx - 1][3] if idx > 0 else _MARGIN
                    next_top = caps[idx + 1][2] if idx + 1 < len(caps) else ph - _MARGIN
                    # 先试题注上方，太矮回落到下方
                    region = _graphics_union(page, prev_bottom, ctop)
                    if not region or (region[3] - region[1]) < _MIN_REGION:
                        region = _graphics_union(page, cbot, next_top)
                    if not region:
                        continue
                    rx0, rtop, rx1, rbot = region
                    if (rx1 - rx0) < _MIN_REGION or (rbot - rtop) < _MIN_REGION:
                        continue
                    # 文字密度护栏：区域内文字过多 → 是正文不是图，跳过（绝不把文字块当图）
                    n_words = sum(
                        1 for w in words
                        if float(w["x0"]) >= rx0 - 2 and float(w["x1"]) <= rx1 + 2
                        and float(w["top"]) >= rtop - 2 and float(w["bottom"]) <= rbot + 2
                    )
                    if n_words > 45:
                        logger.info("p%d fig%s 区域文字过多(%d 词)，判为正文跳过", i + 1, num, n_words)
                        continue
                    box = (max(0, int(rx0 * scale)), max(0, int(rtop * scale)),
                           int(rx1 * scale), int(rbot * scale))
                    crop = rendered.crop(box)
                    fname = f"{'ed' if is_ext else 'fig'}{num}_p{i + 1}.png"
                    crop.save(str(out / fname))
                    figures.append(Figure(
                        label=num, is_extended=is_ext, caption="",
                        page=i + 1, image_path=str(out / fname),
                        width=crop.width, height=crop.height,
                    ))
    finally:
        doc.close()

    manifest_path.write_text(
        json.dumps([asdict(f) for f in figures], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("extracted %d figure(s) from %s", len(figures), Path(pdf_path).name)
    return figures


def match_figure(figures: List[Figure], description: str) -> Optional[Figure]:
    """占位符描述 → 同图号且同类别的图（主图配主图，附录配附录）。

    绝不拿附录图（Extended Data）顶替主图——它们是不同的图，顶替=插错。找不到回 None。
    """
    num = figure_number(description)
    if not num:
        return None
    want_ext = bool(re.search(r"extended\s*data|附录|扩展数据", description or "", re.I))
    pool = [f for f in figures if f.label == num and f.is_extended == want_ext]
    return pool[0] if pool else None
