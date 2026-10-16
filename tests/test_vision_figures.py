"""vision_figures：VLM 抽图（JSON 解析 / 启用判定 / 抽取流程），全程 mock，不触网。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from utils import vision_figures as vf
from utils.figure_crop_geometry import UnsafeFigureCrop


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


class TestSafeVisionCrop(unittest.TestCase):
    def setUp(self):
        self.page = Image.new("RGB", (600, 800), "white")

    def test_model_box_is_not_overwritten_by_unrelated_page_graphics(self):
        self.page.paste("red", (120, 160, 480, 480))
        with patch.object(vf, "_graphics_crop_box", return_value=(0, 0, 375, 500)) as graphics:
            crop = vf._make_crop("paper.pdf", 0, self.page, [0.2, 0.2, 0.8, 0.6])
        self.assertEqual(crop.size, (360, 320))
        self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
        graphics.assert_not_called()

    def test_invalid_bbox_never_returns_full_page(self):
        invalid = [None, [], [0, 0, 1], "0,0,1,1", [0, 0, 1, 1],
                   [0, 0, 0.99, 0.99], [-0.1, 0.1, 0.9, 0.9],
                   [0.1, 0.1, 1.1, 0.9], [0.8, 0.1, 0.2, 0.9],
                   [0.1, 0.1, 0.11, 0.9], [float("nan"), 0.1, 0.9, 0.9],
                   [0.1, 0.1, float("inf"), 0.9], [False, 0.1, 0.9, 0.9]]
        for bbox in invalid:
            with self.subTest(bbox=bbox), self.assertRaises(UnsafeFigureCrop):
                vf._make_crop("paper.pdf", 0, self.page, bbox)


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
        self.assertEqual(labels, ["2", "2"])  # Preserve ambiguity for the shared gate; never choose the first page.
        self.assertTrue(Path(figs[0].image_path).exists())
        self.assertTrue((Path(self.out) / "vision_figures_manifest.json").exists())
        self.assertEqual(figs[0].bbox, (18.75, 18.75, 168.75, 168.75))

    def test_no_figure_returns_empty(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": False}
        self.assertEqual(vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False), [])

    def test_uses_cache(self):
        calls = {"n": 0}

        def counting(c, m, pil):
            calls["n"] += 1
            return {"has_figure": True, "figure_number": "3", "bbox": [0.1, 0.1, 0.9, 0.9]}

        vf._ask_page = counting
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        first = calls["n"]
        self.assertGreater(first, 0)
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=True)  # 命中缓存
        self.assertEqual(calls["n"], first)  # 不再调用 VLM

    def test_redetect_when_crop_version_stale(self):
        """旧缓存的图号和页码不可信，升级时必须重新识别。"""
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2", "bbox": [0.1, 0.1, 0.9, 0.9]}
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        (Path(self.out) / ".crop_version").unlink()  # 模拟"旧裁剪产物"
        ask_calls = {"n": 0}
        vf._ask_page = lambda c, m, pil: ask_calls.__setitem__("n", ask_calls["n"] + 1) or {"has_figure": False}
        with patch.object(vf, "_make_crop") as crop:
            figs = vf.extract_figures_via_vision("x.pdf", self.out, use_cache=True)
        self.assertEqual(ask_calls["n"], 2)
        crop.assert_not_called()
        self.assertTrue((Path(self.out) / ".crop_version").exists())
        self.assertEqual(figs, [])

    def test_stale_cache_is_not_blessed_after_detection_failure(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2", "bbox": [0.1, 0.1, 0.9, 0.9]}
        vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        version = Path(self.out) / ".crop_version"
        version.write_text("0")
        original_manifest = (Path(self.out) / "vision_figures_manifest.json").read_bytes()
        with patch.object(vf, "_ask_page", side_effect=RuntimeError("service unavailable")):
            self.assertEqual(vf.extract_figures_via_vision("x.pdf", self.out), [])
        self.assertEqual(version.read_text(), "0")
        self.assertEqual((Path(self.out) / "vision_figures_manifest.json").read_bytes(), original_manifest)

    def test_bad_model_bbox_does_not_create_success_cache(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2"}
        with self.assertRaises(UnsafeFigureCrop):
            vf.extract_figures_via_vision("x.pdf", self.out, use_cache=False)
        self.assertFalse((Path(self.out) / ".crop_version").exists())
        self.assertFalse((Path(self.out) / "vision_figures_manifest.json").exists())

    def test_missing_cached_file_triggers_fresh_detection(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2", "bbox": [0.1, 0.1, 0.9, 0.9]}
        first = vf.extract_figures_via_vision("x.pdf", self.out)
        Path(first[0].image_path).unlink()
        with patch.object(vf, "_ask_page", return_value={"has_figure": False}) as ask:
            self.assertEqual(vf.extract_figures_via_vision("x.pdf", self.out), [])
        self.assertEqual(ask.call_count, 2)

    def test_ambiguous_model_label_is_not_merged_into_another_number(self):
        vf._ask_page = lambda c, m, pil: {"has_figure": True, "figure_number": "2 and 3", "bbox": [0.1, 0.1, 0.9, 0.9]}
        self.assertEqual(vf.extract_figures_via_vision("x.pdf", self.out), [])


if __name__ == "__main__":
    unittest.main()
