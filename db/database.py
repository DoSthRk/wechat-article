"""wechat-article DB（状态跟踪 + 落盘路径，绝不存大文本）。

- tasks：一次 batch_processor 运行 = 一个 task
- jobs：jobs.yaml 里每个条目，对应一个 task 下的一个 job
- articles：每个 job 一篇基准文章（平台无关；生成产物文件路径 + 元数据）
- article_drafts：早期 1:1 公众号草稿（被 distributions 取代，过渡期保留）
- distributions：Phase 2 —— 一篇基准文章扇出到 N 个平台的落地实例
  （platform × account × lang 唯一；承载组装产物路径 + 平台发布状态）
"""
from __future__ import annotations

import os
from datetime import datetime
from enum import Enum as PyEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from utils.logger import setup_logger

load_dotenv()
logger = setup_logger("database")

DEFAULT_SQLITE_PATH = str(Path(__file__).resolve().parent.parent / "runtime" / "wechat_article.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")
ARTICLE_CONTENT_DIR = os.getenv(
    "ARTICLE_CONTENT_DIR",
    str(Path(__file__).resolve().parent.parent / "outputs" / "jobs"),
)

Base = declarative_base()


class JobStatus(PyEnum):
    PENDING = "pending"
    GENERATING = "generating"
    GENERATED = "generated"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_name = Column(String(255), nullable=False, unique=True, comment="task 名（一次跑的标识）")
    description = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    jobs = relationship("Job", back_populates="task", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(String(128), nullable=False, comment="jobs.yaml 里的 job_id，task 内唯一")
    pdf_path = Column(String(512), nullable=False)
    template_id = Column(String(128), nullable=False)
    product_id = Column(String(128), nullable=False)
    image_pool = Column(String(128), comment="Phase 1 用")
    title_hint = Column(String(255))
    status = Column(
        Enum(JobStatus, name="job_status"),
        default=JobStatus.PENDING,
        nullable=False,
    )
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("Task", back_populates="jobs")
    article = relationship("Article", back_populates="job", uselist=False, cascade="all, delete-orphan")
    draft = relationship("ArticleDraft", back_populates="job", uselist=False, cascade="all, delete-orphan")
    distributions = relationship("Distribution", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("task_id", "job_id", name="uq_task_job"),
        Index("ix_jobs_status", "status"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "pdf_path": self.pdf_path,
            "template_id": self.template_id,
            "product_id": self.product_id,
            "image_pool": self.image_pool,
            "title_hint": self.title_hint,
            "status": self.status.value if self.status else None,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Article(Base):
    """生成产物。内容文件落盘到 outputs/jobs/{job_id}/，DB 只存路径 + 元数据。"""
    __tablename__ = "articles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    title = Column(String(255), comment="文章标题（首 H1）")
    digest = Column(String(255), comment="摘要（首段截断）")
    content_dir = Column(String(512), comment="article.md / article.html 所在目录")
    word_count = Column(Integer, default=0)
    model = Column(String(64))
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    # Phase 6 质量自审
    markdown_health_score = Column(Integer, default=0, comment="markdown 健康度 0-100")
    tonal_score = Column(Integer, default=0, comment="调性分 0-100")
    tonal_feedback = Column(Text, comment="调性自审详情 JSON（命中词 / 正文夹带产品 / 建议）")
    publish_blocked = Column(Boolean, default=False, comment="质量闸：True 则不投放，留人工")
    block_reason = Column(Text, comment="被拦原因")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job = relationship("Job", back_populates="article")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_pk": self.job_pk,
            "title": self.title,
            "digest": self.digest,
            "content_dir": self.content_dir,
            "word_count": int(self.word_count or 0),
            "model": self.model,
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
            "total_tokens": int(self.total_tokens or 0),
            "latency_ms": int(self.latency_ms or 0),
            "markdown_health_score": int(self.markdown_health_score or 0),
            "tonal_score": int(self.tonal_score or 0),
            "tonal_feedback": self.tonal_feedback,
            "publish_blocked": bool(self.publish_blocked),
            "block_reason": self.block_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ArticleDraft(Base):
    """公众号草稿。media_id 是重发时 PATCH 用的关键（沿用 target-running 思路）。"""
    __tablename__ = "article_drafts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    wechat_media_id = Column(String(128), comment="公众号草稿 media_id，重发走 draft/update")
    wechat_url = Column(String(512), comment="草稿在公众号后台的预览 URL（如果 API 返回）")
    publish_status = Column(String(16), default="pending", nullable=False, comment="pending/publishing/published/failed")
    publish_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job = relationship("Job", back_populates="draft")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_pk": self.job_pk,
            "wechat_media_id": self.wechat_media_id,
            "wechat_url": self.wechat_url,
            "publish_status": self.publish_status,
            "publish_error": self.publish_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Distribution(Base):
    """一篇基准文章扇出到某平台的落地实例（1 article : N distribution）。

    ``platform × account × lang`` 唯一。承载组装后产物路径 + 平台发布状态 + 平台标识。
    取代早期 1:1 的 ``article_drafts``（公众号是其中 platform=wechat 的特例）。
    """
    __tablename__ = "distributions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(32), nullable=False, comment="wechat / blog / linkedin")
    account = Column(String(64), default="", nullable=False, comment="平台内账户：aav / immune / ...")
    lang = Column(String(16), default="zh", nullable=False, comment="语言：zh / en / ...")
    assembled_dir = Column(String(512), comment="组装成品（正文 + 产品模块）落盘目录")
    publish_status = Column(
        String(16), default="pending", nullable=False,
        comment="pending/publishing/published/blocked/failed",
    )
    wechat_media_id = Column(String(128), comment="公众号草稿 media_id（重发 PATCH 用）")
    wechat_url = Column(String(512), comment="公众号草稿预览 URL")
    external_url = Column(String(512), comment="blog / 外链投放 URL")
    publish_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job = relationship("Job", back_populates="distributions")

    __table_args__ = (
        UniqueConstraint("job_pk", "platform", "account", "lang", name="uq_distribution_target"),
        Index("ix_distributions_status", "publish_status"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_pk": self.job_pk,
            "platform": self.platform,
            "account": self.account,
            "lang": self.lang,
            "assembled_dir": self.assembled_dir,
            "publish_status": self.publish_status,
            "wechat_media_id": self.wechat_media_id,
            "wechat_url": self.wechat_url,
            "external_url": self.external_url,
            "publish_error": self.publish_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ----------------- Manager -----------------

class DatabaseManager:
    def __init__(self, database_url: Optional[str] = None) -> None:
        url = database_url or DATABASE_URL
        self.engine = create_engine(url, echo=False, future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Path(DEFAULT_SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(bind=self.engine)
        self._ensure_sqlite_columns()
        logger.info("DB initialized: %s", self.engine.url)

    def get_session(self) -> Session:
        return self.SessionLocal()

    def _ensure_sqlite_columns(self) -> None:
        """SQLite 平滑迁移：给既有表补上后加的列（create_all 不会 ALTER 既有表）。

        新库由 create_all 直接建全列，此处对既有库做 ADD COLUMN。仅 SQLite。
        """
        if not self.engine.url.get_backend_name().startswith("sqlite"):
            return
        additions = {
            "articles": {
                "markdown_health_score": "INTEGER DEFAULT 0",
                "tonal_score": "INTEGER DEFAULT 0",
                "tonal_feedback": "TEXT",
                "publish_blocked": "BOOLEAN DEFAULT 0",
                "block_reason": "TEXT",
            },
        }
        with self.engine.begin() as conn:
            for table, cols in additions.items():
                existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in existing:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        logger.info("migrated: added %s.%s", table, col)

    # ---- task / job ----

    def get_or_create_task(self, task_name: str, description: Optional[str] = None) -> Task:
        session = self.get_session()
        try:
            t = session.query(Task).filter(Task.task_name == task_name).first()
            if t:
                return t
            t = Task(task_name=task_name, description=description)
            session.add(t)
            session.commit()
            session.refresh(t)
            return t
        finally:
            session.close()

    def upsert_job(self, task_id: int, job_id: str, **fields: Any) -> Job:
        """按 (task_id, job_id) upsert。重复 job_id 不抛错，更新字段。"""
        session = self.get_session()
        try:
            j = (
                session.query(Job)
                .filter(Job.task_id == task_id, Job.job_id == job_id)
                .first()
            )
            if not j:
                j = Job(task_id=task_id, job_id=job_id, **fields)
                session.add(j)
            else:
                for k, v in fields.items():
                    if hasattr(j, k):
                        setattr(j, k, v)
            session.commit()
            session.refresh(j)
            return j
        finally:
            session.close()

    def update_job_status(self, job_pk: int, status: JobStatus, error_message: Optional[str] = None) -> None:
        session = self.get_session()
        try:
            j = session.query(Job).filter(Job.id == job_pk).first()
            if j:
                j.status = status
                if error_message is not None:
                    j.error_message = error_message
                session.commit()
        finally:
            session.close()

    def list_pending_jobs(self, task_id: Optional[int] = None) -> List[Job]:
        session = self.get_session()
        try:
            q = session.query(Job).filter(Job.status == JobStatus.PENDING)
            if task_id:
                q = q.filter(Job.task_id == task_id)
            return q.order_by(Job.id).all()
        finally:
            session.close()

    # ---- article / draft ----

    def upsert_article(self, job_pk: int, **fields: Any) -> Article:
        session = self.get_session()
        try:
            a = session.query(Article).filter(Article.job_pk == job_pk).first()
            if not a:
                a = Article(job_pk=job_pk, **fields)
                session.add(a)
            else:
                for k, v in fields.items():
                    if hasattr(a, k):
                        setattr(a, k, v)
            session.commit()
            session.refresh(a)
            return a
        finally:
            session.close()

    def get_article(self, job_pk: int) -> Optional[Article]:
        session = self.get_session()
        try:
            return session.query(Article).filter(Article.job_pk == job_pk).first()
        finally:
            session.close()

    def upsert_draft(self, job_pk: int, **fields: Any) -> ArticleDraft:
        session = self.get_session()
        try:
            d = session.query(ArticleDraft).filter(ArticleDraft.job_pk == job_pk).first()
            if not d:
                d = ArticleDraft(job_pk=job_pk, **fields)
                session.add(d)
            else:
                for k, v in fields.items():
                    if hasattr(d, k):
                        setattr(d, k, v)
            session.commit()
            session.refresh(d)
            return d
        finally:
            session.close()

    def get_draft(self, job_pk: int) -> Optional[ArticleDraft]:
        session = self.get_session()
        try:
            return session.query(ArticleDraft).filter(ArticleDraft.job_pk == job_pk).first()
        finally:
            session.close()

    # ---- distribution（1 article : N 平台投放）----

    def upsert_distribution(
        self, job_pk: int, platform: str,
        account: str = "", lang: str = "zh", **fields: Any,
    ) -> Distribution:
        """按 (job_pk, platform, account, lang) upsert 一个投放实例。"""
        session = self.get_session()
        try:
            d = (
                session.query(Distribution)
                .filter(
                    Distribution.job_pk == job_pk,
                    Distribution.platform == platform,
                    Distribution.account == account,
                    Distribution.lang == lang,
                )
                .first()
            )
            if not d:
                d = Distribution(job_pk=job_pk, platform=platform, account=account, lang=lang, **fields)
                session.add(d)
            else:
                for k, v in fields.items():
                    if hasattr(d, k):
                        setattr(d, k, v)
            session.commit()
            session.refresh(d)
            return d
        finally:
            session.close()

    def get_distribution(
        self, job_pk: int, platform: str, account: str = "", lang: str = "zh",
    ) -> Optional[Distribution]:
        session = self.get_session()
        try:
            return (
                session.query(Distribution)
                .filter(
                    Distribution.job_pk == job_pk,
                    Distribution.platform == platform,
                    Distribution.account == account,
                    Distribution.lang == lang,
                )
                .first()
            )
        finally:
            session.close()

    def list_distributions(self, job_pk: int) -> List[Distribution]:
        session = self.get_session()
        try:
            return (
                session.query(Distribution)
                .filter(Distribution.job_pk == job_pk)
                .order_by(Distribution.id)
                .all()
            )
        finally:
            session.close()


# 单例
_instance: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    global _instance
    if _instance is None:
        _instance = DatabaseManager()
    return _instance
