"""tonal_qa 静态扫描单元测试。"""
import tempfile
import unittest
from pathlib import Path

from utils.tonal_qa import load_hard_ad_words, scan_static


class TestTonalQA(unittest.TestCase):
    def test_clean_text_full_score(self):
        r = scan_static("# 标题\n\n中立、克制的科普正文。", ["业界领先", "碾压"])
        self.assertEqual(r.score, 100)
        self.assertFalse(r.blocked)

    def test_hard_ad_hits_deduct(self):
        r = scan_static("这是业界领先的碾压级方案", ["业界领先", "碾压", "秒杀"])
        self.assertEqual(set(r.hard_ad_hits), {"业界领先", "碾压"})
        self.assertEqual(r.score, 80)  # 命中 2 个，扣 20
        self.assertFalse(r.blocked)    # 80 >= 60

    def test_blocked_when_many_hits(self):
        words = ["业界领先", "碾压", "秒杀", "神器", "震惊"]
        r = scan_static(" ".join(words), words)  # 5 命中 -> 50 < 60
        self.assertEqual(r.score, 50)
        self.assertTrue(r.blocked)

    def test_body_product_leak_blocks(self):
        md = "# 标题\n\n正文中段就提到了 PurProX 这个产品。\n\n结尾段。"
        r = scan_static(md, [], product_name="PurProX")
        self.assertTrue(r.body_product_leak)
        self.assertTrue(r.blocked)

    def test_product_only_in_closing_is_ok(self):
        md = "# 标题\n\n纯科普正文，不含任何产品。\n\n结尾自然提及 PurProX 产品。"
        r = scan_static(md, [], product_name="PurProX")
        self.assertFalse(r.body_product_leak)
        self.assertFalse(r.blocked)
        self.assertEqual(r.score, 100)

    def test_load_hard_ad_words(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.txt"
            p.write_text("# 注释\n业界领先\n\n碾压\n", encoding="utf-8")
            self.assertEqual(load_hard_ad_words(str(p)), ["业界领先", "碾压"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_hard_ad_words("/no/such/file.txt"), [])


if __name__ == "__main__":
    unittest.main()
