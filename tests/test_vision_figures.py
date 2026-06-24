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


def _el(x0, top, x1, bottom):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom}


class _FakePage:
    def __init__(self, images=None, rects=None, curves=None, lines=None):
        self.images, self.rects = images or [], rects or []
        self.curves, self.lines = curves or [], lines or []


class TestDominantImageBox(unittest.TestCase):
    """图文摘要 / 整版单图：裁到主导大图（含紧贴边框），绕开散落 Logo/图例点。"""

    AREA = 600.0 * 800.0

    def test_dominant_image_crops_to_image_plus_border_excludes_junk(self):
        pg = _FakePage(
            images=[_el(50, 190, 340, 480),   # 主导大图 ~17.5%
                    _el(50, 700, 66, 716)],   # 角落小徽标
            curves=[_el(48, 188, 342, 482),   # 紧贴主图的边框
                    _el(500, 700, 540, 760),  # 远处 CellPress logo（应排除）
                    _el(120, 600, 130, 610)],  # Highlights 圆点（应排除）
        )
        box = vf._dominant_image_box(pg, self.AREA)
        self.assertIsNotNone(box)
        x0, y0, x1, y1 = box
        self.assertAlmostEqual(x0, 48, delta=1)   # 含边框
        self.assertAlmostEqual(y1, 482, delta=1)
        self.assertLess(x1, 360)                   # 不被远处 logo 拉宽
        self.assertLess(y1, 500)                   # 不被底部圆点/页脚拉高

    def test_multi_panel_returns_none(self):
        pg = _FakePage(images=[_el(40, 40, 300, 300), _el(320, 40, 560, 300)])  # 两张相当大小
        self.assertIsNone(vf._dominant_image_box(pg, self.AREA))

    def test_small_images_return_none(self):
        pg = _FakePage(images=[_el(40, 40, 90, 90), _el(120, 40, 170, 90)])  # 都很小
        self.assertIsNone(vf._dominant_image_box(pg, self.AREA))

    def test_no_image_returns_none(self):
        self.assertIsNone(vf._dominant_image_box(_FakePage(), self.AREA))


class TestRuleLine(unittest.TestCase):
    """剔除「贯穿大半页的细规则线」（分栏线/页眉页脚横线），但不误伤正图内短线。"""
    W, H = 600.0, 800.0

    def test_full_width_thin_horizontal_is_rule(self):
        self.assertTrue(vf._is_rule_line(_el(40, 400, 560, 400.5), self.W, self.H))

    def test_full_height_thin_vertical_is_rule(self):
        self.assertTrue(vf._is_rule_line(_el(300, 40, 301, 760), self.W, self.H))

    def test_short_line_is_not_rule(self):
        self.assertFalse(vf._is_rule_line(_el(100, 100, 200, 100.5), self.W, self.H))

    def test_figure_box_is_not_rule(self):
        self.assertFalse(vf._is_rule_line(_el(100, 100, 500, 400), self.W, self.H))


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

    def test_recrop_when_crop_version_stale(self):
        """裁剪逻辑升级（version 陈旧）→ 命中缓存也就地重切，但不重调 VLM。"""
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2", "bbox": [0, 0, 1, 1]}
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        (Path(self.out) / ".crop_version").unlink()  # 模拟"旧裁剪产物"
        ask_calls = {"n": 0}
        vf._ask_page = lambda c, m, pil: ask_calls.__setitem__("n", ask_calls["n"] + 1) or {"has_figure": False}
        recrop = {"n": 0}
        real_make = vf._make_crop
        vf._make_crop = lambda *a, **k: recrop.__setitem__("n", recrop["n"] + 1) or real_make(*a, **k)
        try:
            figs = vf.extract_figures_via_vision("x.pdf", self.out, use_cache=True)
        finally:
            vf._make_crop = real_make
        self.assertEqual(ask_calls["n"], 0)            # 没重调 VLM
        self.assertGreater(recrop["n"], 0)             # 触发了重切
        self.assertTrue((Path(self.out) / ".crop_version").exists())
        self.assertEqual(len(figs), 1)


if __name__ == "__main__":
    unittest.main()
