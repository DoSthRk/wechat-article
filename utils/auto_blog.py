"""Durable, opt-out multilingual publication after WeChat draft success.

No historical draft scan. One worker drains the outbox; a restart resumes
interrupted tasks, but failures wait for an explicit retry/new draft upload.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from db.database import AutoBlogTask, BLOG_TARGET_LANGS, get_db_manager
from utils.blog_pipeline import BLOG_ACCOUNT, BLOG_PLATFORM, BlogPipelineError, BlogWorkflow
from utils.blog_task_lock import version_lock
from utils import blog_notifications
from utils.blog_urls import public_blog_url
from utils.logger import setup_logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = PROJECT_ROOT / "runtime" / "auto_blog"
logger = setup_logger("auto_blog")


def enabled():
    return os.getenv("AUTO_BLOG_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def held(job_id):
    # Existing explicit publication holds, not a historical backfill policy.
    excluded = os.getenv("AUTO_BLOG_EXCLUDED_JOBS", "免疫客文章-4-3,免疫客文章-4-4")
    return job_id in {item.strip() for item in excluded.split(",") if item.strip()}


def source_hash(db, job_pk):
    version = db.get_article_version(job_pk, "zh")
    if version is None or not Path(version.content_path).is_file():
        raise BlogPipelineError("Chinese source Markdown is missing")
    return hashlib.sha256(Path(version.content_path).read_bytes()).hexdigest()


def _published(db, job_pk, job_id, lang):
    dist = db.get_distribution(job_pk, BLOG_PLATFORM, account=BLOG_ACCOUNT, lang=lang)
    return bool(dist and dist.publish_status == "published" and
                str(dist.external_url or "").rstrip("/") == public_blog_url(job_id, lang))


@contextmanager
def worker_lock():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with (RUN_DIR / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def enqueue(db, job_pk, job_id, owner_line="", *, launch=True):
    if not enabled() or held(job_id):
        return 0
    digest = source_hash(db, job_pk)
    # Serialize duplicate draft callbacks, including the same revision.
    with version_lock(job_id, "auto_enqueue"):
        with db.get_session() as session:
            count = 0
            for lang in BLOG_TARGET_LANGS:
                row = session.query(AutoBlogTask).filter_by(
                    job_pk=job_pk, lang=lang, source_sha256=digest,
                ).first()
                if row is None:
                    session.add(AutoBlogTask(job_pk=job_pk, job_id=job_id, lang=lang,
                                            source_sha256=digest, owner_line=owner_line or ""))
                    count += 1
                elif row.status in {"failed", "cancelled"} or (
                    row.status == "done" and not _published(db, job_pk, job_id, lang)
                ):
                    row.status, row.phase, row.error = "queued", "", None
                    count += 1
            blog_notifications.add_notice(session, job_pk, job_id, digest, owner_line)
            session.commit()
    if launch:
        kick(db)
    return count


def kick(db=None):
    """Launch only for pending outbox rows; never discover old drafts."""
    if not enabled():
        return
    db = db or get_db_manager()
    with db.get_session() as session:
        pending = session.query(AutoBlogTask.id).filter(
            AutoBlogTask.status.in_(("queued", "running")),
        ).first()
    if not pending and not blog_notifications.pending(db):
        return
    with worker_lock() as available:
        if not available:
            return
        with (RUN_DIR / "worker.log").open("a", encoding="utf-8") as log:
            subprocess.Popen(
                [sys.executable, "auto_blog_worker.py"], cwd=str(PROJECT_ROOT),
                stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "DATABASE_URL": db.engine.url.render_as_string(hide_password=False),
                     "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            )


def _update(db, task_id, **fields):
    with db.get_session() as session:
        row = session.get(AutoBlogTask, task_id)
        for name, value in fields.items():
            setattr(row, name, value)
        session.commit()


def _process(db, row, workflow):
    with version_lock(row.job_id, row.lang):
        if held(row.job_id):
            _update(db, row.id, status="cancelled", error="自动发布已暂缓")
            return
        article = db.get_article(row.job_pk)
        if article is None or article.publish_blocked:
            raise BlogPipelineError("Article is blocked by quality gate")
        if source_hash(db, row.job_pk) != row.source_sha256:
            _update(db, row.id, status="cancelled", error="源文章已变更，等待新草稿上传")
            return
        if not _published(db, row.job_pk, row.job_id, row.lang):
            version = db.get_article_version(row.job_pk, row.lang)
            if not (version and version.translation_status == "translated" and Path(version.content_path).is_file()):
                _update(db, row.id, phase="translate")
                workflow.translate(row.job_id, row.lang, job_pk=row.job_pk)
            if source_hash(db, row.job_pk) != row.source_sha256:
                raise BlogPipelineError("Source changed during translation; publication stopped")
            _update(db, row.id, phase="publish")
            workflow.publish(row.job_id, row.lang, job_pk=row.job_pk)
        _update(db, row.id, status="done", phase="", error=None)


def drain(db=None, workflow=None):
    db = db or get_db_manager()
    with worker_lock() as acquired:
        if not acquired or not enabled():
            return
        workflow = workflow or BlogWorkflow(db)
        # The exclusive worker lock proves no surviving worker owns these rows.
        with db.get_session() as session:
            session.query(AutoBlogTask).filter_by(status="running").update({"status": "queued"})
            session.commit()
        while enabled():
            with db.get_session() as session:
                row = session.query(AutoBlogTask).filter_by(status="queued").order_by(AutoBlogTask.id).first()
                if row is not None:
                    row.status = "running"
                    session.commit()
                    session.refresh(row)
                    session.expunge(row)
            if row is None:
                delay = blog_notifications.deliver_ready(db)
                if delay is None:
                    break
                time.sleep(min(30, max(0, delay)))
                continue
            try:
                _process(db, row, workflow)
            except Exception as exc:
                _update(db, row.id, status="failed", error=str(exc) or exc.__class__.__name__)
                logger.exception("Auto Blog failed job=%s lang=%s", row.job_id, row.lang)
            blog_notifications.deliver_ready(db)
    # Close the enqueue-vs-idle-exit race: a competing launch may have observed
    # our lock just before we exited. Any pending rows get a fresh worker now.
    kick(db)


def status(db=None, allowed=None):
    db = db or get_db_manager()
    with db.get_session() as session:
        query = session.query(AutoBlogTask)
        if allowed is not None:
            query = query.filter(AutoBlogTask.owner_line.in_(allowed))
        rows = query.order_by(AutoBlogTask.id).all()
        items = [{"job_id": r.job_id, "lang": r.lang, "status": r.status,
                  "phase": r.phase, "error": r.error} for r in rows]
    notifications = blog_notifications.status(db, allowed)
    active = any(r["status"] in {"queued", "running"} for r in items) or any(
        n["status"] in {"sending", "retry"} for n in notifications
    )
    return {"stage": "auto", "status": "running" if active else "idle",
            "total": len(items), "completed": sum(r["status"] == "done" for r in items),
            "failed": sum(r["status"] == "failed" for r in items), "items": items,
            "notifications": notifications,
            "current": next((r for r in items if r["status"] == "running"), None),
            "errors": [f"{r['job_id']} / {r['lang']}: {r['error']}" for r in items if r["error"]]}
