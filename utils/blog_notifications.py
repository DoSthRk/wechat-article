"""Completion outbox + Feishu bot DM. No display-name recipient guessing.

API contract: https://open.feishu.cn/document/server-docs/im-v1/message/create
The persistent UUID bounds safe automatic retries to the API's dedupe window.
"""
from datetime import datetime, timedelta
import json
import os
from urllib import error as urllib_error, request as urllib_request
import uuid

from db.database import BLOG_LANGS, BlogNotification
from utils.blog_urls import public_blog_url
from utils.blog_task_lock import version_lock

LANG_LABELS = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文", "ru": "俄文"}


class NotificationError(Exception):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def recipient(key="primary"):
    prefix = "FEISHU_BLOG_COPY_RECIPIENT" if key == "self" else "FEISHU_BLOG_RECIPIENT"
    open_id = os.getenv(prefix + "_OPEN_ID", "").strip()
    email = os.getenv(prefix + "_EMAIL", "" if key == "self" else "zxy@genemedi.net").strip()
    if key == "self" and not open_id and not email:
        # Reuse the existing gm-notify personal default in the same Feishu app.
        open_id = os.getenv("FEISHU_RECIPIENT_EDM_OPS_OPEN_ID", "").strip()
    if open_id:
        return open_id, "open_id"
    if email:
        return email, "email"
    raise NotificationError("本人的飞书收件人尚未配置" if key == "self" else "张晓妍的飞书收件人尚未配置")


def recipient_keys():
    keys = ["primary"]
    try:
        copy = recipient("self")
    except NotificationError:
        return keys
    try:
        primary = recipient()
    except NotificationError:
        primary = None
    if copy != primary:
        keys.append("self")
    return keys


