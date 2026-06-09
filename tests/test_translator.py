"""``utils.translator`` 单元测试（中文源 → en/ja/ko/ru）。

用注入的假 LLM 客户端，覆盖：成功翻译、不支持语言、空输入、失败重试、
重试耗尽、最外层代码围栏剥除、术语表/不翻译名单注入、thinking 开关。
"""
import os
import unittest

from utils import translator
from utils.translator import _build_user_message, _glossary_block, translate_markdown


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, total_tokens):
        self.total_tokens = total_tokens


class _FakeResponse:
    def __init__(self, content, total_tokens=120):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(total_tokens)


class _FakeClient:
    """最小假客户端：client.chat.completions.create 按序返回预设响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


class TranslateMarkdownTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TRANSLATION_THINKING_DISABLED", None)

    def tearDown(self):
        os.environ.pop("TRANSLATION_THINKING_DISABLED", None)

    def test_successful_translation(self):
        client = _FakeClient(["# Title\n\nTranslated body."])
        result = translate_markdown("# 标题\n\n正文。", "en", client=client)
        self.assertTrue(result.success)
        self.assertEqual(result.translated_markdown, "# Title\n\nTranslated body.")
        self.assertEqual(result.lang, "en")
        self.assertEqual(result.total_tokens, 120)

    def test_unsupported_lang(self):
        result = translate_markdown("# 标题", "fr", client=_FakeClient(["x"]))
        self.assertFalse(result.success)
        self.assertIn("unsupported", result.error)

    def test_chinese_is_not_a_target(self):
        # 中文是源，不能作为翻译目标
        result = translate_markdown("# 标题", "zh", client=_FakeClient(["x"]))
        self.assertFalse(result.success)
        self.assertIn("unsupported", result.error)

    def test_empty_source(self):
        result = translate_markdown("   ", "en", client=_FakeClient(["x"]))
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)

    def test_retry_then_success(self):
        client = _FakeClient([RuntimeError("api down"), "# OK\nretried."])
        result = translate_markdown("# T", "ja", client=client, max_retries=2)
        self.assertTrue(result.success)
        self.assertEqual(result.translated_markdown, "# OK\nretried.")
        self.assertEqual(len(client.calls), 2)

    def test_all_retries_fail(self):
        client = _FakeClient([RuntimeError("e1"), RuntimeError("e2"), RuntimeError("e3")])
        result = translate_markdown("# T", "ko", client=client, max_retries=2)
        self.assertFalse(result.success)
        self.assertEqual(len(client.calls), 3)  # max_retries=2 -> 3 次尝试
        self.assertIn("e3", result.error)

    def test_strips_outer_code_fence(self):
        client = _FakeClient(["```markdown\n# Title\n\nBody\n```"])
        result = translate_markdown("# 标题\n\n正文", "en", client=client)
        self.assertTrue(result.success)
        self.assertEqual(result.translated_markdown, "# Title\n\nBody")

    def test_empty_output_treated_as_failure(self):
        client = _FakeClient(["   ", "   "])
        result = translate_markdown("# T", "ru", client=client, max_retries=1)
        self.assertFalse(result.success)

    def test_thinking_param_off_by_default(self):
        client = _FakeClient(["译文"])
        translate_markdown("# T", "en", client=client)
        self.assertNotIn("extra_body", client.calls[0])

    def test_thinking_param_via_env(self):
        os.environ["TRANSLATION_THINKING_DISABLED"] = "true"
        client = _FakeClient(["译文"])
        translate_markdown("# T", "en", client=client)
        extra = client.calls[0].get("extra_body", {})
        self.assertEqual(extra.get("thinking", {}).get("type"), "disabled")


class AssetInjectionTests(unittest.TestCase):
    def test_glossary_block_en_has_known_term(self):
        block = _glossary_block("en")
        self.assertIn("药诺生物 => GeneMedi", block)

    def test_user_message_contains_glossary_dnt_and_source(self):
        msg = _build_user_message("en", "# 标题\n\n独一无二的源文标记。")
        self.assertIn("English", msg)
        self.assertIn("GeneMedi", msg)              # glossary 注入
        self.assertIn("PurProX", msg)               # do-not-translate 注入
        self.assertIn("独一无二的源文标记。", msg)    # 源文注入

    def test_supported_langs(self):
        self.assertEqual(set(translator.SUPPORTED_LANGS), {"en", "ja", "ko", "ru"})


if __name__ == "__main__":
    unittest.main()
