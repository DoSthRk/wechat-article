"""markdown → 微信公众号 HTML 转换 —— wechat-article Phase 0。

公众号 HTML 的怪癖：
- 不支持 ``<script>`` / ``<iframe>``
- ``<style>`` 块要内联到元素 ``style=""``
- ``<img>`` 必须用 mmbiz.qpic.cn 域的 URL（外链图片会被剥光或转存）
- 表格要内联 border/padding，否则丑
- ``<a>`` 只能链到公众号文章 / 视频号 / 小程序，外链会被吞（除认证账号）

策略：``markdown`` 库基础渲染 → 给**各级标题（h1-h4，分级字体/字号/颜色，
公众号草稿里一眼看出层次）**和表格 / 引用 / 代码等加内联样式（公众号只认元素
上的 ``style=""``，会剥掉 ``<style>`` 块和 class）→ 保留 ``[图片:xxx]`` 占位符
**原样不动**（由 image_provider 接管替换）。
"""
from __future__ import annotations

import re
from typing import List

import markdown as md_lib

from utils.logger import setup_logger

logger = setup_logger("wechat_html")

# 占位符语法：[图片:描述文字]；描述允许中文/英文/标点，不允许跨行
IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[图片:([^\[\]\n]+?)\]")

# 内联样式（公众号只认 style=""）。标题分级用「字体 + 字号 + 颜色 + 视觉标记」
# 三重区分，确保草稿里大标题 / 小标题 / 子标题层次分明：
#   h1 大标题 —— 衬线、居中、近黑、最大
#   h2 小标题 —— 无衬线、品牌蓝、左侧色条
#   h3 子标题 —— 无衬线、青色、更小
_HEAD_SANS = "-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif"
INLINE_STYLES = {
    "h1": (
        "font-family:'Songti SC','SimSun',serif;font-size:23px;font-weight:700;"
        "color:#15171a;line-height:1.45;margin:6px 0 22px;text-align:center;letter-spacing:1px;"
    ),
    "h2": (
        f"font-family:{_HEAD_SANS};font-size:19px;font-weight:700;color:#2563eb;"
        "line-height:1.5;margin:30px 0 14px;padding-left:11px;border-left:4px solid #2563eb;"
    ),
    "h3": (
        f"font-family:{_HEAD_SANS};font-size:16px;font-weight:600;color:#0f766e;"
        "line-height:1.6;margin:22px 0 10px;"
    ),
    "h4": (
        f"font-family:{_HEAD_SANS};font-size:15px;font-weight:600;color:#57606a;"
        "line-height:1.6;margin:18px 0 8px;"
    ),
    "table": "border-collapse:collapse;width:100%;margin:12px 0;",
    "th": "border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;font-weight:600;text-align:left;",
    "td": "border:1px solid #d0d7de;padding:6px 10px;vertical-align:top;",
    "blockquote": "border-left:3px solid #d0d7de;margin:12px 0;padding:6px 12px;color:#57606a;background:#f6f8fa;",
    "code": "background:#f6f8fa;padding:1px 4px;border-radius:3px;font-family:Consolas,Monaco,monospace;",
    "pre": "background:#f6f8fa;padding:10px;border-radius:5px;overflow-x:auto;",
    "hr": "border:none;border-top:1px solid #d0d7de;margin:18px 0;",
}


def markdown_to_wechat_html(markdown_text: str) -> str:
    """主入口：markdown → 公众号可用 HTML，保留图片占位符。"""
    if not markdown_text or not markdown_text.strip():
        return ""

    # 1. 基础渲染（开启表格、围栏代码、自动链接等常用扩展）
    html = md_lib.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )

    # 2. 注入内联样式到关键标签（公众号会保留 style 属性）
    for tag, style in INLINE_STYLES.items():
        # 只给没有 style 的标签注；已有 style 不动（让后续手工微调有空间）
        html = re.sub(
            rf"<{tag}>",
            f'<{tag} style="{style}">',
            html,
        )

    # 3. 剥掉公众号不接受的标签（保守做法，Phase 1 可放宽）
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    return html


def find_image_placeholders(content: str) -> List[str]:
    """从 markdown 或 HTML 里抽出所有 ``[图片:xxx]`` 的描述字符串。"""
    if not content:
        return []
    return [m.group(1).strip() for m in IMAGE_PLACEHOLDER_PATTERN.finditer(content)]


def replace_image_placeholder(content: str, description: str, mmbiz_url: str) -> str:
    """把首个匹配 ``[图片:描述]`` 替换为 ``<img src="mmbiz_url" .../>``。

    多个相同描述只替换第一个；调用方循环处理。Phase 0 不调；Phase 1 起 image
    流程用。
    """
    if not content:
        return content
    needle = f"[图片:{description}]"
    img_tag = (
        f'<p style="text-align:center;margin:14px 0;">'
        f'<img src="{mmbiz_url}" alt="{_escape_attr(description)}" '
        f'style="max-width:100%;border-radius:4px;"/>'
        f"</p>"
    )
    return content.replace(needle, img_tag, 1)


def extract_title_and_digest(markdown_text: str) -> tuple[str, str]:
    """从 markdown 里挑出首个 H1 作 title、首段（非标题）作 digest（≤ 120 中文字符）。"""
    title = ""
    digest_lines: list[str] = []
    for line in (markdown_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if digest_lines:
                break
            continue
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if stripped.startswith("#"):
            continue
        # 跳过图片占位符 / 表格行 / 引用
        if stripped.startswith(("[图片:", "|", ">", "`")):
            continue
        digest_lines.append(stripped)
        if sum(len(x) for x in digest_lines) > 120:
            break
    digest = "".join(digest_lines)[:120]
    return title, digest


def _escape_attr(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
