"""一次性迁移：把旧 article_drafts 的 media_id 录入新 distributions 表。

背景：早期单账户版把公众号草稿 media_id 存在 ``article_drafts``(1:1)；现在投放走
``distributions``(platform × account × lang)。已发布过的草稿若不迁移，重投会「新建」
而非 PATCH 原草稿。本脚本把每条 legacy media_id 按 job 的 line → wechat_account
录入 distributions，使后续 ``--stage distribute`` 走 PATCH（草稿位置不变）。

幂等：用 upsert_distribution，可重复跑。
用法：python scripts/migrate_legacy_drafts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

from db.database import Article, ArticleDraft, Job, get_db_manager
from utils.job_loader import load_jobs
from utils.line_loader import LineLoadError, load_line_by_id
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger("migrate_legacy_drafts")
LINES_DIR = str(BASE_DIR / "inputs" / "lines")
JOBS_YAML = str(BASE_DIR / "inputs" / "jobs.yaml")


def _job_id_to_account() -> dict:
    """从 jobs.yaml 建 job_id → wechat_account 映射（line.extra.wechat_account）。"""
    mapping: dict = {}
    try:
        for j in load_jobs(JOBS_YAML, project_root=str(BASE_DIR)):
            account = "default"
            if j.line:
                try:
                    account = str((load_line_by_id(LINES_DIR, j.line).extra or {}).get("wechat_account") or "default")
                except LineLoadError:
                    pass
            mapping[j.job_id] = account
    except Exception as exc:  # noqa: BLE001 - 读不到就全回落 default
        logger.warning("读 jobs.yaml 失败（account 全回落 default）：%s", exc)
    return mapping


def main() -> int:
    db = get_db_manager()
    acct_map = _job_id_to_account()

    session = db.get_session()
    try:
        rows = (
            session.query(ArticleDraft, Job, Article)
            .join(Job, ArticleDraft.job_pk == Job.id)
            .outerjoin(Article, Article.job_pk == Job.id)
            .all()
        )
        data = [
            (
                job.id, job.job_id, (draft.wechat_media_id or "").strip(),
                draft.publish_status, (article.content_dir if article else None),
            )
            for draft, job, article in rows
        ]
    finally:
        session.close()

    migrated = 0
    for job_pk, job_id, media_id, status, content_dir in data:
        if not media_id:
            continue
        account = acct_map.get(job_id, "default")
        db.upsert_distribution(
            job_pk, "wechat", account=account, lang="zh",
            wechat_media_id=media_id,
            publish_status=status or "published",
            assembled_dir=content_dir,
        )
        migrated += 1
        logger.info(
            "migrated %s → distributions(wechat/%s) media_id=%s…",
            job_id, account, media_id[:12],
        )

    print(f"migrated {migrated} legacy draft(s) into distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
