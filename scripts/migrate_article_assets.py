#!/usr/bin/env python3
"""Freeze, copy and narrowly migrate existing article assets without republishing.

Every mutating phase requires the SHA-256 of the frozen plan. Source files and
CMS originals are retained; changed CMS records are never blindly overwritten.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIELDS = ("body", "field_cover_url", "field_source_pdf_url")
ORIGIN_URL = re.compile(r"https://(?:www\.)?genemedi\.net/uploads/papers/[a-zA-Z0-9_./%-]+")
PUBLIC_URL = re.compile(r'https://(?:www\.)?img\.genemedi\.cn/[^\s<>"\'\\&?]+')
CONTENT_NAME = re.compile(r"([0-9a-f]{64})\.(pdf|jpg|jpeg|png|webp)$")


class _NoCmsRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("CMS redirect refused; credentials stay on the reviewed HTTPS endpoint")


def cms_urlopen(request, *, timeout):
    return build_opener(_NoCmsRedirect()).open(request, timeout=timeout)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


class Cms:
    def __init__(self, config=None):
        if config:
            values = json.loads(Path(config).read_text())
            self.base = values["cms_base"].rstrip("/")
            user, password = values["cms_user"], values["cms_pass"]
        else:
            from genemedi_blog.genemedi_blog import BlogConfig
            values = BlogConfig.from_env()
            self.base, user, password = values.base_url, values.username, values.password
        if self.base != "https://hub.genemedi.net":
            raise ValueError("Migration is limited to the reviewed GeneMedi Hub")
        self.headers = {"Authorization": "Basic " + base64.b64encode(
            (user + ":" + password).encode()).decode(), "Accept": "application/vnd.api+json",
            "User-Agent": "GeneMedi-Asset-Migration/1.0"}

    def request(self, url, payload=None):
        if url.startswith("/"):
            url = self.base + url
        parsed = urlsplit(url)
        if (parsed.hostname != urlsplit(self.base).hostname or parsed.username or parsed.password
                or parsed.scheme not in {"http", "https"} or parsed.port not in {None, 80, 443}):
            raise ValueError("Unexpected CMS pagination host")
        # Drupal's reverse-proxy pagination emits HTTP links. Never send
        # credentials over HTTP; keep the same reviewed path on HTTPS.
        url = urlunsplit(("https", urlsplit(self.base).netloc, parsed.path, parsed.query, ""))
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = "application/vnd.api+json"
        body = None if payload is None else json.dumps(payload).encode()
        with cms_urlopen(Request(url, data=body, headers=headers,
                                 method="GET" if payload is None else "PATCH"), timeout=45) as response:
            return json.load(response)

    def articles(self):
        url, result, visited = "/jsonapi/node/article?page%5Blimit%5D=50", [], set()
        while url:
            if url in visited:
                raise ValueError("CMS pagination cycle")
            visited.add(url)
            page = self.request(url)
            result.extend(page["data"])
            url = page.get("links", {}).get("next")
            if isinstance(url, dict):
                url = url["href"]
        return result

    def path(self, article):
        language = article["original"]["langcode"]
        if not re.fullmatch(r"[a-zA-Z-]+", language):
            raise ValueError("Invalid CMS language")
        return ("/" + language if language != "en" else "") + "/jsonapi/node/article/" + article["uuid"]


def changed_fields(attributes, mapping):
    updated = {}
    for field in FIELDS:
        value = attributes.get(field)
        original = value.get("value", "") if field == "body" and isinstance(value, dict) else value
        if not isinstance(original, str):
            continue
        def replacement(match):
            url = match.group()
            if url not in mapping:
                raise ValueError("Source asset missing from frozen inventory: " + url)
            return mapping[url]
        rewritten = ORIGIN_URL.sub(replacement, original)
        if rewritten != original:
            updated[field] = ({"value": rewritten, "format": value["format"]}
                              if field == "body" else rewritten)
    return updated


def freeze(source, cms, target, inventory=None):
    if target != "https://img.genemedi.cn":
        raise ValueError("Unexpected asset origin")
    assets, mapping = [], {}
    if inventory:
        files = json.loads(Path(inventory).read_text())["files"]
    else:
        source = Path(source).resolve()
        files = []
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError("Symlinks are not migration sources")
            files.append({"relative_path": path.relative_to(source).as_posix(),
                          "sha256": sha(path), "size": path.stat().st_size})
    for item in files:
        relative = item["relative_path"]
        path = Path(relative)
        match = CONTENT_NAME.fullmatch(path.name)
        if (path.is_absolute() or ".." in path.parts or not match
                or item["sha256"] != match[1] or item["size"] <= 0):
            raise ValueError("Source filename/content identity mismatch: " + relative)
        kind = "pdf" if match[2] == "pdf" else "image"
        key = ("article-assets/source-pdfs/" + match[1][:2] + "/" + path.name
               if kind == "pdf" else "article-assets/blog/" + path.name)
        url = target + "/" + key
        assets.append({"relative_path": relative, "sha256": match[1], "size": item["size"],
                       "kind": kind, "key": key, "url": url})
        for host in ["www.genemedi.net", "genemedi.net"]:
            mapping["https://" + host + "/uploads/papers/" + relative] = url
    articles, originals, unchanged_urls = [], cms.articles(), set()
    for entity in originals:
        attributes = json.loads(json.dumps(entity["attributes"]))
        for field in FIELDS:
            unchanged_urls.update(PUBLIC_URL.findall(json.dumps(attributes.get(field), ensure_ascii=False)))
        patch = changed_fields(attributes, mapping)
        if patch:
            articles.append({"uuid": entity["id"], "original": attributes, "patch": patch})
    return {"version": 1, "target": target, "assets": assets, "mapping": mapping,
            "article_entities_scanned": len(originals), "articles": articles,
            "existing_cdn_urls": sorted(unchanged_urls)}


def verify(url, digest, size=None):
    if urlsplit(url).hostname not in {"img.genemedi.cn", "www.img.genemedi.cn"}:
        raise ValueError("Unreviewed public asset host")
    total, actual, first = 0, hashlib.sha256(), b""
    with urlopen(Request(url, headers={"User-Agent": "GeneMedi-Asset-Migration/1.0"}), timeout=180) as response:
        if urlsplit(response.url).hostname not in {"img.genemedi.cn", "www.img.genemedi.cn"}:
            raise ValueError("Asset redirected away from the reviewed resource origin")
        if response.status != 200:
            raise ValueError("Public asset is not HTTP 200")
        content_type = response.headers.get_content_type()
        expected_type = "application/pdf" if urlsplit(url).path.endswith(".pdf") else "image/"
        if not content_type.startswith(expected_type):
            raise ValueError("Incorrect asset Content-Type")
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            if not first:
                first = chunk[:1024]
            total += len(chunk)
            if total > (size if size is not None else 50 * 1024 * 1024):
                raise ValueError("Unexpected public asset size")
            actual.update(chunk)
    if actual.hexdigest() != digest or size is not None and total != size:
        raise ValueError("Public asset content differs from frozen source")
    if expected_type == "application/pdf" and b"%PDF-" not in first:
        raise ValueError("Missing PDF header")


def upload(plan, source, receipt):
    from utils.source_pdf_store import OssSourcePdfStore
    store = OssSourcePdfStore.from_env()
    result = {"plan_sha256": receipt["plan_sha256"], "verified": [], "existing_cdn_verified": []}
    save(receipt["path"], result)
    for asset in plan["assets"]:
        untrusted_path = Path(source).resolve() / asset["relative_path"]
        path = untrusted_path.resolve()
        if untrusted_path.is_symlink() or not path.is_relative_to(Path(source).resolve()) or sha(path) != asset["sha256"]:
            raise ValueError("Local source changed since planning")
        if store.bucket.object_exists(asset["key"]):
            remote = store.bucket.get_object(asset["key"])
            digest, size = hashlib.sha256(), 0
            try:
                for chunk in iter(lambda: remote.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > asset["size"]:
                        raise ValueError("Existing object is larger than the frozen source")
                    digest.update(chunk)
            finally:
                remote.close()
            if digest.hexdigest() != asset["sha256"] or size != asset["size"]:
                raise ValueError("Existing OSS object differs; refusing overwrite")
        else:
            store.bucket.put_object_from_file(asset["key"], str(path), headers={
                "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "Cache-Control": "public, max-age=31536000, immutable",
                "x-oss-meta-sha256": asset["sha256"]})
        verify(asset["url"], asset["sha256"], asset["size"])
        result["verified"].append(asset["key"])
        save(receipt["path"], result)
        print("Verified", len(result["verified"]), "of", len(plan["assets"]), flush=True)
    for url in plan["existing_cdn_urls"]:
        match = CONTENT_NAME.search(urlsplit(url).path)
        if not match:
            raise ValueError("Historical CDN asset has no content identity")
        verify(url, match[1])
        result["existing_cdn_verified"].append(url)
        save(receipt["path"], result)


def same_fields(actual, expected):
    return all(actual.get(key) == value for key, value in expected.items())


def patch_articles(plan, cms, journal, *, rollback=False):
    states = json.loads(Path(journal).read_text()) if Path(journal).exists() else {}
    for article in plan["articles"]:
        original, patch = article["original"], article["patch"]
        before = {key: ({"value": original[key]["value"], "format": original[key]["format"]}
                        if key == "body" else original.get(key)) for key in patch}
        desired, expected = (before, patch) if rollback else (patch, before)
        current = cms.request(cms.path(article))["data"]["attributes"]
        comparable = {key: ({"value": current[key]["value"], "format": current[key]["format"]}
                            if key == "body" else current.get(key)) for key in patch}
        if same_fields(comparable, desired):
            continue
        if not same_fields(comparable, expected):
            raise ValueError("CMS fields changed concurrently: " + article["uuid"])
        if not rollback and current.get("changed") != original.get("changed"):
            raise ValueError("CMS revision changed since planning: " + article["uuid"])
        states[article["uuid"]] = {"status": "rollback_pending" if rollback else "pending",
                                  "before": comparable, "after": desired}
        save(journal, states)
        cms.request(cms.path(article), {"data": {"type": "node--article", "id": article["uuid"],
                                                "attributes": desired}})
        actual = cms.request(cms.path(article))["data"]["attributes"]
        for key in ["title", "langcode", "status", "field_slug", "field_product_series", "path"]:
            if actual.get(key) != current.get(key):
                raise ValueError("Unrelated CMS field changed: " + key)
        comparable = {key: ({"value": actual[key]["value"], "format": actual[key]["format"]}
                            if key == "body" else actual.get(key)) for key in desired}
        if not same_fields(comparable, desired):
            raise ValueError("CMS readback mismatch: " + article["uuid"])
        states[article["uuid"]]["status"] = "rolled_back" if rollback else "verified"
        save(journal, states)
        print("CMS verified", article["uuid"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["plan", "upload", "apply", "rollback"])
    parser.add_argument("--plan", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--source-inventory", help="Read-only origin hash inventory, for planning before transfer finishes")
    parser.add_argument("--cms-config")
    parser.add_argument("--env-file")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--receipt")
    parser.add_argument("--journal")
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    if args.phase == "plan":
        if not (args.source_root or args.source_inventory) or Path(args.plan).exists():
            parser.error("Plan requires a source root/inventory and a new plan path")
        save(args.plan, freeze(args.source_root, Cms(args.cms_config), "https://img.genemedi.cn", args.source_inventory))
        print("PLAN_SHA256", sha(args.plan))
        return
    if sha(args.plan) != args.confirm_plan_sha256:
        parser.error("--confirm-plan-sha256 must match the frozen plan")
    plan = json.loads(Path(args.plan).read_text())
    receipt = {"plan_sha256": args.confirm_plan_sha256, "path": args.receipt}
    if args.phase == "upload":
        if not args.source_root or not args.receipt:
            parser.error("Upload requires --source-root and --receipt")
        upload(plan, args.source_root, receipt)
    else:
        if not args.journal:
            parser.error("CMS changes require --journal")
        if args.phase == "apply":
            if not args.receipt:
                parser.error("CMS changes require the complete --receipt from upload")
            verified = json.loads(Path(args.receipt).read_text())
            if (verified["plan_sha256"] != args.confirm_plan_sha256
                    or set(verified["verified"]) != {a["key"] for a in plan["assets"]}
                    or set(verified["existing_cdn_verified"]) != set(plan["existing_cdn_urls"])):
                raise ValueError("Complete public asset verification is required before CMS changes")
        patch_articles(plan, Cms(args.cms_config), args.journal, rollback=args.phase == "rollback")


if __name__ == "__main__":
    main()
