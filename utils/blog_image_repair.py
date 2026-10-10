"""Patch only figure URLs in an existing published CMS article, with backup/readback."""
from datetime import datetime
from html import escape, unescape
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from utils.blog_task_lock import serialized_version
from utils.figure_strategy import figure_key_from_description
from utils.image_placeholders import normalize_image_placeholders, unwrap_linked_image_placeholders
from utils.wechat_html import find_image_placeholders

BACKUP_DIR = Path(__file__).resolve().parent.parent / 'runtime' / 'blog_repairs'


class _Image(HTMLParser):
    def __init__(self, tag):
        super().__init__(convert_charrefs=True)
        self.attrs = {}
        self.feed(tag)

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.attrs = dict(attrs)


@serialized_version
def repair_images(workflow, job_id, lang='zh'):
    from batch_processor import _resolve_job_figures, _resolve_figure_path
    from utils.blog_pipeline import BlogPipelineError, _build_source_job
    from utils.blog_urls import public_blog_url
    from db.database import BLOG_LANGS

    if lang not in BLOG_LANGS:
        raise BlogPipelineError('不支持的 Blog 语言，未更新')
    job_pk = workflow._resolve_job_pk(job_id, None)
    article = workflow.db.get_article(job_pk) if job_pk is not None else None
    job = workflow.db.get_job(job_pk) if article else None
    dist = workflow.db.get_distribution(job_pk, 'blog', account='genemedi', lang=lang) if job else None
    if not article or article.publish_blocked or not dist or dist.publish_status != 'published' or not dist.external_id:
        raise BlogPipelineError('没有通过质量检查的已发布 Blog，未更新')
    if str(dist.external_url or '').rstrip('/') != public_blog_url(job_id, lang):
        raise BlogPipelineError('Blog 地址不匹配，未更新')
    source = Path(article.content_dir) / 'article.md'
    descriptions = find_image_placeholders(unwrap_linked_image_placeholders(normalize_image_placeholders(source.read_text(encoding='utf-8'))))
    keys = [figure_key_from_description(desc) for desc in descriptions]
    if not keys or None in keys or len(set(keys)) != len(keys):
        raise BlogPipelineError('源稿图片标记无法唯一对应，未更新')
    client = workflow.blog_client_factory()
    langcode = workflow._drupal_language(lang)
    original = client.get(dist.external_id, langcode=langcode)['data']['attributes']
    body = original.get('body') or {}
    content = str(body.get('value') or '')
    if original.get('langcode') != langcode or not original.get('status') or body.get('format') != 'full_html':
        raise BlogPipelineError('CMS 语言、发布状态或正文格式不匹配，未更新')
    matches = []
    for key in keys:
        candidates = [m for m in re.finditer(r'<img\b[^>]*>', content, re.I)
                      if figure_key_from_description(_Image(m.group()).attrs.get('alt') or '') == key]
        if len(candidates) != 1:
            raise BlogPipelineError('CMS 配图与源稿不能唯一对应，未更新')
        matches.append(candidates[0])
    figures, directory = _resolve_job_figures(_build_source_job(job), set(keys))
    paths = [_resolve_figure_path(desc, directory, figures) for desc in descriptions]
    if any(not path or not Path(path).is_file() for path in paths) or len(set(paths)) != len(paths):
        raise BlogPipelineError('真实图片缺失或重复，未更新')
    hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]
    if len(set(hashes)) != len(hashes):
        raise BlogPipelineError('多个图号对应同一张图片，未更新')
    store = workflow.asset_store_factory()
    urls = [store.upload(path) for path in paths]  # Store verifies public asset bytes before CMS writes.
    if len(set(urls)) != len(urls):
        raise BlogPipelineError('上传图片没有唯一对应，未更新')
    updated = content
    replacements = {}
    for match, url in sorted(zip(matches, urls), key=lambda item: item[0].start(), reverse=True):
        attr = re.search(r'(?<![\w-])src\s*=\s*([\"\'])(.*?)\1', match.group(), re.I | re.S)
        if not attr:
            raise BlogPipelineError('CMS 图片缺少可替换的 src，未更新')
        replacements[unescape(attr.group(2))] = url
        tag = match.group()[:attr.start(2)] + escape(url, quote=True) + match.group()[attr.end(2):]
        updated = updated[:match.start()] + tag + updated[match.end():]
    if updated == content:
        return {'job_id': job_id, 'lang': lang, 'status': 'already_repaired', 'verified': True, 'url': dist.external_url}
    payload = {'body': updated, 'body_format': body['format'], 'langcode': langcode}
    cover = original.get('field_cover_url')
    if cover in replacements:
        payload['cover_url'] = replacements[cover]
    if client.get(dist.external_id, langcode=langcode)['data']['attributes'] != original:
        raise BlogPipelineError('CMS 文章刚被修改，未覆盖新内容')
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f'{uuid.uuid4().hex}.json'
    record = {'job_id': job_id, 'lang': lang, 'external_id': dist.external_id,
              'original': original, 'payload': payload, 'image_sha256': hashes,
              'status': 'prepared', 'created_at': datetime.utcnow().isoformat()}
    with backup.open('x', encoding='utf-8') as stream:
        os.chmod(backup, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
    try:
        client.update(dist.external_id, payload)  # Never create or change publication status.
        actual = client.get(dist.external_id, langcode=langcode)['data']['attributes']
        preserved = ('title', 'langcode', 'status', 'path', 'field_slug', 'field_source_pdf_url', 'field_product_series')
        verified = (actual.get('body', {}).get('value') == updated
                    and actual.get('body', {}).get('format') == body['format']
                    and all(actual.get(key) == original.get(key) for key in preserved)
                    and actual.get('field_cover_url') == payload.get('cover_url', cover))
        record['status'] = 'verified' if verified else 'needs_review'
    except Exception:
        record['status'] = 'needs_review'
        raise BlogPipelineError('CMS 图片更新结果待核对，已保留备份，不要盲目重试') from None
    finally:
        backup.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if not verified:
        raise BlogPipelineError('CMS 图片更新回读校验未通过，已保留备份，不要盲目重试')
    return {'job_id': job_id, 'lang': lang, 'status': 'repaired', 'verified': True,
            'url': dist.external_url, 'images': urls, 'backup_id': backup.stem}
