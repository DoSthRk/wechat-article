"""wechat-article 主入口 —— generate / Blog / WeChat 流水线。

阶段（``--stage``）：
    generate  ：jobs.yaml → 逐 job 生成基准正文（方案 B）→ 落盘 + 写 articles 表
    distribute：发布中文 Blog → 上传公众号草稿 → 入队异步多语言 Blog。
                account 从 line 配置的 wechat_account 取。
    all       ：先 generate，再执行 distribute（默认）；不会群发公众号。

内容与投放解耦：一篇基准文章（article）可扇出到多个 distribution（platform × account × lang）。
多语言入队仅由成功的草稿创建/更新触发，不回扫历史草稿。

用法：
    python batch_processor.py                       # generate + distribute
    python batch_processor.py --stage generate      # 只生成
    python batch_processor.py --stage distribute    # 只投放（需先 generate）
    python batch_processor.py --dry-run             # 生成但不投放
    python batch_processor.py --only <job_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# 让 utils / core / db 都能 from-import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.main import ArticleAnalyzer
from db.database import ARTICLE_CONTENT_DIR, JobStatus, get_db_manager
from utils.auto_blog import enqueue as enqueue_auto_blog
from utils.blog_urls import BlogUrlError, resolve_published_blog_url
from utils.blog_task_lock import version_lock
from utils.image_packages import ImagePackageError, build_package, require_approved_package
from utils.wechat_cover import approved_cover_geometry, full_width_cover_verified
from utils.figure_strategy import (
    FigureKey,
    FigureSignals,
    figure_key_from_description,
    inspect_pdf_figure_signals as _inspect_pdf_figure_signals,
    merge_preferred_figures,
    missing_figure_keys,
    planned_figure_strategies,
)
from utils.figure_quality import QUALITY_VERSION, filter_candidates
from utils.health_check import markdown_health_score
from utils.image_placeholders import deduplicate_image_placeholders
from utils.job_loader import Job, load_jobs
from utils.line_loader import LineLoadError, load_line_by_id
from utils.logger import setup_logger
from utils.product_loader import load_product_by_id
from utils.source_pdf_store import (
    SourcePdfError,
    create_source_pdf_store,
    inspect_pdf,
    verify_public_pdf,
)
from utils.tonal_qa import load_hard_ad_words, scan_static
from utils.wechat_client import WeChatAPIError, WeChatClient
from utils.wechat_template_assets import TemplateAssetError, append_source_pdf_guide
from utils.pdf_figure_extractor import (
    Figure,
)
from utils.wechat_html import (
    extract_title_and_digest,
    find_image_placeholders,
    markdown_to_wechat_html,
    replace_image_placeholder,
)

load_dotenv()
logger = setup_logger("batch_processor")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JOBS_YAML = str(PROJECT_ROOT / "inputs" / "jobs.yaml")
LINES_DIR = str(PROJECT_ROOT / "inputs" / "lines")
DATA_DIR = PROJECT_ROOT / "data"
PRODUCTS_DIR = str(PROJECT_ROOT / "inputs" / "products")
RUNTIME_DIR = PROJECT_ROOT / "runtime"

WECHAT_PLATFORM = "wechat"
DEFAULT_LANG = "zh"
HEALTH_THRESHOLD = int(os.getenv("MARKDOWN_HEALTH_THRESHOLD", "30") or 30)
TONAL_THRESHOLD = int(os.getenv("TONAL_BLOCKED_THRESHOLD", "60") or 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-article batch processor")
    parser.add_argument("--jobs", default=DEFAULT_JOBS_YAML, help="jobs.yaml 路径")
    parser.add_argument(
        "--task",
        default=f"wechat-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        help="task 名（DB 里用）",
    )
    parser.add_argument(
        "--stage", choices=["generate", "distribute", "all"], default="all",
        help="generate=只生成 / distribute=只投放 / all=两者（默认）",
    )
    parser.add_argument("--only", action="append", help="只跑指定 job_id（可多次传）")
    parser.add_argument("--dry-run", action="store_true", help="生成但不投放（distribute 跳过）")
    parser.add_argument(
        "--placeholder-author",
        default=os.getenv("DEFAULT_AUTHOR", "TarMart"),
        help="草稿的 author 字段（公众号要求非空）",
    )
    parser.add_argument(
        "--placeholder-thumb-media",
        default=os.getenv("DEFAULT_THUMB_MEDIA_ID", ""),
        help=(
            "公众号草稿要求 thumb_media_id 非空（封面图素材 id）。"
            "图片管线落地前需手动准备一张永久素材并填 .env DEFAULT_THUMB_MEDIA_ID。"
        ),
    )
    args = parser.parse_args()

    do_generate = args.stage in ("generate", "all")
    do_distribute = args.stage in ("distribute", "all") and not args.dry_run
    if not do_generate and not do_distribute:
        logger.warning("nothing to do (stage=%s dry_run=%s)", args.stage, args.dry_run)
        return 0

    # 加载 + 入库 jobs
    try:
        all_jobs = load_jobs(args.jobs, project_root=str(PROJECT_ROOT))
    except Exception as exc:
        logger.error("load jobs failed: %s", exc)
        return 2

    selected = _filter_jobs(all_jobs, args.only)
    if not selected:
        logger.error("no jobs to run (use --only or check jobs.yaml)")
        return 2

    db = get_db_manager()
    task = db.get_or_create_task(args.task, description="two-stage batch")
    logger.info(
        "task=%s (id=%d) stage=%s will run %d/%d jobs",
        task.task_name, task.id, args.stage, len(selected), len(all_jobs),
    )

    for j in selected:
        # 只 upsert 配置，不强制 status（避免 distribute 阶段把已生成的 job 打回 pending）
        db.upsert_job(
            task.id, j.job_id,
            pdf_path=j.pdf, template_id=j.template, product_id=j.product,
            image_pool=j.image_pool, title_hint=j.title_hint,
            line_id=j.line,
        )

    analyzer: Optional[ArticleAnalyzer] = None
    if do_generate:
        try:
            analyzer = ArticleAnalyzer()
        except Exception as exc:
            logger.error("ArticleAnalyzer init failed: %s", exc)
            return 3

    # 投放：按账户惰性建 client（token 隔离）；凭据/封面缺失在 _distribute_one 内按 job 报错
    get_client: Optional[Callable[[str], WeChatClient]] = (
        _make_client_getter() if do_distribute else None
    )

    success = 0
    failed = 0
    for j in selected:
        ok = _run_one_job(db, task.id, j, analyzer, get_client, args, do_generate, do_distribute)
        if ok:
            success += 1
        else:
            failed += 1

    logger.info("done. total=%d success=%d failed=%d", len(selected), success, failed)
    return 0 if failed == 0 else 1


def _filter_jobs(jobs: List[Job], only: Optional[List[str]]) -> List[Job]:
    if not only:
        return jobs
    wanted = set(only)
    return [j for j in jobs if j.job_id in wanted]


def _run_one_job(
    db, task_id: int, job: Job,
    analyzer: Optional[ArticleAnalyzer],
    get_client: Optional[Callable[[str], WeChatClient]],
    args: argparse.Namespace,
    do_generate: bool,
    do_distribute: bool,
) -> bool:
    job_pk = db.upsert_job(task_id, job.job_id, line_id=job.line).id

    if do_generate:
        if analyzer is None or not _generate_one(db, job_pk, job, analyzer):
            return False

    if do_distribute:
        if get_client is None:
            return True
        with version_lock(job.job_id, 'image_job'):
            try:
                package = require_approved_package(job, expected_job_pk=job_pk)
            except ImagePackageError:
                package = build_package(job, job_pk=job_pk)
                db.update_job_status(job_pk, JobStatus.GENERATED,
                                     error_message='needs_review: 请先审核配图，确认后继续发布')
                logger.info('[%s] 正文已保留，图包 %s 等待首次人工审核；未发布 Blog 或公众号',
                            job.job_id, package.get('bundle_id', ''))
                return True
            if not _publish_chinese_blog(db, job_pk, job, expected_package_id=package.bundle_id):
                return False
            return _distribute_one(db, job_pk, job, get_client, args,
                                   expected_package_id=package.bundle_id)

    return True


def _publish_chinese_blog(db, job_pk: int, job: Job, *, expected_package_id=None) -> bool:
    """Publish and verify the canonical Chinese Blog before WeChat drafting."""
    from utils.blog_pipeline import BlogPipelineError, BlogWorkflow

    try:
        result = BlogWorkflow(db).publish(job.job_id, "zh", job_pk=job_pk,
                                         expected_package_id=expected_package_id)
    except BlogPipelineError as exc:
        error = f"blog: 中文官网文章发布失败：{exc}"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] %s", job.job_id, error)
        return False
    logger.info("[%s] 中文 Blog 已就绪：%s", job.job_id, result.get("url"))
    return True


def _serialized_image_job(function):
    @wraps(function)
    def locked(db, job_pk, job, *args, **kwargs):
        with version_lock(job.job_id, 'image_job'):
            return function(db, job_pk, job, *args, **kwargs)
    return locked


@_serialized_image_job
def _generate_one(db, job_pk: int, job: Job, analyzer: ArticleAnalyzer) -> bool:
    """生成阶段：方案 B 出基准正文 → 落盘 → 写 articles 表。"""
    db.update_job_status(job_pk, JobStatus.GENERATING)
    generation_source = _figure_source_fingerprint(job)
    result = analyzer.analyze(job)
    if not result.success:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=result.error_message)
        logger.error("[%s] generate failed: %s", job.job_id, result.error_message)
        return False
    if _figure_source_fingerprint(job) != generation_source:
        error = "generate: 原文 PDF 在生成过程中发生变化，请重新生成正文"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] %s", job.job_id, error)
        return False

    result.markdown, duplicate_figures = deduplicate_image_placeholders(result.markdown)
    if duplicate_figures:
        result.extra["duplicate_figure_placeholders_removed"] = duplicate_figures
        logger.warning(
            "[%s] 已移除 %d 个重复 Figure 占位符：%s",
            job.job_id, len(duplicate_figures), "; ".join(duplicate_figures),
        )

    out_dir = Path(ARTICLE_CONTENT_DIR) / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.md").write_text(result.markdown, encoding="utf-8")
    title, digest = extract_title_and_digest(result.markdown)
    html = markdown_to_wechat_html(result.markdown)
    (out_dir / "article.html").write_text(html, encoding="utf-8")

    # 质量自审：健康度 + 调性 + 正文夹带产品（方案 B）
    health = markdown_health_score(result.markdown)
    hard_ad = load_hard_ad_words(str(DATA_DIR / "hard_ad_words.txt"))
    tonal = scan_static(
        result.markdown, hard_ad,
        product_name=_safe_product_name(job), threshold=TONAL_THRESHOLD,
    )
    reasons: List[str] = []
    if health < HEALTH_THRESHOLD:
        reasons.append(f"markdown_unhealthy:{health}")
    if tonal.body_product_leak:
        reasons.append("body_product_leak")
    if tonal.score < TONAL_THRESHOLD:
        reasons.append(f"tonal_low:{tonal.score}")
    publish_blocked = bool(reasons)
    block_reason = ";".join(reasons) or None

    (out_dir / "meta.json").write_text(
        json.dumps({
            "job_id": job.job_id, "line": job.line, "title": title, "digest": digest,
            "source_pdf_sha256": generation_source.get("sha256", ""),
            "model": result.model, "tokens": result.total_tokens,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": result.latency_ms,
            "char_count": len(result.markdown),
            "markdown_health_score": health,
            "tonal_score": tonal.score,
            "publish_blocked": publish_blocked,
            "block_reason": block_reason,
            "duplicate_figure_placeholders_removed": duplicate_figures,
            "generated_at": datetime.utcnow().isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    db.upsert_article(
        job_pk,
        title=title, digest=digest, content_dir=str(out_dir),
        word_count=len(result.markdown),
        model=result.model,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens, latency_ms=result.latency_ms,
        markdown_health_score=health,
        tonal_score=tonal.score,
        tonal_feedback=json.dumps({
            "hard_ad_hits": tonal.hard_ad_hits,
            "body_product_leak": tonal.body_product_leak,
            "suggestions": tonal.suggestions,
        }, ensure_ascii=False),
        publish_blocked=publish_blocked,
        block_reason=block_reason,
    )
    # Generation only creates Blog queue records. Translation and Blog publishing
    # remain explicit administrator actions and never affect WeChat drafting.
    db.ensure_blog_versions(job_pk, str(out_dir), reset=True)
    db.update_job_status(job_pk, JobStatus.GENERATED)
    if publish_blocked:
        logger.warning("[%s] generated but BLOCKED: %s", job.job_id, block_reason)
    logger.info(
        "[%s] generated: title=%s len=%d tokens=%d health=%d tonal=%d%s",
        job.job_id, (title or "?")[:40], len(result.markdown), result.total_tokens,
        health, tonal.score, " [BLOCKED]" if publish_blocked else "",
    )
    return True


def _serialized_wechat(function):
    @wraps(function)
    def locked(db, job_pk, job, *args, **kwargs):
        with version_lock(job.job_id, 'image_job'), version_lock(job.job_id, 'wechat'):
            return function(db, job_pk, job, *args, **kwargs)
    return locked


@_serialized_wechat
def _distribute_one(
    db, job_pk: int, job: Job,
    get_client: Callable[[str], WeChatClient],
    args: argparse.Namespace, *, expected_package_id=None, enqueue_followups=True, receipt_callback=None,
) -> bool:
    """投放阶段：取基准正文 → 该线对应账户的公众号草稿（已有 media_id 则走 PATCH）。"""
    article = db.get_article(job_pk)
    if not article or not article.content_dir:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message="distribute: no article, run --stage generate first")
        logger.error("[%s] distribute: 还没生成基准正文", job.job_id)
        return False
    if getattr(article, "publish_blocked", False):
        # 质量闸拦下：稿子已落盘供人工 review，但不投放（不算失败）
        logger.warning("[%s] 质量闸拦下，跳过投放：%s", job.job_id, article.block_reason)
        return True
    md_path = Path(article.content_dir) / "article.md"
    if not md_path.exists():
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"distribute: missing {md_path}")
        logger.error("[%s] distribute: 缺 article.md (%s)", job.job_id, md_path)
        return False
    try:
        package = require_approved_package(job, expected_package_id=expected_package_id, expected_job_pk=job_pk)
    except ImagePackageError as exc:
        db.update_job_status(job_pk, JobStatus.GENERATED, error_message=f'needs_review: {exc}')
        return False
    # 从 article.md（唯一事实源）实时渲染 HTML —— 标题样式等改动无需重生成即可生效
    html = markdown_to_wechat_html((package.directory / 'article.md').read_text(encoding="utf-8"))
    account = _resolve_wechat_account(job)
    try:
        client = get_client(account)
    except WeChatAPIError as exc:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"distribute: {exc}")
        logger.error("[%s] distribute: 账户 %s 凭据未配置：%s", job.job_id, account, exc)
        return False
    existing = db.get_distribution(job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG)
    required_figure_keys = _required_figure_keys(html)
    try:
        resolved_figures = (package.figures, package.directory)
        _preflight_figure_paths(html, resolved_figures)
    except Exception as exc:
        error = f"distribute: 配图质量闸拦下：{exc}"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] %s", job.job_id, error)
        return False
    try:
        blog_url = resolve_published_blog_url(db, job_pk, job.job_id, lang="zh")
    except BlogUrlError as exc:
        error = f"distribute: 中文 Blog 未就绪：{exc}"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] Blog 质量闸拦下，未创建/更新公众号草稿：%s", job.job_id, exc)
        return False
    try:
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
        source_pdf_url = _ensure_source_pdf_published(
            db, job_pk, replace(job, pdf=str(package.directory / 'source.pdf')),
        )
    except (SourcePdfError, ImagePackageError) as exc:
        error = f"distribute: 原文 PDF 上传失败：{exc}"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] 原文 PDF 质量闸拦下，未创建/更新公众号草稿：%s", job.job_id, exc)
        return False
    # 所有平台使用审核时固定的封面，不再在投放时裁图或回退其他封面。
    try:
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
        thumb_media_id = _upload_cover_cached(client, account, package.cover_path)
    except (ImagePackageError, WeChatAPIError, OSError) as exc:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f'distribute: 审批封面不可用：{exc}')
        return False
    if not thumb_media_id:
        db.update_job_status(
            job_pk, JobStatus.FAILED,
            error_message=f"distribute: account '{account}' 缺封面 thumb_media_id",
        )
        logger.error(
            "[%s] distribute: 账户 %s 缺封面（设 WECHAT_%s_THUMB_MEDIA_ID；或确保原草稿在以复用其封面）",
            job.job_id, account, account.upper(),
        )
        return False

    try:
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
        html, n_figs = _apply_figures(html, job, client, account, resolved=resolved_figures, package=package)
    except (ValueError, OSError, WeChatAPIError) as exc:
        error = f"distribute: 配图失败，未创建/更新公众号草稿：{exc}"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] %s", job.job_id, error)
        return False
    leftover = len(find_image_placeholders(html))
    if leftover or n_figs < len(required_figure_keys):
        error = "distribute: 必需配图未完整插入，未创建/更新公众号草稿"
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=error)
        logger.error("[%s] %s", job.job_id, error)
        return False
    if n_figs < 1:
        logger.warning("[%s] 正文没有成功配图，继续生成草稿并保留阅读原文入口", job.job_id)

    # 拼接产品模块（line×platform 平台专属视觉块：文章链接 + 公众号名片卡 + 产品/二维码图）
    module = _load_product_module(job.line, WECHAT_PLATFORM)
    if module:
        html = html + module
        logger.info("[%s] 已拼接产品模块 %s-%s", job.job_id, job.line, WECHAT_PLATFORM)

    # 必须放在正文及产品模块之后，才能紧邻微信底部的“阅读原文”入口。
    try:
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
        html, guide_url = append_source_pdf_guide(html, client, account)
    except ImagePackageError as exc:
        db.update_job_status(job_pk, JobStatus.GENERATED, error_message=f'needs_review: {exc}')
        return False
    except TemplateAssetError as exc:
        if not enqueue_followups:
            db.update_job_status(job_pk, JobStatus.GENERATED, error_message='阅读原文引导图准备失败，未提交公开发布')
            return False
        logger.warning("[%s] 阅读原文引导图失败，继续生成草稿：%s", job.job_id, exc)
    else:
        logger.info("[%s] 已追加阅读原文引导图：%s", job.job_id, guide_url)

    db.update_job_status(job_pk, JobStatus.PUBLISHING)
    payload = _build_article_payload(
        title=article.title or job.title_hint or job.job_id,
        digest=article.digest or "", content_html=html,
        author=_resolve_author(account, args),
        thumb_media_id=thumb_media_id,
        content_source_url=blog_url,
    )
    media_id = ''
    try:
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
        if receipt_callback:
            receipt_callback('intent', {'content': payload['content'], 'title': payload['title'], 'thumb_media_id': thumb_media_id})
        if existing and existing.wechat_media_id:
            try:
                client.update_draft(existing.wechat_media_id, 0, payload)
                media_id = existing.wechat_media_id
                logger.info("[%s] PATCH wechat/%s media_id=%s", job.job_id, account, media_id)
            except WeChatAPIError as exc:
                if exc.errcode != 40007:  # 40007=media_id 失效（草稿已被删/过期）→ 重建；其它错抛出
                    raise
                logger.warning("[%s] 原草稿 media_id 失效(40007)，改为新建草稿", job.job_id)
                require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
                media_id = client.create_draft([payload])
                logger.info("[%s] POST wechat/%s media_id=%s（原草稿已删，重建）", job.job_id, account, media_id)
        else:
            media_id = client.create_draft([payload])
            logger.info("[%s] POST wechat/%s media_id=%s", job.job_id, account, media_id)
        if receipt_callback:
            receipt_callback('draft', {'media_id': media_id})
        from utils.wechat_draft_repair import _Body, _first, _image_key
        actual = _first(client.get_draft(media_id))
        expected_images = [_image_key(url) for url in _Body(payload['content']).images]
        actual_images = [_image_key(url) for url in _Body(str(actual.get('content') or '')).images]
        if (actual_images != expected_images or actual.get('thumb_media_id') != thumb_media_id
                or not full_width_cover_verified(actual)):
            raise WeChatAPIError('公众号草稿图片/封面回读不匹配，未启动多语言发布')
        require_approved_package(job, expected_package_id=package.bundle_id, expected_job_pk=job_pk)
    except Exception as exc:
        if receipt_callback:
            receipt_callback('error', {'definite': isinstance(exc, WeChatAPIError) and exc.errcode is not None,
                                       'code': exc.errcode if isinstance(exc, WeChatAPIError) else None})
        db.upsert_distribution(
            job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
            publish_status="failed", publish_error=str(exc), assembled_dir=article.content_dir,
            wechat_media_id=media_id or (existing.wechat_media_id if existing else None),
            image_package_id=package.bundle_id, image_verification_status='failed',
        )
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"publish: {exc}")
        logger.error("[%s] publish failed: %s", job.job_id, exc)
        return False

    db.upsert_distribution(
        job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
        wechat_media_id=media_id, publish_status="published" if enqueue_followups else "drafted", publish_error=None,
        assembled_dir=article.content_dir,
        image_package_id=package.bundle_id, image_verification_status='verified',
        image_receipt_json=json.dumps({'image_package_id': package.bundle_id,
            'media_id': media_id, 'thumb_media_id': thumb_media_id,
            'image_keys': actual_images, 'draft_readback_verified': True,
            'wide_cover_crop_verified': True, 'square_preview_status': 'not_reviewed'}, ensure_ascii=False),
    )
    db.update_job_status(job_pk, JobStatus.PUBLISHED if enqueue_followups else JobStatus.GENERATED)
    if not enqueue_followups:
        return True
    try:
        count = enqueue_auto_blog(db, job_pk, job.job_id, owner_line=job.line or "",
                                  image_package_id=package.bundle_id)
        logger.info("[%s] 公众号草稿成功，多语言后台队列新增 %s 个版本", job.job_id, count)
    except Exception:
        # A Blog failure must not undo a valid WeChat draft or its success state.
        logger.exception("[%s] 多语言后台队列启动失败；公众号草稿已保留，可手动重试", job.job_id)
    return True


def _safe_product_name(job: Job) -> str:
    """取产品显示名（用于正文夹带扫描）；取不到回空串（最佳努力，不阻断生成）。"""
    if not job.product:
        return ""
    try:
        return (load_product_by_id(PRODUCTS_DIR, job.product).name or "").strip()
    except Exception:
        return ""


def _ensure_source_pdf_published(db, job_pk: int, job: Job) -> str:
    """Upload the original PDF once and verify its public URL before drafting."""
    from config import asset_public_base_url, asset_storage_mode

    path, digest, size = inspect_pdf(job.pdf)
    try:
        target_base = asset_public_base_url() if asset_storage_mode("SOURCE_PDF_STORAGE") == "oss" else ""
    except ValueError as exc:
        raise SourcePdfError(str(exc)) from exc
    db_job = db.get_job(job_pk)
    if (
        db_job
        and db_job.source_pdf_url
        and db_job.source_pdf_sha256 == digest
        and int(db_job.source_pdf_size or 0) == size
        and (not target_base or db_job.source_pdf_url.startswith(target_base + "/"))
    ):
        try:
            if target_base:
                verify_public_pdf(db_job.source_pdf_url, expected_sha256=digest, expected_size=size)
            else:
                verify_public_pdf(db_job.source_pdf_url)
            logger.info("[%s] 复用已校验原文 PDF：%s", job.job_id, db_job.source_pdf_url)
            return db_job.source_pdf_url
        except SourcePdfError as exc:
            logger.warning("[%s] 已存原文 PDF 链接失效，重新上传：%s", job.job_id, exc)

    published = create_source_pdf_store().upload(str(path))
    if published.sha256 != digest or int(published.size) != size:
        raise SourcePdfError("上传结果与本地原文 PDF 的哈希或大小不一致")
    db.update_job_source_pdf(
        job_pk, url=published.url, sha256=published.sha256, size=published.size,
    )
    logger.info(
        "[%s] 原文 PDF 已发布：%s bytes=%d sha256=%s",
        job.job_id, published.url, published.size, published.sha256[:12],
    )
    return published.url


def _resolve_wechat_account(job: Job) -> str:
    """从 line 配置取该线对应的公众号账户（extra.wechat_account）；取不到回 default。"""
    if not job.line:
        return "default"
    try:
        line = load_line_by_id(LINES_DIR, job.line)
    except LineLoadError:
        return "default"
    return str((line.extra or {}).get("wechat_account") or "default")


def _make_client_getter() -> Callable[[str], WeChatClient]:
    """返回按账户建并缓存 WeChatClient 的 getter —— token 按账户隔离（头号坑）。"""
    cache: Dict[str, WeChatClient] = {}

    def get_client(account: str) -> WeChatClient:
        if account not in cache:
            cache[account] = WeChatClient(account=account)
        return cache[account]

    return get_client


def _load_product_module(line: Optional[str], platform: str) -> str:
    """读取 (line, platform) 的产品模块 HTML，拼到正文尾；无则回空串（不强制每线都有）。

    模块是平台专属视觉块（公众号文章链接 + 公众号名片卡 + 产品/二维码大图），由人工在公众号
    编辑器做好、用 get_draft 抽出存档到 inputs/product_modules/{line}-{platform}.html。微信编辑器
    原样 HTML（含 mp-common-profile 名片卡、data-src 图片）经 create_draft 重提交可正常渲染。
    """
    if not line:
        return ""
    path = PROJECT_ROOT / "inputs" / "product_modules" / f"{line}-{platform}.html"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _resolve_author(account: str, args: argparse.Namespace) -> str:
    """草稿作者署名（用公众号名）：WECHAT_{ACCOUNT}_AUTHOR > --placeholder-author > DEFAULT_AUTHOR。"""
    return (
        os.getenv(f"WECHAT_{account.upper()}_AUTHOR", "").strip()
        or (getattr(args, "placeholder_author", "") or "").strip()
        or os.getenv("DEFAULT_AUTHOR", "").strip()
    )


def _resolve_thumb_media_id(account: str, args: argparse.Namespace) -> str:
    """该账户封面素材 id：WECHAT_{ACCOUNT}_THUMB_MEDIA_ID > --placeholder-thumb-media > DEFAULT。"""
    return (
        os.getenv(f"WECHAT_{account.upper()}_THUMB_MEDIA_ID", "").strip()
        or (getattr(args, "placeholder_thumb_media", "") or "").strip()
        or os.getenv("DEFAULT_THUMB_MEDIA_ID", "").strip()
    )


def _existing_draft_thumb(client: WeChatClient, media_id: str) -> str:
    """取已存草稿当前封面 thumb_media_id（重投 PATCH 时复用）；取不到回空串。"""
    try:
        data = client.get_draft(media_id)
    except WeChatAPIError:
        return ""
    items = data.get("news_item") or []
    if items:
        return str(items[0].get("thumb_media_id") or "").strip()
    return ""


def _fig_max_pages(job: Job) -> Optional[int]:
    """配图扫描页数上限，与正文 ``max_pages`` 解耦。

    投稿稿常把 Figure Legends 和真正图页放在全文末尾；沿用正文前 N 页上限会把这些页
    整体截掉。默认扫描完整 PDF，只在 job.extra.figure_max_pages 或环境变量
    PDF_FIGURE_MAX_PAGES 明确配置时限页。
    """
    raw = (job.extra or {}).get("figure_max_pages")
    if raw in (None, ""):
        raw = os.getenv("PDF_FIGURE_MAX_PAGES", "").strip()
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _auto_pdf_figures_enabled() -> bool:
    """是否允许从 PDF 自动裁剪配图；关闭后仍保留 image_pool/人工 figN 文件。"""
    return os.getenv("PDF_AUTO_FIGURES_ENABLED", "1").strip() not in ("0", "false", "False")


_MIN_PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS = 75.0
_MIN_FIGURE_FALLBACK_BUDGET_SECONDS = 20.0
_DEFAULT_FIGURE_TOTAL_BUDGET_SECONDS = 180.0
_DEFAULT_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS = 180.0


def _figure_extract_timeout_seconds() -> float:
    raw = os.getenv("PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS", "75").strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = 75.0
    return max(_MIN_PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS, timeout)


def _figure_total_budget_seconds() -> float:
    raw = os.getenv("PDF_FIGURE_TOTAL_BUDGET_SECONDS", str(int(_DEFAULT_FIGURE_TOTAL_BUDGET_SECONDS))).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = _DEFAULT_FIGURE_TOTAL_BUDGET_SECONDS
    return max(_figure_extract_timeout_seconds(), timeout)


def _figure_strategy_timeout_seconds(kind: str) -> float:
    """VLM 需要覆盖渲染和多页请求，不能与本地确定性抽图共用短上限。"""
    base_timeout = _figure_extract_timeout_seconds()
    if kind != "vision":
        return base_timeout
    raw = os.getenv(
        "PDF_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS",
        str(int(_DEFAULT_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS)),
    ).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = _DEFAULT_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS
    return max(base_timeout, timeout)


def _run_pdf_figure_worker(
    kind: str, job: Job, figures_dir: Path, timeout_seconds: Optional[float] = None,
) -> List[Figure]:
    """在独立 Python 进程里抽 PDF 图，避免复杂 PDF 把面板任务拖死。"""
    if not (job.pdf and Path(job.pdf).exists()):
        return []
    figures_dir.mkdir(parents=True, exist_ok=True)
    result_path = figures_dir / f".{kind}_worker_result.json"
    try:
        result_path.unlink()
    except FileNotFoundError:
        pass
    timeout = _figure_extract_timeout_seconds() if timeout_seconds is None else max(1.0, timeout_seconds)
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not env.get("PYTHONPATH")
        else str(PROJECT_ROOT) + os.pathsep + str(env["PYTHONPATH"])
    )
    cmd = [
        sys.executable,
        "-m",
        "utils.figure_extract_worker",
        kind,
        str(job.pdf),
        str(figures_dir),
        str(_fig_max_pages(job) or ""),
        str(result_path),
    ]
    if kind == 'caption' and job.extra.get('_required_figure_keys'):
        cmd.append(json.dumps(job.extra['_required_figure_keys']))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[%s] %s 抽图超时 %.0fs，跳过该抽图方式", job.job_id, kind, timeout)
        return []
    if proc.returncode != 0:
        if proc.returncode == 3:
            from utils.figure_crop_geometry import UnsafeFigureCrop
            try:
                failure = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                failure = {}
            if failure.get("error") == "unsafe_figure_crop":
                raise UnsafeFigureCrop(failure.get("message", "图片裁剪需要人工检查"))
        detail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-3:]).strip()
        logger.warning("[%s] %s 抽图子进程失败 rc=%s：%s", job.job_id, kind, proc.returncode, detail)
        return []
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[%s] %s 抽图结果读取失败：%s", job.job_id, kind, exc)
        return []
    figs: List[Figure] = []
    for item in data if isinstance(data, list) else []:
        try:
            figs.append(Figure(
                label=str(item.get("label") or ""),
                is_extended=bool(item.get("is_extended")),
                caption=str(item.get("caption") or ""),
                page=int(item.get("page") or 0),
                image_path=str(item.get("image_path") or ""),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                bbox=tuple(item["bbox"]) if item.get("bbox") else None,
                source_sha256=str(item.get("source_sha256") or ""),
            ))
        except (AttributeError, TypeError, ValueError):
            continue
    return figs


def _resolve_figure_path(description: str, figures_dir: Path, extracted) -> Optional[str]:
    """仅接受已通过来源和质量检查的匹配图，不凭文件名信任手工缓存。"""
    key = figure_key_from_description(description)
    return next((figure.image_path for figure in extracted
                 if key == (figure.label, figure.is_extended)), None) if key else None


def _preflight_figure_paths(html: str, resolved: Tuple[List[Figure], Path]) -> dict[str, str]:
    """上传任何资源之前，确认每个占位符可用且不同图号不复用同一张图。"""
    figures, directory = resolved
    paths: dict[str, str] = {}
    content_keys: dict[str, FigureKey | None] = {}
    for description in find_image_placeholders(html):
        path = _resolve_figure_path(description, directory, figures)
        if not path or not Path(path).is_file():
            raise ValueError(f"缺少经校验的配图：{description}")
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        key = figure_key_from_description(description)
        if digest in content_keys and content_keys[digest] != key:
            raise ValueError(f"不同图号不能使用同一张图片：{description}")
        content_keys[digest] = key
        paths[description] = path
    return paths


def _upload_cached(client: WeChatClient, account: str, local_path: str) -> str:
    """上传图片到该账户，sha256 缓存避免重复上传（公众号 uploadimg 5000/日配额）。"""
    manifest_path = RUNTIME_DIR / f"image_upload_manifest_{account}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        manifest = {}
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    if sha in manifest:
        return str(manifest[sha])
    url = client.upload_image(local_path)
    manifest[sha] = url
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return url


def _upload_cover_cached(client: WeChatClient, account: str, local_path: str) -> str:
    """上传封面到永久素材（material/add）拿 media_id，sha256 缓存避免重复（永久素材有数量上限）。"""
    manifest_path = RUNTIME_DIR / f"cover_material_manifest_{account}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        manifest = {}
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    if sha in manifest:
        return str(manifest[sha])
    media_id = client.add_permanent_material(local_path, "image")
    manifest[sha] = media_id
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return media_id


def _load_pool_figures(job: Job, *, rejections: Optional[list[dict]] = None) -> List[Figure]:
    """从 image_pool 的 figures_manifest.json 载入预备配图（按图号匹配 ``[图片:Figure N]``）。

    人工素材必须声明同一 PDF 的 SHA256、图片文件 SHA256 和 reviewed=true。
    旧的无来源图片池不再可信，忽略后由 PDF 抽取兜底。
    """
    if not job.image_pool:
        return []
    pool = PROJECT_ROOT / "inputs" / "image_pools" / job.image_pool
    manifest = pool / "figures_manifest.json"
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("[%s] image_pool manifest 解析失败：%s", job.job_id, exc)
        return []
    source_sha = _figure_source_fingerprint(job).get("sha256")
    if not source_sha or not isinstance(data, dict) or data.get("source_sha256") != source_sha:
        rejection = {"strategy": "pool", "reason": "pool_source_mismatch", "path": str(manifest)}
        if rejections is not None:
            rejections.append(rejection)
        logger.warning("[%s] image_pool 未绑定当前 PDF，忽略旧图片池", job.job_id)
        return []
    figs: List[Figure] = []
    for item in data.get("figures", []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        p = PROJECT_ROOT / rel
        if not p.exists():
            p = Path(rel)  # path 可能已是绝对路径
        if not p.exists():
            logger.warning("[%s] image_pool 图缺失：%s", job.job_id, rel)
            continue
        if item.get("reviewed") is not True or item.get("sha256") != hashlib.sha256(p.read_bytes()).hexdigest():
            if rejections is not None:
                rejections.append({"strategy": "pool", "reason": "pool_unreviewed_or_hash_mismatch", "path": str(p)})
            logger.warning("[%s] image_pool 图未经审核或文件已变化：%s", job.job_id, rel)
            continue
        caption = str(item.get("caption") or "")
        key = figure_key_from_description(caption)
        figs.append(Figure(
            label=key[0] if key else "", is_extended=key[1] if key else False, caption=caption,
            page=int(item.get("page") or 0), image_path=str(p),
            width=int(item.get("width") or 0), height=int(item.get("height") or 0),
            bbox=tuple(item["bbox"]) if item.get("bbox") else None,
            source_sha256=source_sha,
        ))
    return figs


def _figure_source_fingerprint(job: Job) -> dict:
    """内容哈希用于避免同名 PDF 重传时误用旧抽图缓存。"""
    if not (job.pdf and Path(job.pdf).exists()):
        return {}
    path = Path(job.pdf)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
        "max_pages": _fig_max_pages(job),
    }


def _prepare_figure_cache(job: Job, figures_dir: Path) -> dict:
    """PDF 内容变动时清理自动生成物；未声明来源的手工文件不会被解析器使用。"""
    figures_dir.mkdir(parents=True, exist_ok=True)
    source = _figure_source_fingerprint(job)
    if not source:
        return source
    manifest = figures_dir / "figure_source_manifest.json"
    try:
        previous = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if previous != source:
        names = (
            "caption_figures_manifest.json", "legend_figures_manifest.json",
            "vision_figures_manifest.json", "figures_manifest.json",
            ".caption_version", ".crop_version", "figure_resolution_report.json",
            "_cover_page1.png", "_wechat_cover.jpg",
        )
        patterns = ("cap*_p*.jpg", "figvis*_p*.jpg", "legend_*_p*.png", "fig*_p*.png")
        for name in names:
            (figures_dir / name).unlink(missing_ok=True)
        for pattern in patterns:
            for artifact in figures_dir.glob(pattern):
                artifact.unlink(missing_ok=True)
        if previous:
            logger.info("[%s] PDF 内容已变化，已失效旧的自动抽图缓存", job.job_id)
    manifest.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    return source


def _article_source_binding(job: Job, source: dict, *, warn: bool = True) -> str:
    """已绑定的正文不能静默改用另一份同名 PDF 的图片。"""
    meta_path = Path(ARTICLE_CONTENT_DIR) / job.job_id / "meta.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        metadata = {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法核对正文的原文来源：{exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("正文元数据格式错误，无法核对原文来源")
    expected = metadata.get("source_pdf_sha256")
    if expected:
        if source.get("sha256") != expected:
            raise ValueError("正文生成后原文 PDF 已变化，须重新生成或人工核对来源，不能继续发布配图")
        return "verified"
    if warn:
        logger.warning("[%s] 旧正文没有原文 SHA256，来源尚未绑定；本次配图报告保留审计标记", job.job_id)
    return "legacy_unbound"


def _resolution_complete(figures: List[Figure], required_keys: set[FigureKey]) -> bool:
    return bool(figures) if not required_keys else not missing_figure_keys(required_keys, figures)


def _merge_accepted_figures(preferred: List[Figure], fallback: List[Figure]) -> Tuple[List[Figure], list[dict]]:
    """跨策略也不能把相同文件内容当作不同图号。"""
    merged = merge_preferred_figures(preferred, fallback)
    hashes: dict[str, list[Figure]] = {}
    for figure in merged:
        digest = hashlib.sha256(Path(figure.image_path).read_bytes()).hexdigest()
        hashes.setdefault(digest, []).append(figure)
    rejected = []
    for group in hashes.values():
        if len({(figure.label, figure.is_extended) for figure in group}) > 1:
            for figure in group:
                merged.remove(figure)
                rejected.append({"label": figure.label, "path": figure.image_path,
                                 "reason": "cross_strategy_duplicate_image", "quality_version": QUALITY_VERSION})
    return merged, rejected


def _write_figure_resolution_report(
    figures_dir: Path, job: Job, source: dict, required_keys: set[FigureKey], signals, attempts: list[dict], figures: List[Figure],
) -> None:
    """保留机器报告，面板和排障可区分空结果、超时与覆盖不足。"""
    if _figure_source_fingerprint(job) != source:
        raise ValueError("配图处理期间原文 PDF 已变化，请重试并核对来源")
    report = {
        "job_id": job.job_id,
        "quality_version": QUALITY_VERSION,
        "pdf": str(job.pdf or ""),
        "source": source,
        "article_source_binding": _article_source_binding(job, source, warn=False),
        "required": [f"{'extended:' if ext else ''}{label}" for label, ext in sorted(required_keys)],
        "signals": {
            "caption": [f"{'extended:' if ext else ''}{label}" for label, ext in sorted(signals.caption_keys)],
            "has_legend_section": bool(signals.has_legend_section),
            "page_count": signals.page_count,
            "scan_error": signals.scan_error,
        },
        "attempts": attempts,
        "selected": [
            {"label": figure.label, "extended": figure.is_extended, "page": figure.page,
             "path": figure.image_path, "width": figure.width, "height": figure.height,
             "bbox": figure.bbox, "source_sha256": figure.source_sha256,
             "sha256": hashlib.sha256(Path(figure.image_path).read_bytes()).hexdigest()}
            for figure in figures
        ],
        "missing": [
            f"{'extended:' if ext else ''}{label}"
            for label, ext in sorted(missing_figure_keys(required_keys, figures))
        ],
    }
    (figures_dir / "figure_resolution_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _resolve_job_figures(
    job: Job, required_keys: Optional[set[FigureKey]] = None, *, figures_dir: Optional[Path] = None,
) -> Tuple[List[Figure], Path]:
    """按文章实际所需图号选择抽图器，合并回退结果并保留策略报告。"""
    figures_dir = Path(figures_dir) if figures_dir is not None else Path(ARTICLE_CONTENT_DIR) / job.job_id / "figures"
    required_keys = set(required_keys or set())
    source = _prepare_figure_cache(job, figures_dir)
    _article_source_binding(job, source)
    rejected: list[dict] = []
    pool = _load_pool_figures(job, rejections=rejected)
    accepted, quality_rejections = filter_candidates(job.pdf, pool, "pool") if pool else ([], [])
    rejected.extend(quality_rejections)
    figures = merge_preferred_figures([], accepted)
    attempts: list[dict] = [{
        "strategy": "pool", "status": "success" if accepted else "rejected" if rejected else "empty",
        "result_count": len(accepted), "rejected": rejected,
    }]
    signals = FigureSignals(frozenset(), False, 0)
    if _resolution_complete(figures, required_keys):
        _write_figure_resolution_report(figures_dir, job, source, required_keys, signals, attempts, figures)
        return figures, figures_dir
    if not _auto_pdf_figures_enabled():
        logger.warning("[%s] PDF 自动配图已关闭，跳过 PDF 裁剪；仅使用人工/预置配图", job.job_id)
        _write_figure_resolution_report(figures_dir, job, source, required_keys, signals, attempts, figures)
        return figures, figures_dir
    if not (job.pdf and Path(job.pdf).exists()):
        _write_figure_resolution_report(figures_dir, job, source, required_keys, signals, attempts, figures)
        return figures, figures_dir
    try:
        from utils.caption_figures import caption_enabled
        from utils.vision_figures import vision_enabled

        caption_available = caption_enabled()
        vision_available = vision_enabled()
    except Exception as exc:  # noqa: BLE001 - 导入异常仍保留确定性抽图兜底
        logger.warning("[%s] 抽图能力配置读取失败：%s", job.job_id, exc)
        caption_available = True
        vision_available = False

    signals = _inspect_pdf_figure_signals(job.pdf, _fig_max_pages(job))
    strategies = [
        kind for kind in planned_figure_strategies(signals, vision_available)
        if kind != "caption" or caption_available
    ]
    # Narrow layout parsing to all occurrences of the requested captions. Do not
    # spend the worker's budget interpreting unreferenced vector-heavy appendices.
    if required_keys:
        job = replace(job, extra={**job.extra, '_required_figure_keys': sorted(required_keys)})
    budget = _figure_total_budget_seconds()
    started = time.monotonic()
    for kind in strategies:
        if _resolution_complete(figures, required_keys):
            break
        remaining = budget - (time.monotonic() - started)
        if remaining < _MIN_FIGURE_FALLBACK_BUDGET_SECONDS:
            attempts.append({"strategy": kind, "status": "skipped_budget", "remaining_seconds": round(max(0, remaining), 3)})
            break
        strategy_timeout = _figure_strategy_timeout_seconds(kind)
        timeout = min(strategy_timeout, remaining)
        attempt_started = time.monotonic()
        try:
            extracted = (
                _run_pdf_figure_worker(kind, job, figures_dir)
                if timeout >= strategy_timeout
                else _run_pdf_figure_worker(kind, job, figures_dir, timeout)
            )
        except Exception as exc:  # noqa: BLE001 - 单个策略异常必须继续回退
            from utils.figure_crop_geometry import UnsafeFigureCrop
            if isinstance(exc, UnsafeFigureCrop):
                raise
            logger.warning("[%s] %s 抽图异常：%s", job.job_id, kind, exc)
            extracted = []
        accepted, rejected = filter_candidates(job.pdf, extracted, kind)
        if any(item.get('reason') == 'ambiguous_figure_number_across_regions'
               and (not required_keys or (item.get('label'), bool(item.get('extended'))) in required_keys)
               for item in rejected):
            from utils.figure_crop_geometry import UnsafeFigureCrop
            raise UnsafeFigureCrop('同一 PDF 的图号对应多个区域，须人工确认来源，不能用其他策略猜选首图')
        figures, duplicate_rejections = _merge_accepted_figures(figures, accepted)
        rejected.extend(duplicate_rejections)
        missing = missing_figure_keys(required_keys, figures)
        attempts.append({
            "strategy": kind,
            "status": "success" if accepted else "rejected" if rejected else "empty",
            "seconds": round(time.monotonic() - attempt_started, 3),
            "result_count": len(accepted),
            "candidate_count": len(extracted),
            "rejected": rejected,
            "labels": [f"{'extended:' if figure.is_extended else ''}{figure.label}" for figure in accepted],
            "missing": [f"{'extended:' if ext else ''}{label}" for label, ext in sorted(missing)],
        })
    _write_figure_resolution_report(figures_dir, job, source, required_keys, signals, attempts, figures)
    logger.info(
        "[%s] 配图策略完成：图=%d，缺=%d，策略=%s",
        job.job_id, len(figures), len(missing_figure_keys(required_keys, figures)),
        ",".join(item["strategy"] for item in attempts) or "none",
    )
    return figures, figures_dir


def _fit_wechat_cover(source_path: str | Path, figures_dir: Path) -> Optional[str]:
    """完整保留论文图的所有面板，等比缩放并留白到公众号 900x383 尺寸。"""
    try:
        from PIL import Image, ImageOps

        source = Path(source_path)
        with Image.open(source) as image:
            if image.size == (900, 383):
                return str(source)
            contained = ImageOps.contain(
                image.convert("RGB"),
                (900, 383),
                method=Image.Resampling.LANCZOS,
            )
            cover = Image.new("RGB", (900, 383), "white")
            cover.paste(contained, ((900 - contained.width) // 2, (383 - contained.height) // 2))
        figures_dir.mkdir(parents=True, exist_ok=True)
        out = figures_dir / "_wechat_cover.jpg"
        cover.save(str(out), quality=92)
        return str(out)
    except Exception as exc:  # noqa: BLE001 - 由上层回落缺封面处理
        logger.warning("公众号封面适配失败 %s：%s", source_path, exc)
        return None


def _required_figure_keys(html: str) -> set[FigureKey]:
    return {
        key for description in find_image_placeholders(html)
        if (key := figure_key_from_description(description)) is not None
    }


def _auto_cover_media_id(
    client: WeChatClient, account: str, job: Job, html: str,
    resolved: Optional[Tuple[List[Figure], Path]] = None, package=None,
) -> str:
    """首次发该账户草稿、又没配封面时：用文章首图自动当封面（image_pool 优先，PDF 抽取兜底）。

    取 html 里第一个 ``[图片:…]`` 占位符对应的图；取不到回落到首张可用配图。
    传永久素材拿 thumb media_id（sha 缓存）。任何环节取不到图就回空串，由上层报"缺封面"。
    """
    package = require_approved_package(job, expected_package_id=package.bundle_id if package else None,
        expected_job_pk=getattr(package, 'manifest', {}).get('job_pk') if package else None)
    cover_path = package.cover_path
    try:
        media_id = _upload_cover_cached(client, account, cover_path)
    except WeChatAPIError as exc:
        logger.error("[%s] 自动封面：上传永久素材失败 %s", job.job_id, exc)
        return ""
    logger.info("[%s] 自动封面：%s -> thumb media_id=%s", job.job_id, Path(cover_path).name, media_id)
    return media_id


def _apply_figures(
    html: str, job: Job, client: WeChatClient, account: str,
    resolved: Optional[Tuple[List[Figure], Path]] = None, package=None,
) -> Tuple[str, int]:
    """全部占位符通过检查后才上传；任何必需图缺失或上传失败都不生成残缺正文。"""
    package = require_approved_package(job, expected_package_id=package.bundle_id if package else None,
        expected_job_pk=getattr(package, 'manifest', {}).get('job_pk') if package else None)
    placeholders = find_image_placeholders(html)
    if not placeholders:
        return html, 0
    resolved = (package.figures, package.directory)
    paths = _preflight_figure_paths(html, resolved)
    used = 0
    used_paths: set = set()
    for desc in placeholders:
        path = paths[desc]
        if path in used_paths:
            # 同一张图已用过（如 Figure 1 与 Figure 1e 都指向 Fig 1）→ 删掉重复占位符，不重复插图
            html = html.replace(f"[图片:{desc}]", "", 1)
            continue
        require_approved_package(job, expected_package_id=package.bundle_id,
                                 expected_job_pk=getattr(package, 'manifest', {}).get('job_pk'))
        url = _upload_cached(client, account, path)
        if not url:
            raise ValueError(f"配图上传未返回图片地址：{desc}")
        html = replace_image_placeholder(html, desc, url)
        used_paths.add(path)
        used += 1
    return html, used


def _build_article_payload(
    title: str, digest: str, content_html: str,
    author: str, thumb_media_id: str,
    content_source_url: str = "",
) -> dict:
    """公众号 draft/add 单篇 article 的最小字段集。"""
    return {
        "title": title[:64] or "未命名",          # 公众号上限 64 字
        "author": author[:8] or "TarMart",      # 公众号上限 8 字
        "digest": digest[:120] or title[:120],   # 公众号上限 120 字
        "content": content_html,
        "content_source_url": content_source_url,
        "thumb_media_id": thumb_media_id,
        "show_cover_pic": 1,
        **approved_cover_geometry(),
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


if __name__ == "__main__":
    raise SystemExit(main())
