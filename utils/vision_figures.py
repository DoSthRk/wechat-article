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
    """按归一化 bbox 裁图；越界/过小则回落整页。"""
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return pil
    w, h = pil.size
    box = (max(0, int(x0 * w)), max(0, int(y0 * h)), min(w, int(x1 * w)), min(h, int(y1 * h)))
    if box[2] - box[0] < _MIN_CROP_PX or box[3] - box[1] < _MIN_CROP_PX:
        return pil
    return pil.crop(box)


def extract_figures_via_vision(
    pdf_path: str, out_dir: str, *, max_pages: Optional[int] = None, use_cache: bool = True,
) -> List[Figure]:
    """渲染含图页 → VLM 给图号+bbox → 裁整图 → 返回 Figure 列表（按图号，可喂 match_figure）。

    缓存到 out_dir/vision_figures_manifest.json；同图号只取首张。VLM/网络失败的页跳过。
    """
    out = Path(out_dir)
    manifest = out / "vision_figures_manifest.json"
    if use_cache and manifest.exists():
        try:
            return [Figure(**d) for d in json.loads(manifest.read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            pass
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
        crop = _crop_bbox(pil, info.get("bbox") or [0, 0, 1, 1])
        fp = out / f"figvis{num}_p{idx + 1}.jpg"
        crop.save(str(fp), quality=85)
        figs.append(Figure(
            label=num, is_extended=False, caption=str(info.get("caption") or ""),
            page=idx + 1, image_path=str(fp), width=crop.width, height=crop.height,
        ))
        seen.add(num)
        logger.info("VLM 第 %d 页 → 图%s（%s）", idx + 1, num, str(info.get("caption") or "")[:30])
    manifest.write_text(
        json.dumps([asdict(f) for f in figs], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("VLM 抽到 %d 张图：%s", len(figs), sorted(f.label for f in figs))
    return figs
