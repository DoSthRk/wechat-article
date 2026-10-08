import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib import error as urllib_error

from utils import blog_notifications as notifications


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


class FeishuNotificationClientTests(unittest.TestCase):
    def setUp(self):
        self.row = SimpleNamespace(recipient_id="zxy@genemedi.net", recipient_type="email",
                                   delivery_uuid="ebd5ef2b-fbca-4a52-8df4-2f28f64268ac")
        self.env = patch.dict(os.environ, {"FEISHU_APP_ID": "test-app", "FEISHU_APP_SECRET": "test-secret",
                                          "FEISHU_BLOG_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_RECIPIENT_EMAIL": "zxy@genemedi.net",
                                          "FEISHU_BLOG_COPY_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_COPY_RECIPIENT_EMAIL": "",
                                          "FEISHU_RECIPIENT_EDM_OPS_OPEN_ID": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_message_uses_email_stable_uuid_and_requires_receipt(self):
        with patch.object(notifications.urllib_request, "urlopen", side_effect=[
            response({"code": 0, "tenant_access_token": "test-token"}),
            response({"code": 0, "data": {"message_id": "om_confirmed"}}),
        ]) as http:
            self.assertEqual(notifications.send_notice(self.row, "Blog 已发布\n中文：https://example.com"), "om_confirmed")
        token_request = http.call_args_list[0].args[0]
        self.assertEqual(json.loads(token_request.data)["app_secret"], "test-secret")
        request = http.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=email")
        payload = json.loads(request.data)
        self.assertEqual(payload["receive_id"], "zxy@genemedi.net")
        self.assertEqual(payload["uuid"], self.row.delivery_uuid)
        self.assertIn("Blog 已发布", json.loads(payload["content"])["text"])

    def test_api_rejection_sanitizes_response_and_does_not_claim_sent(self):
        with patch.object(notifications.urllib_request, "urlopen", return_value=response(
            {"code": 230013, "msg": "sensitive remote message"},
        )):
            with self.assertRaises(notifications.NotificationError) as error:
                notifications.send_notice(self.row, "Blog 已发布")
        self.assertIn("230013", str(error.exception))
        self.assertNotIn("sensitive", str(error.exception))
        self.assertFalse(error.exception.retryable)

    def test_transport_failure_is_retryable_without_credential_leak(self):
        with patch.object(notifications.urllib_request, "urlopen", side_effect=OSError("test-secret")):
            with self.assertRaises(notifications.NotificationError) as error:
                notifications.send_notice(self.row, "Blog 已发布")
        self.assertTrue(error.exception.retryable)
        self.assertNotIn("test-secret", str(error.exception))

    def test_http_429_and_503_retry_but_403_does_not(self):
        for status, retryable in ((429, True), (503, True), (403, False)):
            with self.subTest(status=status), patch.object(notifications.urllib_request, "urlopen",
                    side_effect=urllib_error.HTTPError("url", status, "sensitive", {}, None)):
                with self.assertRaises(notifications.NotificationError) as error:
                    notifications.send_notice(self.row, "text")
                self.assertEqual(error.exception.retryable, retryable)

    def test_http_rejection_retains_numeric_error_code_without_remote_body(self):
        body = io.BytesIO(json.dumps({"code": 230001, "msg": "sensitive remote detail"}).encode())
        with patch.object(notifications.urllib_request, "urlopen", side_effect=urllib_error.HTTPError(
                "url", 400, "bad request", {}, body)):
            with self.assertRaises(notifications.NotificationError) as error:
                notifications.send_notice(self.row, "text")
        self.assertIn("230001", str(error.exception))
        self.assertNotIn("sensitive", str(error.exception))

    def test_missing_credentials_do_not_make_network_call(self):
        with patch.dict(os.environ, {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}), patch.object(
                notifications.urllib_request, "urlopen") as http:
            with self.assertRaises(notifications.NotificationError):
                notifications.send_notice(self.row, "text")
        http.assert_not_called()

    def test_recipient_supports_confirmed_open_id_or_email_not_display_name_guess(self):
        with patch.dict(os.environ, {"FEISHU_BLOG_RECIPIENT_OPEN_ID": "ou_confirmed", "FEISHU_BLOG_RECIPIENT_EMAIL": ""}):
            self.assertEqual(notifications.recipient(), ("ou_confirmed", "open_id"))
        with patch.dict(os.environ, {"FEISHU_BLOG_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_RECIPIENT_EMAIL": ""}):
            with self.assertRaises(notifications.NotificationError):
                notifications.recipient()

    def test_copy_recipient_reuses_existing_default_and_deduplicates_identity(self):
        with patch.dict(os.environ, {"FEISHU_BLOG_COPY_RECIPIENT_OPEN_ID": "", "FEISHU_BLOG_COPY_RECIPIENT_EMAIL": "",
                                     "FEISHU_RECIPIENT_EDM_OPS_OPEN_ID": "ou_existing_default"}):
            self.assertEqual(notifications.recipient("self"), ("ou_existing_default", "open_id"))
            self.assertEqual(notifications.recipient_keys(), ["primary", "self"])
        with patch.dict(os.environ, {"FEISHU_BLOG_COPY_RECIPIENT_OPEN_ID": "",
                                     "FEISHU_BLOG_COPY_RECIPIENT_EMAIL": "zxy@genemedi.net",
                                     "FEISHU_BLOG_RECIPIENT_OPEN_ID": ""}):
            self.assertEqual(notifications.recipient_keys(), ["primary"])


if __name__ == "__main__":
    unittest.main()
