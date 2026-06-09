"""中文 Markdown 学术内容 → 目标语言 Markdown 翻译（仅 blog 链路用）。

移植自 target-running 的 ``openclaw-academic-translation``，**源语言由英文改为中文**：

- 翻译规则   → ``prompts/translation.system.md``
- 术语表     → ``data/translation_glossary.csv``（``中文`` 列为源，其余列为目标语）
- 不翻译名单 → ``data/do_not_translate.txt``

公众号链路用中文原文、不翻译；只有 blog（genemedi {lang}）链路需要本模块。

翻译默认用 DeepSeek ``deepseek-chat``、temperature=0（确定性任务）。某些模型支持
"关闭思考模式"以更快更省（env ``TRANSLATION_THINKING_DISABLED=true`` 时透传，
默认不发，避免 deepseek-chat 不识别该参数而报错）。
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from utils.logger import setup_logger

logger = setup_logger("translator")

_BASE_DIR = Path(__file__).resolve().parents[1]
_PROMPT_PATH = _BASE_DIR / "prompts" / "translation.system.md"
_GLOSSARY_PATH = _BASE_DIR / "data" / "translation_glossary.csv"
_DNT_PATH = _BASE_DIR / "data" / "do_not_translate.txt"

#: 目标语言码 -> (英文可读名, glossary.csv 中对应列名)。源语言中文不在此表内。
SUPPORTED_LANGS: Dict[str, Tuple[str, str]] = {
    "en": ("English", "英文"),
    "ja": ("Japanese", "日语"),
    "ko": ("Korean", "韩语"),
    "ru": ("Russian", "俄语"),
}
#: 术语表里的源语言列名（中文原文）。
_SOURCE_COLUMN = "中文"

_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_MAX_TOKENS = 16000


class TranslationError(Exception):
    """翻译初始化失败（如缺少 API Key）。"""


@dataclass
class TranslationResult:
    """单次翻译结果。失败时 ``success=False`` 且 ``error`` 非空。"""

    success: bool
    lang: str
    translated_markdown: str = ""
    model: str = ""
    total_tokens: int = 0
    error: str = ""


# ---- 资产加载（带模块级缓存）-------------------------------------------

_prompt_cache: Optional[str] = None
_glossary_cache: Optional[List[Dict[str, str]]] = None
_dnt_cache: Optional[List[str]] = None


def _load_system_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return _prompt_cache


def _load_glossary() -> List[Dict[str, str]]:
    global _glossary_cache
    if _glossary_cache is None:
        rows: List[Dict[str, str]] = []
        if _GLOSSARY_PATH.exists():
            with _GLOSSARY_PATH.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        _glossary_cache = rows
    return _glossary_cache


def _load_do_not_translate() -> List[str]:
    global _dnt_cache
    if _dnt_cache is None:
        terms: List[str] = []
        if _DNT_PATH.exists():
            for line in _DNT_PATH.read_text(encoding="utf-8").splitlines():
                term = line.strip()
                if term and not term.startswith("#"):
                    terms.append(term)
        _dnt_cache = terms
    return _dnt_cache


def _glossary_block(lang: str) -> str:
    """为目标语言构造 ``中文 => target`` 术语行。"""
    _name, column = SUPPORTED_LANGS[lang]
    lines: List[str] = []
    for row in _load_glossary():
        source = (row.get(_SOURCE_COLUMN) or "").strip()
        target = (row.get(column) or "").strip()
        if source and target:
            lines.append(f"{source} => {target}")
    return "\n".join(lines)


def _build_user_message(lang: str, markdown: str) -> str:
    language_name, _column = SUPPORTED_LANGS[lang]
    glossary = _glossary_block(lang) or "(none)"
    dnt = "\n".join(_load_do_not_translate()) or "(none)"
    return (
        f"Translate the following Simplified Chinese Markdown into {language_name}.\n\n"
        f"## GLOSSARY (Chinese => {language_name}) — use these exact target terms\n"
        f"{glossary}\n\n"
        f"## DO-NOT-TRANSLATE — keep these exactly as the original\n"
        f"{dnt}\n\n"
        f"## SOURCE MARKDOWN\n"
        f"{markdown}"
    )


def _strip_outer_code_fence(text: str) -> str:
    """模型偶尔把整篇输出包进 ``` 围栏 —— 去掉最外层。"""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return text


def _thinking_disabled() -> bool:
    return os.getenv("TRANSLATION_THINKING_DISABLED", "").strip().lower() in ("1", "true", "yes")


# ---- 翻译客户端 ---------------------------------------------------------

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise TranslationError("DEEPSEEK_API_KEY 未配置")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()
        timeout = float(os.getenv("TRANSLATION_TIMEOUT", "180") or 180)
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    return _client


def translate_markdown(
    markdown: str,
    lang: str,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
    max_retries: int = 2,
) -> TranslationResult:
    """把中文 Markdown 翻译成 ``lang``（en/ja/ko/ru）。

    Args:
        markdown: 中文源 Markdown。
        lang: 目标语言码（en/ja/ko/ru）；中文是源，不能作为目标。
        client: 可注入的 LLM 客户端（测试用）；默认走 DeepSeek。
        model: 翻译模型；默认 env ``TRANSLATION_MODEL`` 或 deepseek-chat。
        max_retries: 失败重试次数。

    Returns:
        TranslationResult；失败时 ``success=False`` 并带 ``error``。
    """
    if lang not in SUPPORTED_LANGS:
        return TranslationResult(False, lang, error=f"unsupported lang: {lang}")
    if not (markdown or "").strip():
        return TranslationResult(False, lang, error="empty source markdown")

    model = model or os.getenv("TRANSLATION_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    max_tokens = int(
        os.getenv("TRANSLATION_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS)) or _DEFAULT_MAX_TOKENS
    )
    llm = client or _get_client()
    messages = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "user", "content": _build_user_message(lang, markdown)},
    ]

    last_error = ""
    for attempt in range(1, max_retries + 2):
        try:
            kwargs: Dict[str, Any] = dict(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                messages=messages,
            )
            if _thinking_disabled():
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = llm.chat.completions.create(**kwargs)
            text = _strip_outer_code_fence((response.choices[0].message.content or "").strip())
            if not text:
                last_error = "empty translation output"
                logger.warning("translate_markdown %s attempt %d: empty output", lang, attempt)
                continue
            usage = getattr(response, "usage", None)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            return TranslationResult(
                True,
                lang,
                translated_markdown=text,
                model=model,
                total_tokens=total_tokens,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning("translate_markdown %s attempt %d failed: %s", lang, attempt, exc)

    return TranslationResult(
        False, lang, model=model, error=last_error or "translation failed"
    )
