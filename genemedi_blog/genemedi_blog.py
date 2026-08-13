#!/usr/bin/env python3
"""Create and update GeneMedi Blog articles through Drupal JSON:API."""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import uuid as uuid_lib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse


ARTICLE_RESOURCE_TYPE = "node--article"
ARTICLE_ENDPOINT = "/jsonapi/node/article"
ALLOWED_BODY_FORMATS = {"full_html", "markdown"}
SUPPORTED_ARTICLE_KEYS = {
    "title",
    "body",
    "body_format",
    "langcode",
    "product_series",
    "cover_url",
    "slug",
}


class BlogPublisherError(Exception):
    """Configuration, validation, transport, or Drupal API failure."""


def _parse_bool(value: str, variable_name: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BlogPublisherError(
        f"{variable_name} must be true/false, yes/no, on/off, or 1/0"
    )


def _validate_base_url(value: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BlogPublisherError(
            "GENEMEDI_BLOG_BASE_URL must be an absolute HTTP(S) URL"
        )
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise BlogPublisherError(
            "GENEMEDI_BLOG_BASE_URL must contain only scheme and host"
        )
    return base_url


@dataclass(frozen=True)
class BlogConfig:
    base_url: str
    username: str
    password: str
    timeout: float = 30.0
    verify_ssl: bool = True

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        require_credentials: bool = True,
    ) -> "BlogConfig":
        values = os.environ if env is None else env
        base_url = _validate_base_url(
            values.get("GENEMEDI_BLOG_BASE_URL", "https://hub.genemedi.net")
        )
        username = values.get("GENEMEDI_BLOG_USER", "").strip()
        password = values.get("GENEMEDI_BLOG_PASSWORD", "")
        if require_credentials and (not username or not password):
            raise BlogPublisherError(
                "GENEMEDI_BLOG_USER and GENEMEDI_BLOG_PASSWORD are required"
            )

        timeout_raw = values.get("GENEMEDI_BLOG_TIMEOUT", "30").strip()
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise BlogPublisherError(
                "GENEMEDI_BLOG_TIMEOUT must be a positive number"
            ) from exc
        if timeout <= 0:
            raise BlogPublisherError(
                "GENEMEDI_BLOG_TIMEOUT must be a positive number"
            )

        verify_ssl = _parse_bool(
            values.get("GENEMEDI_BLOG_VERIFY_SSL", "true"),
            "GENEMEDI_BLOG_VERIFY_SSL",
        )
        return cls(
            base_url=base_url,
            username=username,
            password=password,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )


def load_article(path: str) -> Dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BlogPublisherError(f"Cannot read article file {path!r}: {exc}") from exc
    try:
        article = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BlogPublisherError(f"Article file is not valid JSON: {exc}") from exc
    if not isinstance(article, dict):
        raise BlogPublisherError("Article JSON must be an object")
    return article


def _require_nonempty_text(article: Mapping[str, Any], key: str) -> str:
    value = article.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BlogPublisherError(f"{key} must be a non-empty string")
    return value


def _validate_optional_text(article: Mapping[str, Any], key: str) -> str:
    value = article.get(key)
    if not isinstance(value, str):
        raise BlogPublisherError(f"{key} must be a string")
    return value


def _validate_cover_url(value: str) -> None:
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BlogPublisherError("cover_url must be an absolute HTTP(S) URL")


def _normalize_uuid(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlogPublisherError("A non-empty Drupal UUID is required for update")
    try:
        return str(uuid_lib.UUID(value.strip()))
    except ValueError as exc:
        raise BlogPublisherError("Drupal UUID is not valid") from exc


def build_payload(
    article: Mapping[str, Any],
    *,
    operation: str,
    uuid: Optional[str] = None,
    status_override: Optional[bool] = None,
) -> Dict[str, Any]:
    if operation not in {"create", "update"}:
        raise BlogPublisherError("operation must be create or update")
    if not isinstance(article, Mapping):
        raise BlogPublisherError("article must be an object")

    unknown_keys = sorted(set(article) - SUPPORTED_ARTICLE_KEYS)
    if unknown_keys:
        raise BlogPublisherError(
            "Unsupported article keys: " + ", ".join(unknown_keys)
        )

    normalized_uuid: Optional[str] = None
    if operation == "create":
        _require_nonempty_text(article, "title")
        _require_nonempty_text(article, "body")
    else:
        normalized_uuid = _normalize_uuid(uuid)

    attrs: Dict[str, Any] = {}

    if "title" in article:
        attrs["title"] = _require_nonempty_text(article, "title")

    if "body_format" in article and "body" not in article:
        raise BlogPublisherError("body_format can only be used together with body")
    if "body" in article:
        body = _require_nonempty_text(article, "body")
        body_format = article.get("body_format", "full_html")
        if not isinstance(body_format, str) or body_format not in ALLOWED_BODY_FORMATS:
            raise BlogPublisherError("body_format must be full_html or markdown")
        attrs["body"] = {"value": body, "format": body_format}

    if operation == "create" or "langcode" in article:
        langcode = article.get("langcode", "en")
        if not isinstance(langcode, str) or not langcode.strip():
            raise BlogPublisherError("langcode must be a non-empty string")
        attrs["langcode"] = langcode.strip()

    field_mapping = {
        "product_series": "field_product_series",
        "cover_url": "field_cover_url",
        "slug": "field_slug",
    }
    for input_key, drupal_key in field_mapping.items():
        if input_key in article:
            value = _validate_optional_text(article, input_key)
            if input_key == "cover_url":
                _validate_cover_url(value)
            attrs[drupal_key] = value

    if operation == "create":
        attrs["status"] = bool(status_override) if status_override is not None else False
    elif status_override is not None:
        attrs["status"] = bool(status_override)

    if operation == "update" and not attrs:
        raise BlogPublisherError("Update input does not contain any fields to change")

    resource: Dict[str, Any] = {
        "type": ARTICLE_RESOURCE_TYPE,
        "attributes": attrs,
    }
    if operation == "update":
        resource["id"] = normalized_uuid
    return {"data": resource}


def _summarize_api_error(status: int, body: str) -> str:
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        detail = (body or "").strip()[:500]
        return f"Drupal returned HTTP {status}" + (f": {detail}" if detail else "")

    errors = document.get("errors") if isinstance(document, dict) else None
    if not isinstance(errors, list) or not errors:
        return f"Drupal returned HTTP {status}"

    parts = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        title = str(error.get("title") or "API error")
        detail = str(error.get("detail") or "").strip()
        parts.append(f"{title}: {detail}" if detail else title)
    suffix = "; ".join(parts)
    return f"Drupal returned HTTP {status}" + (f": {suffix}" if suffix else "")


class GeneMediBlogClient:
    def __init__(self, config: BlogConfig) -> None:
        self.config = config
        auth_value = base64.b64encode(
            f"{config.username}:{config.password}".encode("utf-8")
        ).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {auth_value}",
            "Accept": "application/vnd.api+json",
            "User-Agent": "genemedi-blog-publisher/1.0",
        }
        if config.verify_ssl:
            self._ssl_context: Optional[ssl.SSLContext] = None
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        expected_statuses: Sequence[int],
    ) -> Tuple[int, Dict[str, Any], Mapping[str, str]]:
        body_bytes = None
        headers = dict(self._headers)
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/vnd.api+json"

        req = urllib_request.Request(
            self.config.base_url + path,
            data=body_bytes,
            headers=headers,
            method=method,
        )
        try:
            with urllib_request.urlopen(
                req,
                timeout=self.config.timeout,
                context=self._ssl_context,
            ) as response:
                status = response.status
                raw_body = response.read().decode("utf-8", errors="replace")
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise BlogPublisherError(
                _summarize_api_error(exc.code, raw_body)
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BlogPublisherError(f"Drupal request failed: {exc}") from exc

        if status not in expected_statuses:
            raise BlogPublisherError(_summarize_api_error(status, raw_body))
        if not raw_body.strip():
            return status, {}, response_headers
        try:
            document = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise BlogPublisherError(
                f"Drupal returned invalid JSON after HTTP {status}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise BlogPublisherError("Drupal response must be a JSON object")
        return status, document, response_headers

    def create(
        self,
        article: Mapping[str, Any],
        *,
        publish: bool = False,
    ) -> Dict[str, Any]:
        payload = build_payload(
            article,
            operation="create",
            status_override=True if publish else None,
        )
        status, document, _ = self._request(
            "POST",
            ARTICLE_ENDPOINT,
            payload=payload,
            expected_statuses=(201,),
        )
        return self._normalize_write_result("create", status, document)

    def update(
        self,
        uuid: str,
        article: Mapping[str, Any],
        *,
        publish: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload = build_payload(
            article,
            operation="update",
            uuid=uuid,
            status_override=publish,
        )
        normalized_uuid = payload["data"]["id"]
        status, document, _ = self._request(
            "PATCH",
            f"{ARTICLE_ENDPOINT}/{normalized_uuid}",
            payload=payload,
            expected_statuses=(200,),
        )
        return self._normalize_write_result("update", status, document)

    def probe(self) -> Dict[str, Any]:
        root_status, root_document, _ = self._request(
            "GET", "/jsonapi", expected_statuses=(200,)
        )
        options_status, _, options_headers = self._request(
            "OPTIONS", ARTICLE_ENDPOINT, expected_statuses=(200,)
        )
        sparse_path = (
            ARTICLE_ENDPOINT
            + "?page%5Blimit%5D=1"
            + "&fields%5Bnode--article%5D="
            + "title,field_product_series,field_cover_url,field_slug"
        )
        article_status, article_document, _ = self._request(
            "GET", sparse_path, expected_statuses=(200,)
        )

        data = article_document.get("data")
        sample_attrs: Mapping[str, Any] = {}
        if isinstance(data, list) and data and isinstance(data[0], dict):
            attributes = data[0].get("attributes")
            if isinstance(attributes, dict):
                sample_attrs = attributes

        jsonapi_info = root_document.get("jsonapi")
        version = jsonapi_info.get("version") if isinstance(jsonapi_info, dict) else None
        allow_header = next(
            (
                value
                for name, value in options_headers.items()
                if name.lower() == "allow"
            ),
            "",
        )
        allowed_methods = [
            method.strip() for method in allow_header.split(",") if method.strip()
        ]
        expected_fields = (
            "field_product_series",
            "field_cover_url",
            "field_slug",
        )
        return {
            "operation": "probe",
            "base_url": self.config.base_url,
            "jsonapi_status": root_status,
            "jsonapi_version": version,
            "article_status": article_status,
            "options_status": options_status,
            "allowed_methods": allowed_methods,
            "fields": {
                field: field in sample_attrs for field in expected_fields
            },
        }

    def _normalize_write_result(
        self,
        operation: str,
        status: int,
        document: Mapping[str, Any],
    ) -> Dict[str, Any]:
        data = document.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise BlogPublisherError(
                "Drupal success response is missing data.id (the node UUID)"
            )
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        path = attributes.get("path")
        alias = path.get("alias") if isinstance(path, dict) else None
        node_id = attributes.get("drupal_internal__nid")
        public_url = None
        if isinstance(alias, str) and alias.startswith("/"):
            public_url = self.config.base_url + alias
        elif node_id is not None:
            public_url = f"{self.config.base_url}/node/{node_id}"
        return {
            "operation": operation,
            "http_status": status,
            "uuid": str(data["id"]),
            "node_id": str(node_id) if node_id is not None else None,
            "path_alias": alias,
            "public_url": public_url,
        }


def _dry_run_result(
    config: BlogConfig,
    *,
    operation: str,
    method: str,
    path: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "operation": operation,
        "dry_run": True,
        "method": method,
        "url": config.base_url + path,
        "payload": payload,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update GeneMedi Blog articles via Drupal JSON:API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("probe", help="Run authenticated read-only API checks.")

    create_parser = subparsers.add_parser(
        "create", help="Create an Article node (draft by default)."
    )
    create_parser.add_argument("article_json", help="UTF-8 article JSON file.")
    create_parser.add_argument(
        "--publish", action="store_true", help="Create the article as public."
    )
    create_parser.add_argument(
        "--dry-run", action="store_true", help="Print the request without sending it."
    )

    update_parser = subparsers.add_parser(
        "update", help="Update an existing Article node by Drupal UUID."
    )
    update_parser.add_argument("uuid", help="Drupal Article node UUID.")
    update_parser.add_argument("article_json", help="UTF-8 article JSON file.")
    status_group = update_parser.add_mutually_exclusive_group()
    status_group.add_argument(
        "--publish", action="store_true", help="Set the article to public."
    )
    status_group.add_argument(
        "--draft", action="store_true", help="Set the article to draft."
    )
    update_parser.add_argument(
        "--dry-run", action="store_true", help="Print the request without sending it."
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    is_dry_run = bool(getattr(args, "dry_run", False))
    try:
        config = BlogConfig.from_env(require_credentials=not is_dry_run)
        if not config.verify_ssl and not is_dry_run:
            print(
                "WARNING: TLS certificate verification is disabled.",
                file=sys.stderr,
            )

        if args.command == "probe":
            result = GeneMediBlogClient(config).probe()
        elif args.command == "create":
            article = load_article(args.article_json)
            payload = build_payload(
                article,
                operation="create",
                status_override=True if args.publish else None,
            )
            if is_dry_run:
                result = _dry_run_result(
                    config,
                    operation="create",
                    method="POST",
                    path=ARTICLE_ENDPOINT,
                    payload=payload,
                )
            else:
                result = GeneMediBlogClient(config).create(
                    article, publish=args.publish
                )
        elif args.command == "update":
            article = load_article(args.article_json)
            publication_state: Optional[bool] = None
            if args.publish:
                publication_state = True
            elif args.draft:
                publication_state = False
            payload = build_payload(
                article,
                operation="update",
                uuid=args.uuid,
                status_override=publication_state,
            )
            path = f"{ARTICLE_ENDPOINT}/{payload['data']['id']}"
            if is_dry_run:
                result = _dry_run_result(
                    config,
                    operation="update",
                    method="PATCH",
                    path=path,
                    payload=payload,
                )
            else:
                result = GeneMediBlogClient(config).update(
                    args.uuid, article, publish=publication_state
                )
        else:
            parser.error("Unknown command")
            return 2
    except BlogPublisherError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
