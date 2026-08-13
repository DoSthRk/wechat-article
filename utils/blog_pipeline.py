"""Independent translation and GeneMedi Blog publication workflow.

Generation creates queue rows only. Translation and Blog publication are
explicit admin actions and never run from the PDF/WeChat batch processor.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import markdown as md_lib

from db.database import BLOG_SOURCE_LANG, BLOG_TARGET_LANGS, DatabaseManager
from utils.job_loader import Job as SourceJob
from utils.logger import setup_logger
from utils.translator import TranslationResult, translate_markdown
from utils.wechat_html import extract_title_and_digest, find_image_placeholders, replace_image_placeholder

logger = setup_logger("blog_pipeline")

BLOG_PLATFORM = "blog"
BLOG_ACCOUNT = "genemedi"
_H1_RE = re.compile(r"<h1\b[^>]*>.*?</h1>", re.IGNORECASE | re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


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
        return f"{self.cdn_base_url}/{key}"


def _markdown_to_blog_html(markdown_text: str) -> str:
    html = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    return _H1_RE.sub("", html, count=1).strip()


def _slug(job_id: str, lang: str) -> str:
    normalized = _SLUG_RE.sub("-", (job_id or "article").lower()).strip("-")
    return f"{normalized or 'article'}-{lang}"


def _product_series(product_id: str) -> str:
    normalized = (product_id or "").lower()
    key = "AAV" if "aav" in normalized or "purprox" in normalized else "SOLIDEX" if "solidex" in normalized or "pan_t" in normalized else ""
    return os.getenv(f"GENEMEDI_BLOG_PRODUCT_SERIES_{key}", "").strip() if key else ""


def _build_source_job(db_job: Any) -> SourceJob:
    return SourceJob(
        job_id=db_job.job_id, pdf=db_job.pdf_path, template=db_job.template_id,
        product=db_job.product_id, image_pool=db_job.image_pool, title_hint=db_job.title_hint,
    )


class BlogWorkflow:
    def __init__(
        self,
        db: DatabaseManager,
        *,
        translator: Callable[[str, str], TranslationResult] = translate_markdown,
        blog_client_factory: Optional[Callable[[], Any]] = None,
        asset_store_factory: Callable[[], OssImageStore] = OssImageStore.from_env,
    ) -> None:
        self.db = db
        self.translator = translator
        self.blog_client_factory = blog_client_factory or self._new_blog_client
        self.asset_store_factory = asset_store_factory

    @staticmethod
    def _new_blog_client() -> Any:
        try:
            from genemedi_blog.genemedi_blog import BlogConfig, GeneMediBlogClient
            return GeneMediBlogClient(BlogConfig.from_env())
        except Exception as exc:
            raise BlogPipelineError(f"Blog client configuration failed: {exc}") from exc

    def translate(self, job_id: str, lang: str = "en") -> Dict[str, Any]:
        if lang not in BLOG_TARGET_LANGS:
            raise BlogPipelineError(f"Unsupported translation language: {lang}")
        job_pk = self.db.find_job_pk(job_id)
        if job_pk is None:
            raise BlogPipelineError(f"Unknown job: {job_id}")
        source = self.db.get_article_version(job_pk, BLOG_SOURCE_LANG)
        target = self.db.get_article_version(job_pk, lang)
        if source is None or target is None:
            raise BlogPipelineError("Blog versions have not been initialized for this article")
        source_path = Path(source.content_path)
        if not source_path.is_file():
            raise BlogPipelineError(f"Chinese source Markdown is missing: {source_path}")

        self.db.upsert_article_version(job_pk, lang, translation_status="translating", translation_error=None)
        try:
            result = self.translator(source_path.read_text(encoding="utf-8"), lang)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            self.db.upsert_article_version(job_pk, lang, translation_status="failed", translation_error=error)
            raise BlogPipelineError(error) from exc
        if not result.success or not result.translated_markdown.strip():
            error = result.error or "Translation returned empty content"
            self.db.upsert_article_version(job_pk, lang, translation_status="failed", translation_error=error, translation_model=result.model or None)
            raise BlogPipelineError(error)

        output_path = Path(target.content_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.translated_markdown, encoding="utf-8")
        self.db.upsert_article_version(
            job_pk, lang, content_path=str(output_path), translation_status="translated",
            translation_error=None, translation_model=result.model, total_tokens=result.total_tokens,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        )
        self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="pending", publish_error=None)
        return {"job_id": job_id, "lang": lang, "status": "translated", "tokens": result.total_tokens}

    def publish(self, job_id: str, lang: str) -> Dict[str, Any]:
        job_pk = self.db.find_job_pk(job_id)
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
        legacy_hub_publication = (
            distribution.publish_status == "published"
            and urlparse(distribution.external_url or "").netloc == "hub.genemedi.net"
        )
        if distribution.publish_status == "published" and not legacy_hub_publication:
            return {"job_id": job_id, "lang": lang, "status": "already_published", "url": distribution.external_url}
        if lang in BLOG_TARGET_LANGS and version.translation_status != "translated":
            raise BlogPipelineError(f"{lang} article must be translated before publishing")
        if lang == BLOG_SOURCE_LANG and version.translation_status != "ready":
            raise BlogPipelineError("Chinese source is not ready")
        markdown_path = Path(version.content_path)
        if not markdown_path.is_file():
            raise BlogPipelineError(f"Article Markdown is missing: {markdown_path}")

        self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="publishing", publish_error=None)
        try:
            markdown_text = markdown_path.read_text(encoding="utf-8")
            html, cover_url = self._render_with_images(markdown_text, _build_source_job(db_job))
            title, _digest = extract_title_and_digest(markdown_text)
            payload: Dict[str, str] = {
                "title": title or article.title or job_id,
                "body": html,
                "body_format": "full_html",
                "langcode": self._drupal_language(lang),
                "slug": _slug(job_id, lang),
            }
            series = _product_series(db_job.product_id)
            if series:
                payload["product_series"] = series
            if cover_url:
                payload["cover_url"] = cover_url
            client = self.blog_client_factory()
            result = (
                client.update(distribution.external_id, payload, publish=True)
                if distribution.external_id and not legacy_hub_publication
                else client.create(payload, publish=True)
            )
        except Exception as exc:
            error = str(exc)
            self.db.upsert_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="failed", publish_error=error)
            logger.warning("Blog publication failed job=%s lang=%s: %s", job_id, lang, error)
            raise BlogPipelineError(error) from exc

        self.db.upsert_distribution(
            job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang, publish_status="published", publish_error=None,
            external_id=str(result.get("uuid") or distribution.external_id or ""),
            external_node_id=str(result.get("node_id") or ""), external_url=str(result.get("public_url") or ""),
        )
        return {"job_id": job_id, "lang": lang, "status": "published", "url": result.get("public_url")}

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

    def _render_with_images(self, markdown_text: str, source_job: SourceJob) -> Tuple[str, str]:
        placeholders = find_image_placeholders(markdown_text)
        html = _markdown_to_blog_html(markdown_text)
        if not placeholders:
            return html, ""
        from batch_processor import _resolve_figure_path, _resolve_job_figures

        extracted, figures_dir = _resolve_job_figures(source_job)
        store = self.asset_store_factory()
        used_paths = set()
        cover_url = ""
        missing = []
        for description in placeholders:
            path = _resolve_figure_path(description, figures_dir, extracted)
            if not path:
                missing.append(description)
                continue
            if path in used_paths:
                html = html.replace(f"[图片:{description}]", "", 1)
                continue
            url = store.upload(path)
            html = replace_image_placeholder(html, description, url)
            used_paths.add(path)
            if not cover_url:
                cover_url = url
        if missing:
            raise BlogPipelineError("Missing required figures: " + "; ".join(missing[:5]))
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
            payload = workflow.translate(job_id, lang) if action == "translate" else workflow.publish(job_id, lang)
            results.append({"ok": True, **payload})
        except BlogPipelineError as exc:
            results.append({"ok": False, "job_id": job_id, "lang": lang, "error": str(exc)})
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}
