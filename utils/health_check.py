"""Markdown 健康度安全网 —— 拦截损坏 / 异常的生成稿。

移植自 target-running 的 ``_markdown_health_score``。返回 0-100，越低越可疑。
用于在 generate 阶段拦截"以 HTML 开头 / 含合并残留标记 / 过短 / 没标题 / 章节太少"
这类明显坏稿，不让它流到投放。
"""
from __future__ import annotations

import re

_MIN_LENGTH = 500
_MIN_H2 = 3


def markdown_health_score(markdown: str) -> int:
    """给生成稿打 0-100 分，低分表示疑似损坏输出。"""
    md = (markdown or "").strip()
    if not md:
        return 0
    score = 100
    if md.startswith("<"):                                  # HTML 串入（不是 markdown）
        score -= 80
    if "===PART_" in md:                                    # 多路合并残留标记
        score -= 80
    if len(md) < _MIN_LENGTH:                               # 太短，疑似截断
        score -= 50
    if not re.search(r"^#\s+\S+", md, flags=re.MULTILINE):  # 没 H1 标题
        score -= 35
    h2_count = len(re.findall(r"^##\s+\S+", md, flags=re.MULTILINE))
    if h2_count < _MIN_H2:                                  # H2 章节太少
        score -= (_MIN_H2 - h2_count) * 15
    return max(0, min(100, score))


def is_markdown_healthy(markdown: str, threshold: int = 30) -> bool:
    return markdown_health_score(markdown) >= threshold
