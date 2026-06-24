"""markdown → 微信公众号 HTML 转换 —— wechat-article Phase 0。

公众号 HTML 的怪癖：
- 不支持 ``<script>`` / ``<iframe>``
- ``<style>`` 块要内联到元素 ``style=""``
- ``<img>`` 必须用 mmbiz.qpic.cn 域的 URL（外链图片会被剥光或转存）
- 表格要内联 border/padding，否则丑
- ``<a>`` 只能链到公众号文章 / 视频号 / 小程序，外链会被吞（除认证账号）

策略：``markdown`` 库基础渲染 → **删掉正文 h1 大标题**（与草稿 title 重复）→ 给
正文 / 小标题 / 表格 / 引用 等加内联样式（统一微软雅黑、正文 14px、小标题 15px
配色 #ab1942；公众号只认元素上的 ``style=""``，会剥掉 ``<style>`` 块和 class）→
保留 ``[图片:xxx]`` 占位符**原样不动**（由 image_provider 接管替换）。
"""
from __future__ import annotations

import re
from typing import List

import markdown as md_lib

from utils.logger import setup_logger

logger = setup_logger("wechat_html")

# 占位符语法：[图片:描述文字]；描述允许中文/英文/标点，不允许跨行
IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[图片:([^\[\]\n]+?)\]")

# 列表项符号（公众号编辑器会给语义 <ul>/<li> 插入空 bullet，故弃用真列表，改手动符号）
_LIST_BULLET = "•"

# 内联样式（公众号只认 style=""）。格式规则（用户拍板）：
#   - 大标题(h1) 不进正文 —— 与草稿 title 重复，整体删除（见 markdown_to_wechat_html）
#   - 统一一种字体：电脑端微信/浏览器默认 微软雅黑（不用宋体/衬线）
#   - 正文 14px、小标题(h2) 15px；小标题颜色 #ab1942（不用蓝色）
_FONT = "'Microsoft YaHei','微软雅黑',-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif"
_ACCENT = "#ab1942"   # 小标题主色
_BODY_COLOR = "#333333"
INLINE_STYLES = {
    "h2": (  # 小标题
        f"font-family:{_FONT};font-size:15px;font-weight:700;color:{_ACCENT};"
        f"line-height:1.6;margin:26px 0 12px;padding-left:10px;border-left:3px solid {_ACCENT};"
    ),
    "h3": (  # 子标题（当前文章未用；保留以防）
        f"font-family:{_FONT};font-size:15px;font-weight:600;color:{_ACCENT};"
        "line-height:1.6;margin:20px 0 10px;"
    ),
    "h4": (
        f"font-family:{_FONT};font-size:14px;font-weight:600;color:{_BODY_COLOR};"
        "line-height:1.6;margin:16px 0 8px;"
    ),
    "p": f"font-family:{_FONT};font-size:14px;line-height:1.75;color:{_BODY_COLOR};margin:14px 0;",
    "table": "border-collapse:collapse;width:100%;margin:12px 0;",
    "th": (
        f"border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;font-weight:600;"
        f"text-align:left;font-family:{_FONT};font-size:14px;"
    ),
    "td": f"border:1px solid #d0d7de;padding:6px 10px;vertical-align:top;font-family:{_FONT};font-size:14px;",
    "blockquote": (
        f"border-left:3px solid #d0d7de;margin:12px 0;padding:6px 12px;color:#57606a;"
        f"background:#f6f8fa;font-family:{_FONT};font-size:14px;"
    ),
    "code": "background:#f6f8fa;padding:1px 4px;border-radius:3px;font-family:Consolas,Monaco,monospace;",
    "pre": "background:#f6f8fa;padding:10px;border-radius:5px;overflow-x:auto;",
    "hr": "border:none;border-top:1px solid #d0d7de;margin:18px 0;",
}

# 列表项样式：与正文同字号，悬挂缩进让换行对齐到符号后（公众号里好看）
_LIST_ITEM_STYLE = (
    f"font-family:{_FONT};font-size:14px;line-height:1.75;color:{_BODY_COLOR};"
    "margin:8px 0;padding-left:1.2em;text-indent:-1.2em;"
)


def _delist(html: str) -> str:
    """把 ``<ul>``/``<ol>`` 转成带手动符号的 ``<p>``。

    公众号编辑器对语义列表支持差：导入 ``<ul><li>`` 后会在每项前插入一个空 bullet
    （正文看到一行空圆点 + 一行内容，排版错乱）。改成手动 ``• `` / ``1. `` 前缀的段落，
    编辑器对 ``<p>`` 处理稳定。仅处理扁平列表（当前内容无嵌套）。
    """
    def convert(match: "re.Match[str]") -> str:
        block = match.group(0)
        ordered = block[:3].lower() == "<ol"
        items = re.findall(r"<li\b[^>]*>(.*?)</li>", block, flags=re.DOTALL | re.IGNORECASE)
        out = []
        for i, item in enumerate(items, 1):
            marker = f"{i}. " if ordered else f"{_LIST_BULLET} "
            out.append(f'<p style="{_LIST_ITEM_STYLE}">{marker}{item.strip()}</p>')
        return "".join(out)

    html = re.sub(r"<ul\b[^>]*>.*?</ul>", convert, html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<ol\b[^>]*>.*?</ol>", convert, html, flags=re.DOTALL | re.IGNORECASE)
    return html


def markdown_to_wechat_html(markdown_text: str) -> str:
    """主入口：markdown → 公众号可用 HTML，保留图片占位符。"""
    if not markdown_text or not markdown_text.strip():
        return ""

    # 1. 基础渲染（开启表格、围栏代码、自动链接等常用扩展）
    html = md_lib.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )

    # 2. 删除正文大标题(h1)：标题已单独作草稿 title 字段，正文里不再重复
    html = re.sub(r"<h1\b[^>]*>.*?</h1>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # 2b. 列表转手动符号段落（公众号编辑器会给 <ul>/<li> 插空 bullet）
    html = _delist(html)

    # 3. 注入内联样式到关键标签（公众号会保留 style 属性）
    for tag, style in INLINE_STYLES.items():
        # 只给没有 style 的标签注；已有 style 不动（让后续手工微调有空间）
        html = re.sub(
            rf"<{tag}>",
            f'<{tag} style="{style}">',
            html,
        )

    # 4. 剥掉公众号不接受的标签（保守做法，Phase 1 可放宽）
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
