"""image_pool 配图 → 自动封面（永久素材）解析，不触网（fake client）。

覆盖：_load_pool_figures（按 caption 图号载入）、_auto_cover_media_id（取首图占位符
对应图传永久素材当封面）、_upload_cover_cached 的 sha 去重缓存。
"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PIL import Image

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


class TestFigureExtractionTimeout(unittest.TestCase):
    def test_figure_page_limit_is_independent_from_text_limit(self):
        saved = os.environ.get("PDF_FIGURE_MAX_PAGES")
        os.environ.pop("PDF_FIGURE_MAX_PAGES", None)
        try:
            job = Job(
                job_id="tail-figures", pdf="paper.pdf", template="t", product="p", line="aav",
                extra={"max_pages": 20},
            )
            self.assertIsNone(bp._fig_max_pages(job))
            job.extra["figure_max_pages"] = 80
            self.assertEqual(bp._fig_max_pages(job), 80)
        finally:
            if saved is None:
                os.environ.pop("PDF_FIGURE_MAX_PAGES", None)
            else:
                os.environ["PDF_FIGURE_MAX_PAGES"] = saved

    def test_clamps_legacy_timeout_below_caption_runtime_floor(self):
        saved = os.environ.get("PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS")
        os.environ["PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS"] = "45"
        try:
            self.assertEqual(bp._figure_extract_timeout_seconds(), 75.0)
        finally:
            if saved is None:
                os.environ.pop("PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS", None)
            else:
                os.environ["PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS"] = saved

    def test_vision_strategy_has_a_larger_budget_than_deterministic_extractors(self):
        saved = os.environ.get("PDF_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS")
        os.environ.pop("PDF_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS", None)
        try:
            self.assertEqual(bp._figure_strategy_timeout_seconds("caption"), 75.0)
            self.assertEqual(bp._figure_strategy_timeout_seconds("vision"), 180.0)
        finally:
            if saved is None:
                os.environ.pop("PDF_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS", None)
            else:
                os.environ["PDF_VISION_FIGURE_EXTRACT_TIMEOUT_SECONDS"] = saved


class TestAutoCoverFromPool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        pool = self.base / "inputs" / "image_pools" / "poolX"
        pool.mkdir(parents=True)
        Image.new("RGB", (900, 383), "blue").save(pool / "fig00.jpg")
        self.pdf = self.base / "source.pdf"
        Image.new("RGB", (600, 800), "white").save(self.pdf, "PDF")
        (pool / "figures_manifest.json").write_text(
            json.dumps({
                "source_sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
                "figures": [
                    {"page": 1, "index": 0,
                     "path": "inputs/image_pools/poolX/fig00.jpg",
                     "width": 100, "height": 80,
                     "reviewed": True,
                     "sha256": hashlib.sha256((pool / "fig00.jpg").read_bytes()).hexdigest(),
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
        self.job = Job(job_id="j", pdf=str(self.pdf), template="t", product="p",
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

    def test_pool_requires_source_binding_review_and_unchanged_image(self):
        manifest = self.base / "inputs/image_pools/poolX/figures_manifest.json"
        original = json.loads(manifest.read_text())
        for mutation in ("wrong_pdf", "unbound", "unreviewed", "changed_image"):
            with self.subTest(mutation=mutation):
                data = json.loads(json.dumps(original))
                if mutation == "wrong_pdf":
                    data["source_sha256"] = "0" * 64
                elif mutation == "unbound":
                    del data["source_sha256"]
                elif mutation == "unreviewed":
                    data["figures"][0].pop("reviewed")
                else:
                    data["figures"][0]["sha256"] = "0" * 64
                manifest.write_text(json.dumps(data))
                self.assertEqual(bp._load_pool_figures(self.job), [])

    def test_arbitrary_fig_filename_cannot_bypass_accepted_candidates(self):
        directory = self.base / "figures"
        directory.mkdir()
        (directory / "fig1.jpg").write_bytes(b"old unrelated image")
        self.assertIsNone(bp._resolve_figure_path("Figure 1", directory, []))

    def test_main_and_supplementary_figure_must_not_be_confused(self):
        main = Figure("1", False, "", 1, "main.jpg", 100, 100)
        supplementary = Figure("1", True, "", 2, "supplementary.jpg", 100, 100)
        self.assertEqual(bp._resolve_figure_path("Figure 1 supplementary panel", self.base, [main, supplementary]),
                         "supplementary.jpg")

    def test_source_changed_since_generation_blocks_before_extract_or_upload(self):
        content = Path(bp.ARTICLE_CONTENT_DIR) / self.job.job_id
        content.mkdir(parents=True)
        (content / "meta.json").write_text(json.dumps({"source_pdf_sha256": "0" * 64}))
        with patch.object(bp, "_run_pdf_figure_worker") as worker:
            with self.assertRaisesRegex(ValueError, "原文 PDF 已变化"):
                bp._resolve_job_figures(self.job, {("1", False)})
        worker.assert_not_called()

    def test_generation_records_the_pdf_content_hash(self):
        from core.main import AnalysisResult
        analyzer = Mock()
        analyzer.analyze.return_value = AnalysisResult(
            job_id=self.job.job_id, success=True, markdown="# 标题\n\n正文。", model="fake",
        )
        self.assertTrue(bp._generate_one(Mock(), 1, self.job, analyzer))
        metadata = json.loads((Path(bp.ARTICLE_CONTENT_DIR) / self.job.job_id / "meta.json").read_text())
        self.assertEqual(metadata["source_pdf_sha256"], hashlib.sha256(self.pdf.read_bytes()).hexdigest())

    def test_generation_rejects_pdf_changed_while_analyzing(self):
        from core.main import AnalysisResult
        def changed_source(_job):
            self.pdf.write_bytes(b"replacement PDF")
            return AnalysisResult(job_id=self.job.job_id, success=True, markdown="# 标题", model="fake")
        analyzer = Mock()
        analyzer.analyze.side_effect = changed_source
        db = Mock()
        self.assertFalse(bp._generate_one(db, 1, self.job, analyzer))
        db.upsert_article.assert_not_called()
        self.assertFalse((Path(bp.ARTICLE_CONTENT_DIR) / self.job.job_id / "article.md").exists())

    def test_legacy_source_binding_is_explicit_in_resolution_report(self):
        figures, directory = bp._resolve_job_figures(self.job, {("1", False)})
        self.assertEqual(len(figures), 1)
        report = json.loads((directory / "figure_resolution_report.json").read_text())
        self.assertEqual(report["article_source_binding"], "legacy_unbound")
        self.assertEqual(report["selected"][0]["source_sha256"], hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        self.assertEqual(report["quality_version"], bp.QUALITY_VERSION)

    def test_rejected_candidate_does_not_satisfy_coverage_and_fallback_is_used(self):
        image = self.base / "inputs/image_pools/poolX/fig00.jpg"
        candidate = Figure("1", False, "", 1, str(image), 900, 383)
        job = Job(job_id="rejected", pdf=str(self.pdf), template="t", product="p")
        signals = bp.FigureSignals(frozenset({("1", False)}), False, 1)
        rejection = {"label": "1", "reason": "caption_or_body_text_in_crop"}
        with patch.object(bp, "_inspect_pdf_figure_signals", return_value=signals), \
             patch.object(bp, "planned_figure_strategies", return_value=("caption", "heuristic")), \
             patch.object(bp, "_run_pdf_figure_worker", return_value=[candidate]) as worker, \
             patch.object(bp, "filter_candidates", side_effect=[([], [rejection]), ([candidate], [])]), \
             patch.dict(os.environ, {"CAPTION_FIGURES_ENABLED": "1"}):
            figures, directory = bp._resolve_job_figures(job, {("1", False)})
        self.assertEqual([call.args[0] for call in worker.call_args_list], ["caption", "heuristic"])
        self.assertEqual(figures, [candidate])
        report = json.loads((directory / "figure_resolution_report.json").read_text())
        self.assertEqual(report["attempts"][1]["status"], "rejected")
        self.assertEqual(report["attempts"][1]["rejected"], [rejection])

    def test_cross_strategy_identical_images_do_not_satisfy_distinct_figures(self):
        image = self.base / "inputs/image_pools/poolX/fig00.jpg"
        first = Figure("1", False, "", 1, str(image), 900, 383)
        second = Figure("2", False, "", 2, str(image), 900, 383)
        figures, rejected = bp._merge_accepted_figures([first], [second])
        self.assertEqual(figures, [])
        self.assertEqual(len(rejected), 2)

    def test_unlabeled_pool_falls_back_not_used(self):
        # caption 全空的 pool（如 AAV）→ 图号解析为空，无法匹配占位符；_resolve_job_figures
        # 不能用它，应回落（这里无真实 PDF → 返回空），避免正文漏掉所有配图。
        pool = self.base / "inputs" / "image_pools" / "poolX"
        data = json.loads((pool / "figures_manifest.json").read_text())
        data["figures"][0]["caption"] = ""
        (pool / "figures_manifest.json").write_text(json.dumps(data), encoding="utf-8")
        loaded = bp._load_pool_figures(self.job)
        self.assertEqual([f.label for f in loaded], [""])      # 载入了，但无图号
        figs, _ = bp._resolve_job_figures(self.job)            # 无图号 + 无 PDF → 不用 pool
        self.assertEqual(figs, [])

    def test_no_valid_figure_does_not_use_pdf_first_page_as_cover(self):
        client = FakeMaterialClient()
        media = bp._auto_cover_media_id(client, "immune", self.job, "<p>正文</p>",
                                       resolved=([], self.base / "figs"))
        self.assertEqual(media, "")
        self.assertEqual(client.uploaded, [])

    def test_cover_letterboxes_instead_of_clipping_panels(self):
        source = self.base / "tall.png"
        image = Image.new("RGB", (300, 600), "red")
        image.paste("blue", (0, 550, 300, 600))
        image.save(source)
        cover_path = bp._fit_wechat_cover(source, self.base / "figs")
        with Image.open(cover_path) as cover:
            self.assertEqual(cover.size, (900, 383))
            self.assertEqual(cover.getpixel((0, 100)), (255, 255, 255))
            self.assertGreater(cover.getpixel((450, 5))[0], 240)
            self.assertGreater(cover.getpixel((450, 378))[2], 240)

    def test_invalid_cover_is_not_uploaded(self):
        source = self.base / "broken.jpg"
        source.write_bytes(b"not an image")
        self.assertIsNone(bp._fit_wechat_cover(source, self.base / "figs"))

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

    def test_auto_cover_fits_article_figure_to_wechat_headline_dimensions(self):
        source = self.base / "source-figure.jpg"
        Image.new("RGB", (879, 827), "white").save(source)
        figures_dir = self.base / "outputs" / "jobs" / self.job.job_id / "figures"
        figure = Figure(
            label="1", is_extended=False, caption="", page=4,
            image_path=str(source), width=879, height=827,
        )
        saved = bp._resolve_job_figures
        bp._resolve_job_figures = lambda job: ([figure], figures_dir)
        try:
            client = FakeMaterialClient()
            media_id = bp._auto_cover_media_id(
                client, "immune", self.job, "<p>[图片:Figure 1 机制图]</p>",
            )
        finally:
            bp._resolve_job_figures = saved

        self.assertEqual(media_id, "thumb-media-XYZ")
        uploaded_path = Path(client.uploaded[0][0])
        self.assertNotEqual(uploaded_path, source)
        with Image.open(uploaded_path) as cover:
            self.assertEqual(cover.size, (900, 383))

    def test_auto_cover_uses_pre_resolved_figures_without_second_extraction(self):
        source = self.base / "pre-resolved.jpg"
        Image.new("RGB", (640, 480), "white").save(source)
        figure = Figure(
            label="1", is_extended=False, caption="", page=4,
            image_path=str(source), width=640, height=480,
        )
        saved = bp._resolve_job_figures
        bp._resolve_job_figures = lambda job: self.fail("should reuse resolved figures")
        try:
            client = FakeMaterialClient()
            media_id = bp._auto_cover_media_id(
                client, "immune", self.job, "<p>[图片:Figure 1 机制图]</p>",
                resolved=([figure], self.base / "figures"),
            )
        finally:
            bp._resolve_job_figures = saved

        self.assertEqual(media_id, "thumb-media-XYZ")
        self.assertEqual(len(client.uploaded), 1)

    def test_resolve_job_figures_uses_legend_worker_before_vision(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        legend_fig = Figure(
            label="1", is_extended=False, caption="", page=10,
            image_path=str(self.base / "inputs" / "image_pools" / "poolX" / "fig00.jpg"),
            width=100, height=80,
        )
        saved = {
            "load_pool": bp._load_pool_figures,
            "worker": bp._run_pdf_figure_worker,
            "filter": bp.filter_candidates,
            "env_caption": os.environ.get("CAPTION_FIGURES_ENABLED"),
            "env_vision": os.environ.get("VISION_API_KEY"),
        }
        calls = []
        bp._load_pool_figures = lambda job, **kwargs: []
        bp.filter_candidates = lambda pdf, figures, strategy: (figures, [])

        def fake_worker(kind, job, figures_dir):
            calls.append(kind)
            if kind == "legend":
                return [legend_fig]
            raise AssertionError(f"unexpected extractor {kind}")

        bp._run_pdf_figure_worker = fake_worker
        os.environ["CAPTION_FIGURES_ENABLED"] = "0"
        os.environ["VISION_API_KEY"] = "would-fail-if-called"
        try:
            figs, _ = bp._resolve_job_figures(Job(
                job_id="legend", pdf=str(pdf), template="t", product="p",
                line="aav", image_pool=None,
            ))
        finally:
            bp._load_pool_figures = saved["load_pool"]
            bp._run_pdf_figure_worker = saved["worker"]
            bp.filter_candidates = saved["filter"]
            if saved["env_caption"] is None:
                os.environ.pop("CAPTION_FIGURES_ENABLED", None)
            else:
                os.environ["CAPTION_FIGURES_ENABLED"] = saved["env_caption"]
            if saved["env_vision"] is None:
                os.environ.pop("VISION_API_KEY", None)
            else:
                os.environ["VISION_API_KEY"] = saved["env_vision"]
        self.assertEqual([f.label for f in figs], ["1"])
        self.assertEqual(calls, ["legend"])

    def test_resolve_job_figures_uses_fallback_only_for_missing_labels(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        fig1 = Figure(
            label="1", is_extended=False, caption="", page=4,
            image_path=str(self.base / "fig1.jpg"), width=300, height=200,
        )
        fig2 = Figure(
            label="2", is_extended=False, caption="", page=5,
            image_path=str(self.base / "fig2.jpg"), width=300, height=200,
        )
        Path(fig1.image_path).write_bytes(b"first figure")
        Path(fig2.image_path).write_bytes(b"second figure")
        saved = {
            "load_pool": bp._load_pool_figures,
            "worker": bp._run_pdf_figure_worker,
            "filter": bp.filter_candidates,
            "signals": getattr(bp, "_inspect_pdf_figure_signals", None),
            "env_caption": os.environ.get("CAPTION_FIGURES_ENABLED"),
            "env_vision": os.environ.get("VISION_API_KEY"),
        }
        calls = []
        bp._load_pool_figures = lambda job, **kwargs: []
        bp.filter_candidates = lambda pdf, figures, strategy: (figures, [])
        bp._inspect_pdf_figure_signals = lambda pdf, max_pages=None: type(
            "Signals", (), {"caption_keys": frozenset({("1", False)}),
                              "has_legend_section": False, "page_count": 5,
                              "scan_error": ""},
        )()

        def fake_worker(kind, job, figures_dir, timeout_seconds=None):
            calls.append(kind)
            return {"caption": [fig1], "vision": [fig2]}[kind]

        bp._run_pdf_figure_worker = fake_worker
        os.environ["CAPTION_FIGURES_ENABLED"] = "1"
        os.environ["VISION_API_KEY"] = "would-fail-if-called"
        try:
            figs, _ = bp._resolve_job_figures(
                Job(job_id="coverage", pdf=str(pdf), template="t", product="p", line="aav"),
                required_keys={("1", False), ("2", False)},
            )
        finally:
            bp._load_pool_figures = saved["load_pool"]
            bp._run_pdf_figure_worker = saved["worker"]
            bp.filter_candidates = saved["filter"]
            if saved["signals"] is None:
                delattr(bp, "_inspect_pdf_figure_signals")
            else:
                bp._inspect_pdf_figure_signals = saved["signals"]
            if saved["env_caption"] is None:
                os.environ.pop("CAPTION_FIGURES_ENABLED", None)
            else:
                os.environ["CAPTION_FIGURES_ENABLED"] = saved["env_caption"]
            if saved["env_vision"] is None:
                os.environ.pop("VISION_API_KEY", None)
            else:
                os.environ["VISION_API_KEY"] = saved["env_vision"]

        self.assertEqual([f.label for f in figs], ["1", "2"])
        self.assertEqual(calls, ["caption", "vision"])

    def test_figure_cache_invalidates_generated_files_when_pdf_content_changes(self):
        pdf = self.base / "reuploaded.pdf"
        pdf.write_bytes(b"first version")
        job = Job(job_id="cache", pdf=str(pdf), template="t", product="p", line="aav")
        figures_dir = self.base / "outputs" / "jobs" / job.job_id / "figures"
        bp._prepare_figure_cache(job, figures_dir)
        generated = figures_dir / "cap1_p4.jpg"
        manual = figures_dir / "fig1.jpg"
        generated.write_bytes(b"generated")
        manual.write_bytes(b"manual")

        pdf.write_bytes(b"second version")
        source = bp._prepare_figure_cache(job, figures_dir)

        self.assertFalse(generated.exists())
        self.assertTrue(manual.exists())
        self.assertEqual(source["sha256"], hashlib.sha256(b"second version").hexdigest())

    def test_pdf_figure_worker_timeout_returns_empty(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        saved = {
            "run": bp.subprocess.run,
            "env_timeout": os.environ.get("PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS"),
        }

        def timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

        bp.subprocess.run = timeout_run
        os.environ["PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS"] = "0.1"
        try:
            with self.assertLogs("batch_processor", level="WARNING") as logs:
                figs = bp._run_pdf_figure_worker(
                    "legend",
                    Job(job_id="slow", pdf=str(pdf), template="t", product="p", line="aav"),
                    self.base / "outputs" / "jobs" / "slow" / "figures",
                )
        finally:
            bp.subprocess.run = saved["run"]
            if saved["env_timeout"] is None:
                os.environ.pop("PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS", None)
            else:
                os.environ["PDF_FIGURE_EXTRACT_TIMEOUT_SECONDS"] = saved["env_timeout"]
        self.assertEqual(figs, [])
        self.assertIn("抽图超时", "\n".join(logs.output))

    def test_apply_figures_rejects_missing_placeholders_before_upload(self):
        saved = bp._resolve_job_figures
        bp._resolve_job_figures = lambda job: ([], self.base / "figures")
        try:
            with self.assertRaisesRegex(ValueError, "缺少经校验的配图"):
                bp._apply_figures(
                    "<p>[图片:Figure 1 机制图]</p><p>[图片:Figure 2 统计图]</p>",
                    Job(job_id="nofig", pdf="missing.pdf", template="t", product="p", line="aav"),
                    object(),
                    "immune",
                )
        finally:
            bp._resolve_job_figures = saved

    def test_pdf_auto_figures_can_be_disabled(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        saved = {
            "load_pool": bp._load_pool_figures,
            "worker": bp._run_pdf_figure_worker,
            "env_pdf": os.environ.get("PDF_AUTO_FIGURES_ENABLED"),
            "env_caption": os.environ.get("CAPTION_FIGURES_ENABLED"),
            "env_vision": os.environ.get("VISION_API_KEY"),
        }
        bp._load_pool_figures = lambda job, **kwargs: []
        worker = Mock(side_effect=AssertionError("extraction should be skipped"))
        bp._run_pdf_figure_worker = worker
        os.environ["PDF_AUTO_FIGURES_ENABLED"] = "0"
        os.environ["CAPTION_FIGURES_ENABLED"] = "0"
        os.environ["VISION_API_KEY"] = "would-fail-if-called"
        try:
            figs, figures_dir = bp._resolve_job_figures(Job(
                job_id="skip-pdf", pdf=str(pdf), template="t", product="p",
                line="solidex", image_pool=None,
            ))
        finally:
            bp._load_pool_figures = saved["load_pool"]
            bp._run_pdf_figure_worker = saved["worker"]
            for key, env_name in (
                ("env_pdf", "PDF_AUTO_FIGURES_ENABLED"),
                ("env_caption", "CAPTION_FIGURES_ENABLED"),
                ("env_vision", "VISION_API_KEY"),
            ):
                value = saved[key]
                if value is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = value
        self.assertEqual(figs, [])
        self.assertEqual(figures_dir.name, "figures")
        self.assertEqual(figures_dir.parent.name, "skip-pdf")
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
