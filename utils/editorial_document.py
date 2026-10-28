"""结构化正文的导入与受限导出；浏览器 JSON 不能提供 HTML、文件路径或图片 URL。"""
from __future__ import annotations

import json
import re
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import markdown

from utils.image_placeholders import normalize_image_placeholders, unwrap_linked_image_placeholders
from utils.figure_strategy import figure_key_from_description


class EditorialError(ValueError):
    pass


def figure_key(description: str) -> str:
    key = figure_key_from_description(description)
    if not key:
        raise EditorialError('图片必须有可识别的 Figure 图号')
    return f'{"extended" if key[1] else "main"}:{key[0]}'


def safe_link(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in '\r\n<>"'):
        raise EditorialError('链接格式不正确')
    parsed = urlsplit(value)
    if parsed.scheme not in {'https', 'http', 'mailto'} or (parsed.scheme != 'mailto' and not parsed.hostname):
        raise EditorialError('链接只支持 http、https 或邮件地址')
    return value


def plain(value: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise EditorialError('文字长度或格式不正确')
    return value


def _text(value: str) -> str:
    # 编码尖括号阻止原始 HTML，转义 Markdown 元字符和伪造图片占位符。
    value = escape(plain(value, 200000), quote=False)
    return re.sub(r'([\\`*_{}\[\]#|~])', r'\\\1', value)


def export_document(document: dict) -> tuple[str, list[str]]:
    if not isinstance(document, dict) or document.get('type') != 'doc':
        raise EditorialError('正文格式无效')
    if len(json.dumps(document, ensure_ascii=False)) > 600000:
        raise EditorialError('正文超出编辑长度限制')
    keys = []

    def render(node, depth=0):
        if depth > 30 or not isinstance(node, dict):
            raise EditorialError('正文层级或格式无效')
        kind, attrs = node.get('type'), node.get('attrs') or {}
        if not isinstance(attrs, dict) or not isinstance(node.get('content', []), list):
            raise EditorialError('正文节点格式无效')
        children = [render(c, depth + 1) for c in node.get('content', [])]
        joined = ''.join(children)
        if kind == 'text':
            if '[图片:' in node.get('text', '') or '[图片：' in node.get('text', ''):
                raise EditorialError('请通过“插入配图”加入图片，不要在正文手写图片占位符')
            value = _text(node.get('text', ''))
            for mark in node.get('marks', []):
                mark_type = mark.get('type')
                if mark_type in {'bold', 'italic', 'strike', 'code'}:
                    delimiter = {'bold': '**', 'italic': '*', 'strike': '~~', 'code': '`'}[mark_type]
                    value = delimiter + value + delimiter
                elif mark_type == 'link':
                    href = safe_link((mark.get('attrs') or {}).get('href', ''))
                    value = f'[{value}](<{href}>)'
                elif mark_type == 'underline':
                    value = f'<u>{value}</u>'
                elif mark_type in {'superscript','subscript'}:
                    tag = 'sup' if mark_type == 'superscript' else 'sub'
                    value = f'<{tag}>{value}</{tag}>'
                else:
                    raise EditorialError('正文包含暂不支持的文字格式')
            return value
        if kind == 'doc': return '\n\n'.join(c.strip() for c in children if c.strip())
        if kind == 'paragraph': return joined + '\n\n'
        if kind == 'heading':
            level = attrs.get('level')
            if level not in {2, 3, 4}: raise EditorialError('正文标题只支持二至四级，文章大标题请在标题栏编辑')
            return '#' * level + ' ' + joined.strip() + '\n\n'
        if kind == 'hardBreak': return '  \n'
        if kind == 'horizontalRule': return '\n\n---\n\n'
        if kind == 'blockquote': return '\n'.join('> ' + line for line in joined.strip().splitlines()) + '\n\n'
        if kind in {'bulletList', 'orderedList'}:
            rows = []
            for i, child in enumerate(children):
                prefix = '- ' if kind == 'bulletList' else f'{i + 1}. '
                lines = child.strip().splitlines()
                rows.append(prefix + ('\n' + ' ' * len(prefix)).join(lines))
            return '\n'.join(rows) + '\n\n'
        if kind == 'listItem': return joined
        if kind == 'codeBlock':
            raw = ''.join(plain(c.get('text', ''), 200000) for c in node.get('content', []) if c.get('type') == 'text')
            fence = '`' * max(3, max((len(m.group()) + 1 for m in re.finditer(r'`+', raw)), default=3))
            return f'{fence}\n{raw}\n{fence}\n\n'
        if kind == 'figure':
            key = attrs.get('key', '')
            if not isinstance(key, str) or not re.fullmatch(r'(main|extended):\d+[A-Za-z]?', key):
                raise EditorialError('图片图号格式无效')
            if key in keys: raise EditorialError('同一图号只能插入一次，请检查重复图片')
            keys.append(key)
            caption = plain(attrs.get('caption', ''), 2000).replace('\n', ' ').replace('[', '（').replace(']', '）')
            label = ('Extended Data Figure ' if key.startswith('extended:') else 'Figure ') + key.split(':')[1]
            return f'\n\n[图片:{label}{" — " + escape(caption, quote=False) if caption else ""}]\n\n'
        if kind in {'tableCell', 'tableHeader'}:
            if attrs.get('colspan', 1) != 1 or attrs.get('rowspan', 1) != 1:
                raise EditorialError('表格暂不支持合并单元格')
            return joined.strip().replace('\n', ' ').replace('|', '\\|')
        if kind == 'tableRow': return '| ' + ' | '.join(children) + ' |'
        if kind == 'table':
            if not children: return ''
            count = len(node['content'][0].get('content', []))
            return '\n'.join([children[0], '| ' + ' | '.join(['---'] * count) + ' |', *children[1:]]) + '\n\n'
        raise EditorialError('正文包含暂不支持的内容块')

    result = render(document).strip()
    if not result: raise EditorialError('正文不能为空')
    return result, keys


class _Import(HTMLParser):
    """从受信任的 Markdown 结构导入，丢弃 HTML 样式与可执行属性。"""
    tags = {'p': 'paragraph', 'div': 'paragraph', 'h2': 'heading', 'h3': 'heading', 'h4': 'heading',
            'ul': 'bulletList', 'ol': 'orderedList', 'li': 'listItem', 'blockquote': 'blockquote',
            'table': 'table', 'tr': 'tableRow', 'td': 'tableCell', 'th': 'tableHeader', 'pre': 'codeBlock'}
    marks = {'b': 'bold', 'strong': 'bold', 'i': 'italic', 'em': 'italic', 's': 'strike', 'del': 'strike',
             'code': 'code', 'u': 'underline', 'a': 'link', 'sup':'superscript', 'sub':'subscript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.doc = {'type': 'doc', 'content': []}
        self.stack = [('', self.doc)]
        self.active = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'h1', 'script', 'style', 'iframe', 'object'}:
            self.skip += 1
            return
        if self.skip: return
        if tag == 'figure' and attrs.get('data-key'):
            self.stack[-1][1].setdefault('content', []).append({'type': 'figure', 'attrs': {'key': attrs['data-key'], 'caption': attrs.get('data-caption', '')}})
        elif tag in self.tags:
            node = {'type': self.tags[tag], 'content': []}
            if tag.startswith('h'): node['attrs'] = {'level': int(tag[1])}
            self.stack[-1][1].setdefault('content', []).append(node)
            self.stack.append((tag, node))
        elif tag in self.marks:
            mark = {'type': self.marks[tag]}
            if tag == 'a':
                try: mark['attrs'] = {'href': safe_link(attrs.get('href', ''))}
                except EditorialError: return
            self.active.append((tag, mark))
        elif tag in {'br', 'hr'}:
            self.stack[-1][1].setdefault('content', []).append({'type': 'hardBreak' if tag == 'br' else 'horizontalRule'})

    def handle_endtag(self, tag):
        if tag in {'h1', 'script', 'style', 'iframe', 'object'} and self.skip:
            self.skip -= 1
            return
        if self.skip: return
        if len(self.stack) > 1 and self.stack[-1][0] == tag: self.stack.pop()
        self.active = [pair for pair in self.active if pair[0] != tag]

    def handle_data(self, value):
        if self.skip or (not value.strip() and self.stack[-1][1]['type'] in {'doc', 'table', 'tableRow', 'bulletList', 'orderedList'}): return
        node = {'type': 'text', 'text': value}
        if self.active: node['marks'] = [m for _, m in self.active]
        if self.stack[-1][1]['type'] == 'doc':
            self.doc['content'].append({'type': 'paragraph', 'content': [node]})
        else: self.stack[-1][1].setdefault('content', []).append(node)


def import_markdown(source: str) -> dict:
    source = unwrap_linked_image_placeholders(normalize_image_placeholders(source))
    def figure(match):
        description = match.group(1)
        try: key = figure_key(description)
        except EditorialError: return match.group(0)
        return f'\n\n<figure data-key="{key}" data-caption="{escape(description, quote=True)}"></figure>\n\n'
    source = re.sub(r'\[图片:([^\[\]\n]+)\]', figure, source)
    parser = _Import()
    parser.feed(markdown.markdown(source, extensions=['tables', 'fenced_code']))
    def normalize(node):
        if node['type'] in {'tableCell', 'tableHeader', 'listItem'} and node.get('content') and node['content'][0]['type'] == 'text':
            node['content'] = [{'type': 'paragraph', 'content': node['content']}]
        for child in node.get('content', []): normalize(child)
    normalize(parser.doc)
    return parser.doc
