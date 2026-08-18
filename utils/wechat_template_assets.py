"""Upload and append reusable visual assets used by WeChat article templates."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PDF_GUIDE_PATH = PROJECT_ROOT / "assets" / "source-pdf-guide-v1.png"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "runtime" / "wechat_template_asset_urls.json"
_CACHE_LOCK = threading.Lock()


class TemplateAssetError(RuntimeError):
    """A required WeChat template asset could not be prepared."""


def _cache_path() -> Path:
    configured = os.getenv("WECHAT_TEMPLATE_ASSET_CACHE", "").strip()
    return Path(configured) if configured else DEFAULT_CACHE_PATH


def _read_cache(path: Path) -> Dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(url) for key, url in value.items() if str(url).startswith("https://")}


def _write_cache(path: Path, cache: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def upload_source_pdf_guide(client: Any, account: str) -> str:
    """Return this account's permanent WeChat URL for the versioned guide PNG."""
    try:
        payload = SOURCE_PDF_GUIDE_PATH.read_bytes()
    except OSError as exc:
        raise TemplateAssetError(f"阅读原文引导图不可用：{exc}") from exc

    digest = hashlib.sha256(payload).hexdigest()
    # v2 preserves the exact scheme returned by media/uploadimg. WeChat may
    # discard an image when its URL is rewritten before draft/update.
    cache_key = f"v2:{account or 'default'}:{digest}"
    path = _cache_path()
    with _CACHE_LOCK:
        cache = _read_cache(path)
        cached = cache.get(cache_key, "")
        if cached:
            return cached
        try:
            url = str(client.upload_image(str(SOURCE_PDF_GUIDE_PATH)) or "").strip()
        except Exception as exc:
            raise TemplateAssetError(f"阅读原文引导图上传失败：{exc}") from exc
        if not url.startswith(("http://", "https://")):
            raise TemplateAssetError("阅读原文引导图上传后未返回有效图片地址")
        cache[cache_key] = url
        try:
            _write_cache(path, cache)
        except OSError as exc:
            raise TemplateAssetError(f"阅读原文引导图缓存写入失败：{exc}") from exc
        return url


def source_pdf_guide_html(image_url: str) -> str:
    """Build the final full-width footer block shown immediately above 阅读原文."""
    return (
        '<section data-source-pdf-guide="1" style="margin:28px 0 0;line-height:0;">'
        f'<img src="{image_url}" data-src="{image_url}" '
        'alt="点击文末阅读原文获取原PDF" data-type="png" data-w="1080" '
        'data-ratio="0.4444444444444444" '
        'style="display:block;width:100%;height:auto!important;border-radius:8px;" />'
        '</section>'
    )


def contains_source_pdf_guide(html: str, image_url: str) -> bool:
    """Match the uploaded image even if WeChat rewrites scheme or query parameters."""
    parsed = urlsplit(image_url)
    return bool(parsed.netloc and len(parsed.path) > 1 and parsed.netloc in html and parsed.path in html)


def append_source_pdf_guide(html: str, client: Any, account: str) -> Tuple[str, str]:
    """Append the required guide once and return ``(content, uploaded_url)``."""
    image_url = upload_source_pdf_guide(client, account)
    if contains_source_pdf_guide(html, image_url):
        return html, image_url
    return html + source_pdf_guide_html(image_url), image_url
