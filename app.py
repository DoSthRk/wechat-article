"""wechat-article dashboard —— 轻量服务端渲染（vanilla，无 React / 无前端构建）。

借鉴 target-running 的 app 结构（create_app 工厂 + 文章列表 API + markdown 预览页），
适配本项目的 article / distribution / 质量闸字段。

端点：
    GET /                  流水线看板（内容 × 投放）
    GET /api/health        健康检查
    GET /api/articles      文章 + 投放 + 质量概览（JSON）
    GET /preview/<job_id>  渲染基准正文 markdown 供人工 review

跑：python app.py  → http://127.0.0.1:5000
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import markdown as md_lib
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.database import get_db_manager
from utils.wechat_template_assets import (
    TemplateAssetError,
    append_source_pdf_guide,
    contains_source_pdf_guide,
)


_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 单次上传上限（图多的论文 PDF 可能上百 MB）


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = testing
    app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES

    @app.errorhandler(413)
    def _too_large(_e):
        # 超过 MAX_CONTENT_LENGTH 时 Flask 默认回 HTML；改回 JSON，前端能给出明确提示
        return jsonify({"ok": False, "error": f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"}), 413

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return redirect(url_for("operator"))

    @app.get("/operator")
    def operator():
        return render_template("operator.html")

    @app.get("/admin")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/articles")
    def api_articles():
        from utils.pricing import article_cost_cny, rate_card
        page = max(1, request.args.get("page", 1, type=int))
        page_size = max(1, min(200, request.args.get("page_size", 100, type=int)))
        items, total = get_db_manager().list_article_overview(page=page, page_size=page_size)
        for it in items:  # 每篇估算 LLM 生成成本（CNY）
            it["cost_cny"] = round(
                article_cost_cny(it.get("model"), it.get("prompt_tokens"), it.get("completion_tokens")), 4,
            )
        stats = {
            "total": total,
            "generated": sum(1 for i in items if i.get("title")),
            "blocked": sum(1 for i in items if i.get("publish_blocked")),
            "published": sum(
                1 for i in items
                if any(d.get("publish_status") == "published" for d in i.get("distributions", []))
            ),
            "cost_cny": round(sum(i.get("cost_cny", 0.0) for i in items), 2),
            "tokens": sum(i.get("total_tokens", 0) for i in items),
        }
        return jsonify({
            "articles": items, "page": page, "page_size": page_size,
            "total": total, "stats": stats, "rates": rate_card(),
        })

    @app.post("/api/blog/translate")
    def api_blog_translate():
        from utils.blog_pipeline import BlogWorkflow, run_batch
        data = request.get_json(silent=True) or {}
        selections = data.get("selections")
        if not isinstance(selections, list) or not selections:
            return jsonify({"ok": False, "error": "请至少选择一篇英文文章"}), 400
        return jsonify(run_batch(BlogWorkflow(get_db_manager()), selections, "translate"))

    @app.post("/api/blog/publish")
    def api_blog_publish():
        from utils.blog_pipeline import BlogWorkflow, run_batch
        data = request.get_json(silent=True) or {}
        selections = data.get("selections")
        if not isinstance(selections, list) or not selections:
            return jsonify({"ok": False, "error": "请至少选择一篇待发布文章"}), 400
        return jsonify(run_batch(BlogWorkflow(get_db_manager()), selections, "publish"))

    @app.post("/api/workflow/<stage>/run")
    def api_workflow_run(stage: str):
        from utils.workflow_runner import start_workflow
        data = request.get_json(silent=True) or {}
        selections = data.get("selections")
        if not isinstance(selections, list) or not selections:
            return jsonify({"ok": False, "error": "请至少选择一个可处理版本"}), 400
        result = start_workflow(stage, selections)
        return jsonify(result), (202 if result.get("ok") else 409)

    @app.get("/api/workflow/status")
    def api_workflow_status():
        from utils.workflow_runner import workflow_status
        return jsonify(workflow_status())

    @app.post("/api/source-pdf/provision")
    def api_source_pdf_provision():
        """One-time SSH identity bootstrap; never returns private-key material."""
        from utils.source_pdf_store import SourcePdfError, provision_source_pdf_ssh
        try:
            identity = provision_source_pdf_ssh()
        except SourcePdfError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True, **identity})

    @app.post("/api/source-pdf/publish")
    def api_source_pdf_publish():
        """Publish one existing job's original PDF without touching its draft."""
        from utils.source_pdf_store import SourcePdfError, create_source_pdf_store
        data = request.get_json(silent=True) or {}
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return jsonify({"ok": False, "error": "缺少 job_id"}), 400
        db = get_db_manager()
        job_pk = db.find_job_pk(job_id)
        db_job = db.get_job(job_pk) if job_pk is not None else None
        if db_job is None:
            return jsonify({"ok": False, "error": f"未知 job_id：{job_id}"}), 404
        try:
            published = create_source_pdf_store().upload(db_job.pdf_path)
        except SourcePdfError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        db.update_job_source_pdf(
            job_pk, url=published.url, sha256=published.sha256, size=published.size,
        )
        return jsonify({
            "ok": True, "job_id": job_id, "url": published.url,
            "sha256": published.sha256, "size": published.size,
        })

    @app.post("/api/source-pdf/apply-to-draft")
    def api_source_pdf_apply_to_draft():
        """Patch an existing draft's 阅读原文 URL and required footer guide image."""
        from utils.wechat_client import WeChatAPIError, WeChatClient
        data = request.get_json(silent=True) or {}
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return jsonify({"ok": False, "error": "缺少 job_id"}), 400
        db = get_db_manager()
        job_pk = db.find_job_pk(job_id)
        db_job = db.get_job(job_pk) if job_pk is not None else None
        if db_job is None:
            return jsonify({"ok": False, "error": f"未知 job_id：{job_id}"}), 404
        if not db_job.source_pdf_url:
            return jsonify({"ok": False, "error": "该任务尚未发布原文 PDF"}), 409
        distributions = [
            item for item in db.list_distributions(job_pk)
            if item.platform == "wechat" and item.wechat_media_id
        ]
        if not distributions:
            return jsonify({"ok": False, "error": "该任务没有可更新的公众号草稿"}), 409
        distribution = distributions[0]
        client = WeChatClient(account=distribution.account or "default")
        try:
            draft = client.get_draft(distribution.wechat_media_id)
            items = draft.get("news_item") or []
            if not items:
                raise WeChatAPIError("现有公众号草稿没有图文内容")
            current = items[0]
            allowed = (
                "title", "author", "digest", "content", "thumb_media_id",
                "show_cover_pic", "pic_crop_235_1", "pic_crop_1_1",
                "need_open_comment", "only_fans_can_comment",
            )
            payload = {key: current[key] for key in allowed if key in current}
            payload["content"], guide_url = append_source_pdf_guide(
                str(payload.get("content") or ""), client, distribution.account or "default",
            )
            payload["content_source_url"] = db_job.source_pdf_url
            client.update_draft(distribution.wechat_media_id, 0, payload)
            verified = client.get_draft(distribution.wechat_media_id)
            verified_items = verified.get("news_item") or []
            actual_url = str((verified_items[0] if verified_items else {}).get("content_source_url") or "")
            if actual_url != db_job.source_pdf_url:
                raise WeChatAPIError("公众号草稿回读的阅读原文链接不一致")
            actual_content = str((verified_items[0] if verified_items else {}).get("content") or "")
            if not contains_source_pdf_guide(actual_content, guide_url):
                observed = re.findall(r'https?://[^"\'\s>]+', actual_content)
                app.logger.warning(
                    "source PDF guide missing after draft readback: expected=%s observed_tail=%s",
                    guide_url,
                    observed[-5:],
                )
                raise WeChatAPIError("公众号草稿回读未发现阅读原文引导图")
        except (WeChatAPIError, TemplateAssetError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({
            "ok": True, "job_id": job_id, "account": distribution.account,
            "media_id": distribution.wechat_media_id, "content_source_url": actual_url,
            "source_pdf_guide_url": guide_url,
        })

    @app.get("/api/workflow/preflight")
    def api_workflow_preflight():
        cms_required = (
            "GENEMEDI_BLOG_USER", "GENEMEDI_BLOG_PASSWORD",
            "GENEMEDI_BLOG_CHINESE_LANGCODE", "ALIYUN_OSS_ACCESS_KEY_ID",
            "ALIYUN_OSS_ACCESS_KEY_SECRET", "ALIYUN_OSS_ENDPOINT",
            "ALIYUN_OSS_BUCKET", "ALIYUN_OSS_CDN_BASE_URL",
        )
        cms_missing = [name for name in cms_required if not os.getenv(name, "").strip()]
        source_pdf_mode = os.getenv("SOURCE_PDF_STORAGE", "ssh").strip().lower()
        if source_pdf_mode == "ssh":
            from utils.source_pdf_store import (
                DEFAULT_PUBLIC_BASE_URL, DEFAULT_SSH_HOST, DEFAULT_SSH_KNOWN_HOSTS,
                DEFAULT_SSH_PRIVATE_KEY, DEFAULT_SSH_REMOTE_DIR, DEFAULT_SSH_USER,
            )
            source_pdf_values = {
                "SOURCE_PDF_SSH_HOST": os.getenv("SOURCE_PDF_SSH_HOST", DEFAULT_SSH_HOST),
                "SOURCE_PDF_SSH_USER": os.getenv("SOURCE_PDF_SSH_USER", DEFAULT_SSH_USER),
                "SOURCE_PDF_SSH_PRIVATE_KEY": os.getenv("SOURCE_PDF_SSH_PRIVATE_KEY", DEFAULT_SSH_PRIVATE_KEY),
                "SOURCE_PDF_SSH_KNOWN_HOSTS": os.getenv("SOURCE_PDF_SSH_KNOWN_HOSTS", DEFAULT_SSH_KNOWN_HOSTS),
                "SOURCE_PDF_SSH_REMOTE_DIR": os.getenv("SOURCE_PDF_SSH_REMOTE_DIR", DEFAULT_SSH_REMOTE_DIR),
                "SOURCE_PDF_PUBLIC_BASE_URL": os.getenv("SOURCE_PDF_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL),
            }
        else:
            source_pdf_values = {
                "SOURCE_PDF_OSS_ACCESS_KEY_ID": os.getenv("SOURCE_PDF_OSS_ACCESS_KEY_ID") or os.getenv("ALIYUN_OSS_ACCESS_KEY_ID", ""),
                "SOURCE_PDF_OSS_ACCESS_KEY_SECRET": os.getenv("SOURCE_PDF_OSS_ACCESS_KEY_SECRET") or os.getenv("ALIYUN_OSS_ACCESS_KEY_SECRET", ""),
                "SOURCE_PDF_OSS_ENDPOINT": os.getenv("SOURCE_PDF_OSS_ENDPOINT") or os.getenv("ALIYUN_OSS_ENDPOINT", ""),
                "SOURCE_PDF_OSS_BUCKET": os.getenv("SOURCE_PDF_OSS_BUCKET") or os.getenv("ALIYUN_OSS_BUCKET", ""),
                "SOURCE_PDF_PUBLIC_BASE_URL": os.getenv("SOURCE_PDF_PUBLIC_BASE_URL", ""),
            }
        source_pdf_missing = [name for name, value in source_pdf_values.items() if not (value or "").strip()]
        return jsonify({
            "translation": {"configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip())},
            "cms": {"configured": not cms_missing, "missing": cms_missing},
            "source_pdf": {
                "configured": source_pdf_mode in {"ssh", "oss"} and not source_pdf_missing,
                "mode": source_pdf_mode,
                "missing": source_pdf_missing,
            },
            "translation_languages": ["en", "ja", "ko", "ru"],
            "cms_languages": ["zh", "en", "ja", "ko", "ru"],
        })

    @app.get("/api/sources")
    def api_sources():
        from utils.panel_runner import list_sources
        return jsonify({"lines": list_sources()})

    @app.post("/api/upload")
    def api_upload():
        from utils.panel_runner import save_uploaded_pdf
        line_id = request.form.get("line_id", "")
        files = [f for f in request.files.getlist("file") if f and f.filename]
        if not files:
            return jsonify({"ok": False, "error": "未收到文件"}), 400
        results = [save_uploaded_pdf(line_id, f.filename, f.read()) for f in files]
        return jsonify({"ok": all(r.get("ok") for r in results), "results": results})

    @app.post("/api/pdf/delete")
    def api_delete_pdf():
        from utils.panel_runner import delete_pending_pdf
        data = request.get_json(silent=True) or {}
        return jsonify(delete_pending_pdf(str(data.get("line_id", "")), str(data.get("pdf", ""))))

    @app.post("/api/run")
    def api_run():
        from utils.panel_runner import start_run
        data = request.get_json(silent=True) or {}
        return jsonify(start_run(str(data.get("line_id", "")), list(data.get("pdfs") or [])))

    @app.post("/api/run/cancel")
    def api_cancel_run():
        from utils.panel_runner import cancel_run
        return jsonify(cancel_run())

    @app.get("/api/runs")
    def api_runs():
        from utils.panel_runner import runs_status
        return jsonify(runs_status())

    @app.get("/preview/<job_id>")
    def preview(job_id: str):
        content_dir = get_db_manager().latest_content_dir(job_id)
        if not content_dir:
            abort(404)
        md_path = Path(content_dir) / "article.md"
        if not md_path.exists():
            abort(404)
        md_text = md_path.read_text(encoding="utf-8")
        wechat = bool(request.args.get("wechat"))
        if wechat:
            # 公众号草稿样式：正是投放到草稿的 HTML（含分级标题内联样式）
            from utils.wechat_html import markdown_to_wechat_html
            content = markdown_to_wechat_html(md_text)
        else:
            content = md_lib.markdown(
                md_text, extensions=["tables", "fenced_code", "sane_lists"],
            )
        return render_template("markdown_preview.html", job_id=job_id, content=content, wechat=wechat)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
