"""题注锚定抽图 —— 移植 pdffigures2 思路，纯 pdfplumber + pypdfium2，CPU/Windows 友好。

为什么这么做（见 GitHub 调研）：版面检测类工具（MinerU/docling/marker）会把密集多面板图
**拆成单个面板**；而 pdffigures2 用「题注锚定」把一整张图（含所有子面板）当一个单位，且显式
排除正文 —— 正好治本项目「裁进正文 / 多面板被拆」两个病。这里移植它的核心算法：

  1. 找题注行 "Figure N" / "Fig. N" / "Extended Data Figure N"；
  2. 图在题注**上方**：先用「上一题注以下、本题注以上」的图形并集定出图的**所在列**；
  3. 在该列内从题注向上走，**遇到正文行就停**（正文是边界，天然被排除）；
  4. 该带内图形并集 = 整张图框（多面板一起，不拆）；图号直接从题注拿（省 VLM）。

找不到题注的页 → 返回空，由上层回落到 VLM / 确定性裁剪（如 Cell 图文摘要无 "Figure N" 题注）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

from utils.figure_crop_geometry import is_rule_line, trim_detached_edge_bands, sanitize_figure_region, figure_crop_words
from utils.logger import setup_logger
from utils.pdf_figure_extractor import Figure

logger = setup_logger("caption_figures")

# 题注：行首 "Figure 3" / "Fig. 3" / "Extended Data Figure 3" / "Supplementary Fig 3" / "图3"
_CAPTION_RE = re.compile(
    r"^(extended\s+data\s+fig(?:ure)?|sup(?:plementary|pl)?\.?\s+fig(?:ure)?|fig(?:ure)?|图)"
    r"\.?\s*([0-9]+)(?=[\s\.\:\|、。]|$)",
    re.IGNORECASE,
)
# Deliberately broader than layout captions: include right-column starts and
# inline mentions in the prefilter rather than hiding possible duplicate keys.
_CAPTION_SCAN_RE = re.compile(r'(?<![A-Za-z])' + _CAPTION_RE.pattern[1:], re.IGNORECASE)
_NEXT_PAGE_CAPTION_RE = re.compile(
    r"caption\s+(?:is\s+)?on\s+(?:the\s+)?next\s+page",
    re.IGNORECASE,
)
_LINE_TOL = 3.0          # 同一行 top 容差 (pt)
_MIN_FIG_PT = 40.0       # 图框任一边 < 此值 → 视为噪声，丢弃
_RENDER_SCALE = 1.6
_CAPTION_VERSION = 10    # v10: selective layout parsing; retain labels within caption-bounded figure bands


def caption_enabled() -> bool:
    return os.getenv("CAPTION_FIGURES_ENABLED", "1").strip() not in ("0", "false", "False")


def _mk_line(words: list) -> dict:
    ws = sorted(words, key=lambda w: float(w["x0"]))
    return {
        "text": " ".join(w["text"] for w in ws),
        "x0": min(float(w["x0"]) for w in ws),
        "x1": max(float(w["x1"]) for w in ws),
        "top": min(float(w["top"]) for w in ws),
        "bottom": max(float(w["bottom"]) for w in ws),
        "n_words": len(ws),
    }


def _group_lines(page) -> List[dict]:
    """words → 行（按 top 贪心聚类）。"""
    words = sorted(page.extract_words(use_text_flow=False) or [],
                   key=lambda w: (float(w["top"]), float(w["x0"])))
    lines: List[dict] = []
    cur: list = []

    def append_line(row: list) -> None:
        lines.append(_mk_line(row))
        # In two-column papers a right-column caption can share a baseline with
        # left-column prose. Preserve that caption as its own line.
        ordered = sorted(row, key=lambda word: float(word["x0"]))
        for pos, word in enumerate(ordered):
            # Only a real column boundary can start a second caption. A
            # mid-sentence "Fig. 2" has ordinary word spacing, not a gutter.
            if (pos and float(word["x0"]) >= float(page.width) * 0.45
                    and float(word["x0"]) - float(ordered[pos - 1]["x1"]) >= 8):
                right_line = _mk_line(ordered[pos:])
                if _CAPTION_RE.match(right_line["text"]):
                    lines.append(right_line)
                    break

    for w in words:
        if cur and float(w["top"]) - float(cur[0]["top"]) > _LINE_TOL:
            append_line(cur)
            cur = []
        cur.append(w)
    if cur:
        append_line(cur)
    return lines


def _gfx(page) -> list:
    out: list = []
    for kind in ("images", "rects", "curves", "lines"):
        out.extend(getattr(page, kind, None) or [])
    return out


def _cy(e) -> float:
    return (float(e["top"]) + float(e["bottom"])) / 2.0


def _union_box(band: list) -> Optional[Tuple[float, float, float, float]]:
    """一组图形元素的紧致并集框；过小返回 None。"""
    if not band:
        return None
    x0 = min(float(e["x0"]) for e in band)
    x1 = max(float(e["x1"]) for e in band)
    y0 = min(float(e["top"]) for e in band)
    y1 = max(float(e["bottom"]) for e in band)
    if x1 - x0 < _MIN_FIG_PT or y1 - y0 < _MIN_FIG_PT:
        return None
    return (max(0.0, x0), max(0.0, y0), x1, y1)


def _box_for_caption(cap_top: float, cap_bottom: float, gfx: list,
                     cap_tops: List[float], cap_bottoms: List[float],
                     page_w: float, page_h: float, words=None) -> Optional[Tuple[float, float, float, float]]:
    """题注对应那张图的框：图相对题注通常在**上方**（少数在下方）。

    关键简化：正文没有图形元素，所以「相邻两题注之间的图形并集」天然只含图、不含正文 —— 无需
    再判别正文行（图内流程框/示意图文字会被误判成正文，正是 PARK/GBM 翻车的原因）。
    先剔除贯穿大半页的规则线（分栏/页眉脚横线），避免撑大并集。
    """
    clean = [e for e in gfx if not is_rule_line(e, page_w, page_h)]

    def sanitized_union(elements: list, text_bounds) -> Optional[Tuple[float, float, float, float]]:
        box = _union_box(elements)
        if box is None:
            return None
        if words is not None:
            return sanitize_figure_region(elements, words, box, page_w, page_h, text_bounds=text_bounds)
        return trim_detached_edge_bands(elements, box, page_w, page_h)

    # 上方：上一题注以下、本题注以上
    lo = max([b for b in cap_bottoms if b < cap_top - _MIN_FIG_PT], default=0.0)
    above = sanitized_union([e for e in clean if lo < _cy(e) < cap_top], (lo, cap_top))
    if above:
        return above
    # 下方兜底（题注在图上方的版式）：本题注以下、下一题注以上
    hi = min([t for t in cap_tops if t > cap_bottom + _MIN_FIG_PT], default=page_h)
    return sanitized_union([e for e in clean if cap_bottom < _cy(e) < hi], (cap_bottom, hi))


def _has_next_page_caption_marker(lines: List[dict]) -> bool:
    """Return whether a page explicitly says its figure caption continues next page."""
    return any(_NEXT_PAGE_CAPTION_RE.search(str(line.get("text") or "")) for line in lines)


def _full_page_figure_box(page) -> Optional[Tuple[float, float, float, float]]:
    """Find the main graphics union on a preceding full-figure page."""
    page_w, page_h = float(page.width), float(page.height)
    clean = [e for e in _gfx(page) if not is_rule_line(e, page_w, page_h)]
    box = _union_box(clean)
    if box is None:
        return None
    return sanitize_figure_region(clean, figure_crop_words(page), box, page_w, page_h,
                                  text_bounds=(0, page_h * .95))


def _following_page_figure_box(page) -> Optional[Tuple[float, float, float, float]]:
    """Accept a graphics-dominant figure page following a bottom-of-page caption."""
    page_w, page_h = float(page.width), float(page.height)
    clean = [e for e in _gfx(page) if not is_rule_line(e, page_w, page_h)]
    raster_area = sum((e['x1'] - e['x0']) * (e['bottom'] - e['top']) for e in (page.images or []))
    if len(clean) < 150 and raster_area < page_w * page_h * 0.20:
        return None
    box = _union_box(clean)
    if box is None or box[1] > page_h * 0.35 or box[3] - box[1] < page_h * 0.35:
        return None
    # Apply the existing edge padding before fitting complete words. Applying
    # it afterwards can still slice a rotated tick label crossing that edge.
    box = (*box[:3], min(page_h * 0.95, box[3] + 7))
    return sanitize_figure_region(
        clean, figure_crop_words(page), box, page_w, page_h,
        text_bounds=(0, page_h * .95),
    )


def _without_caption_backgrounds(page, cap, gfx):
    """Shaded legend boxes are graphics, but not scientific figures.

    Exclude the enclosing box AND decorations inside it, not only its border.
    The caption must be near its top; a figure frame enclosing a bottom legend
    must not hide actual panels.
    """
    regions = [r for r in (page.rects or [])
               if r['x0'] <= cap['x0'] + 2 and r['x1'] >= cap['x1'] - 2
               and r['top'] - 2 <= cap['top'] <= r['top'] + 20
               and r['bottom'] > cap['bottom'] + 20]
    # Some publishers draw the final legend line in a second overlapping box.
    for _ in range(2):
        for rect in page.rects or []:
            if any(abs(rect['x0'] - r['x0']) < 2 and abs(rect['x1'] - r['x1']) < 2
                   and r['top'] <= rect['top'] <= r['bottom'] for r in regions):
                if rect not in regions:
                    regions.append(rect)
    return [e for e in gfx if not any(
        e['x0'] >= r['x0'] - 2 and e['x1'] <= r['x1'] + 2
        and e['top'] >= r['top'] - 2 and e['bottom'] <= r['bottom'] + 2
        for r in regions)]


def _detached_preceding_box(page):
    """Only pair an immediately preceding graphics-only page, never prose."""
    from utils.figure_quality import prose_lines
    if prose_lines(page.extract_words() or [], (0, 0, float(page.width), float(page.height))) >= 4:
        return None
    return _following_page_figure_box(page)


def _left_figure_for_right_caption(page, cap: dict) -> Optional[Tuple[float, float, float, float]]:
    """Recover a figure occupying the left column beside its right-column legend."""
    page_w, page_h = float(page.width), float(page.height)
    if float(cap["x0"]) < page_w * 0.55 or float(cap["top"]) > page_h * 0.25:
        return None
    clean = [
        e for e in _gfx(page)
        if not is_rule_line(e, page_w, page_h)
        and float(e["x1"]) < float(cap["x0"]) - 8
        and float(cap["top"]) < _cy(e) < page_h * 0.80
    ]
    if len(clean) < 250:
        return None
    box = _union_box(clean)
    if box is None or box[3] - box[1] < page_h * 0.35:
        return None
    words = [w for w in figure_crop_words(page) if float(w["x1"]) < float(cap["x0"]) - 8]
    return sanitize_figure_region(clean, words, box, page_w, page_h)


def _caption_page_indices(pdf_path, required_keys, max_pages=None):
    """Scan text in PDFium before expensive vector/layout interpretation.

    Scan the whole source for *every* occurrence of a requested key, preserving
    ambiguity checks. If text cannot account for all keys, keep the original
    full-layout fallback rather than assuming the missing figures do not exist.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        limit = min(max_pages, len(document)) if max_pages else len(document)
        indices, found = set(), set()
        for index in range(limit):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded() or ''
                finally:
                    textpage.close()
                for match in _CAPTION_SCAN_RE.finditer(text):
                    key = (match.group(2), bool(re.search(r'extended|sup', match.group(1), re.I)))
                    if key in required_keys:
                        indices.add(index)
                        found.add(key)
            finally:
                page.close()
        return indices if found == set(required_keys) else None
    finally:
        document.close()


