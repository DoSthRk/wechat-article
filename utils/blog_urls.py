"""Canonical public Blog URLs and publication readback checks."""
from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_PUBLIC_BASE_URLS = {
    "zh": "https://genemedi.cn",
    "en": "https://en.genemedi.com",
    "ja": "https://ja.genemedi.com",
    "ko": "https://ko.genemedi.com",
    "ru": "https://ru.genemedi.com",
    "fr": "https://fr.genemedi.com",
}


class BlogUrlError(Exception):
    """The expected public Blog article is missing or inconsistent."""


def blog_slug(job_id: str, lang: str) -> str:
    normalized = _SLUG_RE.sub("-", (job_id or "article").lower()).strip("-")
    return f"{normalized or 'article'}-{lang}"


def public_blog_url(
    job_id: str,
    lang: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    values = os.environ if env is None else env
    normalized_lang = (lang or "").strip().lower()
    if normalized_lang not in _DEFAULT_PUBLIC_BASE_URLS:
        raise BlogUrlError(f"Unsupported Blog language: {lang}")
    variable = f"GENEMEDI_BLOG_PUBLIC_BASE_URL_{normalized_lang.upper()}"
    base_url = (values.get(variable) or _DEFAULT_PUBLIC_BASE_URLS[normalized_lang]).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise BlogUrlError(f"{variable} must be an HTTPS origin without a path")
    return f"{base_url}/blog/{blog_slug(job_id, normalized_lang)}"


def verify_public_blog_url(url: str, timeout: float = 30.0) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or "/blog/" not in parsed.path:
        raise BlogUrlError("Blog URL must be an absolute HTTPS article URL")
    req = urllib_request.Request(
        url,
        headers={"User-Agent": "GeneMedi-WeChat-Article/1.0"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            prefix = response.read(4096).lower()
    except HTTPError as exc:
        raise BlogUrlError(f"Blog 公开页返回 HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise BlogUrlError(f"Blog 公开页不可访问：{exc}") from exc
    if status != 200:
        raise BlogUrlError(f"Blog 公开页返回 HTTP {status}")
    if "text/html" not in content_type or b"<html" not in prefix:
        raise BlogUrlError("Blog 公开页没有返回 HTML")
    return url


def resolve_published_blog_url(
    db: Any,
    job_pk: int,
    job_id: str,
    *,
    lang: str = "zh",
    verify: bool = True,
) -> str:
    expected = public_blog_url(job_id, lang)
    distribution = db.get_distribution(job_pk, "blog", account="genemedi", lang=lang)
    if distribution is None or distribution.publish_status != "published":
        raise BlogUrlError(f"{lang} Blog 尚未发布")
    actual = str(distribution.external_url or "").rstrip("/")
    if actual != expected:
        raise BlogUrlError(f"{lang} Blog 地址不是当前官网地址：{actual or 'empty'}")
    if verify:
        verify_public_blog_url(expected)
    return expected
