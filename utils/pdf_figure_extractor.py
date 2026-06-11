"""从 PDF 抽取插图 —— 题注定位 + 图页识别 + 渲染裁剪（pypdfium2 + pdfplumber）。

版式洞察：很多期刊 PDF 是「题注在文本页底部、图在下一张整页」。所以：
1. pdfplumber 找真题注：行首「(Extended Data )Fig. N |」（竖线是 Nature 题注标记，
   正文引用没有竖线，可靠区分）。
2. 给每页分类：图页 = 图形元素多 + 文字少。
3. **主图题注**（文本页上的 Fig N）→ 取**下一张图页**的整页图形并集区域；
   取不到再回落到题注同页的图形区域（附录图 / 同页图属此类）。
4. pypdfium2 渲染目标页，按区域裁剪成 PNG。

产出 figures_manifest.json：{label, is_extended, page, image_path, ...}。
正文 [图片:Figure N …] 按图号 + 类别匹配（主图配主图，绝不拿附录顶替）。

注：复杂论文版式不规则，不保证 100%（接受尽力自动）；配不上的留占位符，可人工放图。
版权：复用已发表论文图需自行确认（开放获取 CC-BY 才稳）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pdfplumber
import pypdfium2 as pdfium

from utils.logger import setup_logger

logger = setup_logger("pdf_figure_extractor")

_CAP_RE = re.compile(r"^(Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)\s*[|｜]", re.I)
_NUM_RE = re.compile(r"(?:Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)", re.I)
_DPI = 170
_MARGIN = 24
_MIN_REGION = 120     # 区域最小边长（点）
_FIGPAGE_MIN_GFX = 300   # 图页：图形元素数下限
_FIGPAGE_MAX_WORDS = 650  # 图页：文字数上限
_TEXT_GUARD_WORDS = 45    # 同页区域内文字超过此数 → 判为正文，不当图


@dataclass
class Figure:
    label: str           # 图号 "1" / "3"
    is_extended: bool     # True = Extended Data 附录图
    caption: str
    page: int             # 图所在页（1-based）
    image_path: str
    width: int
    height: int


def figure_number(text: str) -> str:
    """从占位符描述或题注取图号（'Figure 1e …' / 'Fig. 3' → '1' / '3'）。"""
    m = _NUM_RE.match((text or "").strip())
    return m.group(1) if m else ""


def match_figure(figures: List[Figure], description: str) -> Optional[Figure]:
    """占位符 → 同图号且同类别的图（主图配主图，附录配附录）；绝不拿附录顶替主图。找不到回 None。"""
    num = figure_number(description)
    if not num:
        return None
    want_ext = bool(re.search(r"extended\s*data|附录|扩展数据", description or "", re.I))
    pool = [f for f in figures if f.label == num and f.is_extended == want_ext]
    return pool[0] if pool else None


def _gfx_elements(page) -> list:
    out: list = []
    for kind in ("images", "rects", "curves", "lines"):
        out.extend(getattr(page, kind, None) or [])
    return out


def _union(elems: list, band_top: Optional[float] = None,
           band_bottom: Optional[float] = None) -> Optional[Tuple[float, float, float, float]]:
    """图形元素并集 bbox（可限定纵向带 band_top..band_bottom）。"""
    xs0: List[float] = []
    ys0: List[float] = []
    xs1: List[float] = []
    ys1: List[float] = []
    for e in elems:
        t, b = float(e.get("top", 0)), float(e.get("bottom", 0))
        if band_top is not None and b <= band_top:
            continue
        if band_bottom is not None and t >= band_bottom:
            continue
        xs0.append(float(e.get("x0", 0)))
        xs1.append(float(e.get("x1", 0)))
        ys0.append(max(t, band_top) if band_top is not None else t)
        ys1.append(min(b, band_bottom) if band_bottom is not None else b)
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _words_in(words: list, region: Tuple[float, float, float, float]) -> int:
    rx0, rt, rx1, rb = region
    return sum(
        1 for w in words
        if float(w["x0"]) >= rx0 - 2 and float(w["x1"]) <= rx1 + 2
        and float(w["top"]) >= rt - 2 and float(w["bottom"]) <= rb + 2
    )


def _is_figure_page(nwords: int, ngfx: int) -> bool:
    return ngfx >= _FIGPAGE_MIN_GFX and nwords <= _FIGPAGE_MAX_WORDS


def extract_figures(
    pdf_path: str, out_dir: str, *,
    max_pages: Optional[int] = None, dpi: int = _DPI, use_cache: bool = True,
) -> List[Figure]:
    """抽图到 out_dir，写 figures_manifest.json。use_cache=True 且 manifest 在则直接读。"""
    out = Path(out_dir)
    manifest_path = out / "figures_manifest.json"
    if use_cache and manifest_path.exists():
        try:
            return [Figure(**d) for d in json.loads(manifest_path.read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            pass

    out.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    figures: List[Figure] = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            limit = min(max_pages, total) if max_pages else total
            prof: List[Dict] = []
            for i in range(limit):
                pg = pdf.pages[i]
                words = pg.extract_words() or []
                gfx = _gfx_elements(pg)
                caps = []
                for ln in pg.extract_text_lines():
                    m = _CAP_RE.match((ln.get("text") or "").strip())
                    if m:
                        caps.append((bool(m.group(1)), m.group(2),
                                     float(ln.get("top", 0)), float(ln.get("bottom", 0))))
                prof.append({
                    "pg": pg, "words": words, "gfx": gfx, "caps": caps,
                    "is_fig": _is_figure_page(len(words), len(gfx)),
                })

            used_pages: set = set()
            for i in range(limit):
                for (is_ext, num, ctop, cbot) in prof[i]["caps"]:
                    fp: Optional[int] = None
                    region: Optional[Tuple[float, float, float, float]] = None
                    # (a) 主图题注在文本页 → 下一张整页图页
                    if (not is_ext) and (not prof[i]["is_fig"]) \
                            and i + 1 < limit and prof[i + 1]["is_fig"] and (i + 1) not in used_pages:
                        fp = i + 1
                        region = _union(prof[fp]["gfx"])
                        used_pages.add(fp)
                    # (b) 回落：题注同页的图形区域（附录图 / 同页图）
                    if region is None:
                        pg = prof[i]["pg"]
                        ph = float(pg.height)
                        region = _union(prof[i]["gfx"], _MARGIN, ctop) \
                            or _union(prof[i]["gfx"], cbot, ph - _MARGIN)
                        fp = i
                        if region and not prof[i]["is_fig"] \
                                and _words_in(prof[i]["words"], region) > _TEXT_GUARD_WORDS:
                            region = None  # 文字密集 → 是正文，跳过
                    if region is None or fp is None:
                        continue
                    rx0, rt, rx1, rb = region
                    if (rx1 - rx0) < _MIN_REGION or (rb - rt) < _MIN_REGION:
                        continue
                    rendered = doc[fp].render(scale=scale).to_pil()
                    box = (max(0, int(rx0 * scale)), max(0, int(rt * scale)),
                           int(rx1 * scale), int(rb * scale))
                    crop = rendered.crop(box)
                    fname = f"{'ed' if is_ext else 'fig'}{num}_p{fp + 1}.png"
                    crop.save(str(out / fname))
                    figures.append(Figure(
                        label=num, is_extended=is_ext, caption="",
                        page=fp + 1, image_path=str(out / fname),
                        width=crop.width, height=crop.height,
                    ))
    finally:
        doc.close()

    manifest_path.write_text(
        json.dumps([asdict(f) for f in figures], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("extracted %d figure(s) from %s", len(figures), Path(pdf_path).name)
    return figures