def find_figure_boxes(pdf_path: str, max_pages: Optional[int] = None, *, required_keys=None):
    """返回所有独立图号/页码/裁框候选；真实同号冲突交给质量闸拦截。"""
    import pdfplumber

    out = []
    seen = set()
    indices = None
    if required_keys:
        try:
            indices = _caption_page_indices(pdf_path, required_keys, max_pages)
        except Exception:
            logger.warning('快速题注扫描失败，保留完整布局解析', exc_info=True)
    with pdfplumber.open(pdf_path) as pdf:
        limit = min(max_pages, len(pdf.pages)) if max_pages else len(pdf.pages)
        for idx in range(limit):
            if indices is not None and idx not in indices:
                continue
            page = pdf.pages[idx]
            page_w, page_h = float(page.width), float(page.height)
            lines = _group_lines(page)
            caps = [(m, ln) for ln in lines if (m := _CAPTION_RE.match(ln["text"]))]
            if not caps:
                continue
            gfx = _gfx(page)
            cap_tops = [ln["top"] for _, ln in caps]
            cap_bottoms = [ln["bottom"] for _, ln in caps]
            for m, cap in caps:
                num = re.sub(r"[^0-9]", "", m.group(2))
                is_ext = bool(re.search(r"extended|sup", m.group(1), re.I))
                key = (num, is_ext)
                if not num or (required_keys and key not in required_keys):
                    continue
                figure_idx = idx
                cap_gfx = _without_caption_backgrounds(page, cap, gfx)
                cap_words = figure_crop_words(page)
                if float(cap["x0"]) >= page_w * 0.5:
                    cap_gfx = [e for e in cap_gfx if float(e["x0"]) >= page_w * 0.5 - 10]
                    cap_words = [w for w in cap_words if float(w["x0"]) >= page_w * 0.5 - 10]
                box = _left_figure_for_right_caption(page, cap)
                if box is None:
                    box = _box_for_caption(cap["top"], cap["bottom"], cap_gfx,
                                           cap_tops, cap_bottoms, page_w, page_h,
                                           words=cap_words)
                facing = 'facing page' in cap['text'].lower()
                if box is None and idx + 1 < limit and (facing or float(cap["top"]) >= page_h * 0.65):
                    box = _following_page_figure_box(pdf.pages[idx + 1])
                    if box is not None:
                        figure_idx = idx + 1
                if (
                    box is None
                    and idx > 0
                    and float(cap["top"]) <= page_h * 0.30
                ):
                    if _has_next_page_caption_marker(_group_lines(pdf.pages[idx - 1])):
                        box = _full_page_figure_box(pdf.pages[idx - 1])
                    else:
                        box = _detached_preceding_box(pdf.pages[idx - 1])
                    if box is not None:
                        figure_idx = idx - 1
                if box is None:
                    continue
                candidate_key = (key, figure_idx, tuple(box))
                if candidate_key in seen:
                    continue
                out.append((num, is_ext, figure_idx, box, cap["text"][:80]))
                seen.add(candidate_key)
    return out


