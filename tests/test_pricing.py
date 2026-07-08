"""每篇文章成本估算（token × 单价，环境变量可覆盖）。"""
import os
import unittest

from utils import pricing


class TestPricing(unittest.TestCase):
    def setUp(self):
        for k in ("PRICE_DEEPSEEK_INPUT_CNY_PER_1M", "PRICE_DEEPSEEK_OUTPUT_CNY_PER_1M"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("PRICE_DEEPSEEK_INPUT_CNY_PER_1M", "PRICE_DEEPSEEK_OUTPUT_CNY_PER_1M"):
            os.environ.pop(k, None)

    def test_default_deepseek_rates(self):
        # 默认 输入¥2/M、输出¥8/M：1M+1M = 2 + 8 = 10
        self.assertAlmostEqual(pricing.article_cost_cny("deepseek-chat", 1_000_000, 1_000_000), 10.0)

    def test_input_heavy_article(self):
        # 实测量级：输入 28961、输出 1210 → 0.0579 + 0.0097 ≈ 0.0676
        cost = pricing.article_cost_cny("deepseek-chat", 28961, 1210)
        self.assertAlmostEqual(cost, 28961 * 2 / 1e6 + 1210 * 8 / 1e6, places=6)

    def test_env_override(self):
        os.environ["PRICE_DEEPSEEK_INPUT_CNY_PER_1M"] = "1.0"
        os.environ["PRICE_DEEPSEEK_OUTPUT_CNY_PER_1M"] = "4.0"
        self.assertAlmostEqual(pricing.article_cost_cny("deepseek-chat", 1_000_000, 1_000_000), 5.0)

    def test_zero_tokens(self):
        self.assertEqual(pricing.article_cost_cny("deepseek-chat", 0, 0), 0.0)
        self.assertEqual(pricing.article_cost_cny(None, None, None), 0.0)

    def test_unknown_model_uses_default(self):
        self.assertAlmostEqual(pricing.article_cost_cny("mystery", 1_000_000, 0), 2.0)

    def test_rate_card_shape(self):
        rc = pricing.rate_card()
        self.assertIn("deepseek-chat", rc)
        self.assertIn("input_per_1m", rc["deepseek-chat"])


if __name__ == "__main__":
    unittest.main()
