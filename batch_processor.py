"""wechat-article 主入口 —— generate / distribute 两阶段。

阶段（``--stage``）：
    generate  ：jobs.yaml → 逐 job 生成基准正文（方案 B）→ 落盘 + 写 articles 表
    distribute：逐 job 取基准正文 → 投放到平台 distribution（当前只接公众号 wechat；
                blog / linkedin 是 Phase 4）。account 从 line 配置的 wechat_account 取。
    all       ：先 generate 再 distribute（默认）

内容与投放解耦：一篇基准文章（article）可扇出到多个 distribution（platform × account × lang）。
当前 distribute 只实现公众号单平台；产品模块组装（Phase 3）、多平台（Phase 4）后续接入。

用法：
    python batch_processor.py                       # generate + distribute
    python batch_processor.py --stage generate      # 只生成
    python batch_processor.py --stage distribute    # 只投放（需先 generate）
    python batch_processor.py --dry-run             # 生成但不投放
    python batch_processor.py --only <job_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# 让 utils / core / db 都能 from-import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.main import ArticleAnalyzer
from db.database import ARTICLE_CONTENT_DIR, JobStatus, get_db_manager
from utils.job_loader import Job, load_jobs
from utils.line_loader import LineLoadError, load_line_by_id
from utils.logger import setup_logger
from utils.wechat_client import WeChatAPIError, WeChatClient
from utils.wechat_html import extract_title_and_digest, markdown_to_wechat_html

load_dotenv()
logger = setup_logger("batch_processor")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JOBS_YAML = str(PROJECT_ROOT / "inputs" / "jobs.yaml")
LINES_DIR = str(PROJECT_ROOT / "inputs" / "lines")

WECHAT_PLATFORM = "wechat"
DEFAULT_LANG = "zh"


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
        )

    analyzer: Optional[ArticleAnalyzer] = None
    if do_generate:
        try:
            analyzer = ArticleAnalyzer()
        except Exception as exc:
            logger.error("ArticleAnalyzer init failed: %s", exc)
            return 3

    wechat_client: Optional[WeChatClient] = None
    if do_distribute:
        if not args.placeholder_thumb_media:
            logger.error(
                "no DEFAULT_THUMB_MEDIA_ID / --placeholder-thumb-media; "
                "公众号草稿需要封面素材。用 --dry-run 或 --stage generate 跳过投放。"
            )
            return 3
        try:
            wechat_client = WeChatClient()
        except WeChatAPIError as exc:
            logger.error("WeChatClient init failed: %s", exc)
            return 3

    success = 0
    failed = 0
    for j in selected:
        ok = _run_one_job(db, task.id, j, analyzer, wechat_client, args, do_generate, do_distribute)
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
    wechat_client: Optional[WeChatClient],
    args: argparse.Namespace,
    do_generate: bool,
    do_distribute: bool,
) -> bool:
    job_pk = db.upsert_job(task_id, job.job_id).id

    if do_generate:
        if analyzer is None or not _generate_one(db, job_pk, job, analyzer):
            return False

    if do_distribute:
        if wechat_client is None:
            return True
        return _distribute_one(db, job_pk, job, wechat_client, args)

    return True


def _generate_one(db, job_pk: int, job: Job, analyzer: ArticleAnalyzer) -> bool:
    """生成阶段：方案 B 出基准正文 → 落盘 → 写 articles 表。"""
    db.update_job_status(job_pk, JobStatus.GENERATING)
    result = analyzer.analyze(job)
    if not result.success:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=result.error_message)
        logger.error("[%s] generate failed: %s", job.job_id, result.error_message)
        return False

    out_dir = Path(ARTICLE_CONTENT_DIR) / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.md").write_text(result.markdown, encoding="utf-8")
    title, digest = extract_title_and_digest(result.markdown)
    html = markdown_to_wechat_html(result.markdown)
    (out_dir / "article.html").write_text(html, encoding="utf-8")
    (out_dir / "meta.json").write_text(
        json.dumps({
            "job_id": job.job_id, "line": job.line, "title": title, "digest": digest,
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
    return True


def _distribute_one(
    db, job_pk: int, job: Job,
    wechat_client: WeChatClient,
    args: argparse.Namespace,
) -> bool:
    """投放阶段：取基准正文 → 公众号草稿（同 distribution 已有 media_id 则走 PATCH）。"""
    article = db.get_article(job_pk)
    if not article or not article.content_dir:
        db.update_job_status(job_pk, JobStatus.FAILED, error_message="distribute: no article, run --stage generate first")
        logger.error("[%s] distribute: 还没生成基准正文", job.job_id)
        return False
    html_path = Path(article.content_dir) / "article.html"
    if not html_path.exists():
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"distribute: missing {html_path}")
        logger.error("[%s] distribute: 缺 article.html (%s)", job.job_id, html_path)
        return False
    html = html_path.read_text(encoding="utf-8")
    account = _resolve_wechat_account(job)

    db.update_job_status(job_pk, JobStatus.PUBLISHING)
    existing = db.get_distribution(job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG)
    payload = _build_article_payload(
        title=article.title or job.title_hint or job.job_id,
        digest=article.digest or "", content_html=html,
        author=args.placeholder_author,
        thumb_media_id=args.placeholder_thumb_media,
    )
    try:
        if existing and existing.wechat_media_id:
            wechat_client.update_draft(existing.wechat_media_id, 0, payload)
            media_id = existing.wechat_media_id
            logger.info("[%s] PATCH wechat/%s media_id=%s", job.job_id, account, media_id)
        else:
            media_id = wechat_client.create_draft([payload])
            logger.info("[%s] POST wechat/%s media_id=%s", job.job_id, account, media_id)
    except WeChatAPIError as exc:
        db.upsert_distribution(
            job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
            publish_status="failed", publish_error=str(exc), assembled_dir=article.content_dir,
        )
        db.update_job_status(job_pk, JobStatus.FAILED, error_message=f"publish: {exc}")
        logger.error("[%s] publish failed: %s", job.job_id, exc)
        return False

    db.upsert_distribution(
        job_pk, WECHAT_PLATFORM, account=account, lang=DEFAULT_LANG,
        wechat_media_id=media_id, publish_status="published", publish_error=None,
        assembled_dir=article.content_dir,
    )
    db.update_job_status(job_pk, JobStatus.PUBLISHED)
    return True


def _resolve_wechat_account(job: Job) -> str:
    """从 line 配置取该线对应的公众号账户（extra.wechat_account）；取不到回 default。"""
    if not job.line:
        return "default"
    try:
        line = load_line_by_id(LINES_DIR, job.line)
    except LineLoadError:
        return "default"
    return str((line.extra or {}).get("wechat_account") or "default")


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