def _manifest(out: Path) -> Path:
    return out / "caption_figures_manifest.json"


def _version_path(out: Path) -> Path:
    return out / ".caption_version"


def extract_figures_by_caption(
    pdf_path: str, out_dir: str, *, max_pages: Optional[int] = None, use_cache: bool = True,
    required_keys=None,
) -> List[Figure]:
    """题注锚定抽图主入口：找图框 → 渲染裁剪 → List[Figure]（label=图号，喂 match_figure）。

    缓存到 out_dir/caption_figures_manifest.json；算法版本(``_CAPTION_VERSION``)变更则失效重算。
    """
    out = Path(out_dir)
    mani = _manifest(out)
    if use_cache and mani.exists() and _read_version(out) == _CAPTION_VERSION:
        try:
            cached = [Figure(**d) for d in json.loads(mani.read_text(encoding="utf-8"))]
            if not required_keys:
                # A selective cache is not proof of a complete-document scan.
                if not (out / '.caption_required_keys.json').exists():
                    return cached
            elif set(required_keys) <= {(f.label, f.is_extended) for f in cached}:
                return [f for f in cached if (f.label, f.is_extended) in required_keys]
        except Exception:  # noqa: BLE001
            pass

    boxes = find_figure_boxes(pdf_path, max_pages, required_keys=required_keys)
    if not boxes:
        return []
    out.mkdir(parents=True, exist_ok=True)
    from utils.vision_figures import _render  # 复用渲染（pypdfium2）

    figs: List[Figure] = []
    for num, is_ext, idx, box, caption in boxes:
        try:
            pil = _render(pdf_path, idx, _RENDER_SCALE)
            w, h = pil.size
            # Keep antialiased edge labels and panel letters inside the crop.
            box = (max(0, box[0] - 3), max(0, box[1] - 3),
                   min(w / _RENDER_SCALE, box[2] + 3), min(h / _RENDER_SCALE, box[3] + 3))
            x0, y0, x1, y1 = (int(v * _RENDER_SCALE) for v in box)
            crop = pil.crop((max(0, x0), max(0, y0), min(w, x1), min(h, y1)))
            tag = f"cap{num}{'e' if is_ext else ''}_p{idx + 1}"
            fp = out / f"{tag}.jpg"
            crop.save(str(fp), quality=85)
            figs.append(Figure(
                label=num, is_extended=is_ext, caption=caption,
                page=idx + 1, image_path=str(fp), width=crop.width, height=crop.height,
                bbox=tuple(box),
            ))
        except Exception as exc:  # noqa: BLE001 - 单图失败不阻断
            logger.warning("题注图%s(p%d)裁剪失败：%s", num, idx + 1, exc)
    _write_manifest(mani, figs)
    selection = out / '.caption_required_keys.json'
    if required_keys:
        selection.write_text(json.dumps(sorted(required_keys)), encoding='utf-8')
    else:
        selection.unlink(missing_ok=True)
    logger.info("题注锚定抽到 %d 张图：%s", len(figs), sorted(f.label for f in figs))
    return figs


def _read_version(out: Path) -> int:
    try:
        return int(_version_path(out).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_manifest(mani: Path, figs: List[Figure]) -> None:
    mani.write_text(json.dumps([asdict(f) for f in figs], ensure_ascii=False, indent=2), encoding="utf-8")
    _version_path(mani.parent).write_text(str(_CAPTION_VERSION), encoding="utf-8")
