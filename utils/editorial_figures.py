"""Recover missing draft figures independently; never approve or publish them."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time

from PIL import Image

from utils.editorial_document import EditorialError, export_document
from utils.editorial_store import Conflict, digest


def figure_status(info):
    _, keys = export_document(info['document'])
    missing = [key for key in keys if key not in info.get('figures', {})]
    return {'status': 'needs_images' if missing or not keys else 'ready_for_review',
            'total': len(keys), 'available': len(keys)-len(missing), 'missing': missing}


def recover(db, store, pk, author, expected_revision):
    from utils.editorial_service import source_job
    from batch_processor import _resolve_job_figures
    from utils.figure_quality import filter_candidates
    from utils.figure_crop_geometry import UnsafeFigureCrop
    job = source_job(db, pk)
    head = store.head(pk)
    if not head or head['revision'] != expected_revision:
        raise Conflict('草稿已变化，请重新核对后补图')
    current = store.revision(pk, expected_revision)
    if digest(Path(job.pdf).read_bytes()) != current['source_hash']:
        raise EditorialError('源 PDF 已变化，不能向现有正文补图')
    missing = figure_status(current)['missing']
    if not missing:
        return current
    figures = dict(current['figures'])
    diagnostics = {}
    # Individual figure failures must not discard unrelated valid candidates.
    # Each resolver still enforces provenance, geometry and ambiguity checks.
    with tempfile.TemporaryDirectory(prefix='.recover-', dir=store.directory(pk)) as temporary:
        root = Path(temporary)
        source = root / 'source.pdf'
        source.write_bytes(store.asset(pk, current['pdf_asset']).read_bytes())
        snapshot_job = replace(job, pdf=str(source))
        candidates = []
        started = time.monotonic()
        for key in missing:
            if time.monotonic() - started > 300:
                diagnostics[key] = '本轮抽图时间已用完，可重试缺图或从原页裁剪'
                continue
            label = key.split(':', 1)[1]
            pair = (label, key.startswith('extended:'))
            try:
                found, _ = _resolve_job_figures(snapshot_job, {pair}, figures_dir=root / key.replace(':', '-'))
                matching = [f for f in found if (f.label, f.is_extended) == pair]
                if len(matching) == 1:
                    candidates.extend(matching)
                else:
                    diagnostics[key] = '未找到唯一且通过校验的图片，请从 PDF 原页裁剪'
            except UnsafeFigureCrop as exc:
                diagnostics[key] = str(exc)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Draft figure recovery failed job=%s key=%s', pk, key)
                diagnostics[key] = '该图提取未完成，请重试缺图或从 PDF 原页裁剪'
        accepted, rejected = filter_candidates(source, candidates, 'caption')
        for item in rejected:
            key = ('extended:' if item.get('extended') else 'main:') + item['label']
            diagnostics[key] = '图片对应关系或完整性校验未通过，请从 PDF 原页裁剪'
        for item in accepted:
            key = ('extended:' if item.is_extended else 'main:') + item.label
            try:
                import io
                with Image.open(item.image_path) as image:
                    output = io.BytesIO(); image.convert('RGB').save(output, 'JPEG', quality=95)
                asset = store.put(pk, output.getvalue(), '.jpg')
                if any(value['asset'] == asset for value in figures.values()):
                    diagnostics[key] = '与已有配图重复，请核对图号后从原页裁剪'
                    continue
                figures[key] = {'asset': asset, 'page': item.page, 'bbox': list(item.bbox),
                                'caption': item.caption, 'origin': 'automatic'}
                diagnostics.pop(key, None)
            except (OSError, ValueError):
                diagnostics[key] = '图片读取失败，请重试缺图'
    if digest(Path(job.pdf).read_bytes()) != current['source_hash']:
        raise EditorialError('抽图期间源 PDF 已变化，本次结果未导入')
    updated = {**current, 'figures': figures, 'figure_diagnostics': diagnostics}
    # CAS preserves concurrent body/crop edits. A new immutable revision keeps history.
    return store.save(pk, job.job_id, job.line, updated, expected=expected_revision, author=author)
