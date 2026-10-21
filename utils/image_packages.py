"""Immutable figure bundles and explicit, version-bound human approval.

Extraction is permitted only while building a candidate. Publishing reads an
approved snapshot, never a mutable extraction cache or a 'reviewed' pool flag.
All operations share the outer image_job lock with the publishing consumers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid

from PIL import Image, ImageOps

from db.database import ARTICLE_CONTENT_DIR
from utils.blog_task_lock import version_lock
from utils.figure_strategy import figure_key_from_description
from utils.image_placeholders import normalize_image_placeholders, unwrap_linked_image_placeholders
from utils.pdf_figure_extractor import Figure
from utils.figure_crop_geometry import UnsafeFigureCrop
from utils.logger import setup_logger
from utils.wechat_html import find_image_placeholders

logger = setup_logger('image_packages')
SCHEMA_VERSION = 1
REVIEW_POLICY_VERSION = 1
SINGLE_CONFIRMATION_MODE = 'images_correct_v1'
CONFIRMATIONS = ('source', 'number', 'completeness', 'cover')
_ID = re.compile(r'^[0-9a-f]{64}$')


class ImagePackageError(ValueError):
    """A package cannot safely be reviewed or used for publication."""


@dataclass(frozen=True)
class ApprovedImagePackage:
    bundle_id: str
    figures: list[Figure]
    directory: Path
    cover_path: str
    source_markdown_sha256: str
    manifest: dict


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    try:
        return hashlib.sha256(_canonical(value)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ImagePackageError('图片包记录不能规范化校验') from exc


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _job_directory(job):
    name = str(job.job_id)
    if not name or name in {'.', '..'} or any(c in name for c in ('/', '\\', '\0')):
        raise ImagePackageError('任务标识不能用于安全的图包路径')
    root = Path(ARTICLE_CONTENT_DIR).resolve()
    directory = root / name
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ImagePackageError('文章目录不是安全的普通目录')
    return directory


def _root(job):
    directory = _job_directory(job) / 'image_packages'
    if directory.is_symlink():
        raise ImagePackageError('图包根目录不能是符号链接')
    return directory


def get_package_directory(job, bundle_id):
    with version_lock(job.job_id, 'image_job'):
        if not isinstance(bundle_id, str) or not _ID.fullmatch(bundle_id):
            raise ImagePackageError('图片包标识无效')
        directory = _root(job) / bundle_id
        if directory.is_symlink():
            raise ImagePackageError('图片包目录不能是符号链接')
        return directory


def _json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ImagePackageError('图片包记录缺失或不是普通文件')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ImagePackageError('图片包记录无法读取') from exc
    if not isinstance(value, dict):
        raise ImagePackageError('图片包记录格式无效')
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ImagePackageError('图片包状态路径不能是符号链接')
    temporary = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _state(job):
    path = _root(job) / 'state.json'
    return _json(path) if path.exists() or path.is_symlink() else {}


def _set_state(job, **fields):
    _atomic_json(_root(job) / 'state.json', {'job_id': job.job_id, 'updated_at': _now(), **fields})


def _safe_asset(directory, relative):
    if not isinstance(relative, str) or '\\' in relative:
        raise ImagePackageError('图片包资产路径无效')
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(p in {'.', '..'} for p in pure.parts):
        raise ImagePackageError('图片包资产路径越界')
    current = directory
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ImagePackageError('图片包资产不能包含符号链接')
    if not current.is_file() or not current.resolve().is_relative_to(directory.resolve()):
        raise ImagePackageError('图片包资产缺失或越界')
    return current


def _asset_record(path, directory, *, image=False):
    result = {'path': Path(path).relative_to(directory).as_posix(), 'sha256': _sha(path),
              'size': Path(path).stat().st_size}
    if image:
        with Image.open(path) as picture:
            picture.load()
            result.update(width=picture.width, height=picture.height)
    return result


def _records(manifest):
    yield manifest['source']['pdf'], False
    yield manifest['source']['markdown'], False
    for figure in manifest['figures']:
        yield figure, True
    for page in manifest['pages']:
        yield page, True
    yield manifest['cover'], True


def _bounds(bbox, width, height):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ImagePackageError('图片缺少有效原文裁框')
    if any(isinstance(v, bool) for v in bbox):
        raise ImagePackageError('图片原文裁框无效')
    try:
        x0, y0, x1, y1 = map(float, bbox)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ImagePackageError('图片原文裁框无效') from exc
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or not (0 <= x0 < x1 <= width + 1 and 0 <= y0 < y1 <= height + 1):
        raise ImagePackageError('图片原文裁框越界')
    return [x0, y0, x1, y1]


def _validate_manifest(job, directory, manifest, bundle_id, *, decode_previews=True):
    if _digest(manifest) != bundle_id:
        raise ImagePackageError('图片包清单已变化，原审核失效')
    if manifest.get('schema_version') != SCHEMA_VERSION or manifest.get('job_id') != job.job_id:
        raise ImagePackageError('图片包版本或所属任务不匹配')
    if manifest.get('line') != getattr(job, 'line', None):
        raise ImagePackageError('任务业务线已变化，须重新建立并审核图片包')
    if manifest.get('review_policy_version') != REVIEW_POLICY_VERSION:
        raise ImagePackageError('图片包验收策略已变化，须重新审核')
    try:
        if not manifest['figures'] or not manifest['slots']:
            raise ImagePackageError('没有可审核的正文图，禁止默认无图发布')
        page_sizes = {p['page']: (p['pdf_width'], p['pdf_height']) for p in manifest['pages']}
        if not page_sizes or set(page_sizes) != set(range(1, len(page_sizes) + 1)):
            raise ImagePackageError('原文页面预览不完整')
        registered = {}
        preview_paths = {p['path'] for p in manifest['pages']}
        for record, is_image in _records(manifest):
            path = _safe_asset(directory, record['path'])
            if record['path'] in registered and registered[record['path']] != record['sha256']:
                raise ImagePackageError('图片包存在冲突的资产记录')
            registered[record['path']] = record['sha256']
            if path.stat().st_size != record['size'] or _sha(path) != record['sha256']:
                raise ImagePackageError('图片包资产内容已变化，原审核失效')
            if is_image and (decode_previews or record['path'] not in preview_paths):
                with Image.open(path) as picture:
                    picture.load()
                    if picture.size != (record['width'], record['height']) or min(picture.size) < 60:
                        raise ImagePackageError('图片尺寸或解码结果与清单不一致')
        figures = {}
        image_hashes = set()
        for figure in manifest['figures']:
            key = (figure['label'], figure['is_extended'])
            if key in figures or figure['sha256'] in image_hashes:
                raise ImagePackageError('不同图号或区域重复使用图片，不能批准')
            if figure['page'] not in page_sizes:
                raise ImagePackageError('图片原文页码无效')
            bbox = _bounds(figure['bbox'], *page_sizes[figure['page']])
            if abs(figure['width'] / figure['height'] - (bbox[2] - bbox[0]) / (bbox[3] - bbox[1])) > .04:
                raise ImagePackageError('图片与原文裁框比例不一致')
            figures[key] = figure
            image_hashes.add(figure['sha256'])
        if set(figures) != {(s['label'], s['is_extended']) for s in manifest['slots']}:
            raise ImagePackageError('正文图片引用与固定图包不一致')
        for slot in manifest['slots']:
            if figure_key_from_description(slot['description']) != (slot['label'], slot['is_extended']):
                raise ImagePackageError('正文图片描述与图号不一致')
        cover = manifest['cover']
        if (cover['width'], cover['height']) != (900, 383) or cover['transform'] != 'contain-900x383-v1':
            raise ImagePackageError('封面规格与审核约定不一致')
        if (cover['source_figure_key']['label'], cover['source_figure_key']['is_extended']) not in figures:
            raise ImagePackageError('封面来源图不在本图片包')
    except ImagePackageError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ImagePackageError('图片包清单或文件完整性检查失败') from exc


def _load(job, bundle_id, *, decode_previews=True):
    directory = get_package_directory(job, bundle_id)
    manifest = _json(directory / 'manifest.json')
    _validate_manifest(job, directory, manifest, bundle_id, decode_previews=decode_previews)
    return directory, manifest


def resolve_review_asset(job, bundle_id, relative):
    """只校验并返回一个已登记的审核资产；此只读接口绝不授予发布资格。

    原子落盘的包不会出现半包。逐图请求在统一任务锁内仅检查本资产，
    不重复解码/哈希整个包；业务线访问权限由调用 API 在进入前校验。
    """
    with version_lock(job.job_id, 'image_job'):
        return _resolve_review_asset(job, bundle_id, relative)


def _resolve_review_asset(job, bundle_id, relative):
    directory = get_package_directory(job, bundle_id)
    manifest = _json(directory / 'manifest.json')
    if (_digest(manifest) != bundle_id or manifest.get('job_id') != job.job_id
            or manifest.get('schema_version') != SCHEMA_VERSION
            or manifest.get('line') != getattr(job, 'line', None)):
        raise ImagePackageError('审核资产清单或任务身份不匹配')
    try:
        review_assets = [manifest['source']['pdf'], *manifest['figures'],
                         *manifest['pages'], manifest['cover']]
        matches = [record for record in review_assets if record['path'] == relative]
        if not matches or len({(m['sha256'], m['size']) for m in matches}) != 1:
            raise ImagePackageError('请求的资产不在图片包审核白名单中')
        record = matches[0]
        path = _safe_asset(directory, relative)
        if path.stat().st_size != record['size'] or _sha(path) != record['sha256']:
            raise ImagePackageError('审核资产已变化，请重新建立候选')
        return path
    except ImagePackageError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ImagePackageError('审核资产无法安全读取') from exc


def _current_sources(job, manifest):
    try:
        if _sha(job.pdf) != manifest['source']['pdf']['sha256']:
            raise ImagePackageError('原文 PDF 已变化，须建立并审核新图片包')
        if _sha(_job_directory(job) / 'article.md') != manifest['source']['markdown']['sha256']:
            raise ImagePackageError('基准正文已变化，须建立并审核新图片包')
    except OSError as exc:
        raise ImagePackageError('当前原文或正文缺失，不能继续使用旧图片包') from exc


def _review_record(job, record_id):
    if not isinstance(record_id, str) or not _ID.fullmatch(record_id):
        raise ImagePackageError('审核记录标识无效')
    directory = _root(job) / 'reviews'
    if directory.is_symlink():
        raise ImagePackageError('审核记录目录不能是符号链接')
    record = _json(directory / f'{record_id}.json')
    if _digest(record) != record_id or record.get('job_id') != job.job_id:
        raise ImagePackageError('审核记录已变化或不属于本任务')
    return record


def _save_review(job, record):
    record_id = _digest(record)
    _atomic_json(_root(job) / 'reviews' / f'{record_id}.json', record)
    return record_id


def _get(job, package_id=None, *, decode_previews=True):
    state = _state(job)
    bundle_id = package_id or state.get('bundle_id')
    if not bundle_id:
        return {'bundle_id': '', 'status': state.get('status', 'missing'),
                'reason': state.get('reason', '尚未构建待审核图片包'), 'manifest': None, 'approval': None}
    result = {'bundle_id': bundle_id, 'status': 'blocked', 'reason': '', 'manifest': None, 'approval': None}
    try:
        directory, manifest = _load(job, bundle_id, decode_previews=decode_previews)
        result.update(manifest=manifest, directory=str(directory))
        _current_sources(job, manifest)
        if state.get('bundle_id') != bundle_id:
            return {**result, 'status': 'superseded', 'reason': '这不是当前候选或批准包，不能回退到旧版本'}
        record_id = state.get('approval_id')
        if record_id:
            approval = _review_record(job, record_id)
            if approval.get('bundle_id') != bundle_id:
                raise ImagePackageError('批准记录与当前图片包不匹配')
            result['approval'] = approval
            if approval.get('decision') == 'approved':
                if approval.get('review_policy_version') != REVIEW_POLICY_VERSION:
                    raise ImagePackageError('图片包审核策略版本不匹配')
                mode = approval.get('confirmation_mode')
                if mode == SINGLE_CONFIRMATION_MODE:
                    if approval.get('images_correct') is not True:
                        raise ImagePackageError('图片包缺少明确的图片正确确认')
                elif mode is None:
                    # Preserve detailed historical approvals without pretending
                    # the single-button reviewer supplied their old fields.
                    if (not manifest.get('paper_scope') or approval.get('paper_scope') != manifest['paper_scope']
                            or any(approval.get('confirmations', {}).get(k) is not True for k in CONFIRMATIONS)):
                        raise ImagePackageError('图片包缺少完整来源范围或审核确认')
                    if approval.get('figure_confirmations') is not None:
                        _figure_confirmations(approval['figure_confirmations'], manifest)
                else:
                    raise ImagePackageError('图片包审核确认方式无效')
                result['status'] = 'approved'
            else:
                result.update(status='revoked', reason='此图片包的批准已撤销，不能发布')
        else:
            result.update(status='needs_review', reason='请确认原文归属、图号、面板完整性和封面后批准')
    except ImagePackageError as exc:
        result.update(status='stale' if result['manifest'] else 'blocked', reason=str(exc))
    return result


def get_package(job, package_id=None):
    with version_lock(job.job_id, 'image_job'):
        return _get(job, package_id)


def _render_pages(pdf_path, directory):
    """Render untouched full source pages, not reconstructed scientific figures."""
    import pypdfium2 as pdfium

    pages = []
    (directory / 'pages').mkdir()
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if not 1 <= len(document) <= 200:
            raise ImagePackageError('原文页数超出本次审核预览范围，请人工处理')
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
                rendered = page.render(scale=1.5)
                try:
                    picture = rendered.to_pil()
                    target = directory / 'pages' / f'page-{index + 1}.png'
                    picture.save(target, format='PNG')
                finally:
                    rendered.close()
                pages.append({**_asset_record(target, directory, image=True), 'page': index + 1,
                              'pdf_width': width, 'pdf_height': height})
            finally:
                page.close()
    finally:
        document.close()
    return pages


def _commit(job, stage, manifest):
    bundle_id = _digest(manifest)
    destination = get_package_directory(job, bundle_id)
    (stage / 'manifest.json').write_bytes(_canonical(manifest))
    _validate_manifest(job, stage, manifest, bundle_id)
    if destination.exists():
        _load(job, bundle_id)
    else:
        os.replace(stage, destination)
    return bundle_id


def _source_binding(job, pdf_sha):
    path = _job_directory(job) / 'meta.json'
    if not path.exists():
        return 'legacy_unbound'
    metadata = _json(path)
    expected = metadata.get('source_pdf_sha256')
    if expected and expected != pdf_sha:
        raise ImagePackageError('正文生成时的原文与当前 PDF 不一致，不能建立图包')
    return 'verified' if expected else 'legacy_unbound'


def build_package(job, *, job_pk=None):
    """Build a complete unapproved candidate; any failure invalidates old admission."""
    with version_lock(job.job_id, 'image_job'):
        root = _root(job)
        root.mkdir(parents=True, exist_ok=True)
        _set_state(job, bundle_id='', approval_id=None, status='building',
                   reason='正在建立新候选，旧批准不能用于本次发布')
        try:
            markdown = _job_directory(job) / 'article.md'
            pdf_sha, markdown_sha = _sha(job.pdf), _sha(markdown)
            binding = _source_binding(job, pdf_sha)
            text = unwrap_linked_image_placeholders(normalize_image_placeholders(markdown.read_text(encoding='utf-8')))
            descriptions = find_image_placeholders(text)
            keys = [figure_key_from_description(description) for description in descriptions]
            if not keys or None in keys:
                raise ImagePackageError('正文没有完整可识别的图号引用，禁止默认无图发布')
            with tempfile.TemporaryDirectory(prefix='.build-', dir=root) as temporary:
                stage = Path(temporary) / 'bundle'
                stage.mkdir()
                shutil.copyfile(job.pdf, stage / 'source.pdf')
                shutil.copyfile(markdown, stage / 'article.md')
                from batch_processor import _resolve_job_figures
                # Extract only from the copied immutable source. Even an outside
                # writer that changes then restores the input cannot mix sources
                # between the reviewed PDF and its candidate crops.
                extraction_job = replace(job, pdf=str(stage / 'source.pdf'))
                candidates, _ = _resolve_job_figures(extraction_job, set(keys), figures_dir=Path(temporary) / 'extraction')
                by_key = {}
                for figure in candidates:
                    key = (figure.label, figure.is_extended)
                    if key not in keys:
                        continue
                    if key in by_key:
                        raise ImagePackageError('同一图号对应多个候选区域，须先解决歧义')
                    by_key[key] = figure
                if set(by_key) != set(keys):
                    raise ImagePackageError('必需图片未全部找到，不能建立可批准图片包')
                pages = _render_pages(stage / 'source.pdf', stage)
                figures = []
                (stage / 'images').mkdir()
                for key in dict.fromkeys(keys):
                    figure = by_key[key]
                    suffix = Path(figure.image_path).suffix.lower()
                    if suffix not in {'.png', '.jpg', '.jpeg', '.webp'}:
                        raise ImagePackageError('候选图不是支持的完整图片文件')
                    digest = _sha(figure.image_path)
                    target = stage / 'images' / f'{digest}{suffix}'
                    shutil.copyfile(figure.image_path, target)
                    record = _asset_record(target, stage, image=True)
                    if record['sha256'] != digest or (figure.source_sha256 and figure.source_sha256 != pdf_sha):
                        raise ImagePackageError('候选图片在构建期间发生变化')
                    figures.append({**record, 'label': key[0], 'is_extended': key[1],
                                    'caption': figure.caption, 'page': figure.page, 'bbox': list(figure.bbox or [])})
                cover_file = stage / 'cover.jpg'
                with Image.open(stage / figures[0]['path']) as source_image:
                    fitted = ImageOps.contain(source_image.convert('RGB'), (900, 383), Image.Resampling.LANCZOS)
                    canvas = Image.new('RGB', (900, 383), 'white')
                    canvas.paste(fitted, ((900 - fitted.width) // 2, (383 - fitted.height) // 2))
                    canvas.save(cover_file, quality=92)
                from utils.figure_quality import QUALITY_VERSION
                from utils.caption_figures import _CAPTION_VERSION
                from utils.pdf_figure_extractor import _CROP_VERSION
                from utils.vision_figures import _CROP_VERSION as vision_version
                manifest = {'schema_version': SCHEMA_VERSION, 'review_policy_version': REVIEW_POLICY_VERSION,
                            'job_id': job.job_id, 'job_pk': job_pk, 'line': getattr(job, 'line', None),
                            'source': {'pdf': _asset_record(stage / 'source.pdf', stage),
                                       'markdown': _asset_record(stage / 'article.md', stage), 'binding': binding},
                            'extractors': {'caption': _CAPTION_VERSION, 'heuristic': _CROP_VERSION, 'vision': vision_version},
                            'screening_version': QUALITY_VERSION, 'paper_scope': None,
                            'slots': [{'description': description, 'label': key[0], 'is_extended': key[1]}
                                      for description, key in zip(descriptions, keys)], 'figures': figures, 'pages': pages,
                            'cover': {**_asset_record(cover_file, stage, image=True), 'transform': 'contain-900x383-v1',
                                      'source_figure_key': {'label': keys[0][0], 'is_extended': keys[0][1]}}}
                if manifest['source']['pdf']['sha256'] != pdf_sha or manifest['source']['markdown']['sha256'] != markdown_sha:
                    raise ImagePackageError('原文快照在复制期间发生变化')
                _current_sources(job, manifest)
                bundle_id = _commit(job, stage, manifest)
            _set_state(job, bundle_id=bundle_id, approval_id=None, status='needs_review')
            return _get(job, bundle_id)
        except Exception as exc:
            if isinstance(exc, (ImagePackageError, UnsafeFigureCrop)):
                reason = str(exc)
                logger.warning('[%s] 图片候选被拦下：%s', job.job_id, reason)
            else:
                diagnostic_id = uuid.uuid4().hex[:12]
                logger.exception('[%s] 图片包构建失败，诊断编号 %s', job.job_id, diagnostic_id)
                reason = f'图片包构建失败，未批准任何图片。请联系管理员并提供诊断编号 {diagnostic_id}'
            _set_state(job, bundle_id='', approval_id=None, status='blocked', reason=reason)
            return {'bundle_id': '', 'status': 'blocked', 'reason': reason, 'manifest': None, 'approval': None}


def _paper_scope(value, manifest):
    if not isinstance(value, dict):
        raise ImagePackageError('请填写原文身份和范围')
    title, doi, evidence = (str(value.get(k) or '').strip() for k in ('title', 'doi', 'evidence'))
    if not (title or doi) or not evidence:
        raise ImagePackageError('请填写原文标题或 DOI，以及身份核对依据')
    pages = value.get('pages')
    if not isinstance(pages, list) or not pages or any(type(p) is not int for p in pages):
        raise ImagePackageError('请填写原文的有效页码列表')
    sizes = {p['page']: (p['pdf_width'], p['pdf_height']) for p in manifest['pages']}
    if not set(pages).issubset(sizes) or not {f['page'] for f in manifest['figures']}.issubset(pages):
        raise ImagePackageError('论文范围必须覆盖全部配图且不能超出原文')
    result = {'title': title, 'doi': doi, 'evidence': evidence, 'pages': sorted(set(pages))}
    for field in ('regions', 'exclude_regions'):
        regions = value.get(field, [])
        if not isinstance(regions, list):
            raise ImagePackageError('同页论文区域格式无效')
        checked = []
        for region in regions:
            if not isinstance(region, dict) or type(region.get('page')) is not int or region['page'] not in pages:
                raise ImagePackageError('同页论文区域页码无效')
            checked.append({'page': region['page'], 'bbox': _bounds(region.get('bbox'), *sizes[region['page']])})
        result[field] = checked
    for figure in manifest['figures']:
        x0, y0, x1, y1 = figure['bbox']
        allowed = [r['bbox'] for r in result['regions'] if r['page'] == figure['page']]
        if allowed and not any(a <= x0 and b <= y0 and c >= x1 and d >= y1 for a, b, c, d in allowed):
            raise ImagePackageError('配图不在人工确认的同页论文区域内')
        for region in result['exclude_regions']:
            a, b, c, d = region['bbox']
            if region['page'] == figure['page'] and max(a, x0) < min(c, x1) and max(b, y0) < min(d, y1):
                raise ImagePackageError('配图包含人工标记的其他论文区域')
    return result


def _figure_confirmations(value, manifest):
    keys = {f"{'extended' if figure['is_extended'] else 'main'}:{figure['label']}"
            for figure in manifest['figures']}
    if not isinstance(value, dict) or set(value) != keys or any(item is not True for item in value.values()):
        raise ImagePackageError('逐图确认必须完整对应当前图片包，且每张图均明确确认')
    return {key: True for key in sorted(keys)}


def approve_package(job, package_id, reviewer, confirmations, paper_scope, note='', *, figure_confirmations=None):
    with version_lock(job.job_id, 'image_job'):
        reviewer = str(reviewer or '').strip()
        if not reviewer or not isinstance(confirmations, dict) or any(confirmations.get(k) is not True for k in CONFIRMATIONS):
            raise ImagePackageError('必须由已授权审核者逐项确认来源、图号、完整性和封面')
        state = _state(job)
        if state.get('bundle_id') != package_id:
            raise ImagePackageError('候选图片包已过期，请重新打开当前候选')
        directory, manifest = _load(job, package_id)
        _current_sources(job, manifest)
        checked_figures = (_figure_confirmations(figure_confirmations, manifest)
                           if figure_confirmations is not None else None)
        scope = _paper_scope(paper_scope, manifest)
        final_manifest = {**manifest, 'paper_scope': scope}
        # Scope is approved data, so it becomes part of the final immutable ID.
        with tempfile.TemporaryDirectory(prefix='.approve-', dir=_root(job)) as temporary:
            stage = Path(temporary) / 'bundle'
            stage.mkdir()
            for record, _ in _records(manifest):
                target = stage / record['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_safe_asset(directory, record['path']), target)
            _current_sources(job, final_manifest)
            final_id = _commit(job, stage, final_manifest)
        record = {'job_id': job.job_id, 'bundle_id': final_id, 'candidate_id': package_id,
                  'decision': 'approved', 'reviewer': reviewer, 'reviewed_at': _now(),
                  'confirmations': {k: True for k in CONFIRMATIONS}, 'paper_scope': scope,
                  'figure_confirmations': checked_figures,
                  'legacy_source_confirmed': manifest['source']['binding'] == 'legacy_unbound',
                  'note': str(note or ''), 'review_policy_version': REVIEW_POLICY_VERSION}
        review_id = _save_review(job, record)
        _set_state(job, bundle_id=final_id, approval_id=review_id, status='approved')
        return _get(job, final_id)


def confirm_package(job, package_id, reviewer, images_correct, *, expected_job_pk):
    """Record the human's single image-correctness decision on this exact bundle."""
    with version_lock(job.job_id, 'image_job'):
        reviewer = str(reviewer or '').strip()
        if not reviewer or images_correct is not True:
            raise ImagePackageError('请由已登录的审核人明确确认图片正确')
        package = _get(job, package_id)
        if package['status'] not in {'needs_review', 'revoked', 'approved'}:
            raise ImagePackageError(package['reason'] or '当前图片包不能确认，请联系负责人处理')
        manifest = package['manifest']
        if (type(expected_job_pk) is not int or type(manifest.get('job_pk')) is not int
                or manifest['job_pk'] != expected_job_pk):
            raise ImagePackageError('图片包不属于当前数据库文章版本，不能确认')
        if package['status'] == 'approved':
            return package
        record = {'job_id': job.job_id, 'bundle_id': package_id, 'candidate_id': package_id,
                  'decision': 'approved', 'reviewer': reviewer, 'reviewed_at': _now(),
                  'confirmation_mode': SINGLE_CONFIRMATION_MODE, 'images_correct': True,
                  'review_policy_version': REVIEW_POLICY_VERSION}
        review_id = _save_review(job, record)
        _set_state(job, bundle_id=package_id, approval_id=review_id, status='approved')
        return _get(job, package_id)