def _post(path, payload, token=""):
    request = urllib_request.Request(
        "https://open.feishu.cn/open-apis/" + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        try:
            code = json.loads(exc.read().decode("utf-8")).get("code")
        except (OSError, ValueError, AttributeError):
            code = None
        detail = f"（code={code}）" if isinstance(code, int) else ""
        raise NotificationError(f"飞书 HTTP {exc.code}{detail}", retryable=exc.code == 429 or exc.code >= 500) from None
    except (OSError, ValueError) as exc:
        # Do not leak credentials, URLs, remote response bodies, or article text.
        raise NotificationError(f"飞书请求未确认：{type(exc).__name__}", retryable=True) from None
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "invalid_response"
        raise NotificationError(f"飞书接口未接受通知（code={code}）")
    return body


def send_notice(row, text):
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not secret:
        raise NotificationError("飞书应用凭据尚未配置")
    auth = _post("auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": secret})
    token = auth.get("tenant_access_token")
    if not token:
        raise NotificationError("飞书未返回可用访问令牌")
    result = _post(f"im/v1/messages?receive_id_type={row.recipient_type}", {
        "receive_id": row.recipient_id, "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": row.delivery_uuid,
    }, token)
    message_id = str((result.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise NotificationError("飞书未返回消息回执", retryable=True)
    return message_id


def add_notice(session, job_pk, job_id, source_sha256, owner_line, *, image_package_id=""):
    for key in recipient_keys():
        row = session.query(BlogNotification).filter_by(job_pk=job_pk, source_sha256=source_sha256,
                                                      recipient_key=key).first()
        if row is None:
            session.add(BlogNotification(job_pk=job_pk, job_id=job_id, source_sha256=source_sha256,
                image_package_id=image_package_id, recipient_key=key,
                owner_line=owner_line or "", delivery_uuid=str(uuid.uuid4())))
        elif row.status in {"waiting", "needs_review"} and not row.attempts:
            # 尚未发送的同正文通知跟随本次明确批准的包；已发/不确定结果不重置。
            row.image_package_id = image_package_id
            row.status, row.error = "waiting", None
    # Sent/uncertain notices are never reset by a duplicate draft callback.


def links_for(db, row):
    if not row.image_package_id:
        return []
    links = []
    for lang in BLOG_LANGS:
        dist = db.get_distribution(row.job_pk, "blog", account="genemedi", lang=lang)
        if (not dist or dist.publish_status != "published"
                or dist.image_package_id != row.image_package_id
                or dist.image_verification_status != "verified"):
            return []
        actual = str(dist.external_url or "").rstrip("/")
        if actual != public_blog_url(row.job_id, lang):
            return []
        links.append((lang, actual))
    return links


def render_notice(db, row, links):
    article = db.get_article(row.job_pk)
    title = str(article.title or row.job_id) if article else row.job_id
    business_line = str(row.owner_line or "").strip().upper() or "未标记"
    return "\n".join([
        "Blog 已发布", f"业务线：{business_line}", f"文章：{title}", f"任务：{row.job_id}", "",
        *[f"{LANG_LABELS[lang]}：{url}" for lang, url in links],
    ])


def _rows(db):
    with db.get_session() as session:
        rows = session.query(BlogNotification).filter(
            BlogNotification.status.in_(("waiting", "sending", "retry")),
        ).order_by(BlogNotification.id).all()
        for row in rows:
            session.expunge(row)
        return rows


def pending(db):
    now = datetime.utcnow()
    return any(row.status == "sending" or (
        row.status == "retry" and (not row.retry_at or row.retry_at <= now)
    ) or (row.status == "waiting" and links_for(db, row)) for row in _rows(db))


def _save(db, row, **fields):
    with db.get_session() as session:
        stored = session.get(BlogNotification, row.id)
        for key, value in fields.items():
            setattr(stored, key, value)
            setattr(row, key, value)
        session.commit()


def deliver_ready(db, *, sender=None):
    """Called under the automatic worker lock. Return next retry delay, if any."""
    from utils.auto_blog import approved_package, held, source_hash
    sender = sender or send_notice
    delays = []
    for snapshot in _rows(db):
        # Queue reapproval may replace the package while this worker waits.
        # Never write a stale A-package decision onto a current B-package row.
        with version_lock(snapshot.job_id, "image_job"):
            with db.get_session() as session:
                row = session.get(BlogNotification, snapshot.id)
                if row is None or row.status not in {"waiting", "sending", "retry"}:
                    continue
                session.expunge(row)
            delay = _deliver_one(db, row, sender, approved_package, held, source_hash)
            if delay is not None:
                delays.append(delay)
    return min(delays) if delays else None


def _deliver_one(db, row, sender, approved_package, held, source_hash):
    """One fresh outbox row; caller holds its image_job lock through the send."""
    from utils.image_packages import ImagePackageError
    now = datetime.utcnow()
    try:
        if not row.image_package_id:
            _save(db, row, status="needs_review", error="历史通知未绑定经确认的图包，不自动发送")
            return
        if held(row.job_id) or source_hash(db, row.job_pk) != row.source_sha256:
            _save(db, row, status="cancelled", error="文章已变更或暂缓发布")
            return
        article = db.get_article(row.job_pk)
        if article is None or article.publish_blocked:
            return
        links = links_for(db, row)
        if not links:
            return
        if row.retry_at and row.retry_at > now:
            return (row.retry_at - now).total_seconds()
        # Feishu deduplicates UUIDs for one hour; late ambiguous sends need review.
        if row.first_attempt_at and now - row.first_attempt_at >= timedelta(minutes=55):
            _save(db, row, status="uncertain", error="通知结果待确认；已超过安全自动重试窗口")
            return
        try:
            approved_package(db, row.job_pk, row.job_id, row.image_package_id)
        except ImagePackageError:
            _save(db, row, status="needs_review", error="通知对应的图片确认已失效")
            return
        links = links_for(db, row)
        if not links or source_hash(db, row.job_pk) != row.source_sha256:
            return
        recipient_id, recipient_type = (row.recipient_id, row.recipient_type) if row.recipient_id else recipient(row.recipient_key)
        _save(db, row, status="sending", recipient_id=recipient_id, recipient_type=recipient_type,
              attempts=row.attempts + 1, first_attempt_at=row.first_attempt_at or now, retry_at=None)
        message_id = sender(row, render_notice(db, row, links))
        if not message_id:
            raise NotificationError("飞书未返回消息回执", retryable=True)
        _save(db, row, status="sent", message_id=message_id, error=None)
    except Exception as exc:
        retryable = isinstance(exc, NotificationError) and exc.retryable
        if retryable and row.attempts < 5:
            delay = 30 * (2 ** max(0, row.attempts - 1))
            _save(db, row, status="retry", retry_at=now + timedelta(seconds=delay), error=str(exc))
            return delay
        error = str(exc) if isinstance(exc, NotificationError) else f"通知处理失败：{type(exc).__name__}"
        uncertain = retryable or (not isinstance(exc, NotificationError) and row.status == "sending")
        _save(db, row, status="uncertain" if uncertain else "failed", error=error)


def retry(db, job_id):
    with version_lock(job_id, "image_job"), version_lock(job_id, "notify_retry"):
        return _retry(db, job_id)


def _retry(db, job_id):
    from utils.auto_blog import held, source_hash
    job_pk = db.find_job_pk(job_id)
    if job_pk is None or held(job_id):
        return {"ok": False, "error": "文章不存在或已暂缓发布"}
    try:
        digest = source_hash(db, job_pk)
    except Exception:
        return {"ok": False, "error": "源文章不可用"}
    with db.get_session() as session:
        rows = session.query(BlogNotification).filter_by(job_pk=job_pk, source_sha256=digest, status="failed").all()
        if not rows:
            return {"ok": False, "error": "仅可重试明确失败的通知；结果待确认时请先核对飞书消息"}
        for row in rows:
            row.status, row.error, row.attempts = "waiting", None, 0
            row.first_attempt_at, row.retry_at = None, None
            row.delivery_uuid = str(uuid.uuid4())
        session.commit()
    return {"ok": True}


def status(db, allowed=None):
    with db.get_session() as session:
        query = session.query(BlogNotification)
        if allowed is not None:
            query = query.filter(BlogNotification.owner_line.in_(allowed))
        return [{"job_id": row.job_id, "recipient_key": row.recipient_key,
                 "status": row.status, "message_id": row.message_id,
                 "error": row.error} for row in query.order_by(BlogNotification.id).all()]
