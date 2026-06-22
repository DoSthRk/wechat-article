"""vision_figures：VLM 抽图（JSON 解析 / 启用判定 / 抽取流程），全程 mock，不触网。"""
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from utils import vision_figures as vf


class TestParseJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(vf._parse_json('{"a": 1}'), {"a": 1})

    def test_fenced_and_surrounded(self):
        self.assertEqual(vf._parse_json('讲解一下 ```json\n{"has_figure": true, "figure_number": "3"}\n``` 完毕')["figure_number"], "3")

    def test_garbage(self):
        self.assertIsNone(vf._parse_json("no json here"))


class TestVisionEnabled(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("VISION_API_KEY", "VISION_FIGURES_ENABLED")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_enabled_when_key_set(self):
        os.environ["VISION_API_KEY"] = "k"
        os.environ.pop("VISION_FIGURES_ENABLED", None)
        self.assertTrue(vf.vision_enabled())

    def test_disabled_flag_wins(self):
        os.environ["VISION_API_KEY"] = "k"
        os.environ["VISION_FIGURES_ENABLED"] = "0"
        self.assertFalse(vf.vision_enabled())

    def test_disabled_without_key(self):
        os.environ.pop("VISION_API_KEY", None)
        os.environ.pop("VISION_FIGURES_ENABLED", None)
        self.assertFalse(vf.vision_enabled())


class TestExtractFlow(unittest.TestCase):
    """mock _image_pages/_render/_client/_ask_page，验证抽取→裁剪→标号→manifest。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = self._tmp.name
        self._orig = (vf._image_pages, vf._render, vf._client, vf._ask_page)
        vf._image_pages = lambda pdf, mp: [4, 7]
        vf._render = lambda pdf, idx, scale=1.6: Image.new("RGB", (300, 300), "white")
        vf._client = lambda: object()

    def tearDown(self):
        vf._image_pages, vf._render, vf._client, vf._ask_page = self._orig
        self._tmp.cleanup()

    def test_extracts_and_labels_figure(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "Figure 2", "caption": "cap", "bbox": [0.1, 0.1, 0.9, 0.9]}
        figs = vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        labels = sorted(f.label for f in figs)
        self.assertEqual(labels, ["2"])  # 同图号去重（两页都返回图2）
        self.assertTrue(Path(figs[0].image_path).exists())
        self.assertTrue((Path(self.out) / "vision_figures_manifest.json").exists())

    def test_no_figure_returns_empty(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": False}
        self.assertEqual(vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False), [])

    def test_uses_cache(self):
        calls = {"n": 0}

        def counting(c, m, pil):
            calls["n"] += 1
            return {"has_figure": True, "figure_number": "3", "bbox": [0, 0, 1, 1]}

        vf._ask_page = counting
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        first = calls["n"]
        self.assertGreater(first, 0)
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=True)  # 命中缓存
        self.assertEqual(calls["n"], first)  # 不再调用 VLM


if __name__ == "__main__":
    unittest.main()
