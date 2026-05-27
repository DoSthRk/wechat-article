"""
Moonshot billing helpers:
1. Query account balance
2. Estimate kimi-k2.5 / kimi-k2.6 task cost from token usage

The native-route model is configurable via NATIVE_KIMI_MODEL; use
get_native_kimi_pricing() / estimate_native_kimi_cost() so dashboard cost
estimates track the actual model in use instead of being locked to k2.5.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib import error, request

from dotenv import load_dotenv

load_dotenv()


def _to_float(value: Optional[str], default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_kimi_k25_pricing() -> Dict[str, Any]:
    """
    Return active pricing for kimi-k2.5.
    Defaults are from Moonshot docs (checked on 2026-03-27).
    Env vars can override when pricing changes.
    """
    prompt_price = _to_float(
        os.getenv("KIMI_K25_PROMPT_PRICE_CNY_PER_1M"),
        4.0,
    )
    completion_price = _to_float(
        os.getenv("KIMI_K25_COMPLETION_PRICE_CNY_PER_1M"),
        21.0,
    )
    caching_hit_price = _to_float(
        os.getenv("KIMI_K25_CACHING_HIT_PRICE_CNY_PER_1M"),
        0.7,
    )

    return {
        "model": "kimi-k2.5",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "prompt_price_per_1m": prompt_price,
        "completion_price_per_1m": completion_price,
        "caching_hit_price_per_1m": caching_hit_price,
        "pricing_source": "moonshot_docs_snapshot_2026-03-27",
    }


def get_kimi_k26_pricing() -> Dict[str, Any]:
    """
    Return active pricing for kimi-k2.6.
    Defaults are from Moonshot docs (checked on 2026-05-26).
    Env vars can override when pricing changes.

    Note: k2.6 splits input pricing into cache-hit vs cache-miss tiers.
    The "prompt_price_per_1m" used below is the cache-miss (worst-case)
    price, matching how get_kimi_k25_pricing reports its non-cached input.
    """
    prompt_price = _to_float(
        os.getenv("KIMI_K26_PROMPT_PRICE_CNY_PER_1M"),
        6.5,
    )
    completion_price = _to_float(
        os.getenv("KIMI_K26_COMPLETION_PRICE_CNY_PER_1M"),
        27.0,
    )
    caching_hit_price = _to_float(
        os.getenv("KIMI_K26_CACHING_HIT_PRICE_CNY_PER_1M"),
        1.1,
    )

    return {
        "model": "kimi-k2.6",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "prompt_price_per_1m": prompt_price,
        "completion_price_per_1m": completion_price,
        "caching_hit_price_per_1m": caching_hit_price,
        "pricing_source": "moonshot_docs_snapshot_2026-05-26",
    }


def get_native_kimi_pricing() -> Dict[str, Any]:
    """Dispatch pricing by NATIVE_KIMI_MODEL env (default kimi-k2.6)."""
    model = (os.getenv("NATIVE_KIMI_MODEL") or "").strip().lower()
    if model in ("kimi-k2.5", "kimi-k2_5", "k2.5"):
        return get_kimi_k25_pricing()
    # default to k2.6 (the new official model)
    return get_kimi_k26_pricing()


def _estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Dict[str, Any],
) -> Dict[str, Any]:
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)

    prompt_cost = prompt_tokens / 1_000_000 * float(pricing["prompt_price_per_1m"])
    completion_cost = completion_tokens / 1_000_000 * float(pricing["completion_price_per_1m"])
    total_cost = prompt_cost + completion_cost

    return {
        "currency": pricing["currency"],
        "model": pricing.get("model", "unknown"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_cost": round(prompt_cost, 6),
        "completion_cost": round(completion_cost, 6),
        "estimated_cost": round(total_cost, 6),
        "estimated_cost_text": f"{total_cost:.4f} {pricing['currency']}",
        "note": "Estimate excludes cache-hit split and tool/web-search extra billing.",
    }


def estimate_kimi_k25_cost(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Estimate cost using k2.5 prices (kept for callers that still want k2.5)."""
    return _estimate_cost(prompt_tokens, completion_tokens, pricing or get_kimi_k25_pricing())


def estimate_kimi_k26_cost(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Estimate cost using k2.6 prices."""
    return _estimate_cost(prompt_tokens, completion_tokens, pricing or get_kimi_k26_pricing())


def estimate_native_kimi_cost(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Estimate cost using whichever model NATIVE_KIMI_MODEL points to."""
    return _estimate_cost(prompt_tokens, completion_tokens, pricing or get_native_kimi_pricing())


class MoonshotAPIClient:
    """Tiny client for Moonshot Open Platform endpoints used by dashboard."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("MOONSHOT_API_KEY", "")
        self.base_url = (base_url or os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")).rstrip("/")

    def _request_json(self, path: str) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("MOONSHOT_API_KEY is not configured")

        url = f"{self.base_url}/{path.lstrip('/')}"
        req = request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        with request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            return json.loads(payload)

    def get_balance(self) -> Dict[str, Any]:
        """Query account balance from /v1/users/me/balance."""
        try:
            raw = self._request_json("/users/me/balance")
            data = raw.get("data", {}) if isinstance(raw, dict) else {}
            return {
                "success": bool(raw.get("status", True)) if isinstance(raw, dict) else True,
                "code": raw.get("code") if isinstance(raw, dict) else None,
                "available_balance": float(data.get("available_balance", 0) or 0),
                "voucher_balance": float(data.get("voucher_balance", 0) or 0),
                "cash_balance": float(data.get("cash_balance", 0) or 0),
                "currency": "CNY",
                "raw": raw,
            }
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return {
                "success": False,
                "error": f"HTTP {exc.code}",
                "detail": body[:500],
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }
