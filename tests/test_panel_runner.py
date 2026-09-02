"""操作面板后端：job_id 规整、单活跃任务状态机、内容源列举（子进程 mock，不真跑）。"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import db.database as dbmod
from db.database import DatabaseManager, JobStatus
from utils import panel_runner as pr


class _FakeProc:
    def __init__(self):
        import os
        self.pid = os.getpid()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def finish(self, rc=0):
        self.returncode = rc

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class TestJobId(unittest.TestCase):
    def test_sanitize_spaces(self):
        self.assertEqual(pr._job_id_from_pdf("免疫客文章-CAR T-7"), "免疫客文章-CAR-T-7")
        self.assertEqual(pr._job_id_from_pdf("  a  b "), "a-b")


class TestRunStateMachine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_dir, pr.RUN_DIR = pr.RUN_DIR, Path(self._tmp.name)
        self._saved_popen, self.proc = subprocess.Popen, _FakeProc()
        subprocess.Popen = lambda *a, **k: self.proc  # 不真起子进程
        pr._current, pr._history = None, []

    def tearDown(self):
        pr.RUN_DIR = self._saved_dir
        subprocess.Popen = self._saved_popen
        pr._current, pr._history = None, []
        self._tmp.cleanup()

    def test_start_then_busy_then_reap(self):
        r1 = pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        self.assertTrue(r1["ok"], r1)
        # 单活跃：第二个被拒
        r2 = pr.start_run("solidex", ["inputs/pdfs/免疫客/y.pdf"])
        self.assertFalse(r2["ok"])
        st = pr.runs_status()
        self.assertTrue(st["busy"])
        self.assertEqual(st["current"]["line_id"], "solidex")
        # 子进程结束 → 归档 history，恢复空闲
        self.proc.finish(0)
        st2 = pr.runs_status()
        self.assertFalse(st2["busy"])
        self.assertEqual(len(st2["history"]), 1)
        self.assertEqual(st2["history"][0]["status"], "done")

    def test_run_state_visible_without_worker_memory(self):
        r1 = pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        self.assertTrue(r1["ok"], r1)
        pr._current = None  # 模拟下一次轮询命中另一个 gunicorn worker

        st = pr.runs_status()

        self.assertTrue(st["busy"])
        self.assertEqual(st["current"]["run_id"], r1["run_id"])
        self.assertEqual(st["current"]["line_id"], "solidex")

    def test_stale_running_state_infers_done_from_log(self):
        run_id = "staleok"
        log = pr.RUN_DIR / f"{run_id}.log"
        log.write_text("2026 - INFO - [a] POST wechat/aav media_id=m1\n"
                       "2026 - INFO - done. total=1 success=1 failed=0\n",
                       encoding="utf-8")
        pr._write_run_state({
            "run_id": run_id,
            "task": "panel-aav-test",
            "line_id": "aav",
            "jobs": ["a"],
            "log_path": str(log),
            "pid": 99999999,
            "status": "running",
            "started": 1.0,
        })

        st = pr.runs_status()

        self.assertFalse(st["busy"])
        self.assertEqual(st["history"][0]["status"], "done")
        self.assertIn("公众号草稿箱", st["history"][0]["summary"]["message"])

    def test_start_rejects_running_state_from_other_worker(self):
        pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        pr._current = None

        r2 = pr.start_run("solidex", ["inputs/pdfs/免疫客/y.pdf"])

        self.assertFalse(r2["ok"])
        self.assertIn("已有任务", r2["error"])

    def test_failed_run_marked(self):
        pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        self.proc.finish(1)
        self.assertEqual(pr.runs_status()["history"][0]["status"], "failed")

    def test_cancel_active_run_archives_history(self):
        pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        r = pr.cancel_run()
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.proc.terminated)
        st = pr.runs_status()
        self.assertFalse(st["busy"])
        self.assertEqual(st["history"][0]["status"], "cancelled")

    def test_no_pdfs_rejected(self):
        self.assertFalse(pr.start_run("solidex", [])["ok"])

    def test_bad_line_rejected(self):
        self.assertFalse(pr.start_run("does-not-exist", ["a.pdf"])["ok"])


class TestUpload(unittest.TestCase):
    _PDF = b"%PDF-1.7\n%minimal\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_project_root = pr.PROJECT_ROOT
        self._saved_pdfs, pr.PDFS_DIR = pr.PDFS_DIR, Path(self._tmp.name)
        self._run_tmp = tempfile.TemporaryDirectory()
        self._saved_run_dir, pr.RUN_DIR = pr.RUN_DIR, Path(self._run_tmp.name)
        self._db_tmp = tempfile.TemporaryDirectory()
        self._orig_db_instance = dbmod._instance
        dbmod._instance = DatabaseManager(database_url=f"sqlite:///{(Path(self._db_tmp.name) / 't.db').as_posix()}")

    def tearDown(self):
        pr.PROJECT_ROOT = self._saved_project_root
        pr.PDFS_DIR = self._saved_pdfs
        pr.RUN_DIR = self._saved_run_dir
        dbmod._instance.engine.dispose()
        dbmod._instance = self._orig_db_instance
        self._tmp.cleanup()
        self._run_tmp.cleanup()
        self._db_tmp.cleanup()

    def test_saves_into_line_folder(self):
        r = pr.save_uploaded_pdf("solidex", "新文章.pdf", self._PDF)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["name"], "新文章.pdf")
        self.assertFalse(r["overwrite"])
        self.assertEqual(r["job_id"], "新文章")
        self.assertTrue((pr.PDFS_DIR / "免疫客" / "新文章.pdf").exists())

    def test_overwrite_flagged(self):
        pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)
        r = pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)
        self.assertTrue(r["overwrite"])
        self.assertFalse(r["already_generated"])

    def test_overwrite_generated_file_is_flagged(self):
        db = dbmod.get_db_manager()
        first = pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)
        task = db.get_or_create_task("upload-generated")
        jpk = db.upsert_job(
            task.id, "x", pdf_path=first["pdf"], template_id="t", product_id="p",
            status=JobStatus.GENERATED,
        ).id
        db.upsert_article(jpk, title="已生成", content_dir="x")

        r = pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)

        self.assertTrue(r["overwrite"])
        self.assertTrue(r["already_generated"])

        src = pr.list_sources()
        sol = next(l for l in src if l["line_id"] == "solidex")
        item = next(f for f in sol["pdfs"] if f["name"] == "x.pdf")
        self.assertTrue(item["has_article"])
        self.assertTrue(item["operator_pending"])
        self.assertTrue(item["already_generated"])

    def test_article_from_previous_release_stays_bound_after_deploy(self):
        current_root = Path(self._tmp.name) / "releases" / "new-release"
        pr.PROJECT_ROOT = current_root
        pr.PDFS_DIR = current_root / "inputs" / "pdfs"
        saved = pr.save_uploaded_pdf("solidex", "免疫客文章-3-1.pdf", self._PDF)
        db = dbmod.get_db_manager()
        task = db.get_or_create_task("previous-release")
        job_pk = db.upsert_job(
            task.id,
            "免疫客文章-3-1",
            pdf_path="/opt/gm-apps/article/releases/old-release/inputs/pdfs/免疫客/免疫客文章-3-1.pdf",
            template_id="t",
            product_id="p",
            status=JobStatus.PUBLISHED,
        ).id
        db.upsert_article(job_pk, title="已完成文章", content_dir="免疫客文章-3-1")

        solidex = next(line for line in pr.list_sources() if line["line_id"] == "solidex")
        item = next(file for file in solidex["pdfs"] if file["name"] == saved["name"])

        self.assertTrue(item["bound"])
        self.assertTrue(item["has_article"])
        self.assertEqual(item["title"], "已完成文章")

    def test_path_traversal_stripped_to_basename(self):
        r = pr.save_uploaded_pdf("solidex", "../../../evil.pdf", self._PDF)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["name"], "evil.pdf")
        self.assertFalse((pr.PDFS_DIR.parent / "evil.pdf").exists())
        self.assertTrue((pr.PDFS_DIR / "免疫客" / "evil.pdf").exists())

    def test_non_pdf_extension_rejected(self):
        self.assertFalse(pr.save_uploaded_pdf("solidex", "a.txt", self._PDF)["ok"])

    def test_bad_pdf_header_rejected(self):
        self.assertFalse(pr.save_uploaded_pdf("solidex", "a.pdf", b"not a pdf")["ok"])

    def test_empty_data_rejected(self):
        self.assertFalse(pr.save_uploaded_pdf("solidex", "a.pdf", b"")["ok"])

    def test_bad_line_rejected(self):
        self.assertFalse(pr.save_uploaded_pdf("does-not-exist", "a.pdf", self._PDF)["ok"])

    def test_write_error_returns_json_shape(self):
        blocked = Path(self._tmp.name) / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        pr.PDFS_DIR = blocked
        r = pr.save_uploaded_pdf("solidex", "a.pdf", self._PDF)
        self.assertFalse(r["ok"])
        self.assertIn("保存失败", r["error"])

    def test_safe_name_helper(self):
        self.assertIsNone(pr._safe_pdf_name(".pdf"))
        self.assertIsNone(pr._safe_pdf_name("a.docx"))
        self.assertEqual(pr._safe_pdf_name("dir\\sub\\论文 1.pdf"), "论文 1.pdf")

    def test_delete_pending_pdf(self):
        saved = pr.save_uploaded_pdf("solidex", "待删.pdf", self._PDF)
        self.assertTrue(saved["ok"], saved)
        target = pr.PDFS_DIR / "免疫客" / "待删.pdf"
        self.assertTrue(target.exists())

        deleted = pr.delete_pending_pdf("solidex", saved["pdf"])

        self.assertTrue(deleted["ok"], deleted)
        self.assertFalse(target.exists())

    def test_delete_rejects_path_outside_line_folder(self):
        r = pr.delete_pending_pdf("solidex", "inputs/pdfs/AAVTx/a.pdf")
        self.assertFalse(r["ok"])
        self.assertIn("不属于", r["error"])

    def test_delete_generated_override_removes_queue_not_file(self):
        db = dbmod.get_db_manager()
        first = pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)
        task = db.get_or_create_task("delete-generated-override")
        jpk = db.upsert_job(
            task.id, "x", pdf_path=first["pdf"], template_id="t", product_id="p",
            status=JobStatus.GENERATED,
        ).id
        db.upsert_article(jpk, title="已生成", content_dir="x")
        marked = pr.save_uploaded_pdf("solidex", "x.pdf", self._PDF)
        target = pr.PDFS_DIR / "免疫客" / "x.pdf"

        deleted = pr.delete_pending_pdf("solidex", marked["pdf"])

        self.assertTrue(deleted["ok"], deleted)
        self.assertTrue(deleted["removed_from_pending"])
        self.assertTrue(target.exists())
        src = pr.list_sources()
        item = next(f for l in src for f in l["pdfs"] if f["name"] == "x.pdf")
        self.assertFalse(item["operator_pending"])

    def test_operator_pending_file_tolerates_utf8_bom(self):
        pr.RUN_DIR.mkdir(parents=True, exist_ok=True)
        (pr.RUN_DIR / "operator_pending.json").write_text(
            '[{"line_id":"solidex","pdf":"inputs/pdfs/免疫客/x.pdf","name":"x.pdf"}]',
            encoding="utf-8-sig",
        )

        keys = pr._operator_pending_keys("solidex")

        self.assertIn("inputs/pdfs/免疫客/x.pdf", keys)

    def test_generated_without_wechat_draft_is_not_operator_pending_state(self):
        db = dbmod.get_db_manager()
        saved = pr.save_uploaded_pdf("solidex", "draft-missing.pdf", self._PDF)
        task = db.get_or_create_task("draft-missing")
        jpk = db.upsert_job(
            task.id, "draft-missing", pdf_path=saved["pdf"], template_id="t", product_id="p",
            status=JobStatus.GENERATED,
        ).id
        db.upsert_article(jpk, title="已生成但没有草稿", content_dir="draft-missing")

        sol = next(l for l in pr.list_sources() if l["line_id"] == "solidex")
        item = next(f for f in sol["pdfs"] if f["name"] == "draft-missing.pdf")
        self.assertTrue(item["has_article"])
        self.assertFalse(item["published"])
        self.assertFalse(item["needs_action"])
        self.assertEqual(sol["counts"]["pending"], 0)

        db.upsert_distribution(
            jpk, "wechat", account="immune", publish_status="published", wechat_media_id="m1",
        )
        sol = next(l for l in pr.list_sources() if l["line_id"] == "solidex")
        item = next(f for f in sol["pdfs"] if f["name"] == "draft-missing.pdf")
        self.assertTrue(item["published"])
        self.assertFalse(item["needs_action"])


class TestNormPdfKey(unittest.TestCase):
    def test_basename_fallback_for_outside_path(self):
        self.assertEqual(pr._norm_pdf_key("C:/somewhere/else/Foo.PDF"), "foo.pdf")

    def test_abs_and_rel_under_project_match(self):
        rel = "inputs/pdfs/免疫客/免疫客文章-CAR T-2.pdf"
        ab = str(pr.PROJECT_ROOT / "inputs" / "pdfs" / "免疫客" / "免疫客文章-CAR T-2.pdf")
        self.assertEqual(pr._norm_pdf_key(rel), pr._norm_pdf_key(ab))

    def test_release_paths_match_across_deployments(self):
        old = "/opt/gm-apps/article/releases/old/inputs/pdfs/免疫客/免疫客文章-3-1.pdf"
        new = "/opt/gm-apps/article/releases/new/inputs/pdfs/免疫客/免疫客文章-3-1.pdf"

        self.assertEqual(pr._norm_pdf_key(old), pr._norm_pdf_key(new))
        self.assertEqual(pr._norm_pdf_key(old), "inputs/pdfs/免疫客/免疫客文章-3-1.pdf")


class TestListSources(unittest.TestCase):
    def test_smoke(self):
        src = pr.list_sources()
        sol = next((l for l in src if l["line_id"] == "solidex"), None)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["folder"], "免疫客")
        self.assertIn("pdfs", sol)

    def test_each_pdf_has_binding_shape(self):
        sol = next((l for l in pr.list_sources() if l["line_id"] == "solidex"), None)
        for f in sol["pdfs"]:
            self.assertIn("bound", f)
            self.assertIn("job_id", f)
            if f["bound"]:  # 绑定到文章时 job_id 应是库里真实 id（非文件名推导）
                self.assertTrue(f["job_id"])

    def test_line_counts_support_collapsed_processed_ui(self):
        sol = next((l for l in pr.list_sources() if l["line_id"] == "solidex"), None)
        counts = sol["counts"]
        self.assertEqual(counts["total"], len(sol["pdfs"]))
        self.assertEqual(counts["processed"], sum(1 for f in sol["pdfs"] if f["has_article"]))
        self.assertEqual(counts["pending"], sum(1 for f in sol["pdfs"] if f["needs_action"]))
        self.assertEqual(counts["published"], sum(1 for f in sol["pdfs"] if f["published"]))


class TestRunSummary(unittest.TestCase):
    def test_done_summary_reports_wechat_draft_count(self):
        lines = [
            "2026 - INFO - [a] generated: title=A len=100 tokens=20 health=90 tonal=80",
            "2026 - INFO - [a] POST wechat/aav media_id=m1",
        ]
        summary = pr._summarize_log(lines, "done", 1)
        self.assertEqual(summary["drafted"], 1)
        self.assertIn("公众号草稿箱", summary["message"])

    def test_summary_extracts_business_counts_from_log_tail(self):
        lines = [
            "2026 - INFO - [a] generated: title=A len=100 tokens=20 health=90 tonal=80",
            "2026 - WARNING - [b] generated but BLOCKED: tonal_score<60",
            "2026 - ERROR - [c] generate failed: PDF 文本为空",
        ]
        summary = pr._summarize_log(lines, "running", 3)
        self.assertEqual(summary["generated"], 1)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["total"], 3)
        self.assertIn("失败", summary["message"])

    def test_summary_deduplicates_wechat_ip_whitelist_errors(self):
        lines = [
            "2026 - INFO - [a] generated: title=A len=100 tokens=20 health=90 tonal=80",
            "2026 - ERROR - [a] 自动封面：上传永久素材失败 refresh access_token failed: {'errcode': 40164, 'errmsg': 'invalid ip 47.102.223.198 ipv6 ::ffff:47.102.223.198, not in whitelist'}",
            "2026 - ERROR - [a] distribute: 账户 aav 缺封面（设 WECHAT_AAV_THUMB_MEDIA_ID；或确保原草稿在以复用其封面）",
        ]
        summary = pr._summarize_log(lines, "failed", 1)

        self.assertEqual(summary["failed"], 1)
        self.assertIn("IP 白名单", summary["message"])
        self.assertIn("47.102.223.198", summary["message"])

    def test_summary_reports_missing_figures_without_counting_failure(self):
        lines = [
            "2026 - INFO - [a] generated: title=A len=100 tokens=20 health=90 tonal=80",
            "2026 - WARNING - [a] 配图：2 个图片占位符未配到图片，已从草稿正文移除",
            "2026 - INFO - [a] POST wechat/aav media_id=m1",
        ]
        summary = pr._summarize_log(lines, "done", 1)

        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["missing_figures"], 2)
        self.assertIn("未配图 2 张", summary["message"])


if __name__ == "__main__":
    unittest.main()
