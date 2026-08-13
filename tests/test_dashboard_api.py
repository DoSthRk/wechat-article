"""dashboard API 测试：/api/health 与 /api/articles（含质量 + 投放概览）+ /api/upload。

用临时库注入 db.database 单例 + Flask test client。
"""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.database as dbmod
from db.database import DatabaseManager, JobStatus

import app as appmod
from utils import panel_runner as pr


class TestDashboardApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_instance = dbmod._instance
        db = DatabaseManager(database_url=f"sqlite:///{(Path(self._tmp.name) / 't.db').as_posix()}")
        dbmod._instance = db   # 让 get_db_manager() 返回这个临时库
        task = db.get_or_create_task("t")
        jpk = db.upsert_job(
            task.id, "job1", pdf_path="p", template_id="aav_x", product_id="purprox",
            status=JobStatus.GENERATED,
        ).id
        db.upsert_article(
            jpk, title="测试标题", content_dir="x",
            markdown_health_score=100, tonal_score=100, publish_blocked=False,
            model="deepseek-chat", prompt_tokens=1_000_000, completion_tokens=0,
        )
        db.upsert_distribution(
            jpk, "wechat", account="aav", lang="zh",
            publish_status="published", wechat_media_id="m1",
        )
        self.db = db
        self.client = appmod.create_app(testing=True).test_client()

    def tearDown(self):
        self.db.engine.dispose()
        dbmod._instance = self._orig_instance
        self._tmp.cleanup()

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")

    def test_root_redirects_to_operator_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/operator"))

    def test_operator_page_is_minimal(self):
        r = self.client.get("/operator")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("operator.js", html)
        self.assertIn('href="https://lab.genemedi.cn/"', html)
        self.assertIn("返回 GM-LAB", html)
        self.assertNotIn("/admin", html)
        self.assertNotIn("管理员版", html)
        self.assertNotIn("文章 × 投放", html)
        self.assertNotIn("成本", html)

        js = self.client.get("/static/operator.js").get_data(as_text=True)
        self.assertIn('aav: "AAV"', js)
        self.assertIn('solidex: "Solidex"', js)
        self.assertIn("上传 PDF", js)
        self.assertIn("生成选中文件", js)
        self.assertIn("row-pick", js)
        self.assertIn("已生成过", js)
        self.assertIn("needsAction", js)
        self.assertNotIn("confirm(", js)
        self.assertIn("/api/pdf/delete", js)
        self.assertIn("/api/workflow/${stage}/run", js)
        self.assertIn("TRANSLATION_LANGS", js)
        self.assertIn("CMS_LANGS", js)

        self.assertIn("PDF → 公众号草稿", html)
        self.assertIn("多语言翻译", html)
        self.assertIn("多语言 CMS 发布", html)

    def test_admin_page_keeps_full_dashboard(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('href="https://lab.genemedi.cn/"', html)
        self.assertIn("返回 GM-LAB", html)
        self.assertIn("内容发布流水线", html)
        self.assertIn("CMS 发布", html)

    def test_articles_overview(self):
        r = self.client.get("/api/articles")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["total"], 1)
        art = data["articles"][0]
        self.assertEqual(art["job_id"], "job1")
        self.assertEqual(art["markdown_health_score"], 100)
        self.assertFalse(art["publish_blocked"])
        self.assertEqual(len(art["distributions"]), 1)
        self.assertEqual(art["distributions"][0]["publish_status"], "published")
        self.assertEqual(data["stats"]["published"], 1)
        self.assertEqual(data["stats"]["blocked"], 0)
        # 成本：1M 输入 token × 默认 ¥2/M = ¥2.0（无 env 覆盖时）
        self.assertAlmostEqual(art["cost_cny"], 2.0, places=4)
        self.assertAlmostEqual(data["stats"]["cost_cny"], 2.0, places=2)
        self.assertIn("deepseek-chat", data["rates"])

    def test_blog_actions_reject_empty_selection(self):
        for path in ("/api/blog/translate", "/api/blog/publish"):
            response = self.client.post(path, json={"selections": []})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["ok"])

    def test_workflow_actions_reject_empty_selection(self):
        for path in ("/api/workflow/translate/run", "/api/workflow/publish/run"):
            response = self.client.post(path, json={"selections": []})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["ok"])

    def test_workflow_preflight_does_not_expose_secrets(self):
        configured = {
            "DEEPSEEK_API_KEY": "translation-secret",
            "GENEMEDI_BLOG_USER": "cms-user",
            "GENEMEDI_BLOG_PASSWORD": "cms-secret",
            "GENEMEDI_BLOG_CHINESE_LANGCODE": "zh-hans",
            "ALIYUN_OSS_ACCESS_KEY_ID": "oss-key",
            "ALIYUN_OSS_ACCESS_KEY_SECRET": "oss-secret",
            "ALIYUN_OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
            "ALIYUN_OSS_BUCKET": "bucket",
            "ALIYUN_OSS_CDN_BASE_URL": "https://img.example.com",
        }
        with patch.dict(os.environ, configured, clear=True):
            response = self.client.get("/api/workflow/preflight")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["translation"]["configured"])
        self.assertTrue(data["cms"]["configured"])
        self.assertEqual(data["cms"]["missing"], [])
        self.assertEqual(data["translation_languages"], ["en", "ja", "ko", "ru"])
        body = response.get_data(as_text=True)
        self.assertNotIn("translation-secret", body)
        self.assertNotIn("cms-secret", body)
        self.assertNotIn("oss-secret", body)

    def test_workflow_preflight_lists_missing_variable_names_only(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/workflow/preflight")
        data = response.get_json()
        self.assertFalse(data["cms"]["configured"])
        self.assertIn("GENEMEDI_BLOG_USER", data["cms"]["missing"])
        self.assertNotIn("cms-secret", response.get_data(as_text=True))


class TestUploadApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_pdfs, pr.PDFS_DIR = pr.PDFS_DIR, Path(self._tmp.name)
        self.client = appmod.create_app(testing=True).test_client()

    def tearDown(self):
        pr.PDFS_DIR = self._saved_pdfs
        self._tmp.cleanup()

    def test_upload_ok(self):
        r = self.client.post("/api/upload", data={
            "line_id": "solidex",
            "file": (io.BytesIO(b"%PDF-1.7\n%x\n"), "新文章.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["results"][0]["name"], "新文章.pdf")
        self.assertTrue((pr.PDFS_DIR / "免疫客" / "新文章.pdf").exists())

    def test_delete_pending_pdf_ok(self):
        upload = self.client.post("/api/upload", data={
            "line_id": "solidex",
            "file": (io.BytesIO(b"%PDF-1.7\n%x\n"), "待删.pdf"),
        }, content_type="multipart/form-data").get_json()

        r = self.client.post("/api/pdf/delete", json={
            "line_id": "solidex",
            "pdf": upload["results"][0]["pdf"],
        })

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"], body)
        self.assertFalse((pr.PDFS_DIR / "免疫客" / "待删.pdf").exists())

    def test_upload_bad_content_is_json_error(self):
        r = self.client.post("/api/upload", data={
            "line_id": "solidex",
            "file": (io.BytesIO(b"not a pdf"), "x.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("PDF", body["results"][0]["error"])

    def test_upload_no_file_400(self):
        r = self.client.post("/api/upload", data={"line_id": "solidex"},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_too_large_returns_json(self):
        app = appmod.create_app(testing=True)
        app.config["MAX_CONTENT_LENGTH"] = 16  # 强制触发 413
        client = app.test_client()
        r = client.post("/api/upload", data={
            "line_id": "solidex",
            "file": (io.BytesIO(b"%PDF-1.7\n" + b"0" * 100), "big.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)
        body = r.get_json()  # 关键：413 也回 JSON（前端能给明确提示）
        self.assertIsNotNone(body)
        self.assertFalse(body["ok"])

    def test_cancel_run_endpoint_returns_json_when_idle(self):
        r = self.client.post("/api/run/cancel")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNotNone(body)
        self.assertFalse(body["ok"])
        self.assertIn("没有", body["error"])


if __name__ == "__main__":
    unittest.main()
