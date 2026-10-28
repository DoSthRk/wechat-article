"""Central typed accessors for environment variables read in multiple modules.

Functions (not constants) so that runtime ``load_dotenv(override=True)`` calls
in ``app.build_child_process_env`` continue to take effect. Each accessor
preserves the exact semantics (.strip(), default, type) used at the original
call sites it replaces.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional
from urllib.parse import urlsplit


def asset_storage_mode(legacy_variable: str, env: Optional[Mapping[str, str]] = None) -> str:
    """One explicit setting switches both PDF and image storage together."""
    values = os.environ if env is None else env
    mode = values.get("ARTICLE_ASSET_STORAGE")
    if mode is None:
        mode = values.get(legacy_variable, "oss")
    return mode.strip().lower()


def asset_public_base_url(fallback: str = "", env: Optional[Mapping[str, str]] = None) -> str:
    values = os.environ if env is None else env
    base = values.get("ARTICLE_ASSET_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        return fallback.strip().rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("ARTICLE_ASSET_PUBLIC_BASE_URL must be an HTTPS origin without a path")
    return base


def _str_env(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or default).strip()


def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# --- API keys (read in 3-4 modules each) ---------------------------------

def moonshot_api_key() -> str:
    """``MOONSHOT_API_KEY`` (no default; empty string when unset)."""
    return os.getenv("MOONSHOT_API_KEY", "")


def deepseek_api_key() -> str:
    """``DEEPSEEK_API_KEY`` stripped; empty when unset."""
    return _str_env("DEEPSEEK_API_KEY")


def relay_kimi_api_key() -> str:
    return _str_env("RELAY_KIMI_API_KEY")


def relay_gemini_api_key() -> str:
    return _str_env("RELAY_GEMINI_API_KEY")


# --- Base URLs (read in 2-3 modules each) --------------------------------

def relay_kimi_base_url() -> str:
    return _str_env("RELAY_KIMI_BASE_URL")


def relay_gemini_base_url() -> str:
    return _str_env("RELAY_GEMINI_BASE_URL")


# --- Numeric tunables ----------------------------------------------------

RELAY_GEMINI_MAX_TOKENS_DEFAULT = 8000


def relay_gemini_max_tokens() -> int:
    """``RELAY_GEMINI_MAX_TOKENS`` parsed as int with 8000 default."""
    return _int_env("RELAY_GEMINI_MAX_TOKENS", RELAY_GEMINI_MAX_TOKENS_DEFAULT)
