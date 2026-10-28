"""站内草稿、原 PDF 裁剪及审核快照；保存和裁剪绝不公开发布。"""
from __future__ import annotations

from copy import deepcopy
import io
import json
import math
from pathlib import Path
import shutil
import tempfile

from PIL import Image, ImageOps

from utils.editorial_document import EditorialError, export_document, import_markdown, plain
from utils.editorial_store import Store, Conflict, digest, encode
from utils.blog_task_lock import version_lock


def source_job(db, pk):
    from utils.blog_pipeline import _build_source_job
    record = db.get_job(pk)
    if record is None:
        raise EditorialError('文章或业务线不存在')
    return _build_source_job(record)


def initialize(db, store: Store, pk: int, author: str) -> dict:
    """由后台初始化；已有人工修订时只读取。抽图失败仍创建可编辑正文。"""
    with version_lock(str(pk), 'editorial_init'):
        head = store.head(pk)
        if head: return store.revision(pk, head['revision'])
        job, article = source_job(db, pk), db.get_article(pk)
        if not article or not article.content_dir: raise EditorialError('正文尚未生成')
        folder = Path(article.content_dir)
        text = (folder / 'article.md').read_text(encoding='utf-8')
        source = Path(job.pdf).read_bytes()
        source_hash = digest(source)
        meta = json.loads((folder / 'meta.json').read_text())
        if meta.get('source_pdf_sha256') != source_hash:
            raise EditorialError('原 PDF 与生成正文来源不一致，请先由负责人核对原文')
        pdf_asset = store.put(pk, source, '.pdf')
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(store.asset(pk, pdf_asset)))
        try:
            if not 1 <= len(document) <= 200: raise EditorialError('PDF 须为 1–200 页')
            pages = []
            for i in range(len(document)):
                page = document[i]
                try:
                    width, height = page.get_size()
                    if not 1 < width <= 3000 or not 1 < height <= 3000: raise EditorialError('PDF 页面尺寸超出支持范围')
                    pages.append({'page': i + 1, 'width': width, 'height': height})
                finally: page.close()
        finally: document.close()
        figures = {}
        from utils.image_packages import get_package
        try:
            package = get_package(job)
            if package.get('manifest') and package.get('status') in {'needs_review', 'approved', 'revoked'}:
                for item in package['manifest']['figures']:
                    key = f'{"extended" if item["is_extended"] else "main"}:{item["label"]}'
                    raw = Path(package['directory'], item['path']).read_bytes()
                    with Image.open(io.BytesIO(raw)) as image:
                        output = io.BytesIO(); image.convert('RGB').save(output, 'JPEG', quality=95)
                    figures[key] = {'asset': store.put(pk, output.getvalue(), '.jpg'), 'page': item['page'],
                                    'bbox': item['bbox'], 'caption': item.get('caption', ''), 'origin': 'automatic'}
        except (ValueError, OSError):
            # 图片不足留在编辑器补图，源文绑定失败仍然阻断。
            figures = {}
        doc = import_markdown(text)
        _, keys = export_document(doc)
        payload = {'line': job.line, 'title': article.title or job.job_id, 'digest': article.digest or '', 'document': doc,
                   'source_hash': source_hash, 'pdf_asset': pdf_asset, 'pages': pages,
                   'figures': figures, 'cover_key': next(iter(figures), keys[0] if keys else ''),
                   'cover_asset': None, 'original_markdown_asset': store.put(pk, text.encode(), '.md'),
                   'generation_warning': article.block_reason or ''}
        return store.save(pk, job.job_id, job.line, payload, expected=None, author=author)


def save_draft(db, store, pk, data, author):
    head = store.head(pk)
    if not head: raise EditorialError('草稿仍在准备中')
    current = store.revision(pk, head['revision'])
    if data.get('expected_revision') != head['revision']: raise Conflict('草稿已有新版本，当前修改尚未保存，请先核对版本冲突。')
    title, summary = plain(data.get('title', ''), 120).strip(), plain(data.get('digest', ''), 250).strip()
    if not title or '\n' in title: raise EditorialError('请填写单行文章标题')
    document = data.get('document')
    export_document(document)
    updated = {**current, 'title': title, 'digest': summary, 'document': document}
    key = data.get('cover_key', current['cover_key'])
    if key != current['cover_key']:
        if key not in current['figures']: raise EditorialError('请选择已有配图作为封面')
        updated['cover_key'], updated['cover_asset'] = key, None
    job = source_job(db, pk)
    return store.save(pk, job.job_id, job.line, updated, expected=head['revision'], author=author)


