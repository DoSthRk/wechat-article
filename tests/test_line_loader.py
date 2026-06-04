"""line_loader 单元测试。"""
import tempfile
import unittest
from pathlib import Path

from utils.line_loader import LineLoadError, load_line_by_id


def _write(dir_: str, name: str, text: str) -> None:
    (Path(dir_) / name).write_text(text, encoding="utf-8")


class TestLineLoader(unittest.TestCase):
    def test_loads_valid_line(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "aav.yaml",
                   "line_id: aav\nname: AAV线\ntemplate: t1\nproduct: p1\nprompt_overlay: aav\n")
            line = load_line_by_id(d, "aav")
            self.assertEqual(line.line_id, "aav")
            self.assertEqual(line.template, "t1")
            self.assertEqual(line.product, "p1")
            self.assertEqual(line.prompt_overlay, "aav")
            self.assertEqual(line.status, "active")

    def test_prompt_overlay_defaults_to_line_id(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "aav.yaml", "name: AAV线\ntemplate: t1\nproduct: p1\n")
            line = load_line_by_id(d, "aav")
            self.assertEqual(line.prompt_overlay, "aav")

    def test_missing_template_or_product_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "bad.yaml", "name: x\n")  # 无 template / product
            with self.assertRaises(LineLoadError):
                load_line_by_id(d, "bad")

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(LineLoadError):
                load_line_by_id(d, "nope")


class TestCommittedLineConfigs(unittest.TestCase):
    """加载真实提交进仓库的 line 配置，确保不坏。"""
    LINES_DIR = str(Path(__file__).resolve().parent.parent / "inputs" / "lines")

    def test_aav(self):
        line = load_line_by_id(self.LINES_DIR, "aav")
        self.assertEqual(line.template, "aav_academic_soft_intro")
        self.assertEqual(line.product, "purprox_aaveasy_spin_columns")
        self.assertEqual(line.prompt_overlay, "aav")

    def test_solidex(self):
        line = load_line_by_id(self.LINES_DIR, "solidex")
        self.assertEqual(line.template, "oncology_wechat_pop_sci")
        self.assertEqual(line.product, "solidex_pan_t_cell_iso_kit")
        self.assertEqual(line.prompt_overlay, "solidex")


if __name__ == "__main__":
    unittest.main()
