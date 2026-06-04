"""方案 B prompt 契约测试：正文零产品、结尾点名固定产品、配图来自 PDF。"""
import unittest
from pathlib import Path

from core.main import ArticleAnalyzer
from utils.job_loader import Job
from utils.product_loader import Product
from utils.template_loader import StyleTemplate

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _template() -> StyleTemplate:
    return StyleTemplate(
        template_id="t", name="测试模板", description="d",
        target_length={"min": 800, "max": 2000},
        section_count={"min": 3, "max": 5},
        tone_keywords=["中立"], forbidden_phrases=["业界领先"],
        opening_guidance="o", closing_guidance="c", extra={},
    )


def _product() -> Product:
    return Product(
        product_id="p", name="测试纯化柱X", one_liner="ol",
        selling_points=["独门卖点ZZZ"], specs={"型号": "M1"},
        use_cases=["uc"], target_users=["tu"],
        forbidden_claims=["不要承诺临床级用途"],
        closing_hint="测试融入角度YYY", extra={},
    )


class TestPromptContractB(unittest.TestCase):
    def setUp(self):
        self.job = Job(
            job_id="j", pdf="x.pdf", template="t", product="p",
            line="aav", title_hint="标题方向H",
        )
        self.msg = ArticleAnalyzer._build_user_message(
            self.job, "PDF正文内容ABC", _template(), _product(),
        )

    def test_body_has_no_product_selling_points(self):
        # 方案 B：正文不喂整块产品信息（卖点 / 规格不应出现）
        self.assertNotIn("独门卖点ZZZ", self.msg)
        self.assertNotIn("## 核心卖点", self.msg)

    def test_closing_info_present(self):
        self.assertIn("测试纯化柱X", self.msg)          # 固定产品名
        self.assertIn("测试融入角度YYY", self.msg)       # closing_hint
        self.assertIn("不要承诺临床级用途", self.msg)     # 合规红线

    def test_task_forbids_product_in_body(self):
        self.assertIn("正文", self.msg)
        self.assertIn("绝不出现", self.msg)
        self.assertIn("最后一段", self.msg)

    def test_pdf_and_title_present(self):
        self.assertIn("PDF正文内容ABC", self.msg)
        self.assertIn("标题方向H", self.msg)


class TestBasePromptFiles(unittest.TestCase):
    def test_base_prompt_contract(self):
        base = (_PROMPTS / "base.system.md").read_text(encoding="utf-8")
        self.assertIn("正文", base)
        self.assertIn("零产品", base)
        self.assertIn("最后一段", base)
        self.assertIn("PDF", base)

    def test_aav_overlay_exists(self):
        overlay = (_PROMPTS / "lines" / "aav.md").read_text(encoding="utf-8")
        self.assertIn("AAV", overlay)

    def test_solidex_overlay_exists(self):
        overlay = (_PROMPTS / "lines" / "solidex.md").read_text(encoding="utf-8")
        self.assertIn("Solidex", overlay)
        self.assertIn("T 细胞", overlay)


if __name__ == "__main__":
    unittest.main()
