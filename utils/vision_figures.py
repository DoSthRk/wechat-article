"""用视觉模型(VLM)从 PDF 抽图并按图号标注 —— 解决密集多面板科学图的自动配图。

思路：传统/版式抽图器对各种期刊版式不稳，图号识别尤其差。改用 VLM：
  1. 渲染每个"含图页"为图片；
  2. 问 VLM：本页主图的**图号(Figure N)** + **整张图(含所有面板、不含题注/正文)的归一化边界框**；
  3. 按 bbox 裁出整图，按图号标注；
  4. 交给已有的 ``match_figure`` 按图号匹配正文 ``[图片:Figure N]`` 占位符。

接口走 OpenAI 兼容（``VISION_API_KEY`` / ``VISION_BASE_URL`` / ``VISION_MODEL``）。
产物缓存到 ``vision_figures_manifest.json``，避免重复调用 VLM（省钱）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from utils.logger import setup_logger
from utils.pdf_figure_extractor import Figure  # 复用 Figure 数据类（喂给 match_figure）

logger = setup_logger("vision_figures")

# 含图页判定：有内嵌图片(raster 整图) 或 矢量元素密集(curve/rect/line，矢量图表) → 送 VLM。
# 注意：很多图是"一张大 raster"(images>=1 但矢量很少)，不能只看矢量数，否则漏掉这类图页。
_VECTOR_MIN = 200
_RENDER_SCALE = 1.6
_MIN_CROP_PX = 60

_PROMPT = (
    "你是科学论文配图助手。下面是一页 PDF 的渲染图。\n"
    "判断本页是否包含一张**带编号的主图**（如 \"Figure 3\" / \"Fig. 3\" / \"图3\"）。\n"
    "规则：多个面板(A/B/C…)算同一张整图；忽略期刊页眉/Logo/纯文字/表格/参考文献。\n"
    "严格只输出 JSON（不要解释、不要代码块），格式：\n"
    '{"has_figure": true, "figure_number": "3", "caption": "一句话主题(中文)", '
    '"bbox": [x0, y0, x1, y1]}\n'
    "bbox = 整张图（含所有面板，**不含题注文字和正文**）在页面中的归一化坐标，"
    "取值 0~1，原点在左上角，[左, 上, 右, 下]。\n"
    "若本页没有带编号的主图，返回 {\"has_figure\": false}。"
)


class VisionConfigError(RuntimeError):
    """VLM 凭据/配置缺失。"""


def vision_enabled() -> bool:
    """配了 key 且未显式关闭，才启用 VLM 抽图。"""
    if os.getenv("VISION_FIGURES_ENABLED", "1").strip() in ("0", "false", "False"):
        return False
    return bool(os.getenv("VISION_API_KEY", "").strip())


def _client():
    from openai import OpenAI

    key = os.getenv("VISION_API_KEY", "").strip()
    if not key:
        raise VisionConfigError("VISION_API_KEY 未配置")
    base = os.getenv("VISION_BASE_URL", "https://api.moonshot.cn/v1").strip()
    timeout = float(os.getenv("VISION_TIMEOUT", "180") or 180)
    return OpenAI(api_key=key, base_url=base, timeout=timeout, max_retries=2)


def _image_pages(pdf_path: str, max_pages: Optional[int]) -> List[int]:
    """挑出有图形元素的页（送 VLM 的候选），过滤纯文字页。"""
    import pdfplumber

    pages: List[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        limit = min(max_pages, total) if max_pages else total
        for i in range(limit):
            p = pdf.pages[i]
            n_img = len(p.images or [])
            n_vec = len(p.rects or []) + len(p.curves or []) + len(p.lines or [])
            if n_img >= 1 or n_vec >= _VECTOR_MIN:
                pages.append(i)
    return pages


def _render(pdf_path: str, idx: int, scale: float = _RENDER_SCALE):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_path)
    try:
        return doc[idx].render(scale=scale).to_pil().convert("RGB")
    finally:
        doc.close()


def _b64(pil, quality: int = 80) -> str:
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_json(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _ask_page(client, model: str, pil) -> Optional[dict]:
    """问 VLM 单页主图信息。

    注意：kimi-k2.6 是**推理模型**，答 JSON 前会产出大量 reasoning（~1800 tokens），
    max_tokens 太小会把答案截没（content 空）。故给足额度（默认 2048，可调 VISION_MAX_TOKENS）。
    temperature 强制 1（k2.6 只接受 1）。
    """
    max_tokens = int(os.getenv("VISION_MAX_TOKENS", "2048") or 2048)
    b64 = _b64(pil)
    last_exc: Optional[Exception] = None
    for attempt in range(3):  # 重试瞬时连接错误（网络偶发抖动）
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=1,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}],
            )
            return _parse_json(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001 - 瞬时错误重试，仍失败由上层跳过该页
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _crop_bbox(pil, bbox) -> "Optional[object]":
    """按归一化 bbox 裁图（VLM bbox 回落用）；越界/过小则回落整页。"""
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return pil
    w, h = pil.size
    box = (max(0, int(x0 * w)), max(0, int(y0 * h)), min(w, int(x1 * w)), min(h, int(y1 * h)))
    if box[2] - box[0] < _MIN_CROP_PX or box[3] - box[1] < _MIN_CROP_PX:
        return pil
    return pil.crop(box)


# 确定性裁剪参数（pdfplumber 图形元素，单位 pt）
_BIN_PT = 4.0           # 纵向占用直方图 bin 大小
_HEADER_MAX_PT = 60.0   # 顶部"小块"高度 < 此值 → 疑似期刊 Logo/页眉
_HEADER_GAP_PT = 18.0   # 该小块与正图之间的空隙 ≥ 此值 → 确认是页眉，剪掉
_LOGO_TOP_FRAC = 0.20   # 仅当小块落在页面顶部这一比例内才剪（避免误伤正图）

# 主导大图参数：图文摘要 / 整版单图常是「一张大 raster」，四周散落 Logo/图例/Highlights 圆点
# 会把图形并集撑到整页 → 直接裁到这张主导图本身。
_DOMINANT_IMG_FRAC = 0.10   # 最大内嵌图 ≥ 页面此比例 → 候选主导
_DOMINANT_IMG_RATIO = 4.0   # 且 ≥ 次大图的此倍数 → 确认主导（排除多面板拼图）
_HUG_PAD_PT = 8.0           # 紧贴主导图这一 pt 范围内的元素（边框/标注）并入裁剪框

# 全宽/全高细线剪除：Nature 等版式正文有分栏线、页眉/页脚横线（贯穿大半页、极细），
# 它们离正图很远却会把图形并集撑到整页（裁进正文）。算并集前先剔除这类「细规则线」。
# 注意：只剔「贯穿大半页的细线」，正图内部的轴线/连接线（短）不受影响 → 不伤稀疏矢量图。
_RULE_SPAN_FRAC = 0.70  # 线长 ≥ 页宽/页高此比例
_RULE_THICK_PT = 3.0    # 且厚度 ≤ 此值 → 判为规则线（分栏/页眉页脚横线）


def _dominant_image_box(pg, page_area: float):
    """单张内嵌大图主导整版时（图文摘要 / 整版单图），裁到该图本身（含紧贴它的边框/标注），
    绕开散落在四周的期刊 Logo / 图例点 / Highlights 圆点。返回 (x0,y0,x1,y1) 或 None。

    判据：最大内嵌图面积 ≥ 页面 _DOMINANT_IMG_FRAC，且 ≥ 次大图的 _DOMINANT_IMG_RATIO 倍
    （多面板拼图有多张相当大小的图 → 不算主导，仍走图形并集）。
    """
    def _area(e) -> float:
        return (float(e["x1"]) - float(e["x0"])) * (float(e["bottom"]) - float(e["top"]))

    imgs = sorted(pg.images or [], key=_area, reverse=True)
    if not imgs or _area(imgs[0]) < _DOMINANT_IMG_FRAC * page_area:
        return None
    if len(imgs) > 1 and _area(imgs[1]) * _DOMINANT_IMG_RATIO > _area(imgs[0]):
        return None  # 多张大图 → 多面板拼图，走并集
    top = imgs[0]
    x0, y0, x1, y1 = float(top["x0"]), float(top["top"]), float(top["x1"]), float(top["bottom"])
    # 并入紧贴主导图的元素（边框线 / 压在图上的标注）；远处散落图形不碰
    for kind in ("images", "rects", "curves", "lines"):
        for e in getattr(pg, kind, None) or []:
            ex0, ey0, ex1, ey1 = float(e["x0"]), float(e["top"]), float(e["x1"]), float(e["bottom"])
            if (ex1 >= x0 - _HUG_PAD_PT and ex0 <= x1 + _HUG_PAD_PT
                    and ey1 >= y0 - _HUG_PAD_PT and ey0 <= y1 + _HUG_PAD_PT):
                x0, y0, x1, y1 = min(x0, ex0), min(y0, ey0), max(x1, ex1), max(y1, ey1)
    return (max(0.0, x0), max(0.0, y0), x1, y1)


def _is_rule_line(e, page_w: float, page_h: float) -> bool:
    """是否为「贯穿大半页的细规则线」（分栏线 / 页眉页脚横线 / 竖分隔线）。

    只认又长又细的：横向 width ≥ 页宽×_RULE_SPAN_FRAC 且 height ≤ _RULE_THICK_PT（或纵向对称）。
    正图内部的轴线、箭头、连接线都短，命中不了 → 不会误伤图。
    """
    w = float(e["x1"]) - float(e["x0"])
    h = float(e["bottom"]) - float(e["top"])
    horiz = w >= _RULE_SPAN_FRAC * page_w and h <= _RULE_THICK_PT
    vert = h >= _RULE_SPAN_FRAC * page_h and w <= _RULE_THICK_PT
    return horiz or vert


def _graphics_crop_box(pdf_path: str, idx: int):
    """用 pdfplumber 图形元素算"整张图"的紧致框（pt）：图形并集 → 题注/页脚/正文(纯文字)自动排除，
    再剪掉顶部 Logo/页眉短带、并按密度剪掉两侧稀疏离群图形。返回 (x0,y0,x1,y1) 或 None（无图形）。"""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        if idx >= len(pdf.pages):
            return None
        pg = pdf.pages[idx]
        h = float(pg.height)
        # 主导大图优先（图文摘要 / 整版单图）：绕开四周散落图形，裁到这张图本身
        dom = _dominant_image_box(pg, float(pg.width) * h)
        if dom:
            return dom
        els = []
        for kind in ("images", "rects", "curves", "lines"):
            els.extend(getattr(pg, kind, None) or [])
        # 先剔除贯穿大半页的细规则线（分栏线/页眉页脚横线）——它们离正图远却撑大并集、裁进正文
        w = float(pg.width)
        els = [e for e in els if not _is_rule_line(e, w, h)]
        if not els:
            return None
        x0 = min(float(e["x0"]) for e in els)
        x1 = max(float(e["x1"]) for e in els)
        y0 = min(float(e["top"]) for e in els)
        y1 = max(float(e["bottom"]) for e in els)
        # 顶部 Logo/页眉剪除：纵向占用直方图找"顶部短带 + 空隙"
        nb = int(h // _BIN_PT) + 1
        occ = [0] * nb
        for e in els:
            for k in range(int(float(e["top"]) // _BIN_PT), int(float(e["bottom"]) // _BIN_PT) + 1):
                if 0 <= k < nb:
                    occ[k] += 1
        cov = [occ[k] >= 1 for k in range(nb)]  # 任意图形元素都算"有内容"
        i = int(y0 // _BIN_PT)
        run_start = i
        while i < nb and cov[i]:                 # 顶部第一块内容（疑似 Logo/页眉）
            i += 1
        block_h = (i - run_start) * _BIN_PT
        gap_start = i
        while i < nb and not cov[i]:             # 其后的空隙
            i += 1
        gap_h = (i - gap_start) * _BIN_PT
        if (block_h < _HEADER_MAX_PT and gap_h >= _HEADER_GAP_PT
                and (run_start * _BIN_PT) < _LOGO_TOP_FRAC * h and i < nb):
            y0 = i * _BIN_PT  # 跳过页眉小块，从正图开始
    return (max(0.0, x0), max(0.0, y0), x1, y1)


def _make_crop(pdf_path: str, idx: int, pil, vlm_bbox):
    """优先用确定性图形框裁剪（排除题注/页脚/正文/顶部 Logo）；失败回落 VLM bbox。"""
    box_pt = None
    try:
        box_pt = _graphics_crop_box(pdf_path, idx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graphics 裁剪失败，回落 VLM bbox：%s", exc)
    if box_pt:
        w, h = pil.size
        x0, y0, x1, y1 = (int(v * _RENDER_SCALE) for v in box_pt)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 - x0 >= _MIN_CROP_PX and y1 - y0 >= _MIN_CROP_PX:
            return pil.crop((x0, y0, x1, y1))
    return _crop_bbox(pil, vlm_bbox or [0, 0, 1, 1])


# 裁剪逻辑版本：升级裁剪算法时 +1。缓存命中但版本陈旧 → 按缓存的 page 就地重切（不重调 VLM）。
# v3: 算并集前剔除「贯穿大半页的细规则线」（分栏线/页眉页脚横线），避免裁进正文
_CROP_VERSION = 3


def _crop_version_path(out: Path) -> Path:
    return out / ".crop_version"


def _read_crop_version(out: Path) -> int:
    try:
        return int(_crop_version_path(out).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_manifest(manifest: Path, figs: List[Figure]) -> None:
    manifest.write_text(
        json.dumps([asdict(f) for f in figs], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    _crop_version_path(manifest.parent).write_text(str(_CROP_VERSION), encoding="utf-8")


def _recrop_cached(pdf_path: str, figs: List[Figure]) -> List[Figure]:
    """裁剪逻辑升级后，用缓存图的 page 信息就地重切（覆盖 jpg、更新尺寸）；不重调 VLM。"""
    for f in figs:
        try:
            pil = _render(pdf_path, f.page - 1)
            crop = _make_crop(pdf_path, f.page - 1, pil, [0, 0, 1, 1])
            crop.save(f.image_path, quality=85)
            f.width, f.height = crop.width, crop.height
        except Exception as exc:  # noqa: BLE001 - 单张重切失败不阻断
            logger.warning("重切图%s(p%d)失败：%s", f.label, f.page, exc)
    return figs


def extract_figures_via_vision(
    pdf_path: str, out_dir: str, *, max_pages: Optional[int] = None, use_cache: bool = True,
) -> List[Figure]:
    """渲染含图页 → VLM 给图号+bbox → 裁整图 → 返回 Figure 列表（按图号，可喂 match_figure）。

    缓存到 out_dir/vision_figures_manifest.json；同图号只取首张。VLM/网络失败的页跳过。
    裁剪逻辑升级（``_CROP_VERSION`` 变化）时，缓存命中也会就地重切（不重调 VLM）。
    """
    out = Path(out_dir)
    manifest = out / "vision_figures_manifest.json"
    if use_cache and manifest.exists():
        try:
            figs = [Figure(**d) for d in json.loads(manifest.read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            figs = None
        if figs is not None:
            if _read_crop_version(out) == _CROP_VERSION:
                return figs
            logger.info("裁剪逻辑已升级(v%s)，就地重切 %d 张缓存图（不调 VLM）", _CROP_VERSION, len(figs))
            figs = _recrop_cached(pdf_path, figs)
            _write_manifest(manifest, figs)
            return figs
    out.mkdir(parents=True, exist_ok=True)
    client = _client()
    model = os.getenv("VISION_MODEL", "kimi-k2.6").strip()
    figs: List[Figure] = []
    seen: set = set()
    for idx in _image_pages(pdf_path, max_pages):
        pil = _render(pdf_path, idx)
        try:
            info = _ask_page(client, model, pil)
        except Exception as exc:  # noqa: BLE001 - 单页失败不阻断
            logger.warning("VLM 第 %d 页失败：%s", idx + 1, exc)
            continue
        if not info or not info.get("has_figure"):
            continue
        num = re.sub(r"[^0-9]", "", str(info.get("figure_number") or ""))
        if not num or num in seen:
            continue
        crop = _make_crop(pdf_path, idx, pil, info.get("bbox"))
        fp = out / f"figvis{num}_p{idx + 1}.jpg"
        crop.save(str(fp), quality=85)
        figs.append(Figure(
            label=num, is_extended=False, caption=str(info.get("caption") or ""),
            page=idx + 1, image_path=str(fp), width=crop.width, height=crop.height,
        ))
        seen.add(num)
        logger.info("VLM 第 %d 页 → 图%s（%s）", idx + 1, num, str(info.get("caption") or "")[:30])
    _write_manifest(manifest, figs)
    logger.info("VLM 抽到 %d 张图：%s", len(figs), sorted(f.label for f in figs))
    return figs