def render_page(store, pk, revision, page_no, *, bbox=None, scale=1.5):
    import pypdfium2 as pdfium
    info = store.revision(pk, revision)
    if type(page_no) is not int or not 1 <= page_no <= len(info['pages']): raise EditorialError('PDF 页码无效')
    size = info['pages'][page_no - 1]
    width, height = size['width'], size['height']
    if bbox is not None:
        if (not isinstance(bbox, list) or len(bbox) != 4 or
                any(type(v) not in {int, float} or not math.isfinite(v) for v in bbox)):
            raise EditorialError('裁剪坐标无效')
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height) or min(x1-x0, y1-y0) * scale < 60:
            raise EditorialError('裁框越界或图片过小，请保留完整图形')
        crop = (x0, height-y1, width-x1, y0)
    else: crop = (0, 0, 0, 0)
    document = pdfium.PdfDocument(str(store.asset(pk, info['pdf_asset'])))
    try:
        page = document[page_no - 1]
        try:
            bitmap = page.render(scale=scale, crop=crop)
            try:
                image = bitmap.to_pil().convert('RGB')
                output = io.BytesIO(); image.save(output, 'JPEG', quality=94)
            finally: bitmap.close()
        finally: page.close()
    finally: document.close()
    return output.getvalue()


def crop(db, store, pk, data, author):
    head = store.head(pk)
    if not head or head['revision'] != data.get('expected_revision'): raise Conflict('草稿版本已变化，请刷新后重新裁剪')
    info = store.revision(pk, head['revision'])
    _, keys = export_document(info['document'])
    key = data.get('key')
    is_cover = key == 'cover'
    if not is_cover and key not in keys: raise EditorialError('图片图号不在当前正文中')
    raw = render_page(store, pk, head['revision'], data.get('page'), bbox=data.get('bbox'), scale=2)
    if is_cover:
        with Image.open(io.BytesIO(raw)) as image:
            fitted = ImageOps.contain(image, (900, 383), Image.Resampling.LANCZOS)
            canvas = Image.new('RGB', (900, 383), 'white')
            canvas.paste(fitted, ((900-fitted.width)//2, (383-fitted.height)//2))
            out = io.BytesIO(); canvas.save(out, 'JPEG', quality=94); raw = out.getvalue()
        info['cover_asset'] = store.put(pk, raw, '.jpg')
        info['cover_crop'] = {'page': data['page'], 'bbox': data['bbox'], 'author': author}
    else:
        info['figures'][key] = {'asset': store.put(pk, raw, '.jpg'), 'page': data['page'], 'bbox': data['bbox'], 'origin': 'manual', 'author': author}
        if info['cover_key'] not in keys or info['cover_key'] not in info['figures']:
            info['cover_key'] = key
        if info['cover_key'] == key: info['cover_asset'] = None
    job = source_job(db, pk)
    return store.save(pk, job.job_id, job.line, info, expected=head['revision'], author=author)


def validate_publication(db, store, pk, revision):
    job = source_job(db, pk)
    if job.job_id in {'免疫客文章-4-3', '免疫客文章-4-4'}:
        raise EditorialError('这篇文章仍在暂缓发布名单中，请先联系负责人')
    info = store.revision(pk, revision)
    if digest(Path(job.pdf).read_bytes()) != info['source_hash']: raise EditorialError('原文 PDF 已变更，不能发布旧来源版本')
    text, keys = export_document(info['document'])
    if not keys: raise EditorialError('正文至少需要一张与原文对应的配图')
    if any(key not in info['figures'] for key in keys): raise EditorialError('正文还有未配图的位置，请先从原页补齐')
    if info['cover_key'] not in keys: raise EditorialError('请选择正文中已有的配图作为封面来源')
    for key in keys:
        figure = info['figures'][key]
        with Image.open(store.asset(pk, figure['asset'])) as picture:
            picture.load()
            if min(picture.size) < 60: raise EditorialError('配图过小')
    hashes = [info['figures'][key]['asset'] for key in keys]
    if len(set(hashes)) != len(hashes): raise EditorialError('不同图号不能使用同一图片，请核对图片对应')
    return info, '# ' + info['title'] + '\n\n' + text + '\n', keys


def assemble_preview(db, store, pk, revision, platform, asset_url):
    from utils.wechat_html import markdown_to_wechat_html, replace_image_placeholder, find_image_placeholders
    from utils.wechat_template_assets import source_pdf_guide_html
    from batch_processor import _load_product_module
    import markdown
    info = store.revision(pk, revision)
    body, _ = export_document(info['document'])
    html = markdown_to_wechat_html(body) if platform == 'wechat' else markdown.markdown(body, extensions=['tables', 'fenced_code'])
    from utils.editorial_document import figure_key
    for description in find_image_placeholders(html):
        key = figure_key(description)
        item = info['figures'].get(key)
        if item: html = replace_image_placeholder(html, description, asset_url(item['asset']))
    job = source_job(db, pk)
    html += _load_product_module(job.line, 'wechat' if platform == 'wechat' else 'blog') or ''
    if platform == 'wechat': html += source_pdf_guide_html('/editorial/template-guide')
    return html


def activate_package(db, store, task):
    """把实名确认的不可变修订转换为现有消费者支持的固定包；不自行选择审核版本。"""
    from utils import image_packages as packages
    if task['payload'].get('confirmed') is not True or not task['payload'].get('reviewer'):
        raise EditorialError('缺少实名审核确认，不能生成发布图片包')
    pk = task['job_pk']; job = source_job(db, pk)
    info, text, keys = validate_publication(db, store, pk, task['revision'])
    with version_lock(job.job_id, 'image_job'):
        directory = packages._job_directory(job)
        root = packages._root(job); root.mkdir(parents=True, exist_ok=True)
        # 原稿已在修订里永久留存，发布使用当前确认版本；编辑草稿不改这里。
        with tempfile.TemporaryDirectory(prefix='.editorial-', dir=root) as tmp:
            stage = Path(tmp) / 'bundle'; stage.mkdir()
            shutil.copyfile(store.asset(pk, info['pdf_asset']), stage / 'source.pdf')
            (stage / 'article.md').write_text(text, encoding='utf-8')
            pages = packages._render_pages(stage / 'source.pdf', stage)
            figures = []; (stage / 'images').mkdir()
            for key in keys:
                item = info['figures'][key]; target = stage / 'images' / item['asset']
                shutil.copyfile(store.asset(pk, item['asset']), target)
                figures.append({**packages._asset_record(target, stage, image=True), 'label': key.split(':')[1],
                                'is_extended': key.startswith('extended:'), 'caption': item.get('caption', ''),
                                'page': item['page'], 'bbox': item['bbox']})
            if info.get('cover_asset'):
                shutil.copyfile(store.asset(pk, info['cover_asset']), stage / 'cover.jpg')
            else:
                with Image.open(store.asset(pk, info['figures'][info['cover_key']]['asset'])) as picture:
                    fitted = ImageOps.contain(picture.convert('RGB'), (900, 383), Image.Resampling.LANCZOS)
                    canvas = Image.new('RGB', (900, 383), 'white')
                    canvas.paste(fitted, ((900-fitted.width)//2, (383-fitted.height)//2)); canvas.save(stage / 'cover.jpg', quality=94)
            from utils.wechat_html import find_image_placeholders
            from utils.figure_strategy import figure_key_from_description
            descriptions = find_image_placeholders(text)
            manifest = {'schema_version': packages.SCHEMA_VERSION, 'review_policy_version': packages.REVIEW_POLICY_VERSION,
                        'job_id': job.job_id, 'job_pk': pk, 'line': job.line,
                        'source': {'pdf': packages._asset_record(stage / 'source.pdf', stage),
                                   'markdown': packages._asset_record(stage / 'article.md', stage), 'binding': 'verified'},
                        'paper_scope': None, 'editorial_revision': task['revision'], 'publication_request': task['id'],
                        'figures': figures, 'pages': pages,
                        'slots': [{'description': d, 'label': figure_key_from_description(d)[0],
                                   'is_extended': figure_key_from_description(d)[1]} for d in descriptions],
                        'cover': {**packages._asset_record(stage / 'cover.jpg', stage, image=True),
                                  'transform': 'contain-900x383-v1',
                                  'source_figure_key': {'label': info['cover_key'].split(':')[1], 'is_extended': info['cover_key'].startswith('extended:')}}}
            bundle_id = packages._commit(job, stage, manifest)
        # 在批准前重新检查资产；正文原子切换，不能生成覆盖人工修订。
        temporary = directory / '.article.editorial.md'; temporary.write_text(text, encoding='utf-8'); temporary.replace(directory / 'article.md')
        from utils.wechat_html import markdown_to_wechat_html
        (directory / 'article.html').write_text(markdown_to_wechat_html(text), encoding='utf-8')
        db.upsert_article(pk, title=info['title'], digest=info['digest'], word_count=len(text),
                          publish_blocked=False, block_reason=None)
        # 人工最终正文确认取代旧生成时的静态调性评分，明确留存在请求中。
        packages._set_state(job, bundle_id=bundle_id, approval_id=None, status='needs_review')
        packages.confirm_package(job, bundle_id, task['payload']['reviewer'], True, expected_job_pk=pk)
        db.ensure_blog_versions(pk, str(directory), reset=False)
        return bundle_id
