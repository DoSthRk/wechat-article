import unittest

from genemedi_blog.genemedi_blog import BlogConfig, build_payload


class GeneMediCmsConfigTests(unittest.TestCase):
    def test_default_target_is_hub_cms(self):
        config = BlogConfig.from_env({}, require_credentials=False)
        self.assertEqual(config.base_url, "https://hub.genemedi.net")

    def test_automatic_publish_payload_sets_status_true(self):
        payload = build_payload(
            {"title": "Title", "body": "<p>Body</p>", "langcode": "en"},
            operation="create",
            status_override=True,
        )
        self.assertTrue(payload["data"]["attributes"]["status"])


if __name__ == "__main__":
    unittest.main()
