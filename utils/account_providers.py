from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib import error, request
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv

from utils.moonshot_billing import MoonshotAPIClient

load_dotenv()


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _env_bool_local(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _origin_from_url(url: str) -> str:
    raw = _normalize_text(url)
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return raw.rstrip("/")


def _request_json(
    *,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    query: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if query:
        url = f"{url}?{urlencode(query)}"
    req = request.Request(url, method=method.upper(), headers=headers or {})
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class DeepSeekAccountClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = _normalize_text(api_key or os.getenv("DEEPSEEK_API_KEY", ""))
        self.base_url = _origin_from_url(base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    def _request_json(self, path: str) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        return _request_json(
            url=f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )

    def get_balance(self) -> Dict[str, Any]:
        try:
            raw = self._request_json("/user/balance")
            balances = raw.get("balance_infos", []) if isinstance(raw, dict) else []
            first = balances[0] if balances else {}
            return {
                "success": True,
                "is_available": bool(raw.get("is_available", True)) if isinstance(raw, dict) else True,
                "currency": _normalize_text(first.get("currency")) or "CNY",
                "total_balance": _to_float(first.get("total_balance")),
                "granted_balance": _to_float(first.get("granted_balance")),
                "topped_up_balance": _to_float(first.get("topped_up_balance")),
                "raw": raw,
            }
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"success": False, "error": f"HTTP {exc.code}", "detail": body[:500]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class QiniuBillingClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = _normalize_text(api_key or os.getenv("RELAY_KIMI_API_KEY", ""))
        fallback_base = os.getenv("QINIU_BILLING_BASE_URL", "") or os.getenv("RELAY_KIMI_BASE_URL", "")
        self.base_url = _origin_from_url(base_url or fallback_base)

    def _request_json(self, period: str) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("RELAY_KIMI_API_KEY is not configured")
        if not self.base_url:
            raise ValueError("QINIU_BILLING_BASE_URL or RELAY_KIMI_BASE_URL is not configured")
        return _request_json(
            url=f"{self.base_url}/v2/stat/usage/apikey/cost",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            query={"type": period},
        )

    @staticmethod
    def _extract_total_fee(raw: Dict[str, Any]) -> float:
        api_keys = (((raw or {}).get("data") or {}).get("api_keys") or [])
        total = 0.0
        for item in api_keys:
            if isinstance(item, dict):
                total += _to_float(item.get("total_fee"))
        return round(total, 4)

    def get_cost_summary(self) -> Dict[str, Any]:
        try:
            day_raw = self._request_json("day")
            week_raw = self._request_json("week")
            month_raw = self._request_json("month")
            return {
                "success": True,
                "currency": "CNY",
                "costs": {
                    "day": self._extract_total_fee(day_raw),
                    "week": self._extract_total_fee(week_raw),
                    "month": self._extract_total_fee(month_raw),
                },
                "raw": {"day": day_raw, "week": week_raw, "month": month_raw},
            }
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"success": False, "error": f"HTTP {exc.code}", "detail": body[:500]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class WellAPIAccountClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, account_path: Optional[str] = None):
        self.api_key = _normalize_text(api_key or os.getenv("WELLAPI_API_KEY", "") or os.getenv("RELAY_GEMINI_API_KEY", ""))
        fallback_base = os.getenv("WELLAPI_BASE_URL", "") or os.getenv("RELAY_GEMINI_BASE_URL", "")
        self.base_url = _origin_from_url(base_url or fallback_base)
        self.account_path = _normalize_text(account_path or os.getenv("WELLAPI_ACCOUNT_PATH", "/api/user/self")) or "/api/user/self"

    def _request_json(self) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("WELLAPI_API_KEY or RELAY_GEMINI_API_KEY is not configured")
        if not self.base_url:
            raise ValueError("WELLAPI_BASE_URL or RELAY_GEMINI_BASE_URL is not configured")
        return _request_json(
            url=f"{self.base_url}/{self.account_path.lstrip('/')}",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )

    def get_account_summary(self) -> Dict[str, Any]:
        try:
            raw = self._request_json()
            data = raw.get("data", raw) if isinstance(raw, dict) else {}
            numeric_keys = [
                "balance",
                "quota",
                "remaining_quota",
                "remain_quota",
                "remainBalance",
                "credit",
                "credits",
            ]
            balance = None
            matched_key = ""
            for key in numeric_keys:
                if key in data and str(data.get(key)).strip() != "":
                    balance = _to_float(data.get(key))
                    matched_key = key
                    break
            account_label = (
                _normalize_text(data.get("email"))
                or _normalize_text(data.get("username"))
                or _normalize_text(data.get("name"))
                or _normalize_text(data.get("id"))
                or "账号已配置"
            )
            return {
                "success": True,
                "metric_type": "quota" if balance is not None else "account",
                "balance": balance,
                "balance_key": matched_key,
                "currency": _normalize_text(data.get("currency")) or "CNY",
                "account_label": account_label,
                "account_status": _normalize_text(data.get("status")) or _normalize_text(data.get("plan")),
                "raw": raw,
            }
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"success": False, "error": f"HTTP {exc.code}", "detail": body[:500]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


def build_accounts_overview(
    *,
    moonshot_payload: Dict[str, Any],
    deepseek_payload: Dict[str, Any],
    qiniu_payload: Dict[str, Any],
    wellapi_payload: Dict[str, Any],
) -> Dict[str, Any]:
    now = datetime.now().isoformat()
    providers: List[Dict[str, Any]] = []

    providers.append(
        {
            "provider": "moonshot",
            "label": "Moonshot",
            "fetched_at": now,
            "configured": bool(os.getenv("MOONSHOT_API_KEY", "")),
            "status": "ok" if moonshot_payload.get("success") else ("not_configured" if "configured" in str(moonshot_payload.get("error", "")).lower() else "error"),
            "metric_type": "balance",
            "currency": moonshot_payload.get("currency", "CNY"),
            "available_balance": float(moonshot_payload.get("available_balance", 0) or 0),
            "voucher_balance": float(moonshot_payload.get("voucher_balance", 0) or 0),
            "cash_balance": float(moonshot_payload.get("cash_balance", 0) or 0),
            "primary_text": f"¥{float(moonshot_payload.get('available_balance', 0) or 0):.2f}",
            "secondary_text": f"代金券 ¥{float(moonshot_payload.get('voucher_balance', 0) or 0):.2f} | 现金 ¥{float(moonshot_payload.get('cash_balance', 0) or 0):.2f}",
            "error": moonshot_payload.get("error"),
        }
    )

    providers.append(
        {
            "provider": "deepseek",
            "label": "DeepSeek",
            "fetched_at": now,
            "configured": bool(os.getenv("DEEPSEEK_API_KEY", "")),
            "status": "ok" if deepseek_payload.get("success") else ("not_configured" if "configured" in str(deepseek_payload.get("error", "")).lower() else "error"),
            "metric_type": "balance",
            "currency": deepseek_payload.get("currency", "CNY"),
            "is_available": bool(deepseek_payload.get("is_available", False)),
            "total_balance": float(deepseek_payload.get("total_balance", 0) or 0),
            "granted_balance": float(deepseek_payload.get("granted_balance", 0) or 0),
            "topped_up_balance": float(deepseek_payload.get("topped_up_balance", 0) or 0),
            "primary_text": f"¥{float(deepseek_payload.get('total_balance', 0) or 0):.2f}",
            "secondary_text": f"赠送 ¥{float(deepseek_payload.get('granted_balance', 0) or 0):.2f} | 充值 ¥{float(deepseek_payload.get('topped_up_balance', 0) or 0):.2f}",
            "error": deepseek_payload.get("error"),
        }
    )

    qiniu_costs = qiniu_payload.get("costs", {}) if isinstance(qiniu_payload, dict) else {}
    providers.append(
        {
            "provider": "qiniu_relay_kimi",
            "label": "Qiniu Relay Kimi",
            "fetched_at": now,
            "configured": bool(os.getenv("RELAY_KIMI_API_KEY", "")),
            "status": "ok" if qiniu_payload.get("success") else ("not_configured" if "configured" in str(qiniu_payload.get("error", "")).lower() else "error"),
            "metric_type": "cost",
            "currency": qiniu_payload.get("currency", "CNY"),
            "costs": {
                "day": float(qiniu_costs.get("day", 0) or 0),
                "week": float(qiniu_costs.get("week", 0) or 0),
                "month": float(qiniu_costs.get("month", 0) or 0),
            },
            "primary_text": f"本月 ¥{float(qiniu_costs.get('month', 0) or 0):.2f}",
            "secondary_text": f"今日 ¥{float(qiniu_costs.get('day', 0) or 0):.2f} | 本周 ¥{float(qiniu_costs.get('week', 0) or 0):.2f}",
            "error": qiniu_payload.get("error"),
        }
    )

    wellapi_balance = wellapi_payload.get("balance")
    providers.append(
        {
            "provider": "wellapi_relay_gemini",
            "label": "WellAPI Relay Gemini",
            "fetched_at": now,
            "configured": bool(os.getenv("WELLAPI_API_KEY", "") or os.getenv("RELAY_GEMINI_API_KEY", "")),
            "status": "ok" if wellapi_payload.get("success") else ("not_configured" if "configured" in str(wellapi_payload.get("error", "")).lower() else "error"),
            "metric_type": wellapi_payload.get("metric_type", "account"),
            "currency": wellapi_payload.get("currency", "CNY"),
            "balance": None if wellapi_balance is None else float(wellapi_balance),
            "account_label": wellapi_payload.get("account_label", "账号已配置"),
            "account_status": wellapi_payload.get("account_status", ""),
            "primary_text": (
                f"¥{float(wellapi_balance):.2f}" if wellapi_balance is not None else str(wellapi_payload.get("account_label", "账号已配置"))
            ),
            "secondary_text": (
                str(wellapi_payload.get("account_status", "")).strip()
                or ("字段已返回，等待进一步细化" if wellapi_payload.get("success") else "")
            ),
            "error": wellapi_payload.get("error"),
        }
    )

    uniprot_enabled = _env_bool_local("UNIPROT_ENABLED", True)
    providers.append(
        {
            "provider": "uniprot",
            "label": "UniProt REST API",
            "fetched_at": now,
            "configured": True,
            "status": "ok" if uniprot_enabled else "disabled",
            "metric_type": "info",
            "primary_text": "无需 API Key",
            "secondary_text": f"直查 UniProt 权威库（organism_id={os.getenv('UNIPROT_ORGANISM_ID', '9606')}），{'已启用' if uniprot_enabled else '已禁用（UNIPROT_ENABLED=false）'}",
            "error": None,
            "docs_url": "https://www.uniprot.org/help/api_queries",
        }
    )

    return {"fetched_at": now, "providers": providers}
