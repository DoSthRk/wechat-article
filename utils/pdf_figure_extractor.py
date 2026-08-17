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

from utils.figure_crop_geometry import trim_detached_edge_bands
from utils.logger import setup_logger

logger = setup_logger("pdf_figure_extractor")

_CAP_RE = re.compile(r"^(Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)\s*[|｜]", re.I)
_LEGEND_HEADING_RE = re.compile(r"\bFIGURE\s+LEGENDS?\b", re.I)
_LEGEND_CAP_RE = re.compile(
    r"^(?:\d+\s+)?(Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)\s*[\.\:\|｜：]",
    re.I,
)
_NUM_RE = re.compile(r"(?:Extended Data\s+)?Fig(?:ure)?\.?\s*(\d+)", re.I)
_DPI = 170
_MARGIN = 24
_MIN_REGION = 120     # 区域最小边长（点）
_FIGPAGE_MIN_GFX = 300   # 图页：图形元素数下限
_FIGPAGE_MAX_WORDS = 650  # 图页：文字数上限
_DETACHED_FIGPAGE_MAX_WORDS = 1000  # 文末整页科研图可含大量坐标轴/图例文字
_TEXT_GUARD_WORDS = 45    # 同页区域内文字超过此数 → 判为正文，不当图
_CONTENT_PAD = 6          # 文本/图形并集外留白，避免坐标轴文字贴边
_CROP_VERSION = 2         # v2: 文末图页纳入坐标轴/图例文字，并支持高密度矢量图


@dataclass
class Figure:
    label: str           # 图号 "1" / "3"
    is_extended: bool     # True = Extended Data 附录图
    caption: str
    page: int             # 图所在页（1-based）
    image_path: str
    width: int
    height: int


def _manifest_version_path(manifest: Path) -> Path:
    return manifest.with_name(f".{manifest.stem}_crop_version")


def _manifest_version_is_current(manifest: Path) -> bool:
    try:
        return int(_manifest_version_path(manifest).read_text(encoding="utf-8").strip()) == _CROP_VERSION
    except (OSError, ValueError):
        return False


def _write_manifest_version(manifest: Path) -> None:
    _manifest_version_path(manifest).write_text(str(_CROP_VERSION), encoding="utf-8")


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


def _legend_caption_numbers(lines: List[str]) -> List[Tuple[bool, str]]:
    """提取独立 Figure legend 行，兼容无 ``FIGURE LEGENDS`` 标题和行号前缀。"""
    out: List[Tuple[bool, str]] = []
    seen = set()
    for raw in lines:
        text = (raw or "").strip()
        if _LEGEND_HEADING_RE.search(text):
            continue
        m = _LEGEND_CAP_RE.match(text)
        if not m:
            continue
        key = (bool(re.search(r"extended|sup", m.group(1) or "", re.I)), m.group(2))
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _detached_figure_page(page, words: list, gfx: list) -> bool:
    """识别文末无文字整页图；兼容单个大栅格图，不再只依赖数百个矢量元素。"""
    if _is_figure_page(len(words), len(gfx)):
        return True
    if len(gfx) >= _FIGPAGE_MIN_GFX and len(words) <= _DETACHED_FIGPAGE_MAX_WORDS:
        return True
    # 复杂科研图会包含数百个坐标轴、图例和面板标签。它们虽然有较多可提取文字，
    # 但只要图形元素密度已达到整页图阈值，就仍应当视为图页。文字上限仅用于
    # 后面的“大栅格图”回退，避免把含普通插图的正文页误判为独立图页。
    if len(words) > 20:
        return False
    page_area = max(1.0, float(page.width) * float(page.height))
    for image in getattr(page, "images", None) or []:
        width = max(0.0, float(image.get("x1", 0)) - float(image.get("x0", 0)))
        height = max(0.0, float(image.get("bottom", 0)) - float(image.get("top", 0)))
        if width * height / page_area >= 0.10:
            return True
    return False


def _legend_document_pairs(prof: List[Dict]) -> List[Tuple[Tuple[bool, str], int]]:
    """把末尾多页 legends 与之后的无编号图页按顺序配对，允许中间夹表格/说明页。"""
    if not prof:
        return []
    heading_pages = [i for i, item in enumerate(prof) if item.get("has_legend_heading")]
    caption_floor = heading_pages[0] if heading_pages else int(len(prof) * 0.55)
    captions: List[Tuple[bool, str]] = []
    seen = set()
    last_caption_page = -1
    for i in range(caption_floor, len(prof)):
        for key in prof[i].get("legend_caps") or []:
            if key in seen:
                continue
            captions.append(key)
            seen.add(key)
            last_caption_page = i
    if not captions or last_caption_page < 0:
        return []
    figure_pages = [
        i for i in range(last_caption_page + 1, len(prof))
        if prof[i].get("is_detached_fig")
    ]
    return list(zip(captions, figure_pages))


def _legend_page_pairs(prof: List[Dict], legend_idx: int, used_pages: set) -> List[Tuple[Tuple[bool, str], int]]:
    """把 FIGURE LEGENDS 中的 Figure N 顺序配到后续连续图页。"""
    caps = list(prof[legend_idx].get("legend_caps") or [])
    pairs: List[Tuple[Tuple[bool, str], int]] = []
    for fp in range(legend_idx + 1, len(prof)):
        if len(pairs) >= len(caps):
            break
        if prof[fp].get("legend_caps"):
            break
        if fp in used_pages:
            continue
        if prof[fp].get("is_fig"):
            pairs.append((caps[len(pairs)], fp))
            continue
        if pairs:
            break
    return pairs


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


