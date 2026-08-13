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

import hmac
import os
import sys
from pathlib import Path

import markdown as md_lib
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.database import get_db_manager


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

    @app.get("/api/workflow/preflight")
    def api_workflow_preflight():
        cms_required = (
            "GENEMEDI_BLOG_USER", "GENEMEDI_BLOG_PASSWORD",
            "GENEMEDI_BLOG_CHINESE_LANGCODE", "ALIYUN_OSS_ACCESS_KEY_ID",
            "ALIYUN_OSS_ACCESS_KEY_SECRET", "ALIYUN_OSS_ENDPOINT",
            "ALIYUN_OSS_BUCKET", "ALIYUN_OSS_CDN_BASE_URL",
        )
        cms_missing = [name for name in cms_required if not os.getenv(name, "").strip()]
        return jsonify({
            "translation": {"configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip())},
            "cms": {"configured": not cms_missing, "missing": cms_missing},
            "translation_languages": ["en", "ja", "ko", "ru"],
            "cms_languages": ["zh", "en", "ja", "ko", "ru"],
        })

    @app.post("/api/internal/bootstrap-publishing")
    def api_bootstrap_publishing():
        """One-shot localhost-only bootstrap; removed after production setup."""
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(404)
        token_path = Path("/tmp/gm-blog-bootstrap/token")
        try:
            expected_token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            abort(404)
        supplied_token = request.headers.get("X-Bootstrap-Token", "")
        if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
            abort(404)

        allowed = {
            "GENEMEDI_BLOG_USER", "GENEMEDI_BLOG_PASSWORD",
            "GENEMEDI_BLOG_CHINESE_LANGCODE", "ALIYUN_OSS_ACCESS_KEY_ID",
            "ALIYUN_OSS_ACCESS_KEY_SECRET", "ALIYUN_OSS_ENDPOINT",
            "ALIYUN_OSS_BUCKET", "ALIYUN_OSS_CDN_BASE_URL", "ALIYUN_OSS_PREFIX",
        }
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != allowed:
            return jsonify({"ok": False, "error": "invalid configuration keys"}), 400
        values = {name: str(payload[name]).strip() for name in allowed}
        if not all(values.values()) or any("\n" in value or "\r" in value for value in values.values()):
            return jsonify({"ok": False, "error": "configuration values must be non-empty single lines"}), 400

        env_path = Path("/opt/gm-apps/article/shared/.env")
        lines = env_path.read_text(encoding="utf-8").splitlines()
        remaining = set(allowed)
        updated = []
        for line in lines:
            name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
            if name in allowed:
                updated.append(f"{name}={values[name]}")
                remaining.discard(name)
            else:
                updated.append(line)
        if updated and updated[-1]:
            updated.append("")
        updated.extend(f"{name}={values[name]}" for name in sorted(remaining))
        temp_path = env_path.with_suffix(".tmp")
        temp_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
        temp_path.chmod(0o600)
        temp_path.replace(env_path)
        token_path.unlink(missing_ok=True)
        return jsonify({"ok": True, "configured": sorted(allowed)})

    @app.get("/api/internal/probe-publishing")
    def api_probe_publishing():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(404)
        try:
            from genemedi_blog.genemedi_blog import BlogConfig, GeneMediBlogClient
            probe = GeneMediBlogClient(BlogConfig.from_env()).probe()
            cms = {
                "ok": True,
                "jsonapi_status": probe["jsonapi_status"],
                "article_status": probe["article_status"],
                "options_status": probe["options_status"],
                "allowed_methods": probe["allowed_methods"],
            }
        except Exception as exc:
            cms = {"ok": False, "error": str(exc)}
        try:
            from utils.blog_pipeline import OssImageStore
            store = OssImageStore.from_env()
            probe_key = f"{store.prefix}/.connectivity-probe"
            store.bucket.put_object(probe_key, b"gm-blog-probe")
            store.bucket.delete_object(probe_key)
            oss = {"ok": True, "write_delete": True}
        except Exception as exc:
            try:
                import oss2
                auth = oss2.Auth(
                    os.environ["ALIYUN_OSS_ACCESS_KEY_ID"],
                    os.environ["ALIYUN_OSS_ACCESS_KEY_SECRET"],
                )
                service = oss2.Service(auth, os.environ["ALIYUN_OSS_ENDPOINT"])
                visible_buckets = [item.name for item in service.list_buckets().buckets]
            except Exception as list_exc:
                visible_buckets = [f"bucket list unavailable: {list_exc}"]
            oss = {"ok": False, "error": str(exc), "visible_buckets": visible_buckets}
        return jsonify({"cms": cms, "oss": oss})

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
