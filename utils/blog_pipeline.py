"""Independent translation and GeneMedi Blog publication workflow.

Generation creates version rows. Successful WeChat draft uploads enqueue
asynchronous translation/publication; manual actions remain available.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib import request as urllib_request
from urllib.parse import urljoin

import markdown as md_lib

from db.database import BLOG_SOURCE_LANG, BLOG_TARGET_LANGS, DatabaseManager
from utils.blog_urls import blog_slug, public_blog_url, verify_public_blog_url
from utils.blog_task_lock import serialized_version
from utils.figure_strategy import figure_key_from_description
from utils.image_packages import ImagePackageError, require_approved_package
from utils.job_loader import Job as SourceJob
from utils.logger import setup_logger
from utils.image_placeholders import (
    normalize_image_placeholders,
    unwrap_linked_image_placeholders,
    validate_image_placeholders,
)
from utils.translator import TranslationResult, translate_markdown
from utils.wechat_html import extract_title_and_digest, find_image_placeholders, replace_image_placeholder

logger = setup_logger("blog_pipeline")

BLOG_PLATFORM = "blog"
BLOG_ACCOUNT = "genemedi"
_H1_RE = re.compile(r"<h1\b[^>]*>.*?</h1>", re.IGNORECASE | re.DOTALL)


class BlogPipelineError(Exception):
    """A recoverable translation, asset, configuration, or publish failure."""


class OssImageStore:
    """Content-addressed OSS uploader, initialized only for real publishing."""

    def __init__(self, bucket: Any, prefix: str, cdn_base_url: str) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "OssImageStore":
        required = {
            "ALIYUN_OSS_ACCESS_KEY_ID": os.getenv("ALIYUN_OSS_ACCESS_KEY_ID", "").strip(),
            "ALIYUN_OSS_ACCESS_KEY_SECRET": os.getenv("ALIYUN_OSS_ACCESS_KEY_SECRET", "").strip(),
            "ALIYUN_OSS_ENDPOINT": os.getenv("ALIYUN_OSS_ENDPOINT", "").strip(),
            "ALIYUN_OSS_BUCKET": os.getenv("ALIYUN_OSS_BUCKET", "").strip(),
            "ALIYUN_OSS_CDN_BASE_URL": os.getenv("ALIYUN_OSS_CDN_BASE_URL", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BlogPipelineError("Missing OSS configuration: " + ", ".join(missing))
        try:
            import oss2
        except ImportError as exc:
            raise BlogPipelineError("oss2 is required for Blog image publishing") from exc
        bucket = oss2.Bucket(
            oss2.Auth(required["ALIYUN_OSS_ACCESS_KEY_ID"], required["ALIYUN_OSS_ACCESS_KEY_SECRET"]),
            required["ALIYUN_OSS_ENDPOINT"], required["ALIYUN_OSS_BUCKET"],
        )
        return cls(bucket, os.getenv("ALIYUN_OSS_PREFIX", "article-assets/blog"), required["ALIYUN_OSS_CDN_BASE_URL"])

    def upload(self, local_path: str) -> str:
        path = Path(local_path)
        if not path.is_file():
            raise BlogPipelineError(f"Figure file not found: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        suffix = path.suffix.lower() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".png"
        key = "/".join(part for part in (self.prefix, f"{digest}{suffix}") if part)
        try:
            self.bucket.put_object_from_file(key, str(path))
        except Exception as exc:
            raise BlogPipelineError(f"OSS upload failed for {path.name}: {exc}") from exc
        return verify_public_image(f"{self.cdn_base_url}/{key}", expected_path=path)


def verify_public_image(url: str, timeout: float = 30.0, *, expected_path=None) -> str:
    """Decode the whole public image and verify dimensions, permitting CDN re-encoding."""
    from PIL import Image, ImageChops, ImageStat
    limit = 50 * 1024 * 1024
    request = urllib_request.Request(
        url,
        headers={"User-Agent": "GeneMedi-Blog-Image-Verify/2.0"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            content_type = (response.headers.get_content_type() or "").lower()
            polished = response.headers.get('CF-Polished', '')
            content = response.read(limit + 1)
    except Exception as exc:
        raise BlogPipelineError(f"Blog image is not publicly reachable: {url}: {exc}") from exc
    if status != 200 or not content_type.startswith("image/") or not content or len(content) > limit:
        raise BlogPipelineError(
            f"Blog image public response is invalid: HTTP {status}, {content_type or 'missing type'}"
        )
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            actual_size = image.size
        if expected_path is not None:
            with Image.open(expected_path) as expected:
                expected.load()
                if actual_size != expected.size:
                    raise ValueError(f"尺寸不匹配：{actual_size} != {expected.size}")
                if hashlib.sha256(content).hexdigest() != hashlib.sha256(Path(expected_path).read_bytes()).hexdigest():
                    if not re.fullmatch(r'\s*ok\s*,\s*orig_size=\d+\s*', str(polished), re.IGNORECASE):
                        raise ValueError('图片内容哈希不匹配，且不是已知 CDN 重编码')
                    with Image.open(io.BytesIO(content)) as actual:
                        diff = ImageChops.difference(actual.convert('RGB'), expected.convert('RGB'))
                        rms = math.sqrt(sum(value * value for value in ImageStat.Stat(diff).rms) / 3)
                    if rms >= 6:
                        raise ValueError(f'CDN 重编码差异过大：RGB RMS={rms:.3f}')
    except Exception as exc:
        raise BlogPipelineError(f"Blog image is incomplete or differs from approved dimensions: {url}: {exc}") from exc
    return url


class _PublicImages(HTMLParser):
    def __init__(self, html, page_url):
        super().__init__(convert_charrefs=True)
        self.urls, self.page_url = set(), page_url
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag != 'img':
            return
        attributes = dict(attrs)
        for key in ('src', 'data-src'):
            if attributes.get(key):
                self.urls.add(urljoin(self.page_url, unescape(attributes[key])))
        for key in ('srcset', 'data-srcset'):
            for candidate in attributes.get(key, '').split(','):
                if candidate.strip():
                    self.urls.add(urljoin(self.page_url, unescape(candidate.strip().split()[0])))


def verify_public_page_images(url, expected_urls, *, timeout=10.0):
    """Require real public HTML image attributes, not merely an HTTP 200 page."""
    expected = {str(value) for value in expected_urls if value}
    if not expected:
        raise BlogPipelineError('公开页面没有预期的已批准图片')
    error = None
    for _attempt in range(3):
        try:
            request = urllib_request.Request(url, headers={'User-Agent': 'GeneMedi-Blog-Page-Verify/1.0'})
            with urllib_request.urlopen(request, timeout=timeout) as response:
                content = response.read(5 * 1024 * 1024 + 1)
                if int(getattr(response, 'status', 200) or 200) != 200 or len(content) > 5 * 1024 * 1024:
                    raise ValueError('公开页面响应状态或大小异常')
            actual = _PublicImages(content.decode('utf-8'), url).urls
            if not expected <= actual:
                raise ValueError('公开页面尚未包含全部已批准正文图片/封面地址')
            return url
        except Exception as exc:
            error = exc
    raise BlogPipelineError(f'公开 Blog 图片尚未验证，保留 CMS ID 等待重试：{error}') from error


class SshImageStore:
    """Publish content-addressed Blog images beside source PDFs on genemedi.net."""

    def __init__(self, source_store: Any, remote_dir: str, public_base_url: str) -> None:
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_dir):
            raise BlogPipelineError("BLOG_IMAGE_SSH_REMOTE_DIR must be a safe absolute path")
        if not public_base_url.lower().startswith("https://"):
            raise BlogPipelineError("BLOG_IMAGE_PUBLIC_BASE_URL must use HTTPS")
        self.source_store = source_store
        self.remote_dir = remote_dir.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "SshImageStore":
        from utils.source_pdf_store import SshSourcePdfStore

        source_store = SshSourcePdfStore.from_env()
        return cls(
            source_store,
            os.getenv(
                "BLOG_IMAGE_SSH_REMOTE_DIR",
                f"{source_store.remote_dir}/blog-images",
            ).strip(),
            os.getenv(
                "BLOG_IMAGE_PUBLIC_BASE_URL",
                f"{source_store.public_base_url}/blog-images",
            ).strip(),
        )

    def upload(self, local_path: str) -> str:
        path = Path(local_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise BlogPipelineError(f"Figure file is missing or empty: {path}")
        suffix = path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise BlogPipelineError(f"Unsupported Blog image type: {suffix or 'missing'}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        remote_subdir = f"{self.remote_dir}/{digest[:2]}"
        remote_path = f"{remote_subdir}/{digest}{suffix}"
        remote_tmp = f"{remote_path}.part"
        store = self.source_store
        try:
            store._run_ssh(f"mkdir -p {remote_subdir}")
            available_text = store._run_ssh(
                f"df -Pk {remote_subdir} | tail -n 1 | awk '{{print $4}}'"
            )
            available_bytes = int(available_text.splitlines()[-1]) * 1024
            if available_bytes < store.min_free_bytes:
                raise BlogPipelineError(
                    f"genemedi.net free space is below the safety threshold: "
                    f"{available_bytes / 1024 ** 3:.1f}GB"
                )
            exists = store._run_ssh(
                f"if test -f {remote_path} && test $(stat -c %s {remote_path}) -eq {size}; "
                "then echo yes; else echo no; fi"
            )
            if exists != "yes":
                result = subprocess.run(
                    [
                        "scp", "-i", store.private_key, "-P", str(store.port),
                        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                        "-o", "StrictHostKeyChecking=yes",
                        "-o", f"UserKnownHostsFile={store.known_hosts}",
                        "-o", f"ConnectTimeout={store.timeout}", str(path),
                        f"{store.user}@{store.host}:{remote_tmp}",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(store.timeout, 120),
                    check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "unknown error").strip()
                    raise BlogPipelineError(f"Blog image SCP returned {result.returncode}: {detail}")
                store._run_ssh(
                    f"test $(stat -c %s {remote_tmp}) -eq {size} && "
                    f"chmod 0644 {remote_tmp} && mv -f {remote_tmp} {remote_path}"
                )
        except BlogPipelineError:
            raise
        except Exception as exc:
            raise BlogPipelineError(f"Blog image SSH upload failed for {path.name}: {exc}") from exc
        return verify_public_image(
            f"{self.public_base_url}/{digest[:2]}/{digest}{suffix}",
            timeout=float(store.timeout),
            expected_path=path,
        )


def create_blog_image_store() -> Any:
    mode = os.getenv("BLOG_IMAGE_STORAGE", "ssh").strip().lower()
    if mode == "ssh":
        return SshImageStore.from_env()
    if mode == "oss":
        return OssImageStore.from_env()
    raise BlogPipelineError(f"Unsupported BLOG_IMAGE_STORAGE: {mode}")


def _markdown_to_blog_html(markdown_text: str) -> str:
    html = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    return _H1_RE.sub("", html, count=1).strip()


def _slug(job_id: str, lang: str) -> str:
    return blog_slug(job_id, lang)


def _product_series(product_id: str) -> str:
    normalized = (product_id or "").lower()
    key = "AAV" if "aav" in normalized or "purprox" in normalized else "SOLIDEX" if "solidex" in normalized or "pan_t" in normalized else ""
    if not key:
        return ""
    configured = os.getenv(f"GENEMEDI_BLOG_PRODUCT_SERIES_{key}", "").strip()
    return configured or {"AAV": "AAV", "SOLIDEX": "Solidex"}[key]


def _build_source_job(db_job: Any) -> SourceJob:
    from utils.line_loader import load_line
    lines = []
    for path in (Path(__file__).resolve().parent.parent / 'inputs/lines').glob('*.yaml'):
        try:
            lines.append(load_line(str(path)))
        except Exception:
            continue
    saved_line = getattr(db_job, 'line_id', None)
    line = saved_line if saved_line in {item.line_id for item in lines} else None
    if not saved_line:
        matches = [item.line_id for item in lines if item.product == db_job.product_id]
        if not matches:
            matches = [item.line_id for item in lines
                       if item.extra.get('pdf_folder') == Path(db_job.pdf_path or '').parent.name]
        line = matches[0] if len(matches) == 1 else None
    if not line:
        raise BlogPipelineError('文章业务线缺失或无法唯一恢复，禁止回退到默认公众号账号')
    return SourceJob(
        job_id=db_job.job_id, pdf=db_job.pdf_path, template=db_job.template_id,
        product=db_job.product_id, image_pool=db_job.image_pool, title_hint=db_job.title_hint,
        line=line,
    )


def require_blog_package(source_job, expected_package_id=None, *, expected_job_pk=None):
    try:
        return require_approved_package(source_job, expected_package_id=expected_package_id, expected_job_pk=expected_job_pk)
    except ImagePackageError as exc:
        raise BlogPipelineError(f"配图审批未通过，已阻止发布：{exc}") from exc


def verify_cms_payload(client, uuid, payload):
    """A successful PATCH/POST alone is not a verified publication."""
    attributes = client.get(uuid, langcode=payload['langcode'])['data']['attributes']
    expected = {'title': payload['title'], 'langcode': payload['langcode'], 'status': True,
                'field_slug': payload['slug'], 'field_source_pdf_url': payload['source_pdf_url'],
                'field_cover_url': payload.get('cover_url', '')}
    if ('body' not in attributes or attributes['body'].get('value') != payload['body']
            or attributes['body'].get('format') != payload['body_format']
            or any(attributes.get(key) != value for key, value in expected.items())):
        raise BlogPipelineError('CMS 正文、图片、封面或来源回读不匹配，发布状态待核对')
    return attributes


class BlogWorkflow:
    def __init__(
        self,
        db: DatabaseManager,
        *,
        translator: Callable[[str, str], TranslationResult] = translate_markdown,
        blog_client_factory: Optional[Callable[[], Any]] = None,
        asset_store_factory: Callable[[], Any] = create_blog_image_store,
        blog_url_verifier: Callable[[str], str] = verify_public_blog_url,
        source_pdf_publisher: Optional[Callable[[Any, int, SourceJob], str]] = None,
    ) -> None:
        self.db = db
        self.translator = translator
        self.blog_client_factory = blog_client_factory or self._new_blog_client
        self.asset_store_factory = asset_store_factory
        self.blog_url_verifier = blog_url_verifier
        self.source_pdf_publisher = source_pdf_publisher or self._publish_source_pdf

    @staticmethod
    def _publish_source_pdf(db: Any, job_pk: int, source_job: SourceJob) -> str:
        from batch_processor import _ensure_source_pdf_published

        return _ensure_source_pdf_published(db, job_pk, source_job)

    @staticmethod
    def _new_blog_client() -> Any:
        try:
            from genemedi_blog.genemedi_blog import BlogConfig, GeneMediBlogClient
            return GeneMediBlogClient(BlogConfig.from_env())
        except Exception as exc:
            raise BlogPipelineError(f"Blog client configuration failed: {exc}") from exc

    @serialized_version
    def translate(self, job_id: str, lang: str = "en", *, job_pk: Optional[int] = None,
                  expected_package_id: Optional[str] = None) -> Dict[str, Any]:
        if lang not in BLOG_TARGET_LANGS:
            raise BlogPipelineError(f"Unsupported translation language: {lang}")
        job_pk = self._resolve_job_pk(job_id, job_pk)
        if job_pk is None:
            raise BlogPipelineError(f"Unknown job: {job_id}")
        source = self.db.get_article_version(job_pk, BLOG_SOURCE_LANG)
        target = self.db.get_article_version(job_pk, lang)
        if source is None or target is None:
            raise BlogPipelineError("Blog versions have not been initialized for this article")
        source_path = Path(source.content_path)
        package = None
        if expected_package_id:
            package = require_blog_package(_build_source_job(self.db.get_job(job_pk)), expected_package_id, expected_job_pk=job_pk)
            source_path = package.directory / 'article.md'
        if not source_path.is_file():
            raise BlogPipelineError(f"Chinese source Markdown is missing: {source_path}")
        source_markdown = source_path.read_text(encoding='utf-8')
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        self.db.upsert_article_version(job_pk, lang, translation_status="translating", translation_error=None)
        try:
            result = self.translator(source_markdown, lang)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            self.db.upsert_article_version(job_pk, lang, translation_status="failed", translation_error=error)
            raise BlogPipelineError(error) from exc
        if not result.success or not result.translated_markdown.strip():
            error = result.error or "Translation returned empty content"
            self.db.upsert_article_version(job_pk, lang, translation_status="failed", translation_error=error, translation_model=result.model or None)
            raise BlogPipelineError(error)

        if package is not None:
            require_blog_package(_build_source_job(self.db.get_job(job_pk)), package.bundle_id, expected_job_pk=job_pk)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise BlogPipelineError('翻译期间源稿已变化，请重新翻译')

        output_path = Path(target.content_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.translated_markdown, encoding="utf-8")
        self.db.upsert_article_version(
            job_pk, lang, content_path=str(output_path), translation_status="translated",
            translation_error=None, translation_model=result.model, total_tokens=result.total_tokens,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            source_markdown_sha256=source_hash,
            image_package_id=package.bundle_id if package else None,
        )
        self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="pending", publish_error=None)
        return {"job_id": job_id, "lang": lang, "status": "translated", "tokens": result.total_tokens}

    def _resolve_job_pk(self, job_id: str, job_pk: Optional[int]) -> Optional[int]:
        if job_pk is None:
            return self.db.find_job_pk(job_id)
        job = self.db.get_job(job_pk)
        if job is None or job.job_id != job_id:
            raise BlogPipelineError(f"Unknown job revision: {job_id} / {job_pk}")
        return job_pk

    @serialized_version
    def publish(self, job_id: str, lang: str, *, force: bool = False, job_pk: Optional[int] = None,
                expected_package_id: Optional[str] = None) -> Dict[str, Any]:
        job_pk = self._resolve_job_pk(job_id, job_pk)
        if job_pk is None:
            raise BlogPipelineError(f"Unknown job: {job_id}")
        article = self.db.get_article(job_pk)
        version = self.db.get_article_version(job_pk, lang)
        distribution = self.db.get_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang)
        db_job = self.db.get_job(job_pk)
        if article is None or version is None or distribution is None or db_job is None:
            raise BlogPipelineError("Blog queue state is incomplete")
        if article.publish_blocked:
            raise BlogPipelineError(f"Article is blocked by quality gate: {article.block_reason or 'unknown'}")
        source_job = _build_source_job(db_job)
        try:
            package = require_blog_package(source_job, expected_package_id, expected_job_pk=job_pk)
        except BlogPipelineError as exc:
            self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang,
                                        image_verification_status='needs_review', publish_error=str(exc))
            raise
        public_url = public_blog_url(job_id, lang)
        existing_url = str(distribution.external_url or "").rstrip("/")
        if (distribution.publish_status == "published" and existing_url == public_url and not force
                and getattr(distribution, 'image_package_id', None) == package.bundle_id
                and getattr(distribution, 'image_verification_status', None) == 'verified'):
            return {"job_id": job_id, "lang": lang, "status": "already_published",
                    "url": distribution.external_url, "image_package_id": package.bundle_id}
        legacy_publication = bool(existing_url and existing_url != public_url)
        if lang in BLOG_TARGET_LANGS and version.translation_status != "translated":
            raise BlogPipelineError(f"{lang} article must be translated before publishing")
        if lang in BLOG_TARGET_LANGS and version.source_markdown_sha256 != package.source_markdown_sha256:
            raise BlogPipelineError('译稿没有绑定当前已批准图包的源稿，须重新翻译后发布')
        if lang == BLOG_SOURCE_LANG and version.translation_status != "ready":
            raise BlogPipelineError("Chinese source is not ready")
        markdown_path = package.directory / 'article.md' if lang == BLOG_SOURCE_LANG else Path(version.content_path)
        if not markdown_path.is_file():
            raise BlogPipelineError(f"Article Markdown is missing: {markdown_path}")

        self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="publishing", publish_error=None)
        try:
            markdown_text = markdown_path.read_text(encoding="utf-8")
            if lang in BLOG_TARGET_LANGS:
                validate_image_placeholders((package.directory / 'article.md').read_text(encoding="utf-8"), markdown_text)
            require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
            source_pdf_url = self.source_pdf_publisher(
                self.db, job_pk, replace(source_job, pdf=str(package.directory / 'source.pdf')),
            )
            html, cover_url = self._render_with_images(markdown_text, source_job, package=package)
            title, _digest = extract_title_and_digest(markdown_text)
            payload: Dict[str, str] = {
                "title": title or article.title or job_id,
                "body": html,
                "body_format": "full_html",
                "langcode": self._drupal_language(lang),
                "slug": _slug(job_id, lang),
                "source_pdf_url": source_pdf_url,
            }
            series = _product_series(db_job.product_id)
            if series:
                payload["product_series"] = series
            if cover_url:
                payload["cover_url"] = cover_url
            client = self.blog_client_factory()
            require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
            result = (
                client.update(distribution.external_id, payload, publish=True)
                if distribution.external_id and not legacy_publication
                else client.create(payload, publish=True)
            )
            self.db.upsert_distribution(
                job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang,
                publish_status="publishing", publish_error=None,
                external_id=str(result.get("uuid") or distribution.external_id or ""),
                external_node_id=str(result.get("node_id") or ""),
                external_url=public_url,
                image_package_id=package.bundle_id, image_verification_status='pending_readback',
            )
            verify_cms_payload(client, str(result.get('uuid') or distribution.external_id or ''), payload)
            self.blog_url_verifier(public_url)
            verify_public_page_images(public_url, _PublicImages(html, public_url).urls | {cover_url})
            require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        except Exception as exc:
            error = str(exc)
            self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang,
                                       publish_status="failed", publish_error=error,
                                       image_package_id=package.bundle_id, image_verification_status='needs_review')
            logger.warning("Blog publication failed job=%s lang=%s: %s", job_id, lang, error)
            raise BlogPipelineError(error) from exc

        self.db.upsert_distribution(
            job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="published", publish_error=None,
            external_id=str(result.get("uuid") or distribution.external_id or ""),
            external_node_id=str(result.get("node_id") or ""), external_url=public_url,
            image_package_id=package.bundle_id, image_verification_status='verified',
            image_receipt_json=json.dumps({'image_package_id': package.bundle_id,
                'source_markdown_sha256': package.source_markdown_sha256, 'cover_url': cover_url,
                'cms_verified': True, 'public_images_verified': True,
                'public_page_images_verified': True}, ensure_ascii=False),
        )
        return {
            "job_id": job_id,
            "lang": lang,
            "status": "published",
            "url": public_url,
            "cms_url": result.get("public_url"),
            "image_package_id": package.bundle_id,
        }

    @staticmethod
    def _drupal_language(lang: str) -> str:
        if lang == BLOG_SOURCE_LANG:
            value = os.getenv("GENEMEDI_BLOG_CHINESE_LANGCODE", "").strip()
            if not value:
                raise BlogPipelineError("GENEMEDI_BLOG_CHINESE_LANGCODE must be set after Blog preflight")
            return value
        if lang in BLOG_TARGET_LANGS:
            return os.getenv(f"GENEMEDI_BLOG_LANGCODE_{lang.upper()}", lang).strip() or lang
        raise BlogPipelineError(f"Unsupported Blog language: {lang}")

    def _render_with_images(self, markdown_text: str, source_job: SourceJob, *, package=None) -> Tuple[str, str]:
        """Consume only an immutable approved package; never extract on a publish path."""
        package = require_blog_package(source_job, package.bundle_id if package else None,
            expected_job_pk=getattr(package, 'manifest', {}).get('job_pk') if package else None)
        markdown_text = unwrap_linked_image_placeholders(
            normalize_image_placeholders(markdown_text)
        )
        placeholders = find_image_placeholders(markdown_text)
        html = _markdown_to_blog_html(markdown_text)
        from batch_processor import _preflight_figure_paths
        try:
            paths = _preflight_figure_paths(html, (package.figures, package.directory))
        except (ValueError, OSError) as exc:
            raise BlogPipelineError(f"正文图片不可用，已阻止发布：{exc}") from exc
        if not package.cover_path or not Path(package.cover_path).is_file():
            raise BlogPipelineError('已批准图包缺少封面，已阻止发布')
        store = self.asset_store_factory()
        uploaded = {}
        for description in placeholders:
            path = paths[description]
            if path in uploaded:
                html = html.replace(f"[图片:{description}]", "", 1)
                continue
            require_blog_package(source_job, package.bundle_id, expected_job_pk=getattr(package, 'manifest', {}).get('job_pk'))
            try:
                url = store.upload(path)
                verify_public_image(url, expected_path=path)
            except Exception as exc:
                raise BlogPipelineError(f"正文图片不可用，已阻止发布：{description}: {exc}") from exc
            if url in uploaded.values():
                raise BlogPipelineError('不同图号上传到同一图片地址，已阻止发布')
            html = replace_image_placeholder(html, description, url)
            uploaded[path] = url
        if find_image_placeholders(html):
            raise BlogPipelineError('正文图片标记未完整替换，已阻止发布')
        require_blog_package(source_job, package.bundle_id, expected_job_pk=getattr(package, 'manifest', {}).get('job_pk'))
        cover_url = uploaded.get(str(package.cover_path)) or store.upload(str(package.cover_path))
        verify_public_image(cover_url, expected_path=package.cover_path)
        return html, cover_url


def run_batch(workflow: BlogWorkflow, selections: Iterable[Dict[str, Any]], action: str) -> Dict[str, Any]:
    """Run selected pairs without letting one failure stop another one."""
    results = []
    for selection in selections:
        job_id = str(selection.get("job_id") or "").strip()
        lang = str(selection.get("lang") or "").strip()
        if not job_id or not lang:
            results.append({"ok": False, "job_id": job_id, "lang": lang, "error": "job_id and lang are required"})
            continue
        try:
            payload = (
                workflow.translate(job_id, lang)
                if action == "translate"
                else workflow.publish(job_id, lang, force=bool(selection.get("force")))
            )
            results.append({"ok": True, **payload})
        except BlogPipelineError as exc:
            results.append({"ok": False, "job_id": job_id, "lang": lang, "error": str(exc)})
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}
