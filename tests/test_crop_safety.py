"""Unsafe/stale crops must not be silently accepted by a fallback or cache."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from PIL import Image
from utils.figure_crop_geometry import UnsafeFigureCrop
from utils import figure_extract_worker as worker, vision_figures as vf
from utils.pdf_figure_extractor import Figure


def test_worker_preserves_machine_readable_crop_failure():
    with TemporaryDirectory() as tmp:
        result = Path(tmp) / 'result.json'
        with patch.object(worker, '_extract', side_effect=UnsafeFigureCrop('overlap')):
            assert worker.main(['caption', 'source.pdf', tmp, '', str(result)]) == 3
        assert json.loads(result.read_text()) == {'error': 'unsafe_figure_crop', 'message': 'overlap'}


def test_vision_does_not_fallback_to_whole_page_after_unsafe_geometry():
    with patch.object(vf, '_graphics_crop_box', side_effect=UnsafeFigureCrop('overlap')):
        with pytest.raises(UnsafeFigureCrop):
            vf._make_crop('source.pdf', 0, Image.new('RGB', (600, 800)), [0, 0, 1, 1])


def test_failed_cached_recrop_does_not_mark_stale_bytes_current():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        figure = Figure('1', False, '', 1, str(root / 'old.jpg'), 600, 800)
        Image.new('RGB', (600, 800)).save(figure.image_path)
        vf._write_manifest(root / 'vision_figures_manifest.json', [figure])
        version = root / '.crop_version'
        version.write_text('0')
        with patch.object(vf, '_render', return_value=Image.new('RGB', (600, 800))), patch.object(vf, '_make_crop', side_effect=UnsafeFigureCrop('overlap')):
            with pytest.raises(UnsafeFigureCrop):
                vf.extract_figures_via_vision('source.pdf', tmp)
        assert version.read_text() == '0'


def test_parent_worker_propagates_unsafe_status():
    import subprocess
    import batch_processor as bp
    from utils.job_loader import Job
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / 'source.pdf'
        pdf.write_bytes(b'%PDF')
        def run(*args, **kwargs):
            (root / '.caption_worker_result.json').write_text(json.dumps({'error': 'unsafe_figure_crop', 'message': 'overlap'}))
            return subprocess.CompletedProcess(args[0], 3, '', '')
        with patch.object(bp.subprocess, 'run', side_effect=run):
            with pytest.raises(UnsafeFigureCrop):
                bp._run_pdf_figure_worker('caption', Job(job_id='x', pdf=str(pdf), template='t', product='p', line='immune'), root)
