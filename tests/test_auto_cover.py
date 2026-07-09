"""image_pool 配图 → 自动封面（永久素材）解析，不触网（fake client）。

覆盖：_load_pool_figures（按 caption 图号载入）、_auto_cover_media_id（取首图占位符
对应图传永久素材当封面）、_upload_cover_cached 的 sha 去重缓存。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from utils.job_loader import Job
from utils.pdf_figure_extractor import Figure
import batch_processor as bp


class FakeMaterialClient:
    """只实现 add_permanent_material，记录上传次数（验证缓存）。"""

    def __init__(self):
        self.uploaded = []

    def add_permanent_material(self, path, material_type="image"):
        self.uploaded.append((str(path), material_type))
        return "thumb-media-XYZ"


class TestAutoCoverFromPool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        pool = self.base / "inputs" / "image_pools" / "poolX"
        pool.mkdir(parents=True)
        (pool / "fig00.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpg-bytes")
        (pool / "figures_manifest.json").write_text(
            json.dumps({
                "figures": [
                    {"page": 1, "index": 0,
                     "path": "inputs/image_pools/poolX/fig00.jpg",
                     "width": 100, "height": 80,
                     "caption": "FIG1. Patient disposition."},
                ]
            }),
            encoding="utf-8",
        )
        # 把模块级路径重定向到临时根（_load_pool_figures / _upload_cover_cached 用）
        self._saved = (bp.PROJECT_ROOT, bp.RUNTIME_DIR, bp.ARTICLE_CONTENT_DIR)
        bp.PROJECT_ROOT = self.base
        bp.RUNTIME_DIR = self.base / "runtime"
        bp.ARTICLE_CONTENT_DIR = str(self.base / "outputs" / "jobs")
        self.job = Job(job_id="j", pdf="nope.pdf", template="t", product="p",
                       line="solidex", image_pool="poolX")

    def tearDown(self):
        bp.PROJECT_ROOT, bp.RUNTIME_DIR, bp.ARTICLE_CONTENT_DIR = self._saved
        self._tmp.cleanup()

    def test_pool_figures_loaded_by_caption_number(self):
        figs = bp._load_pool_figures(self.job)
        self.assertEqual(len(figs), 1)
        self.assertEqual(figs[0].label, "1")
        self.assertTrue(figs[0].image_path.endswith("fig00.jpg"))

    def test_no_pool_returns_empty(self):
        job = Job(job_id="j", pdf="nope.pdf", template="t", product="p", line="x", image_pool=None)
        self.assertEqual(bp._load_pool_figures(job), [])

    def test_unlabeled_pool_falls_back_not_used(self):
        # caption 全空的 pool（如 AAV）→ 图号解析为空，无法匹配占位符；_resolve_job_figures
        # 不能用它，应回落（这里无真实 PDF → 返回空），避免正文漏掉所有配图。
        pool = self.base / "inputs" / "image_pools" / "poolX"
        (pool / "figures_manifest.json").write_text(
            json.dumps({"figures": [
                {"page": 1, "index": 0, "path": "inputs/image_pools/poolX/fig00.jpg",
                 "width": 100, "height": 80, "caption": ""},
            ]}),
            encoding="utf-8",
        )
        loaded = bp._load_pool_figures(self.job)
        self.assertEqual([f.label for f in loaded], [""])      # 载入了，但无图号
        figs, _ = bp._resolve_job_figures(self.job)            # 无图号 + 无 PDF → 不用 pool
        self.assertEqual(figs, [])

    def test_render_pdf_cover_missing_pdf_returns_none(self):
        job = Job(job_id="j", pdf="does-not-exist.pdf", template="t", product="p",
                  line="x", image_pool=None)
        self.assertIsNone(bp._render_pdf_cover(job, self.base / "figs"))

    def test_auto_cover_uploads_first_figure_then_caches(self):
        client = FakeMaterialClient()
        html = "<p>[图片:Figure 1 入组与治疗流程]</p>"
        mid = bp._auto_cover_media_id(client, "immune", self.job, html)
        self.assertEqual(mid, "thumb-media-XYZ")
        self.assertEqual(len(client.uploaded), 1)
        self.assertTrue(client.uploaded[0][0].endswith("fig00.jpg"))
        # 同图二次解析 → 命中 sha 缓存，不再调上传
        mid2 = bp._auto_cover_media_id(client, "immune", self.job, html)
        self.assertEqual(mid2, "thumb-media-XYZ")
        self.assertEqual(len(client.uploaded), 1)

    def test_resolve_job_figures_uses_legend_pages_before_vision(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        legend_fig = Figure(
            label="1", is_extended=False, caption="", page=10,
            image_path=str(self.base / "inputs" / "image_pools" / "poolX" / "fig00.jpg"),
            width=100, height=80,
        )
        saved = {
            "load_pool": bp._load_pool_figures,
            "legend": bp.extract_figures_from_legend_pages,
            "env_caption": os.environ.get("CAPTION_FIGURES_ENABLED"),
            "env_vision": os.environ.get("VISION_API_KEY"),
        }
        bp._load_pool_figures = lambda job: []
        bp.extract_figures_from_legend_pages = lambda pdf_path, out_dir, max_pages=None: [legend_fig]
        os.environ["CAPTION_FIGURES_ENABLED"] = "0"
        os.environ["VISION_API_KEY"] = "would-fail-if-called"
        try:
            figs, _ = bp._resolve_job_figures(Job(
                job_id="legend", pdf=str(pdf), template="t", product="p",
                line="aav", image_pool=None,
            ))
        finally:
            bp._load_pool_figures = saved["load_pool"]
            bp.extract_figures_from_legend_pages = saved["legend"]
            if saved["env_caption"] is None:
                os.environ.pop("CAPTION_FIGURES_ENABLED", None)
            else:
                os.environ["CAPTION_FIGURES_ENABLED"] = saved["env_caption"]
            if saved["env_vision"] is None:
                os.environ.pop("VISION_API_KEY", None)
            else:
                os.environ["VISION_API_KEY"] = saved["env_vision"]
        self.assertEqual([f.label for f in figs], ["1"])


if __name__ == "__main__":
    unittest.main()
