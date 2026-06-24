"""dashboard API 测试：/api/health 与 /api/articles（含质量 + 投放概览）+ /api/upload。

用临时库注入 db.database 单例 + Flask test client。
"""
import io
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