def _figure_region_on_page(page, gfx: list, words: list) -> Optional[Tuple[float, float, float, float]]:
    """整页图页里框出"图"区域：按行统计图形 vs 文字密度，取图形占主导的纵向连续带，
    排除顶部续排正文 / 底部页脚 —— 解决"上文下图"页裁进正文的问题。"""
    ph = float(page.height)
    nb = 24
    band = ph / nb
    g = [0] * nb
    w = [0] * nb
    for e in gfx:
        k = int(float(e.get("top", 0)) // band)
        if 0 <= k < nb:
            g[k] += 1
    for x in words:
        k = int(float(x.get("top", 0)) // band)
        if 0 <= k < nb:
            w[k] += 1
    fig_bands = [k for k in range(nb) if g[k] >= 5 and g[k] > w[k]]
    if not fig_bands:
        return None
    top = fig_bands[0] * band
    bottom = (fig_bands[-1] + 1) * band
    return _union(gfx, top, bottom)


def _legend_page_region(
    gfx: list,
    page_w: Optional[float] = None,
    page_h: Optional[float] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """文末整页图没有题注边界，直接取图形并集，避免密度带截掉稀疏底部面板。"""
    region = _union(gfx)
    if region is not None and page_w is not None and page_h is not None:
        region = trim_detached_edge_bands(gfx, region, page_w, page_h)
    return region


def _legend_page_content_region(
    gfx: list, words: list, page_w: float, page_h: float,
) -> Optional[Tuple[float, float, float, float]]:
    """文末整页图的完整内容区：保留坐标轴/图例文字，同时继续排除独立页眉。"""
    region = _legend_page_region(gfx, page_w, page_h)
    if region is None:
        return None
    rx0, rt, rx1, rb = region
    # 只纳入接近主图或位于主图下方的文字；避免把已剔除的杂志页眉重新并入。
    content_words = [
        word for word in words
        if float(word.get("bottom", 0)) >= rt - _MARGIN
        and not (
            not bool(word.get("upright", True))
            and float(word.get("x1", 0)) >= page_w - _MARGIN
        )
    ]
    word_region = _union(content_words)
    if word_region is not None:
        wx0, wt, wx1, wb = word_region
        rx0, rt, rx1, rb = min(rx0, wx0), min(rt, wt), max(rx1, wx1), max(rb, wb)
    return (
        max(0.0, rx0 - _CONTENT_PAD),
        max(0.0, rt - _CONTENT_PAD),
        min(page_w, rx1 + _CONTENT_PAD),
        min(page_h, rb + _CONTENT_PAD),
    )


def extract_figures_from_legend_pages(
    pdf_path: str, out_dir: str, *,
    max_pages: Optional[int] = None, dpi: int = _DPI, use_cache: bool = True,
) -> List[Figure]:
    """支持 FIGURE LEGENDS 与文末图页分离的 PDF：按图例顺序配后续连续图页。"""
    out = Path(out_dir)
    manifest_path = out / "legend_figures_manifest.json"
    if use_cache and manifest_path.exists() and _manifest_version_is_current(manifest_path):
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
                lines = [(ln.get("text") or "").strip() for ln in pg.extract_text_lines()]
                prof.append({
                    "pg": pg,
                    "words": words,
                    "gfx": gfx,
                    "legend_caps": _legend_caption_numbers(lines),
                    "is_fig": _is_figure_page(len(words), len(gfx)),
                    "is_detached_fig": _detached_figure_page(pg, words, gfx),
                    "has_legend_heading": any(_LEGEND_HEADING_RE.search(line) for line in lines),
                })

            used_pages: set = set()
            for (is_ext, num), fp in _legend_document_pairs(prof):
                if fp in used_pages:
                    continue
                figure_page = prof[fp]["pg"]
                region = _legend_page_content_region(
                    prof[fp]["gfx"], prof[fp]["words"],
                    float(figure_page.width), float(figure_page.height),
                )
                if region is None:
                    continue
                rx0, rt, rx1, rb = region
                if (rx1 - rx0) < _MIN_REGION or (rb - rt) < _MIN_REGION:
                    continue
                rendered = doc[fp].render(scale=scale).to_pil()
                box = (max(0, int(rx0 * scale)), max(0, int(rt * scale)),
                       int(rx1 * scale), int(rb * scale))
                crop = rendered.crop(box)
                fname = f"{'legend_ed' if is_ext else 'legend_fig'}{num}_p{fp + 1}.png"
                crop.save(str(out / fname))
                figures.append(Figure(
                    label=num, is_extended=is_ext, caption="",
                    page=fp + 1, image_path=str(out / fname),
                    width=crop.width, height=crop.height,
                ))
                used_pages.add(fp)
    finally:
        doc.close()

    manifest_path.write_text(
        json.dumps([asdict(f) for f in figures], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    _write_manifest_version(manifest_path)
    if figures:
        logger.info("legend pages extracted %d figure(s) from %s: %s",
                    len(figures), Path(pdf_path).name, sorted(f.label for f in figures))
    return figures


def extract_figures(
    pdf_path: str, out_dir: str, *,
    max_pages: Optional[int] = None, dpi: int = _DPI, use_cache: bool = True,
) -> List[Figure]:
    """抽图到 out_dir，写 figures_manifest.json。use_cache=True 且 manifest 在则直接读。"""
    out = Path(out_dir)
    manifest_path = out / "figures_manifest.json"
    if use_cache and manifest_path.exists() and _manifest_version_is_current(manifest_path):
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
                        region = _figure_region_on_page(prof[fp]["pg"], prof[fp]["gfx"], prof[fp]["words"]) \
                            or _union(prof[fp]["gfx"])
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
                    figure_page = prof[fp]["pg"]
                    region = trim_detached_edge_bands(
                        prof[fp]["gfx"], region,
                        float(figure_page.width), float(figure_page.height),
                    )
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
    _write_manifest_version(manifest_path)
    logger.info("extracted %d figure(s) from %s", len(figures), Path(pdf_path).name)
    return figures
