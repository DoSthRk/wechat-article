"""Replace only source-matched figure paragraphs in an existing WeChat draft."""
from datetime import datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from utils.blog_task_lock import version_lock
from utils.image_placeholders import normalize_image_placeholders, unwrap_linked_image_placeholders
from utils.wechat_client import WeChatClient
from utils.wechat_html import (find_image_placeholders, replace_image_placeholder,
                               prepare_wechat_update_html, restore_wechat_body_styles)

BACKUP_DIR = Path(__file__).resolve().parent.parent / "runtime" / "wechat_repairs"
_PARAGRAPH = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)
_FIELDS = ("title", "author", "digest", "content", "content_source_url", "thumb_media_id",
           "show_cover_pic", "pic_crop_235_1", "pic_crop_1_1", "need_open_comment", "only_fans_can_comment")


class DraftImageRepairError(Exception):
    pass


class _Body(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.text, self.images = [], []
        self.feed(content)

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            attrs = dict(attrs)
            self.images.append(attrs.get("data-src") or attrs.get("src") or "")


def _text(content):
    return "".join("".join(_Body(content).text).split())


def _image_key(url):
    parsed = urlsplit(unescape(url))
    # WeChat rewrites /0 to /640 and adds query parameters after draft updates.
    return parsed.hostname, re.sub(r"/\d+$", "", parsed.path)


def _first(draft):
    items = draft.get("news_item") or []
    if not items:
        raise DraftImageRepairError("现有草稿没有图文内容，未更新")
    return {key: items[0][key] for key in _FIELDS if key in items[0]}


def _style_blocks(content):
    """Critical visible typography, tolerating WeChat's CSS serialization."""
    result = []
    for match in re.finditer(r'<(p|h[234])\b([^>]*)>(.*?)</\1>', content, re.I | re.S):
        attribute = re.search(r'(?<![\w-])style\s*=\s*([\"\'])(.*?)\1', match.group(2), re.I | re.S)
        css = {}
        for declaration in unescape(attribute.group(2) if attribute else '').split(';'):
            key, sep, value = declaration.partition(':')
            if sep and key.strip().lower() in {'font-size', 'font-weight', 'color', 'line-height'}:
                value = re.sub(r'\s+', '', value.lower())
                value = {'bold': '700', 'normal': '400', 'rgb(171,25,66)': '#ab1942',
                         'rgb(51,51,51)': '#333333'}.get(value, value)
                css[key.strip().lower()] = value
        result.append((match.group(1).lower(), _text(match.group(3)), css))
    return result


def _styles_preserved(expected, actual):
    wanted, found = _style_blocks(expected), _style_blocks(actual)
    return len(wanted) == len(found) and all(
        tag == other_tag and text == other_text and all(other_css.get(k) == v for k, v in css.items())
        for (tag, text, css), (other_tag, other_text, other_css) in zip(wanted, found))


def _update_verified(client, job_id, draft_ref, original, updated, expected_text, urls=(), operation='images'):
    content = str(original.get('content') or '')
    updated = prepare_wechat_update_html(updated)
    if _first(client.get_draft(draft_ref['media_id'])) != original:
        raise DraftImageRepairError('草稿刚被修改，请重新核对后修复；未覆盖新内容')
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f'{uuid.uuid4().hex}.json'
    record = {'job_id': job_id, **draft_ref, 'created_at': datetime.utcnow().isoformat(),
              'original': original, 'uploaded_images': list(urls), 'operation': operation, 'status': 'prepared'}
    with backup.open('x', encoding='utf-8') as stream:
        os.chmod(backup, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
    # No create fallback, regeneration, Blog requeue, or mass-publication call.
    client.update_draft(draft_ref['media_id'], 0, {**original, 'content': updated})
    actual = _first(client.get_draft(draft_ref['media_id']))
    actual_body = str(actual.get('content') or '')
    image_keys = {_image_key(url) for url in _Body(actual_body).images}
    verified = (all(_image_key(url) in image_keys for url in list(urls) + _Body(content).images)
                and _text(actual_body) == expected_text
                and _styles_preserved(updated, actual_body)
                and not (re.search(r'<h1\b', actual_body, re.I) and not re.search(r'<h1\b', content, re.I))
                and all(actual.get(key) == value for key, value in original.items() if key != 'content'))
    record.update(status='verified' if verified else 'needs_review',
                  actual_content_sha256=hashlib.sha256(actual_body.encode()).hexdigest())
    backup.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if not verified:
        raise DraftImageRepairError('草稿已更新，但文字、图片或格式回读校验未通过；已保留备份，请核对，不要盲目重试')
    return {'ok': True, **draft_ref, 'verified': True, 'backup_id': backup.stem}


def repair_formatting(db, job_id, *, client_factory=WeChatClient):
    with version_lock(job_id, 'wechat_repair'):
        draft_ref = db.latest_wechat_draft(job_id)
        if not draft_ref:
            raise DraftImageRepairError('没有现有草稿，未更新')
        client = client_factory(account=draft_ref['account'])
        original = _first(client.get_draft(draft_ref['media_id']))
        content = str(original.get('content') or '')
        headings = re.findall(r'<h[234]\b[^>]*>.*?</h[234]>', content, re.I | re.S)
        if not headings or re.search(r'<h1\b', content, re.I):
            raise DraftImageRepairError('现有正文标题结构需人工核对，未更新')
        # Keep the manually edited product/footer module byte-for-byte in the payload.
        footer = next((m.start() for m in _PARAGRAPH.finditer(content)
                       if '【相关产品推荐】' in _text(m.group())), len(content))
        updated = restore_wechat_body_styles(content[:footer]) + content[footer:]
        if _text(updated) != _text(content):
            raise DraftImageRepairError('格式修复会改变正文文字，未更新')
        result = _update_verified(client, job_id, draft_ref, original, updated, _text(content), operation='formatting')
        return {**result, 'styled_headings': len(headings), 'body_h1': False}


def repair_images(db, job_id, *, client_factory=WeChatClient):
    from batch_processor import _resolve_job_figures, _resolve_figure_path, _upload_cached
    from utils.blog_pipeline import _build_source_job
    from utils.figure_strategy import figure_key_from_description

    with version_lock(job_id, "wechat_repair"):
        job_pk = db.find_job_pk(job_id)
        job = db.get_job(job_pk) if job_pk is not None else None
        article = db.get_article(job_pk) if job else None
        draft_ref = db.latest_wechat_draft(job_id)
        if not article or article.publish_blocked or not draft_ref:
            raise DraftImageRepairError("文章不存在、质量检查未通过或没有现有草稿，未更新")
        source = Path(article.content_dir) / "article.md"
        descriptions = find_image_placeholders(unwrap_linked_image_placeholders(
            normalize_image_placeholders(source.read_text(encoding="utf-8"))))
        if not descriptions or len(set(descriptions)) != len(descriptions):
            raise DraftImageRepairError("源稿图片标记不存在或重复，未更新")
        client = client_factory(account=draft_ref["account"])
        original = _first(client.get_draft(draft_ref["media_id"]))
        content = str(original.get("content") or "")
        patches = []
        for desc in descriptions:
            matches = [m for m in _PARAGRAPH.finditer(content)
                       if _text(m.group()) in {_text(f"[图片:{desc}]"), _text(f"图片:{desc}")}]
            if len(matches) != 1:
                raise DraftImageRepairError("草稿图片文字与源稿不能唯一对应，未更新")
            patches.append((matches[0], desc))
        source_job = _build_source_job(job)
        required = {figure_key_from_description(desc) for desc in descriptions}
        if None in required or len(required) != len(descriptions):
            raise DraftImageRepairError("源稿图号无法唯一对应，未更新")
        figures, directory = _resolve_job_figures(source_job, required)
        paths = [_resolve_figure_path(desc, directory, figures) for desc in descriptions]
        if any(not path or not Path(path).is_file() for path in paths) or len(set(paths)) != len(paths):
            raise DraftImageRepairError("真实图片缺失或重复，未更新")
        if len({hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}) != len(paths):
            raise DraftImageRepairError("多个图号对应同一张图片，未更新")
        urls = [_upload_cached(client, draft_ref["account"], path) for path in paths]
        if any(urlsplit(url).hostname not in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"} for url in urls):
            raise DraftImageRepairError("图片不是可用的微信素材，未更新")
        if len({_image_key(url) for url in urls}) != len(urls):
            raise DraftImageRepairError("上传结果没有对应到独立图片，未更新")
        replacements = [(match, replace_image_placeholder(f"[图片:{desc}]", desc, url))
                        for (match, desc), url in zip(patches, urls)]
        updated = content
        without_markers = content
        for match, image in sorted(replacements, key=lambda item: item[0].start(), reverse=True):
            updated = updated[:match.start()] + image + updated[match.end():]
            without_markers = without_markers[:match.start()] + without_markers[match.end():]
        if _text(updated) != _text(without_markers):
            raise DraftImageRepairError("修复会改变正文文字，未更新")
        return {**_update_verified(client, job_id, draft_ref, original, updated, _text(without_markers), urls),
                'inserted_images': len(urls)}
