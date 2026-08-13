import unittest
from unittest.mock import patch

from genemedi_blog.genemedi_blog import BlogConfig, GeneMediBlogClient, build_payload


class GeneMediCmsConfigTests(unittest.TestCase):
    def test_default_target_is_blog_cms(self):
        config = BlogConfig.from_env({}, require_credentials=False)
        self.assertEqual(config.base_url, "https://blog.genemedi.com")

    def test_legacy_hub_target_is_migrated_to_blog_cms(self):
        config = BlogConfig.from_env(
            {"GENEMEDI_BLOG_BASE_URL": "https://hub.genemedi.net/"},
            require_credentials=False,
        )
        self.assertEqual(config.base_url, "https://blog.genemedi.com")

    def test_automatic_publish_payload_sets_status_true(self):
        payload = build_payload(
            {"title": "Title", "body": "<p>Body</p>", "langcode": "en"},
            operation="create",
            status_override=True,
        )
        self.assertTrue(payload["data"]["attributes"]["status"])

    def test_chinese_public_url_includes_drupal_language_prefix(self):
        client = GeneMediBlogClient(
            BlogConfig("https://blog.genemedi.com", "", "")
        )
        result = client._normalize_write_result(
            "create",
            201,
            {
                "data": {
                    "id": "article-uuid",
                    "attributes": {
                        "drupal_internal__nid": 93,
                        "langcode": "zh-hans",
                        "path": {"alias": "/blog/93-example"},
                    },
                }
            },
        )
        self.assertEqual(
            result["public_url"],
            "https://blog.genemedi.com/zh-hans/blog/93-example",
        )

    def test_chinese_update_uses_language_prefixed_jsonapi_path(self):
        client = GeneMediBlogClient(
            BlogConfig("https://blog.genemedi.com", "", "")
        )
        document = {
            "data": {
                "id": "00000000-0000-0000-0000-000000000123",
                "attributes": {
                    "drupal_internal__nid": 93,
                    "langcode": "zh-hans",
                    "path": {"alias": "/blog/93-example"},
                },
            }
        }
        with patch.object(
            client, "_request", return_value=(200, document, {})
        ) as request:
            client.update(
                "00000000-0000-0000-0000-000000000123",
                {"title": "Title", "body": "<p>Body</p>", "langcode": "zh-hans"},
                publish=True,
            )
        self.assertEqual(
            request.call_args.args[1],
            "/zh-hans/jsonapi/node/article/00000000-0000-0000-0000-000000000123",
        )


if __name__ == "__main__":
    unittest.main()
