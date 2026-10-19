"""Subprocess entrypoint for one translation or CMS publication batch."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from db.database import get_db_manager
from utils.blog_pipeline import BlogPipelineError, BlogWorkflow
from utils.wechat_draft_repair import DraftImageRepairError
from utils.image_packages import ImagePackageError


def run_image_stage(db, stage: str, item: dict) -> dict:
    """后台准备或继续固定版本；绝不重新生成正文或自行批准。"""
    from utils.blog_pipeline import _build_source_job
    from utils.blog_task_lock import version_lock
    from utils.image_packages import build_package, require_approved_package
    pk = db.find_job_pk(item["job_id"])
    article = db.get_article(pk) if pk is not None else None
    if article is None:
        raise ImagePackageError("请先生成基准正文，再准备配图审核")
    job = _build_source_job(db.get_job(pk))
    with version_lock(job.job_id, "image_job"):
        if stage == "images":
            package = build_package(job, job_pk=pk)
            if package.get("status") == "blocked":
                raise ImagePackageError(package.get("reason") or "候选图片不完整，不能确认发布")
            return {"job_id": job.job_id, "bundle_id": package.get("bundle_id"), "status": package.get("status")}
        article = db.get_article(pk)
        if article is None or article.publish_blocked:
            raise BlogPipelineError("文章仍被内容质量检查阻断；配图确认不能代替正文质量验收")
        package = require_approved_package(job, expected_package_id=item["expected_package_id"], expected_job_pk=pk)
        if not job.line:
            raise ImagePackageError("无法确定文章业务线，禁止使用默认公众号账号；请先核对任务配置")
        from batch_processor import _publish_chinese_blog, _distribute_one, _make_client_getter, _resolve_wechat_account
        from utils.blog_urls import public_blog_url
        current = db.get_distribution(pk, "blog", account="genemedi", lang="zh")
        if (current and current.publish_status == "published" and current.external_id
                and str(current.external_url or "").rstrip("/") == public_blog_url(job.job_id, "zh")):
            from utils.blog_image_repair import repair_images
            if current.image_package_id != package.bundle_id or current.image_verification_status != "verified":
                repair_images(BlogWorkflow(db), job.job_id, "zh", repair_cover=True,
                              expected_package_id=package.bundle_id)
        elif not _publish_chinese_blog(db, pk, job, expected_package_id=package.bundle_id):
            raise BlogPipelineError("中文 Blog 发布未完成，请查看任务错误后重试")
        if db.latest_wechat_draft(job.job_id):
            from utils.wechat_draft_repair import repair_existing_images
            from utils.auto_blog import enqueue
            result = repair_existing_images(db, job.job_id, repair_cover=True,
                                            expected_package_id=package.bundle_id)
            if not result.get("verified"):
                raise BlogPipelineError("已有公众号草稿图片同步未通过回读，请先核对草稿")
            enqueue(db, pk, job.job_id, job.line or "", image_package_id=package.bundle_id)
            return {"job_id": job.job_id, "bundle_id": package.bundle_id, "status": "draft_images_verified"}
        options = argparse.Namespace(placeholder_author=None, placeholder_thumb_media=None)
        if not _distribute_one(db, pk, job, _make_client_getter(), options,
                               expected_package_id=package.bundle_id):
            raise BlogPipelineError("公众号草稿未完成或未通过回读，未触发后续自动发布")
        receipt = db.get_distribution(pk, "wechat", account=_resolve_wechat_account(job), lang="zh")
        if not (receipt and receipt.wechat_media_id and receipt.publish_status == "published"
                and receipt.image_package_id == package.bundle_id and receipt.image_verification_status == "verified"):
            raise BlogPipelineError("公众号草稿缺少当前图片包的已验证回执，不能记为完成")
        # 草稿写入和后台启动是两个结果。普通投放对队列失败采用尽力而为；
        # 人工“继续发布”须再次幂等入队，不能把队列未落盘也报告为完整完成。
        from utils.auto_blog import enqueue
        enqueue(db, pk, job.job_id, job.line, image_package_id=package.bundle_id)
        return {"job_id": job.job_id, "bundle_id": package.bundle_id, "status": "draft_verified"}


def write_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("translate", "publish", "images", "review_publish"), required=True)
    parser.add_argument("--selections", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--lock", required=True)
    args = parser.parse_args()
    state_path, lock_path = Path(args.state), Path(args.lock)
    items = json.loads(Path(args.selections).read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = os.getpid()
    write_state(state_path, state)
    workflow = BlogWorkflow(get_db_manager())
    try:
        for item in items:
            state["current"] = item
            write_state(state_path, state)
            try:
                if args.stage in {"images", "review_publish"}:
                    result = run_image_stage(workflow.db, args.stage, item)
                    state.setdefault("results", []).append(result)
                elif args.stage == "translate":
                    workflow.translate(item["job_id"], item["lang"])
                elif item.get('repair_wechat_images') is True:
                    from utils.wechat_draft_repair import repair_existing_images
                    result = repair_existing_images(workflow.db, item['job_id'], repair_cover=bool(item.get('repair_cover')))
                    state.setdefault('results', []).append(result)
                elif item.get('repair_images') is True:
                    from utils.blog_image_repair import repair_images
                    options = {'repair_cover': True} if item.get('repair_cover') is True else {}
                    result = repair_images(workflow, item['job_id'], item['lang'], **options)
                    state.setdefault('results', []).append(result)
                else:
                    workflow.publish(item["job_id"], item["lang"], force=bool(item.get("force")))
                state["completed"] += 1
                print(f"[workflow] {args.stage} {item['job_id']} {item['lang']} ok", flush=True)
            except (BlogPipelineError, DraftImageRepairError, ImagePackageError) as exc:
                state["failed"] += 1
                state["errors"].append(f"{item['job_id']} / {item['lang']}: {exc}")
                print(f"[workflow] {args.stage} {item['job_id']} {item['lang']} failed: {exc}", flush=True)
            write_state(state_path, state)
        state["status"] = "done" if not state["failed"] else "failed"
        return_code = 0 if not state["failed"] else 1
    except Exception as exc:
        state["status"] = "failed"
        state["errors"].append(str(exc) or exc.__class__.__name__)
        return_code = 1
    finally:
        state["current"] = None
        state["finished_at"] = time.time()
        write_state(state_path, state)
        lock_path.unlink(missing_ok=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
