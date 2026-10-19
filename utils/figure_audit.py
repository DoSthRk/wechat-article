"""Offline, read-only integrity audit of existing figure artifacts.

This module never extracts figures, loads configuration, accesses the network,
or imports publication code. Complete structural evidence still requires a
separate semantic/visual review and is not a publication decision.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import hashlib
import json
import math
from pathlib import Path
import re
import warnings
from typing import Any

from PIL import Image

from utils.image_placeholders import normalize_image_placeholders

_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_MARKER = re.compile(r"\[图片:([^\[\]\n]+?)\]")
_NUMBER = re.compile(
    r"(?:(?:(?:Extended Data|Supplementary|Supplemental|Suppl\.?)\s+)?Fig(?:ure)?\.?|图|図|그림|Рисунок|Рис\.?)\s*(\d+)", re.I,
)
_EXTENDED = re.compile(r"extended\s*data|supplementary|supp\.?|附录|扩展数据", re.I)
_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp'}
PathLike = str | Path
PathMappings = Mapping[PathLike, PathLike] | Iterable[tuple[PathLike, PathLike]]


def _inside(path, roots):
    return any(path == root or root in path.parents for root in roots)


class ReadPolicy:
    """先应用明确指定的快照映射，再检查含符号链接解析后的路径边界。"""
    def __init__(self, jobs_root: PathLike, allowed_roots: Iterable[PathLike] = (),
                 path_mappings: PathMappings = ()) -> None:
        self.jobs_root = Path(jobs_root).resolve()
        if not self.jobs_root.is_dir():
            raise ValueError('jobs_root must be an existing directory')
        self.roots = tuple(dict.fromkeys([self.jobs_root, *(Path(p).resolve() for p in allowed_roots)]))
        if any(root == Path(root.anchor) for root in self.roots):
            raise ValueError('a filesystem root is not an allowed audit scope')
        items = path_mappings.items() if isinstance(path_mappings, Mapping) else path_mappings
        self.mappings = []
        for source, target in items:
            source, target = Path(source), Path(target)
            if not source.is_absolute() or not target.is_absolute():
                raise ValueError('path mappings require absolute source and destination paths')
            target = target.resolve()
            if not _inside(target, self.roots):
                raise ValueError('mapped destination is outside allowed roots')
            self.mappings.append((source, target))
        self.mappings.sort(key=lambda pair: len(pair[0].parts), reverse=True)

    def resolve(self, value: PathLike, base: PathLike) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('missing_path')
        path = Path(value)
        if path.is_absolute():
            for source, target in self.mappings:
                if path == source or source in path.parents:
                    path = target / path.relative_to(source)
                    break
        else:
            path = Path(base) / path
        path = path.resolve()
        if not _inside(path, self.roots):
            raise ValueError('path_outside_allowed_roots')
        if any(part == '.env' or part.startswith('.env.') for part in path.parts):
            raise ValueError('configuration_path_forbidden')
        return path


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _figure_key(description):
    match = _NUMBER.match(description.strip())
    if match:
        return ('extended:' if _EXTENDED.search(description) else '') + match.group(1)
    return None


def _valid_key(key):
    return isinstance(key, str) and re.fullmatch(r'(?:extended:)?\d+', key) is not None


def _read_document(path, findings, name, *, json_document=False):
    if not path.is_file():
        findings.append({'category': 'missing_evidence', 'code': f'missing_{name}'})
        return None
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('document_too_large')
        text = path.read_text(encoding='utf-8-sig')
        if not json_document:
            return text
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError('expected_json_object')
        return value
    except OSError:
        findings.append({'category': 'missing_evidence', 'code': f'unreadable_{name}'})
        return None
    except (UnicodeError, ValueError):
        findings.append({'category': 'integrity_error', 'code': f'invalid_{name}'})
        return None


def audit_article(job_dir: PathLike, policy: ReadPolicy,
                  pdf_paths: Mapping[str, PathLike] | None = None) -> dict[str, Any]:
    """只读核对一个现有文章目录；不重裁、补写缓存或作出发布判断。"""
    directory = Path(job_dir)
    result = {'job_id': directory.name, 'needs_review': True, 'semantic_review': 'not_performed',
              'publication_decision': 'not_evaluated', 'pdf_content_validation': 'not_performed',
              'findings': [], 'images': []}
    findings = result['findings']

    def add(category, code, **details):
        findings.append({'category': category, 'code': code, **details})

    def resolve(value, base=directory):
        try:
            return policy.resolve(value, base)
        except (OSError, ValueError, RuntimeError) as exc:
            add('reference_error', str(exc) if isinstance(exc, ValueError) else 'invalid_path')
            return None

    def hash_field(container, field, name):
        value = container.get(field)
        if value in (None, ''):
            add('missing_evidence', f'missing_{name}')
            return None
        if not isinstance(value, str) or not _HASH.fullmatch(value):
            add('integrity_error', f'invalid_{name}')
            return None
        return value.lower()

    directory = resolve(directory)
    if directory is None or not _inside(directory, (policy.jobs_root,)):
        if directory is not None:
            add('reference_error', 'job_directory_outside_jobs_root')
        return _finish(result)
    documents, document_paths = {}, {}
    for name, relative in [('article', 'article.md'), ('metadata', 'meta.json'),
                           ('resolution_report', 'figures/figure_resolution_report.json')]:
        path = resolve(directory / relative)
        document_paths[name] = path
        documents[name] = (_read_document(path, findings, name, json_document=name != 'article')
                           if path is not None else None)
    article, metadata, report = (documents[name] for name in ('article', 'metadata', 'resolution_report'))
    metadata, report = metadata or {}, report or {}
    for name, document in [('metadata', metadata), ('resolution_report', report)]:
        if document and document.get('job_id') not in (None, result['job_id']):
            add('reference_error', f'{name}_job_mismatch')
        elif document and not document.get('job_id'):
            add('missing_evidence', f'missing_{name}_job_id')
    required_sequence = []
    references_available = article is not None
    if article is not None:
        try:
            # 来源哈希必须保留 BOM / CRLF 等原始字节，不能对解码文本重编码。
            result['article_sha256'] = _sha256(document_paths['article'])
        except OSError:
            add('missing_evidence', 'article_hash_unavailable')
        for description in _MARKER.findall(normalize_image_placeholders(article)):
            key = _figure_key(description)
            if key is None:
                add('reference_error', 'unrecognized_article_figure_marker')
                references_available = False
            else:
                required_sequence.append(key)
        if not required_sequence:
            add('missing_evidence', 'no_article_figure_markers')
    required = set(required_sequence)
    result['required_sequence'] = required_sequence
    result['article_references'] = 'available' if references_available else 'not_evaluated'
    meta_hash = hash_field(metadata, 'source_pdf_sha256', 'metadata_source_hash')
    source = report.get('source')
    if not isinstance(source, dict):
        add('missing_evidence' if source is None else 'integrity_error', 'missing_report_source' if source is None else 'invalid_report_source')
        source = {}
    report_hash = hash_field(source, 'sha256', 'report_source_hash')
    article_hash = hash_field(report, 'article_sha256', 'report_article_hash')
    if article_hash and result.get('article_sha256') and article_hash != result['article_sha256']:
        add('integrity_error', 'article_hash_mismatch')
    if meta_hash and report_hash and meta_hash != report_hash:
        add('reference_error', 'metadata_report_source_mismatch')
    if report.get('article_source_binding') != 'verified':
        add('missing_evidence', 'article_source_binding_not_verified')
    quality = report.get('quality_version')
    if quality is None:
        add('missing_evidence', 'missing_quality_version')
    elif type(quality) is not int or quality < 1:
        add('integrity_error', 'invalid_quality_version')
    reported_required = report.get('required')
    if reported_required is None:
        add('missing_evidence', 'missing_report_required')
    elif not isinstance(reported_required, list) or not all(_valid_key(key) for key in reported_required):
        add('integrity_error', 'invalid_report_required')
    elif references_available and set(reported_required) != required:
        add('reference_error', 'article_report_required_mismatch')
    pdf_lookup = (pdf_paths or {}).get(result['job_id']) or source.get('path') or report.get('pdf')
    actual_source_hash = None
    if not pdf_lookup:
        add('missing_evidence', 'missing_source_pdf_path')
    else:
        pdf = resolve(pdf_lookup)
        if pdf is not None:
            if pdf.suffix.lower() != '.pdf':
                add('reference_error', 'source_path_not_pdf')
            elif not pdf.is_file():
                add('missing_evidence', 'source_pdf_unavailable')
            else:
                try:
                    actual_source_hash = _sha256(pdf)
                    result['source_pdf_sha256'] = actual_source_hash
                    if meta_hash and meta_hash != actual_source_hash:
                        add('integrity_error', 'source_pdf_metadata_hash_mismatch')
                    if report_hash and report_hash != actual_source_hash:
                        add('integrity_error', 'source_pdf_report_hash_mismatch')
                except OSError:
                    add('integrity_error', 'source_pdf_unreadable')
    selected = report.get('selected')
    if selected is None:
        add('missing_evidence', 'missing_selected_figures')
        selected = []
    elif not isinstance(selected, list):
        add('integrity_error', 'invalid_selected_figures')
        selected = []
    keys, digests, paths = [], {}, {}
    for index, item in enumerate(selected):
        if not isinstance(item, dict):
            add('integrity_error', 'invalid_selected_figure', index=index)
            continue
        label = item.get('label')
        extended = item.get('extended')
        if not isinstance(label, str) or not label.isdigit() or type(extended) is not bool:
            add('integrity_error', 'invalid_selected_figure_key', index=index)
            continue
        key = ('extended:' if extended else '') + label
        keys.append(key)
        summary = {'key': key, 'index': index}
        result['images'].append(summary)
        expected = hash_field(item, 'sha256', 'selected_image_hash')
        bound = hash_field(item, 'source_sha256', 'selected_source_hash')
        if bound and ((report_hash and bound != report_hash) or (actual_source_hash and bound != actual_source_hash)):
            add('reference_error', 'selected_source_hash_mismatch', key=key)
        page = item.get('page')
        if type(page) is not int or page < 1:
            add('missing_evidence', 'missing_valid_figure_page', key=key)
        bbox = item.get('bbox')
        if bbox is None:
            add('missing_evidence', 'missing_figure_bbox', key=key)
        elif (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
              or not all(type(value) in (int, float) and math.isfinite(value) for value in bbox)
              or not (0 <= bbox[0] < bbox[2] and 0 <= bbox[1] < bbox[3])):
            add('integrity_error', 'invalid_figure_bbox', key=key)
        if item.get('path') in (None, ''):
            add('missing_evidence', 'missing_selected_image_path', key=key)
            continue
        path = resolve(item.get('path'))
        if path is None:
            continue
        if _inside(path, (policy.jobs_root,)) and not _inside(path, (directory,)):
            add('reference_error', 'image_references_other_article', key=key)
            continue
        if path.suffix.lower() not in _IMAGE_SUFFIXES:
            add('reference_error', 'unsupported_image_path', key=key)
            continue
        if not path.is_file():
            add('missing_evidence', 'selected_image_unavailable', key=key)
            continue
        paths.setdefault(str(path), []).append(key)
        try:
            before = path.stat()
            digest = _sha256(path)
            digests.setdefault(digest, []).append(key)
            summary['sha256'] = digest
            if expected and digest != expected:
                add('integrity_error', 'selected_image_hash_mismatch', key=key)
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
                    frames = getattr(image, 'n_frames', 1)
                    if frames > 1000:
                        raise ValueError('excessive_image_frames')
                    for frame in range(frames):
                        image.seek(frame)
                        image.load()
            summary.update(width=width, height=height, decode='complete')
            reported_size = (item.get('width'), item.get('height'))
            if not all(type(value) is int and value > 0 for value in reported_size):
                add('missing_evidence', 'missing_valid_image_dimensions', key=key)
            elif reported_size != (width, height):
                add('integrity_error', 'selected_image_dimensions_mismatch', key=key)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                add('integrity_error', 'image_changed_during_audit', key=key)
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            summary['decode'] = 'incomplete'
            add('integrity_error', 'selected_image_decode_failed', key=key)
    for key, count in Counter(keys).items():
        if count > 1:
            add('reference_error', 'duplicate_selected_figure_key', key=key)
    for group in paths.values():
        if len(set(group)) > 1:
            add('reference_error', 'same_path_for_different_figures', keys=sorted(set(group)))
    for group in digests.values():
        if len(set(group)) > 1:
            add('integrity_error', 'same_bytes_for_different_figures', keys=sorted(set(group)))
    missing = sorted(required - set(keys)) if references_available else []
    if missing:
        add('missing_evidence', 'required_figures_not_selected', keys=missing)
    result['unreferenced_selected_keys'] = sorted(set(keys) - required) if references_available else None
    reported_missing = report.get('missing')
    if reported_missing is None:
        add('missing_evidence', 'missing_report_coverage_result')
    elif not isinstance(reported_missing, list) or not all(_valid_key(key) for key in reported_missing):
        add('integrity_error', 'invalid_report_coverage_result')
    elif references_available and set(reported_missing) != set(missing):
        add('reference_error', 'reported_missing_coverage_mismatch')
    return _finish(result)


def _finish(result):
    categories = {finding['category'] for finding in result['findings']}
    result['status'] = ('structural_issues' if categories & {'integrity_error', 'reference_error'} else
                        'missing_evidence' if 'missing_evidence' in categories else 'awaiting_semantic_review')
    return result


def audit_artifacts(jobs_root: PathLike, *, allowed_roots: Iterable[PathLike] = (),
                    pdf_paths: Mapping[str, PathLike] | None = None,
                    path_mappings: PathMappings = ()) -> dict[str, Any]:
    """枚举文章目录并返回结构化证据；缺证和视觉待核对均不视为发布许可。"""
    policy = ReadPolicy(jobs_root, allowed_roots, path_mappings)
    articles = [audit_article(directory, policy, pdf_paths) for directory in sorted(policy.jobs_root.iterdir())
                if directory.is_dir()]
    statuses = Counter(article['status'] for article in articles)
    categories = ('missing_evidence', 'integrity_error', 'reference_error')
    return {'schema_version': 1, 'scope': 'offline_artifact_integrity_only',
            'semantic_review': 'not_performed', 'publication_decision': 'not_evaluated',
            'pdf_content_validation': 'not_performed',
            'summary': {'articles_total': len(articles), 'status_counts': dict(statuses),
                        'awaiting_semantic_review': len(articles),
                        'articles_with_findings': {category: sum(any(f['category'] == category for f in item['findings'])
                                                               for item in articles) for category in categories}},
            'articles': articles}
