"""Patch only figure URLs in an existing published CMS article, with backup/readback."""
from collections import Counter
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
def repair_images(workflow, job_id, lang='zh', *, repair_cover=False, expected_package_id=None, job_pk=None):
    """Repair figure URLs; opt in to replacing a custom cover with the first figure."""
    from batch_processor import _resolve_figure_path
    from utils.blog_pipeline import (BlogPipelineError, _build_source_job, require_blog_package,
                                     verify_public_image, verify_public_page_images)
    from utils.blog_urls import public_blog_url
    from db.database import BLOG_LANGS, BLOG_SOURCE_LANG

    if lang not in BLOG_LANGS:
        raise BlogPipelineError('不支持的 Blog 语言，未更新')
    job_pk = workflow._resolve_job_pk(job_id, job_pk)
    article = workflow.db.get_article(job_pk) if job_pk is not None else None
    job = workflow.db.get_job(job_pk) if article else None
    dist = workflow.db.get_distribution(job_pk, 'blog', account='genemedi', lang=lang) if job else None
    if not article or article.publish_blocked or not dist or dist.publish_status != 'published' or not dist.external_id:
        raise BlogPipelineError('没有通过质量检查的已发布 Blog，未更新')
    if str(dist.external_url or '').rstrip('/') != public_blog_url(job_id, lang):
        raise BlogPipelineError('Blog 地址不匹配，未更新')
    source_job = _build_source_job(job)
    package = require_blog_package(source_job, expected_package_id, expected_job_pk=job_pk)
    source = package.directory / 'article.md'
    descriptions = find_image_placeholders(unwrap_linked_image_placeholders(normalize_image_placeholders(source.read_text(encoding='utf-8'))))
    keys = [figure_key_from_description(desc) for desc in descriptions]
    if not keys or None in keys:
        raise BlogPipelineError('源稿图片标记无法对应图号，未更新')
    # Reusing a figure in multiple sections is valid. Resolve/upload once per
    # figure key, but retain every source occurrence for CMS alignment checks.
    descriptions_by_key = {}
    for key, description in zip(keys, descriptions):
        descriptions_by_key.setdefault(key, description)
    unique_keys = list(descriptions_by_key)
    target_descriptions = descriptions
    if lang != BLOG_SOURCE_LANG:
        version = workflow.db.get_article_version(job_pk, lang)
        if version is None or not Path(version.content_path).is_file():
            raise BlogPipelineError('目标语言稿件缺失，无法核对 CMS 配图，未更新')
        target_descriptions = find_image_placeholders(unwrap_linked_image_placeholders(
            normalize_image_placeholders(Path(version.content_path).read_text(encoding='utf-8'))))
        if [figure_key_from_description(desc) for desc in target_descriptions] != keys:
            raise BlogPipelineError('目标语言稿件图号与源稿不匹配，未更新')
    allowed_descriptions = {}
    for key, description in zip(keys, target_descriptions):
        allowed_descriptions.setdefault(key, set()).add(description)
    client = workflow.blog_client_factory()
    langcode = workflow._drupal_language(lang)
    original = client.get(dist.external_id, langcode=langcode)['data']['attributes']
    if not repair_cover:
        try:
            receipt = json.loads(getattr(dist, 'image_receipt_json', None) or '{}')
        except (ValueError, TypeError):
            receipt = {}
        if (getattr(dist, 'image_package_id', None) != package.bundle_id
                or receipt.get('cover_url') != original.get('field_cover_url')
                or not receipt.get('cover_url')):
            raise BlogPipelineError('现有封面未验证属于已批准图包，请显式包含封面修复；未更新')
    body = original.get('body') or {}
    content = str(body.get('value') or '')
    if original.get('langcode') != langcode or not original.get('status') or body.get('format') != 'full_html':
        raise BlogPipelineError('CMS 语言、发布状态或正文格式不匹配，未更新')
    matches = []
    cms_keys = []
    for match in re.finditer(r'<img\b[^>]*>', content, re.I):
        alt = str(_Image(match.group()).attrs.get('alt') or '').strip()
        key = figure_key_from_description(alt)
        if key in descriptions_by_key:
            if alt not in allowed_descriptions[key]:
                raise BlogPipelineError('CMS 配图描述与稿件不能唯一对应，未更新')
            matches.append(match)
            cms_keys.append(key)
    # The Blog renderer removes repeat uses of the same image. Permit that
    # deduplication without permitting additional or reordered CMS figures.
    source_counts, cms_counts = Counter(keys), Counter(cms_keys)
    remaining_keys = iter(keys)
    if (set(cms_keys) != set(keys)
            or any(count > source_counts[key] for key, count in cms_counts.items())
            or not all(any(candidate == key for candidate in remaining_keys) for key in cms_keys)):
        raise BlogPipelineError('CMS 配图次数或顺序与源稿不能唯一对应，未更新')
    source_attrs = [re.search(r'(?<![\w-])src\s*=\s*([\"\'])(.*?)\1', match.group(), re.I | re.S)
                    for match in matches]
    if any(attr is None for attr in source_attrs):
        raise BlogPipelineError('CMS 图片缺少可替换的 src，未更新')
    figures, directory = package.figures, package.directory
    paths = [_resolve_figure_path(desc, directory, figures) for desc in descriptions_by_key.values()]
    if (any(not path or not Path(path).is_file() for path in paths)
            or len({Path(path).resolve() for path in paths}) != len(paths)):
        raise BlogPipelineError('真实图片缺失或重复，未更新')
    hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]
    if len(set(hashes)) != len(hashes):
        raise BlogPipelineError('多个图号对应同一张图片，未更新')
    store = workflow.asset_store_factory()
    urls = []
    for path in paths:
        require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        url = store.upload(path)
        verify_public_image(url, expected_path=path)
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise BlogPipelineError('上传图片没有唯一对应，未更新')
    urls_by_key = dict(zip(unique_keys, urls))
    updated = content
    replacements = {}
    for match, attr, key in reversed(list(zip(matches, source_attrs, cms_keys))):
        url = urls_by_key[key]
        replacements[unescape(attr.group(2))] = url
        tag = match.group()[:attr.start(2)] + escape(url, quote=True) + match.group()[attr.end(2):]
        updated = updated[:match.start()] + tag + updated[match.end():]
    cover = original.get('field_cover_url')
    if repair_cover:
        require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        updated_cover = store.upload(package.cover_path)
    else:
        updated_cover = cover
    verify_public_image(updated_cover, expected_path=package.cover_path)
    receipt = json.dumps({'image_package_id': package.bundle_id, 'cover_url': updated_cover,
                         'images': urls, 'cms_verified': True, 'public_images_verified': True,
                         'public_page_images_verified': True}, ensure_ascii=False)
    if updated == content and updated_cover == cover:
        require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
            image_package_id=package.bundle_id, image_verification_status='pending_readback')
        if client.get(dist.external_id, langcode=langcode)['data']['attributes'] != original:
            raise BlogPipelineError('CMS 文章刚被修改，未将旧结果标记为已验证')
        verify_public_page_images(dist.external_url, [*urls, updated_cover])
        require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
            image_package_id=package.bundle_id, image_verification_status='verified', image_receipt_json=receipt)
        return {'job_id': job_id, 'lang': lang, 'status': 'already_repaired', 'verified': True, 'url': dist.external_url}
    payload = {'body': updated, 'body_format': body['format'], 'langcode': langcode}
    if updated_cover != cover:
        payload['cover_url'] = updated_cover
    if client.get(dist.external_id, langcode=langcode)['data']['attributes'] != original:
        raise BlogPipelineError('CMS 文章刚被修改，未覆盖新内容')
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f'{uuid.uuid4().hex}.json'
    record = {'job_id': job_id, 'lang': lang, 'external_id': dist.external_id,
              'original': original, 'payload': payload, 'image_sha256': hashes,
              'image_package_id': package.bundle_id,
              'status': 'prepared', 'created_at': datetime.utcnow().isoformat()}
    with backup.open('x', encoding='utf-8') as stream:
        os.chmod(backup, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
    try:
        require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
        workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
            image_package_id=package.bundle_id, image_verification_status='pending_readback')
        client.update(dist.external_id, payload)  # Never create or change publication status.
        actual = client.get(dist.external_id, langcode=langcode)['data']['attributes']
        preserved = ('title', 'langcode', 'status', 'path', 'field_slug', 'field_source_pdf_url', 'field_product_series')
        verified = (actual.get('body', {}).get('value') == updated
                    and actual.get('body', {}).get('format') == body['format']
                    and all(actual.get(key) == original.get(key) for key in preserved)
                    and actual.get('field_cover_url') == payload.get('cover_url', cover))
        record['status'] = 'verified' if verified else 'needs_review'
        if verified:
            require_blog_package(source_job, package.bundle_id, expected_job_pk=job_pk)
            verify_public_page_images(dist.external_url, [*urls, updated_cover])
    except Exception:
        record['status'] = 'needs_review'
        workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
            image_package_id=package.bundle_id, image_verification_status='needs_review')
        raise BlogPipelineError('CMS 图片更新结果待核对，已保留备份，不要盲目重试') from None
    finally:
        backup.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if not verified:
        workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
            image_package_id=package.bundle_id, image_verification_status='needs_review')
        raise BlogPipelineError('CMS 图片更新回读校验未通过，已保留备份，不要盲目重试')
    workflow.db.upsert_distribution(job_pk, 'blog', account='genemedi', lang=lang,
        image_package_id=package.bundle_id, image_verification_status='verified', image_receipt_json=receipt)
    return {'job_id': job_id, 'lang': lang, 'status': 'repaired', 'verified': True,
            'url': dist.external_url, 'images': urls, 'backup_id': backup.stem,
            'image_package_id': package.bundle_id}
