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
from utils.blog_urls import BlogUrlError, resolve_published_blog_url
from utils.wechat_template_assets import (
    TemplateAssetError,
    append_source_pdf_guide,
    contains_source_pdf_guide,
)


_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 单次上传上限（图多的论文 PDF 可能上百 MB）
_USER_LINE_ACCESS = {
    "jhh": frozenset({"solidex"}),
    "hqq": frozenset({"aav"}),
}
_LINE_LABELS = {"solidex": "Solidex", "aav": "AAV"}


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = testing
    app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES

    def _username() -> str:
        username = str(request.headers.get("X-GM-LAB-Username") or "").strip().lower()
        if testing and not username:
            return "admin"
        if not username:
            abort(401)
        return username

    def _admin_users() -> set[str]:
        configured = os.getenv("GM_BLOG_ADMIN_USERS", "admin")
        return {item.strip().lower() for item in configured.split(",") if item.strip()}

    def _line_access() -> set[str] | None:
        username = _username()
        if username in _admin_users():
            return None
        allowed = _USER_LINE_ACCESS.get(username)
        if not allowed:
            abort(403)
        return set(allowed)

    def _require_admin() -> None:
        if _username() not in _admin_users():
            abort(403)

    def _require_line(line_id: str) -> str:
        normalized = str(line_id or "").strip().lower()
        allowed = _line_access()
        if not normalized or (allowed is not None and normalized not in allowed):
            abort(403)
        return normalized

    def _visible_source_lines() -> list[dict]:
        from utils.panel_runner import list_sources
        lines = list_sources()
        allowed = _line_access()
        if allowed is None:
            return lines
        return [line for line in lines if line.get("line_id") in allowed]

    def _job_line_index() -> dict[str, str]:
        from utils.panel_runner import list_sources
        return {
            str(file.get("job_id") or ""): str(line.get("line_id") or "")
            for line in list_sources()
            for file in line.get("pdfs", [])
            if file.get("job_id")
        }

    def _require_job(job_id: str) -> str:
        normalized = str(job_id or "").strip()
        line_id = _job_line_index().get(normalized)
        allowed = _line_access()
        if allowed is None:
            return line_id or "admin"
        if not line_id:
            abort(404)
        if line_id not in allowed:
            abort(403)
        return line_id

    def _require_selections(selections: list) -> str:
        lines = {_require_job(str(item.get("job_id") or "")) for item in selections if isinstance(item, dict)}
        if not lines:
            abort(400)
        return next(iter(lines)) if len(lines) == 1 else "admin"

    @app.errorhandler(401)
    @app.errorhandler(403)
    def _access_denied(error):
        if request.path.startswith("/api/"):
            message = "未识别到 GM-LAB 登录用户" if error.code == 401 else "无权访问该业务线"
            return jsonify({"ok": False, "error": message}), error.code
        return render_template("access_denied.html", code=error.code), error.code

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
        username = _username()
        allowed = _line_access()
        line_id = next(iter(allowed)) if allowed and len(allowed) == 1 else ""
        return render_template(
            "operator.html",
            username=username,
            line_id=line_id,
            line_label=_LINE_LABELS.get(line_id, "全部业务线"),
        )

    @app.get("/admin")
    def dashboard():
        _require_admin()
        return render_template("dashboard.html")

    @app.get("/api/articles")
    def api_articles():
        from utils.pricing import article_cost_cny, rate_card
        allowed = _line_access()
        page = max(1, request.args.get("page", 1, type=int))
        page_size = max(1, min(200, request.args.get("page_size", 100, type=int)))
        if allowed is None:
            items, total = get_db_manager().list_article_overview(page=page, page_size=page_size)
        else:
            visible_jobs = {job_id for job_id, line_id in _job_line_index().items() if line_id in allowed}
            all_items, _ = get_db_manager().list_article_overview(page=1, page_size=10000)
            all_items = [item for item in all_items if item.get("job_id") in visible_jobs]
            total = len(all_items)
            start = (page - 1) * page_size
            items = all_items[start:start + page_size]
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
        _require_selections(selections)
        return jsonify(run_batch(BlogWorkflow(get_db_manager()), selections, "translate"))

    @app.post("/api/blog/publish")
    def api_blog_publish():
        from utils.blog_pipeline import BlogWorkflow, run_batch
        data = request.get_json(silent=True) or {}
        selections = data.get("selections")
        if not isinstance(selections, list) or not selections:
            return jsonify({"ok": False, "error": "请至少选择一篇待发布文章"}), 400
        _require_selections(selections)
        return jsonify(run_batch(BlogWorkflow(get_db_manager()), selections, "publish"))

    @app.post("/api/workflow/<stage>/run")
    def api_workflow_run(stage: str):
        from utils.workflow_runner import start_workflow
        data = request.get_json(silent=True) or {}
        selections = data.get("selections")
        if not isinstance(selections, list) or not selections:
            return jsonify({"ok": False, "error": "请至少选择一个可处理版本"}), 400
        owner_line = _require_selections(selections)
        result = start_workflow(stage, selections, owner_line=owner_line, owner_username=_username())
        return jsonify(result), (202 if result.get("ok") else 409)

    @app.get("/api/workflow/status")
    def api_workflow_status():
        from utils.workflow_runner import workflow_status
        allowed = _line_access()
        states = workflow_status()
        if allowed is not None:
            for state in states.values():
                owner_line = str(state.get("owner_line") or "")
                if owner_line and owner_line not in allowed:
                    state.update(current=None, errors=[], other_line_running=state.get("status") == "running")
                elif not owner_line:
                    current_job = str((state.get("current") or {}).get("job_id") or "")
                    if not current_job or _job_line_index().get(current_job) not in allowed:
                        state.update(current=None, errors=[])
        return jsonify(states)

    @app.post("/api/source-pdf/provision")
    def api_source_pdf_provision():
        """One-time SSH identity bootstrap; never returns private-key material."""
        _require_admin()
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
        _require_job(job_id)
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
        """Point 阅读原文 to the verified Chinese Blog and keep the footer guide."""
        from utils.wechat_client import WeChatAPIError, WeChatClient
        data = request.get_json(silent=True) or {}
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return jsonify({"ok": False, "error": "缺少 job_id"}), 400
        _require_job(job_id)
        db = get_db_manager()
        job_pk = db.find_job_pk(job_id)
        db_job = db.get_job(job_pk) if job_pk is not None else None
        if db_job is None:
            return jsonify({"ok": False, "error": f"未知 job_id：{job_id}"}), 404
        if not db_job.source_pdf_url:
            return jsonify({"ok": False, "error": "该任务尚未发布原文 PDF"}), 409
        try:
            blog_url = resolve_published_blog_url(
                db, job_pk, db_job.job_id, lang="zh",
            )
        except BlogUrlError as exc:
            return jsonify({"ok": False, "error": f"中文 Blog 未就绪：{exc}"}), 409
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
            payload["content_source_url"] = blog_url
            client.update_draft(distribution.wechat_media_id, 0, payload)
            verified = client.get_draft(distribution.wechat_media_id)
            verified_items = verified.get("news_item") or []
            actual_url = str((verified_items[0] if verified_items else {}).get("content_source_url") or "")
            if actual_url != blog_url:
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
            "source_pdf_url": db_job.source_pdf_url,
            "source_pdf_guide_url": guide_url,
        })

    @app.get("/api/workflow/preflight")
    def api_workflow_preflight():
        _line_access()
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
        return jsonify({"lines": _visible_source_lines(), "username": _username()})

    @app.post("/api/upload")
    def api_upload():
        from utils.panel_runner import save_uploaded_pdf
        line_id = _require_line(request.form.get("line_id", ""))
        files = [f for f in request.files.getlist("file") if f and f.filename]
        if not files:
            return jsonify({"ok": False, "error": "未收到文件"}), 400
        results = [save_uploaded_pdf(line_id, f.filename, f.read()) for f in files]
        return jsonify({"ok": all(r.get("ok") for r in results), "results": results})

    @app.post("/api/pdf/delete")
    def api_delete_pdf():
        from utils.panel_runner import delete_pending_pdf
        data = request.get_json(silent=True) or {}
        line_id = _require_line(str(data.get("line_id", "")))
        return jsonify(delete_pending_pdf(line_id, str(data.get("pdf", ""))))

    @app.post("/api/run")
    def api_run():
        from utils.panel_runner import start_run
        data = request.get_json(silent=True) or {}
        line_id = _require_line(str(data.get("line_id", "")))
        return jsonify(start_run(line_id, list(data.get("pdfs") or [])))

    @app.post("/api/run/cancel")
    def api_cancel_run():
        from utils.panel_runner import cancel_run, runs_status
        allowed = _line_access()
        current = runs_status().get("current") or {}
        if allowed is not None and current.get("line_id") not in allowed:
            abort(403)
        return jsonify(cancel_run())

    @app.get("/api/runs")
    def api_runs():
        from utils.panel_runner import runs_status
        allowed = _line_access()
        status = runs_status()
        if allowed is not None:
            current = status.get("current")
            if current and current.get("line_id") not in allowed:
                status["current"] = None
                status["busy_other_line"] = True
            status["history"] = [item for item in status.get("history", []) if item.get("line_id") in allowed]
        return jsonify(status)

    @app.get("/preview/<job_id>")
    def preview(job_id: str):
        _require_job(job_id)
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