def revoke_package(job, package_id, reviewer, note=''):
    with version_lock(job.job_id, 'image_job'):
        if not str(reviewer or '').strip():
            raise ImagePackageError('撤销批准必须记录已授权操作者')
        if _state(job).get('bundle_id') != package_id:
            raise ImagePackageError('不能撤销非当前图片包或回退旧版本')
        review_id = _save_review(job, {'job_id': job.job_id, 'bundle_id': package_id,
                                     'decision': 'revoked', 'reviewer': str(reviewer).strip(),
                                     'reviewed_at': _now(), 'note': str(note or '')})
        _set_state(job, bundle_id=package_id, approval_id=review_id, status='revoked')
        return _get(job, package_id)


def require_approved_package(job, expected_package_id=None, *, expected_job_pk=None):
    with version_lock(job.job_id, 'image_job'):
        if expected_package_id is not None and _state(job).get('bundle_id') != expected_package_id:
            raise ImagePackageError('任务绑定的图片包已失效，禁止切换到其他版本或重新猜图')
        result = _get(job, expected_package_id, decode_previews=False)
        if result['status'] != 'approved':
            raise ImagePackageError(result['reason'] or '图片包尚未批准，已阻止发布')
        manifest, directory = result['manifest'], Path(result['directory'])
        if expected_job_pk is not None and (
                type(expected_job_pk) is not int or type(manifest.get('job_pk')) is not int
                or manifest['job_pk'] != expected_job_pk):
            raise ImagePackageError('图片包不属于当前数据库文章版本，禁止复用其他任务的批准')
        figures = [Figure(label=f['label'], is_extended=f['is_extended'], caption=f['caption'],
                          page=f['page'], image_path=str(directory / f['path']), width=f['width'], height=f['height'],
                          bbox=tuple(f['bbox']), source_sha256=manifest['source']['pdf']['sha256'])
                   for f in manifest['figures']]
        return ApprovedImagePackage(result['bundle_id'], figures, directory, str(directory / manifest['cover']['path']),
                                    manifest['source']['markdown']['sha256'], manifest)
