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
from utils.source_pdf_store import PublishedSourcePdf


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
            "SOURCE_PDF_STORAGE": "ssh",
            "SOURCE_PDF_SSH_HOST": "example.com",
            "SOURCE_PDF_SSH_USER": "uploader",
            "SOURCE_PDF_SSH_PRIVATE_KEY": "/secure/key",
            "SOURCE_PDF_SSH_KNOWN_HOSTS": "/secure/known_hosts",
            "SOURCE_PDF_SSH_REMOTE_DIR": "/srv/papers",
            "SOURCE_PDF_PUBLIC_BASE_URL": "https://pdf.genemedi.net",
        }
        with patch.dict(os.environ, configured, clear=True):
            response = self.client.get("/api/workflow/preflight")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["translation"]["configured"])
        self.assertTrue(data["cms"]["configured"])
        self.assertEqual(data["cms"]["missing"], [])
        self.assertTrue(data["source_pdf"]["configured"])
        self.assertEqual(data["source_pdf"]["mode"], "ssh")
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

    def test_source_pdf_provision_returns_public_identity_only(self):
        with patch(
            "utils.source_pdf_store.provision_source_pdf_ssh",
            return_value={"public_key": "ssh-ed25519 PUBLIC", "fingerprint": "SHA256:test"},
        ):
            response = self.client.post("/api/source-pdf/provision")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["public_key"], "ssh-ed25519 PUBLIC")
        self.assertNotIn("private", response.get_data(as_text=True).lower())

    def test_source_pdf_publish_persists_verified_url_without_drafting(self):
        class Store:
            def upload(self, path):
                self.path = path
                return PublishedSourcePdf(
                    "https://www.genemedi.net/uploads/papers/ab/hash.pdf", "a" * 64, 123,
                )

        store = Store()
        with patch("utils.source_pdf_store.create_source_pdf_store", return_value=store):
            response = self.client.post("/api/source-pdf/publish", json={"job_id": "job1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["url"], "https://www.genemedi.net/uploads/papers/ab/hash.pdf")
        job = self.db.get_job(self.db.find_job_pk("job1"))
        self.assertEqual(job.source_pdf_sha256, "a" * 64)
        self.assertEqual(job.source_pdf_size, 123)

    def test_source_pdf_link_can_patch_existing_draft_without_body_change(self):
        job_pk = self.db.find_job_pk("job1")
        self.db.update_job_source_pdf(
            job_pk,
            url="https://www.genemedi.net/uploads/papers/ab/hash.pdf",
            sha256="a" * 64,
            size=123,
        )

        class FakeClient:
            updated = []

            def __init__(self, account):
                self.account = account

            def get_draft(self, _media_id):
                source = self.updated[-1][2]["content_source_url"] if self.updated else ""
                return {"news_item": [{
                    "title": "原题", "author": "免疫客", "digest": "摘要",
                    "content": "<p>原正文</p>", "thumb_media_id": "thumb",
                    "show_cover_pic": 1, "need_open_comment": 0,
                    "only_fans_can_comment": 0, "content_source_url": source,
                }]}

            def update_draft(self, media_id, index, payload):
                self.updated.append((media_id, index, payload))

        FakeClient.updated = []
        with patch("utils.wechat_client.WeChatClient", FakeClient):
            response = self.client.post(
                "/api/source-pdf/apply-to-draft", json={"job_id": "job1"},
            )
        self.assertEqual(response.status_code, 200)
        payload = FakeClient.updated[0][2]
        self.assertEqual(payload["content"], "<p>原正文</p>")
        self.assertEqual(payload["thumb_media_id"], "thumb")
        self.assertEqual(
            payload["content_source_url"],
            "https://www.genemedi.net/uploads/papers/ab/hash.pdf",
        )


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
