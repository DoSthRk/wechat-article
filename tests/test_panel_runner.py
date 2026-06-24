"""操作面板后端：job_id 规整、单活跃任务状态机、内容源列举（子进程 mock，不真跑）。"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from utils import panel_runner as pr


class _FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def finish(self, rc=0):
        self.returncode = rc


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

    def test_failed_run_marked(self):
        pr.start_run("solidex", ["inputs/pdfs/免疫客/x.pdf"])
        self.proc.finish(1)
        self.assertEqual(pr.runs_status()["history"][0]["status"], "failed")

    def test_no_pdfs_rejected(self):
        self.assertFalse(pr.start_run("solidex", [])["ok"])

    def test_bad_line_rejected(self):
        self.assertFalse(pr.start_run("does-not-exist", ["a.pdf"])["ok"])


class TestUpload(unittest.TestCase):
    _PDF = b"%PDF-1.7\n%minimal\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_pdfs, pr.PDFS_DIR = pr.PDFS_DIR, Path(self._tmp.name)

    def tearDown(self):
        pr.PDFS_DIR = self._saved_pdfs
        self._tmp.cleanup()

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

    def test_safe_name_helper(self):
        self.assertIsNone(pr._safe_pdf_name(".pdf"))
        self.assertIsNone(pr._safe_pdf_name("a.docx"))
        self.assertEqual(pr._safe_pdf_name("dir\\sub\\论文 1.pdf"), "论文 1.pdf")


class TestNormPdfKey(unittest.TestCase):
    def test_basename_fallback_for_outside_path(self):
        self.assertEqual(pr._norm_pdf_key("C:/somewhere/else/Foo.PDF"), "foo.pdf")

    def test_abs_and_rel_under_project_match(self):
        rel = "inputs/pdfs/免疫客/免疫客文章-CAR T-2.pdf"
        ab = str(pr.PROJECT_ROOT / "inputs" / "pdfs" / "免疫客" / "免疫客文章-CAR T-2.pdf")
        self.assertEqual(pr._norm_pdf_key(rel), pr._norm_pdf_key(ab))


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


if __name__ == "__main__":
    unittest.main()
