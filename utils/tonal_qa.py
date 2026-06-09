"""文章调性自审（静态扫描）—— 杜绝硬广腔和"正文夹带产品"流到投放。

两类检查：
1. **硬广词黑名单**：命中营销腔 / 爽词，每命中 1 个扣 10 分。
2. **方案 B 专属：正文夹带产品**——正文（结尾段之前）若出现产品名即违规
   （方案 B 要求产品只在结尾一段出现一次）。

只做静态扫描，不调 LLM（确定、便宜、可单测）；LLM 调性评分留后续按需加。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

DEFAULT_THRESHOLD = 60
_HARD_AD_PENALTY = 10
_BODY_LEAK_PENALTY = 40


@dataclass(frozen=True)
class TonalScanResult:
    score: int
    hard_ad_hits: List[str] = field(default_factory=list)
    body_product_leak: bool = False
    suggestions: List[str] = field(default_factory=list)
    blocked: bool = False


def load_hard_ad_words(path: str) -> List[str]:
    """读硬广词黑名单；每行一个，``#`` 开头是注释。文件缺失返回空表。"""
    p = Path(path)
    if not p.exists():
        return []
    words: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            words.append(word)
    return words


def _split_body_and_closing(markdown: str) -> Tuple[str, str]:
    """把最后一个非空段落当"结尾段"，其余为正文（方案 B 产品只许在结尾段）。"""
    paras = [p for p in (markdown or "").split("\n\n") if p.strip()]
    if not paras:
        return "", ""
    return "\n\n".join(paras[:-1]), paras[-1]


def scan_static(
    markdown: str,
    hard_ad_words: List[str],
    product_name: str = "",
    threshold: int = DEFAULT_THRESHOLD,
) -> TonalScanResult:
    """静态调性扫描。``product_name`` 非空时检查正文是否夹带产品。"""
    md = markdown or ""
    hits: List[str] = []
    for word in hard_ad_words:
        if word and word in md and word not in hits:
            hits.append(word)
    score = 100 - len(hits) * _HARD_AD_PENALTY
    suggestions = [f"将“{word}”改为更中性的表述" for word in hits]

    body_leak = False
    name = (product_name or "").strip()
    if name:
        body, _closing = _split_body_and_closing(md)
        if name in body:
            body_leak = True
            score -= _BODY_LEAK_PENALTY
            suggestions.append(
                f"正文夹带了产品名“{name}”——方案 B 要求产品只在结尾一段出现一次"
            )

    score = max(0, min(100, score))
    return TonalScanResult(
        score=score,
        hard_ad_hits=hits,
        body_product_leak=body_leak,
        suggestions=suggestions,
        blocked=(score < threshold or body_leak),
    )
