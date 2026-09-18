import pytest

from utils.image_placeholders import (
    deduplicate_image_placeholders,
    normalize_image_placeholders,
    validate_image_placeholders,
)


def test_normalize_preserves_captions_and_other_markdown():
    source = "[link](https://example.com)\n[画像：Figure 1 日文図注]\n[이미지:Figure 2 한국어]"
    normalized = normalize_image_placeholders(source)
    assert normalized == "[link](https://example.com)\n[图片:Figure 1 日文図注]\n[图片:Figure 2 한국어]"
    assert normalize_image_placeholders(normalized) == normalized


@pytest.mark.parametrize("target", [
    "[图片:Figure 2 second]\n[图片:Figure 1 first]",
    "[图片:Figure 1 first]\n[图片:Figure 1 duplicated]",
    "[图片:Figure 1 first]",
    "[图片:Figure 1 first]\n[图片:Extended Data Figure 2 wrong type]",
])
def test_changed_figure_sequence_is_rejected(target):
    with pytest.raises(ValueError, match="image_placeholder_mismatch"):
        validate_image_placeholders("[图片:Figure 1 一]\n[图片:Figure 2 二]", target)


def test_translated_captions_and_localized_prefix_are_accepted():
    validate_image_placeholders("[图片:Figure 1 一]\n[图片:Figure 2 二]", "[画像:Figure 1 first]\n[画像:Figure 2 second]")


def test_duplicate_figure_numbers_keep_only_first_placeholder():
    content = (
        "[图片:Figure 5 第一处]\n正文\n"
        "[图片:Figure 5 第二处]\n"
        "[图片:图 5 第三处]\n"
        "[图片:Figure 6 唯一占位]"
    )

    cleaned, removed = deduplicate_image_placeholders(content)

    assert cleaned.count("[图片:") == 2
    assert "[图片:Figure 5 第一处]" in cleaned
    assert "[图片:Figure 6 唯一占位]" in cleaned
    assert removed == ["Figure 5 第二处", "图 5 第三处"]
