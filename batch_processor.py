"""wechat-article Phase 0 主入口。

用法：
    python batch_processor.py
    python batch_processor.py --jobs inputs/jobs.yaml --task my-batch-001
    python batch_processor.py --only 2026-05-27-001   # 只跑指定 job_id

流程（极简，单线）：
    load jobs.yaml
    → 写库（tasks / jobs）
    → 逐个 job：
        ArticleAnalyzer.analyze() → markdown
        落盘 outputs/jobs/{job_id}/article.{md,html}
        wechat_html.markdown_to_wechat_html() → wechat HTML
        WeChatClient.create_draft 或 update_draft → media_id
        更新 articles / article_drafts 表
    打印汇总

Phase 0 不做：图片占位符替换、tonal QA、多路、dashboard。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# 让 utils / core / db 都能 from-import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.main import AnalysisResult, ArticleAnalyzer
from db.database import ARTICLE_CONTENT_DIR, JobStatus, get_db_manager
from utils.job_loader import Job, load_jobs
from utils.logger import setup_logger
from utils.wechat_client import WeChatAPIError, WeChatClient
from utils.wechat_html import (
    extract_title_and_digest,
    markdown_to_wechat_html,
)

load_dotenv()
logger = setup_logger("batch_processor")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JOBS_YAML = str(PROJECT_ROOT / "inputs" / "jobs.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-article batch processor (Phase 0)")
    parser.add_argument("--jobs", default=DEFAULT_JOBS_YAML, help="jobs.yaml 路径")
    parser.add_argument(
        "--task",
        default=f"wechat-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        help="task 名（DB 里用）",
    )
    parser.add_argument("--only", action="append", help="只跑指定 job_id（可多次传）")
    parser.add_argument("--dry-run", action="store_true", help="跑生成，不发布到公众号")
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
            "Phase 0 没图片管线，必须手动准备一张永久素材并填 .env DEFAULT_THUMB_MEDIA_ID 或传 --placeholder-thumb-media。"
        ),
    )
    args = parser.parse_args()

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
    task = db.get_or_create_task(args.task, description="Phase 0 single-line batch")
    logger.info("task=%s (id=%d) will run %d/%d jobs", task.task_name, task.id, len(selected), len(all_jobs))

    for j in selected:
        db.upsert_job(
            task.id, j.job_id,
            pdf_path=j.pdf, template_id=j.template, product_id=j.product,
            image_pool=j.image_pool, title_hint=j.title_hint,
            status=JobStatus.PENDING,
        )

    # 实例化执行体
    try:
        analyzer = ArticleAnalyzer()
    except Exception as exc:
        logger.error("ArticleAnalyzer init failed: %s", exc)
        return 3

    wechat_client: Optional[WeChatClient] = None
    if not args.dry_run:
        if not args.placeholder_thumb_media:
            logger.error(
                "no DEFAULT_THUMB_MEDIA_ID / --placeholder-thumb-media; "
                "Phase 0 needs a manually uploaded cover image. Use --dry-run to skip publish."
            )
            return 3
        try:
            wechat_client = WeChatClient()
        except WeChatAPIError as exc:
            logger.error("WeChatClient init failed: %s", exc)
            return 3

    # 主循环
    success = 0
    failed = 0
    for j in selected:
        ok = _run_one_job(db, task.id, j, analyzer, wechat_client, args)
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
    analyzer: ArticleAnalyzer,
    wechat_client: Optional[WeChatClient],
    args: argparse.Namespace,
) -> bool:
    job_row = db.upsert_job(task_id, job.job_id)
    job_pk = job_row.id

    db.update_job_status(job_pk, JobStatus.GENERATING)

    # 1. 生成 markdown
    result = analyzer.analyze(job)
    if not result.success:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=result.error_message)
        logger.error("[%s] generate failed: %s", job.job_id, result.error_message)
        return False

    # 2. 落盘
    out_dir = Path(ARTICLE_CONTENT_DIR) / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.md").write_text(result.markdown, encoding="utf-8")
    title, digest = extract_title_and_digest(result.markdown)
    html = markdown_to_wechat_html(result.markdown)
    (out_dir / "article.html").write_text(html, encoding="utf-8")
    (out_dir / "meta.json").write_text(
        json.dumps({
            "job_id": job.job_id, "title": title, "digest": digest,
            "model": result.model, "tokens": result.total_tokens,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": result.latency_ms,
            "char_count": len(result.markdown),
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
    )
    db.update_job_status(job_pk, JobStatus.GENERATED)
    logger.info(
        "[%s] generated: title=%s len=%d tokens=%d",
        job.job_id, (title or "?")[:40], len(result.markdown), result.total_tokens,
    )

    # 3. 发布到草稿（除非 --dry-run）
    if args.dry_run or wechat_client is None:
        logger.info("[%s] dry-run, skip publish", job.job_id)
        return True

    db.update_job_status(job_pk, JobStatus.PUBLISHING)
    existing = db.get_draft(job_pk)
    article_payload = _build_article_payload(
        title=title or job.title_hint or job.job_id,
        digest=digest, content_html=html,
        author=args.placeholder_author,
        thumb_media_id=args.placeholder_thumb_media,
    )
    try:
        if existing and existing.wechat_media_id:
            wechat_client.update_draft(existing.wechat_media_id, 0, article_payload)
            media_id = existing.wechat_media_id
            logger.info("[%s] PATCH draft media_id=%s", job.job_id, media_id)
        else:
            media_id = wechat_client.create_draft([article_payload])
            logger.info("[%s] POST draft media_id=%s", job.job_id, media_id)
    except WeChatAPIError as exc:
        db.upsert_draft(job_pk, publish_status="failed", publish_error=str(exc))
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"publish: {exc}")
        logger.error("[%s] publish failed: %s", job.job_id, exc)
        return False

    db.upsert_draft(job_pk, wechat_media_id=media_id, publish_status="published", publish_error=None)
    db.update_job_status(job_pk, JobStatus.PUBLISHED)
    return True


def _build_article_payload(
    title: str, digest: str, content_html: str,
    author: str, thumb_media_id: str,
) -> dict:
    """公众号 draft/add 单篇 article 的最小字段集。"""
    return {
        "title": title[:64] or "未命名",          # 公众号上限 64 字
        "author": author[:8] or "TarMart",      # 公众号上限 8 字
        "digest": digest[:120] or title[:120],   # 公众号上限 120 字
        "content": content_html,
        "content_source_url": "",
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


if __name__ == "__main__":
    raise SystemExit(main())
