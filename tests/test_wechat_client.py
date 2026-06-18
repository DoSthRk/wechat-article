"""WeChatClient 多账户：账户命名空间凭据 + token 文件按账户隔离（不触网）。"""
import os
import tempfile
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


class TestAddPermanentMaterial(unittest.TestCase):
    """add_permanent_material（永久素材 → 草稿封面 media_id）：HTTP 层 mock，不触网。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _VARS}
        os.environ["WECHAT_IMMUNE_APP_ID"] = "i"
        os.environ["WECHAT_IMMUNE_APP_SECRET"] = "s"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _client(self):
        c = WeChatClient(account="immune")
        c.get_access_token = lambda *a, **k: "TKN"  # 绕过 token 刷新（不触网）
        return c

    def _tmp_img(self):
        f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        f.write(b"\xff\xd8\xff\xe0jpgbytes")
        f.close()
        return f.name

    def test_returns_media_id_on_success(self):
        c = self._client()
        c._http_post_multipart = lambda url, path: (200, '{"media_id":"M1","url":"http://x"}')
        p = self._tmp_img()
        try:
            self.assertEqual(c.add_permanent_material(p), "M1")
        finally:
            os.unlink(p)

    def test_missing_media_id_raises(self):
        c = self._client()
        c._http_post_multipart = lambda url, path: (200, '{"errcode":40004,"errmsg":"bad type"}')
        p = self._tmp_img()
        try:
            with self.assertRaises(WeChatAPIError):
                c.add_permanent_material(p)
        finally:
            os.unlink(p)

    def test_missing_file_raises(self):
        c = self._client()
        with self.assertRaises(WeChatAPIError):
            c.add_permanent_material("definitely-not-here.jpg")


class TestFreepublishBatchget(unittest.TestCase):
    """batchget_freepublish（读已发布图文）：HTTP 层 mock，不触网。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _VARS}
        os.environ["WECHAT_IMMUNE_APP_ID"] = "i"
        os.environ["WECHAT_IMMUNE_APP_SECRET"] = "s"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _client(self):
        c = WeChatClient(account="immune")
        c.get_access_token = lambda *a, **k: "TKN"
        return c

    def test_returns_data(self):
        c = self._client()
        c._http_post_json = lambda url, payload: (200, '{"item":[{"article_id":"x"}],"total_count":1}')
        d = c.batchget_freepublish(0, 20, 0)
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(len(d["item"]), 1)

    def test_error_raises(self):
        c = self._client()
        c._http_post_json = lambda url, payload: (200, '{"errcode":40164,"errmsg":"ip"}')
        with self.assertRaises(WeChatAPIError):
            c.batchget_freepublish()


if __name__ == "__main__":
    unittest.main()
