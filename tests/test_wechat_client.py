"""WeChatClient 多账户：账户命名空间凭据 + token 文件按账户隔离（不触网）。"""
import os
import unittest

from utils.wechat_client import WeChatAPIError, WeChatClient

_VARS = [
    "WECHAT_IMMUNE_APP_ID", "WECHAT_IMMUNE_APP_SECRET",
    "WECHAT_AAV_APP_ID", "WECHAT_AAV_APP_SECRET",
    "WECHAT_APP_ID", "WECHAT_APP_SECRET",
]


class TestWeChatClientAccounts(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _VARS}
        for k in _VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_account_namespaced_creds_and_isolated_cache(self):
        os.environ["WECHAT_IMMUNE_APP_ID"] = "id_immune"
        os.environ["WECHAT_IMMUNE_APP_SECRET"] = "sec_immune"
        c = WeChatClient(account="immune")
        self.assertEqual(c.app_id, "id_immune")
        self.assertEqual(c.account, "immune")
        self.assertTrue(str(c.token_cache_path).endswith("wechat_token_immune.json"))

    def test_two_accounts_have_isolated_token_files(self):
        os.environ.update({
            "WECHAT_IMMUNE_APP_ID": "i", "WECHAT_IMMUNE_APP_SECRET": "i",
            "WECHAT_AAV_APP_ID": "a", "WECHAT_AAV_APP_SECRET": "a",
        })
        ci = WeChatClient(account="immune")
        ca = WeChatClient(account="aav")
        self.assertNotEqual(str(ci.token_cache_path), str(ca.token_cache_path))
        self.assertEqual(ci.app_id, "i")
        self.assertEqual(ca.app_id, "a")

    def test_fallback_to_generic_creds(self):
        os.environ["WECHAT_APP_ID"] = "g_id"
        os.environ["WECHAT_APP_SECRET"] = "g_sec"
        c = WeChatClient(account="default")
        self.assertEqual(c.app_id, "g_id")

    def test_missing_creds_raises(self):
        with self.assertRaises(WeChatAPIError):
            WeChatClient(account="nope")

    def test_explicit_args_win(self):
        c = WeChatClient(app_id="x", app_secret="y", account="custom")
        self.assertEqual(c.app_id, "x")
        self.assertTrue(str(c.token_cache_path).endswith("wechat_token_custom.json"))


if __name__ == "__main__":
    unittest.main()
