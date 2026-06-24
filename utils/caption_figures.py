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

from utils.logger import setup_logger
from utils.pdf_figure_extractor import Figure

logger = setup_logger("caption_figures")

# 题注：行首 "Figure 3" / "Fig. 3" / "Extended Data Figure 3" / "Supplementary Fig 3" / "图3"
_CAPTION_RE = re.compile(
    r"^(extended\s+data\s+fig(?:ure)?|sup(?:plementary|pl)?\.?\s+fig(?:ure)?|fig(?:ure)?|图)"
    r"\.?\s*([0-9]+)(?=[\s\.\:\|、。]|$)",
    re.IGNORECASE,
)
_LINE_TOL = 3.0          # 同一行 top 容差 (pt)
_MIN_FIG_PT = 40.0       # 图框任一边 < 此值 → 视为噪声，丢弃
_RENDER_SCALE = 1.6
_CAPTION_VERSION = 1     # 算法版本：变更时 +1，缓存据此失效


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
    for w in words:
        if cur and float(w["top"]) - float(cur[0]["top"]) > _LINE_TOL:
            lines.append(_mk_line(cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(_mk_line(cur))
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
                     page_w: float, page_h: float) -> Optional[Tuple[float, float, float, float]]:
    """题注对应那张图的框：图相对题注通常在**上方**（少数在下方）。

    关键简化：正文没有图形元素，所以「相邻两题注之间的图形并集」天然只含图、不含正文 —— 无需
    再判别正文行（图内流程框/示意图文字会被误判成正文，正是 PARK/GBM 翻车的原因）。
    先剔除贯穿大半页的规则线（分栏/页眉脚横线），避免撑大并集。
    """
    from utils.vision_figures import _is_rule_line  # 复用规则线判定

    clean = [e for e in gfx if not _is_rule_line(e, page_w, page_h)]
    # 上方：上一题注以下、本题注以上
    lo = max([b for b in cap_bottoms if b < cap_top - _MIN_FIG_PT], default=0.0)
    above = _union_box([e for e in clean if lo < _cy(e) < cap_top])
    if above:
        return above
    # 下方兜底（题注在图上方的版式）：本题注以下、下一题注以上
    hi = min([t for t in cap_tops if t > cap_bottom + _MIN_FIG_PT], default=page_h)
    return _union_box([e for e in clean if cap_bottom < _cy(e) < hi])


def find_figure_boxes(pdf_path: str, max_pages: Optional[int] = None):
    """返回 [(num, is_extended, page_idx, (x0,y0,x1,y1)pt, caption_text), ...]，按图号去重取首张。"""
    import pdfplumber

    out = []
    seen = set()
    with pdfplumber.open(pdf_path) as pdf:
        limit = min(max_pages, len(pdf.pages)) if max_pages else len(pdf.pages)
        for idx in range(limit):
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
                if not num or key in seen:
                    continue
                box = _box_for_caption(cap["top"], cap["bottom"], gfx,
                                       cap_tops, cap_bottoms, page_w, page_h)
                if box is None:
                    continue
                out.append((num, is_ext, idx, box, cap["text"][:80]))
                seen.add(key)
    return out


def _manifest(out: Path) -> Path:
    return out / "caption_figures_manifest.json"


def _version_path(out: Path) -> Path:
    return out / ".caption_version"


def extract_figures_by_caption(
    pdf_path: str, out_dir: str, *, max_pages: Optional[int] = None, use_cache: bool = True,
) -> List[Figure]:
    """题注锚定抽图主入口：找图框 → 渲染裁剪 → List[Figure]（label=图号，喂 match_figure）。

    缓存到 out_dir/caption_figures_manifest.json；算法版本(``_CAPTION_VERSION``)变更则失效重算。
    """
    out = Path(out_dir)
    mani = _manifest(out)
    if use_cache and mani.exists() and _read_version(out) == _CAPTION_VERSION:
        try:
            return [Figure(**d) for d in json.loads(mani.read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            pass

    boxes = find_figure_boxes(pdf_path, max_pages)
    if not boxes:
        return []
    out.mkdir(parents=True, exist_ok=True)
    from utils.vision_figures import _render  # 复用渲染（pypdfium2）

    figs: List[Figure] = []
    for num, is_ext, idx, box, caption in boxes:
        try:
            pil = _render(pdf_path, idx, _RENDER_SCALE)
            w, h = pil.size
            x0, y0, x1, y1 = (int(v * _RENDER_SCALE) for v in box)
            crop = pil.crop((max(0, x0), max(0, y0), min(w, x1), min(h, y1)))
            tag = f"cap{num}{'e' if is_ext else ''}_p{idx + 1}"
            fp = out / f"{tag}.jpg"
            crop.save(str(fp), quality=85)
            figs.append(Figure(
                label=num, is_extended=is_ext, caption=caption,
                page=idx + 1, image_path=str(fp), width=crop.width, height=crop.height,
            ))
        except Exception as exc:  # noqa: BLE001 - 单图失败不阻断
            logger.warning("题注图%s(p%d)裁剪失败：%s", num, idx + 1, exc)
    _write_manifest(mani, figs)
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
