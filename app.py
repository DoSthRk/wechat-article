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

import sys
from pathlib import Path

import markdown as md_lib
from flask import Flask, abort, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.database import get_db_manager


_MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 单次上传上限（科普 PDF 一般 < 30MB）


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = testing
    app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/articles")
    def api_articles():
        page = max(1, request.args.get("page", 1, type=int))
        page_size = max(1, min(200, request.args.get("page_size", 100, type=int)))
        items, total = get_db_manager().list_article_overview(page=page, page_size=page_size)
        stats = {
            "total": total,
            "generated": sum(1 for i in items if i.get("title")),
            "blocked": sum(1 for i in items if i.get("publish_blocked")),
            "published": sum(
                1 for i in items
                if any(d.get("publish_status") == "published" for d in i.get("distributions", []))
            ),
        }
        return jsonify({
            "articles": items, "page": page, "page_size": page_size,
            "total": total, "stats": stats,
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

    @app.post("/api/run")
    def api_run():
        from utils.panel_runner import start_run
        data = request.get_json(silent=True) or {}
        return jsonify(start_run(str(data.get("line_id", "")), list(data.get("pdfs") or [])))

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
