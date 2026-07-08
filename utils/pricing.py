"""按 token 估算每篇文章的生成成本（人民币 CNY）。

成本主要来自 LLM 正文生成（DeepSeek）——prompt/completion token 已落库到 ``articles`` 表。
配图首选「题注锚定」不耗 token；VLM 仅在回落时才有，且未按篇计费，故不计入这里。

单价默认取官方价快照，可用环境变量覆盖（改价时不用动代码）：
    PRICE_DEEPSEEK_INPUT_CNY_PER_1M   默认 2.0   （deepseek-chat 输入，缓存未命中价）
    PRICE_DEEPSEEK_OUTPUT_CNY_PER_1M  默认 8.0   （deepseek-chat 输出）
    PRICE_DEFAULT_INPUT_CNY_PER_1M    默认 2.0   （未知模型兜底）
    PRICE_DEFAULT_OUTPUT_CNY_PER_1M   默认 8.0
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

_PER_M = 1_000_000.0


def _f(env: str, default: float) -> float:
    try:
        v = os.getenv(env)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def model_rates(model: str) -> Tuple[float, float]:
    """该模型的 (输入, 输出) 单价（CNY / 1M tokens）。"""
    m = (model or "").strip().lower()
    if m.startswith("deepseek"):
        return (_f("PRICE_DEEPSEEK_INPUT_CNY_PER_1M", 2.0),
                _f("PRICE_DEEPSEEK_OUTPUT_CNY_PER_1M", 8.0))
    return (_f("PRICE_DEFAULT_INPUT_CNY_PER_1M", 2.0),
            _f("PRICE_DEFAULT_OUTPUT_CNY_PER_1M", 8.0))


def article_cost_cny(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """单篇文章 LLM 生成成本（CNY）。token 缺失按 0 计。"""
    rin, rout = model_rates(model)
    p = int(prompt_tokens or 0)
    c = int(completion_tokens or 0)
    return (p * rin + c * rout) / _PER_M


def rate_card() -> Dict[str, Dict[str, float]]:
    """当前生效单价（供面板展示，让成本口径透明）。"""
    din, dout = model_rates("deepseek-chat")
    return {"deepseek-chat": {"input_per_1m": din, "output_per_1m": dout, "currency": "CNY"}}
