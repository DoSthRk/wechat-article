"""翻译图片标记的规范化及源稿一致性检查，不触发网络或文件写入。"""
from __future__ import annotations

import re

_MARKER = re.compile(
    r"\[(?:图片|画像|図|이미지|그림|image|picture|изображение|рисунок)\s*[:：]\s*([^\[\]\n]+?)\]",
    re.IGNORECASE,
)
_CANONICAL = re.compile(r"\[图片:([^\[\]\n]+?)\]")
_FIGURE = re.compile(r"\b(extended\s+data\s+)?fig(?:ure)?\.?\s*(\d+)\b", re.IGNORECASE)


def normalize_image_placeholders(content: str) -> str:
    """只修正标记前缀，保留译文图注及图号。"""
    return _MARKER.sub(lambda match: f"[图片:{match.group(1).strip()}]", content)


def validate_image_placeholders(source: str, translated: str) -> None:
    """翻译不得丢失、增加或调换源稿图片；图注文字允许翻译。"""
    source_descriptions = _CANONICAL.findall(normalize_image_placeholders(source))
    target_descriptions = _CANONICAL.findall(normalize_image_placeholders(translated))
    if len(source_descriptions) != len(target_descriptions):
        raise ValueError("image_placeholder_mismatch: 译文图片数量与源稿不一致")
    source_keys = [_FIGURE.search(description) for description in source_descriptions]
    target_keys = [_FIGURE.search(description) for description in target_descriptions]
    if source_keys and all(source_keys):
        def keys(matches):
            return [(bool(m.group(1)), int(m.group(2))) if m else None for m in matches]
        if keys(source_keys) != keys(target_keys):
            raise ValueError("image_placeholder_mismatch: 译文图号或顺序与源稿不一致")
