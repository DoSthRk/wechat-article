import builtins
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from utils.figure_audit import audit_artifacts, ReadPolicy


class FigureArtifactAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'outputs' / 'jobs'
        self.job = self.jobs / 'paper'
        self.figures = self.job / 'figures'
        self.figures.mkdir(parents=True)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.pdf = self.inputs / 'paper.pdf'
        self.pdf.write_bytes(b'%PDF-1.4\nsource identity only; audit does not parse PDFs')
        self.image = self.figures / 'f1.png'
        Image.new('RGB', (200, 120), 'blue').save(self.image)
        self.article = '# 标题\n\n[图片:Figure 1 实验结果]\n'
        (self.job / 'article.md').write_text(self.article, encoding='utf-8')
        self.source_hash = self.digest(self.pdf)
        self.metadata = {'job_id': 'paper', 'source_pdf_sha256': self.source_hash}
        self.report = {'job_id': 'paper', 'quality_version': 1, 'article_source_binding': 'verified',
                       'article_sha256': hashlib.sha256(self.article.encode()).hexdigest(),
                       'source': {'path': str(self.pdf), 'sha256': self.source_hash},
                       'required': ['1'], 'missing': [],
                       'selected': [{'label': '1', 'extended': False, 'page': 1, 'bbox': [0, 0, 200, 120],
                                     'path': str(self.image), 'sha256': self.digest(self.image),
                                     'width': 200, 'height': 120, 'source_sha256': self.source_hash}]}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save(self):
        (self.job / 'meta.json').write_text(json.dumps(self.metadata), encoding='utf-8')
        (self.figures / 'figure_resolution_report.json').write_text(json.dumps(self.report), encoding='utf-8')

    def audit(self, **kwargs):
        return audit_artifacts(self.jobs, allowed_roots=[self.inputs], **kwargs)

    def codes(self, result):
        return {finding['code'] for finding in result['articles'][0]['findings']}

    def test_complete_structure_is_still_waiting_for_semantic_review(self):
        result = self.audit()
        article = result['articles'][0]
        self.assertEqual(article['status'], 'awaiting_semantic_review')
        self.assertTrue(article['needs_review'])
        self.assertEqual(article['semantic_review'], 'not_performed')
        self.assertEqual(article['publication_decision'], 'not_evaluated')
        self.assertEqual(article['pdf_content_validation'], 'not_performed')
        self.assertEqual(article['images'][0]['decode'], 'complete')
        self.assertEqual(result['summary']['awaiting_semantic_review'], 1)
        self.assertEqual(article['findings'], [])

    def test_source_article_hash_preserves_bom_and_crlf(self):
        raw = b'\xef\xbb\xbf' + self.article.replace('\n', '\r\n').encode('utf-8')
        (self.job / 'article.md').write_bytes(raw)
        self.report['article_sha256'] = hashlib.sha256(raw).hexdigest()
        self.save()
        result = self.audit()
        self.assertEqual(result['articles'][0]['article_sha256'], self.report['article_sha256'])
        self.assertEqual(result['articles'][0]['status'], 'awaiting_semantic_review')

    def test_missing_article_does_not_invent_reference_mismatch(self):
        (self.job / 'article.md').unlink()
        result = self.audit()
        self.assertEqual(result['articles'][0]['status'], 'missing_evidence')
        self.assertIn('missing_article', self.codes(result))
        self.assertFalse({'article_report_required_mismatch', 'reported_missing_coverage_mismatch',
                          'required_figures_not_selected'} & self.codes(result))
        self.assertIsNone(result['articles'][0]['unreferenced_selected_keys'])
        self.assertEqual(result['articles'][0]['article_references'], 'not_evaluated')

    def test_unreadable_article_does_not_invent_reference_mismatch(self):
        (self.job / 'article.md').write_bytes(b'\xff\xfe\xff')
        result = self.audit()
        self.assertIn('invalid_article', self.codes(result))
        self.assertNotIn('article_report_required_mismatch', self.codes(result))
        self.assertNotIn('reported_missing_coverage_mismatch', self.codes(result))
        self.assertIsNone(result['articles'][0]['unreferenced_selected_keys'])

    def test_no_report_is_missing_evidence_not_acceptance(self):
        (self.figures / 'figure_resolution_report.json').unlink()
        result = self.audit(pdf_paths={'paper': self.pdf})
        self.assertIn('missing_resolution_report', self.codes(result))
        self.assertEqual(result['articles'][0]['status'], 'missing_evidence')
        self.assertEqual(result['summary']['articles_with_findings']['missing_evidence'], 1)

    def test_legacy_metadata_and_report_require_review(self):
        self.metadata.pop('source_pdf_sha256')
        self.report.pop('article_sha256')
        self.report['article_source_binding'] = 'legacy_unbound'
        self.save()
        result = self.audit()
        self.assertEqual(result['articles'][0]['status'], 'missing_evidence')
        self.assertTrue({'missing_metadata_source_hash', 'missing_report_article_hash',
                         'article_source_binding_not_verified'} <= self.codes(result))

    def test_missing_image_path_is_missing_evidence_not_proven_bad_reference(self):
        self.report['selected'][0].pop('path')
        self.save()
        result = self.audit()
        self.assertIn('missing_selected_image_path', self.codes(result))
        self.assertEqual(result['articles'][0]['status'], 'missing_evidence')

    def test_corrupt_image_fails_full_decode(self):
        self.image.write_bytes(b'not an image')
        self.report['selected'][0]['sha256'] = self.digest(self.image)
        self.save()
        result = self.audit()
        self.assertIn('selected_image_decode_failed', self.codes(result))
        self.assertEqual(result['articles'][0]['images'][0]['decode'], 'incomplete')

    def test_truncated_png_fails_load_not_just_header_open(self):
        data = self.image.read_bytes()
        self.image.write_bytes(data[:60])
        self.report['selected'][0]['sha256'] = self.digest(self.image)
        self.save()
        self.assertIn('selected_image_decode_failed', self.codes(self.audit()))

    def test_multiframe_eof_is_an_image_finding_not_a_batch_exception(self):
        class BrokenFrames:
            size, n_frames = (200, 120), 2
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def seek(self, frame):
                if frame:
                    raise EOFError('broken frame')
            def load(self):
                pass
        with patch('utils.figure_audit.Image.open', return_value=BrokenFrames()):
            result = self.audit()
        self.assertIn('selected_image_decode_failed', self.codes(result))
        self.assertEqual(result['summary']['articles_total'], 1)

    def test_image_hash_drift_and_dimensions_mismatch_are_reported(self):
        Image.new('RGB', (210, 130), 'green').save(self.image)
        result = self.audit()
        self.assertTrue({'selected_image_hash_mismatch', 'selected_image_dimensions_mismatch'} <= self.codes(result))
        self.assertEqual(result['articles'][0]['status'], 'structural_issues')

    def test_pdf_hash_drift_is_distinguished_from_missing_pdf(self):
        self.pdf.write_bytes(b'%PDF-changed')
        result = self.audit()
        self.assertIn('source_pdf_metadata_hash_mismatch', self.codes(result))
        self.assertIn('source_pdf_report_hash_mismatch', self.codes(result))
        self.assertIn('selected_source_hash_mismatch', self.codes(result))
        self.pdf.unlink()
        self.assertIn('source_pdf_unavailable', self.codes(self.audit()))

    def test_different_figures_cannot_reuse_same_file_or_bytes(self):
        self.report['selected'].append({**self.report['selected'][0], 'label': '2'})
        self.save()
        result = self.audit()
        self.assertIn('same_path_for_different_figures', self.codes(result))
        self.assertIn('same_bytes_for_different_figures', self.codes(result))
        self.assertEqual(result['articles'][0]['unreferenced_selected_keys'], ['2'])

    def test_duplicate_figure_number_is_a_reference_error(self):
        self.report['selected'].append(dict(self.report['selected'][0]))
        self.save()
        self.assertIn('duplicate_selected_figure_key', self.codes(self.audit()))

    def test_wrong_report_identity_required_set_and_source_are_reported(self):
        self.report['job_id'] = 'other-article'
        self.report['required'] = ['2']
        self.report['source']['sha256'] = '0' * 64
        self.save()
        result = self.audit()
        self.assertTrue({'resolution_report_job_mismatch', 'article_report_required_mismatch',
                         'metadata_report_source_mismatch'} <= self.codes(result))

    def test_article_drift_and_missing_selected_coverage_are_reported(self):
        (self.job / 'article.md').write_text(self.article + '\n[图片:Figure 2 另一结果]')
        result = self.audit()
        self.assertTrue({'article_hash_mismatch', 'article_report_required_mismatch',
                         'required_figures_not_selected', 'reported_missing_coverage_mismatch'} <= self.codes(result))

    def test_malformed_json_and_non_object_json_do_not_abort_other_articles(self):
        (self.job / 'meta.json').write_text('[]')
        (self.figures / 'figure_resolution_report.json').write_text('{broken')
        result = self.audit()
        self.assertTrue({'invalid_metadata', 'invalid_resolution_report'} <= self.codes(result))
        self.assertEqual(result['summary']['articles_with_findings']['integrity_error'], 1)

    def test_malformed_selected_data_is_reported_not_executed(self):
        self.report['selected'] = [None, {'label': '1', 'extended': 'false'},
                                   {**self.report['selected'][0], 'bbox': [0, 0, float('nan'), 20]}]
        self.save()
        self.assertTrue({'invalid_selected_figure', 'invalid_selected_figure_key',
                         'invalid_figure_bbox'} <= self.codes(self.audit()))

    def test_outside_path_is_not_read_even_if_report_hash_is_present(self):
        outside = self.root / 'outside.png'
        Image.new('RGB', (200, 120), 'red').save(outside)
        self.report['selected'][0]['path'] = str(outside)
        self.save()
        from utils import figure_audit
        original_hash = figure_audit._sha256
        def guarded_hash(path):
            self.assertNotEqual(path, outside)
            return original_hash(path)
        with patch.object(figure_audit, '_sha256', side_effect=guarded_hash):
            result = self.audit()
        self.assertIn('path_outside_allowed_roots', self.codes(result))

    def test_symlink_escape_is_not_read(self):
        outside = self.root / 'outside.png'
        outside.write_bytes(b'do not read')
        linked = self.figures / 'linked.png'
        linked.symlink_to(outside)
        self.report['selected'][0]['path'] = str(linked)
        self.save()
        self.assertIn('path_outside_allowed_roots', self.codes(self.audit()))

    def test_other_article_image_is_not_silently_adopted(self):
        other = self.jobs / 'other'
        other.mkdir()
        image = other / 'wrong.png'
        Image.new('RGB', (200, 120), 'green').save(image)
        self.report['selected'][0]['path'] = str(image)
        self.save()
        result = self.audit()
        paper = next(item for item in result['articles'] if item['job_id'] == 'paper')
        self.assertIn('image_references_other_article', {f['code'] for f in paper['findings']})

    def test_environment_files_are_forbidden_even_inside_allowed_root(self):
        secret = self.job / '.env'
        secret.write_text('PRIVATE_KEY=not-to-be-read')
        self.report['selected'][0]['path'] = str(secret)
        self.save()
        result = self.audit()
        self.assertIn('configuration_path_forbidden', self.codes(result))
        self.assertNotIn('PRIVATE_KEY', json.dumps(result))

    def test_explicit_snapshot_path_mapping_preserves_original_source_hash(self):
        production = Path('/opt/gm-apps/article/shared')
        self.report['source']['path'] = str(production / 'inputs' / 'paper.pdf')
        self.report['selected'][0]['path'] = str(production / 'outputs/jobs/paper/figures/f1.png')
        self.save()
        result = self.audit(path_mappings={production / 'outputs/jobs': self.jobs,
                                          production / 'inputs': self.inputs})
        self.assertEqual(result['articles'][0]['status'], 'awaiting_semantic_review')
        self.assertEqual(result['articles'][0]['source_pdf_sha256'], self.source_hash)

    def test_unmapped_production_path_never_reads_live_path(self):
        self.report['selected'][0]['path'] = '/opt/gm-apps/article/shared/outputs/jobs/paper/figures/f1.png'
        self.save()
        self.assertIn('path_outside_allowed_roots', self.codes(self.audit()))

    def test_explicit_pdf_job_mapping_still_requires_allowed_root(self):
        self.report['source']['path'] = '/unavailable/original.pdf'
        self.save()
        result = self.audit(pdf_paths={'paper': self.pdf})
        self.assertNotIn('path_outside_allowed_roots', self.codes(result))
        result = audit_artifacts(self.jobs, pdf_paths={'paper': self.pdf})
        self.assertIn('path_outside_allowed_roots', self.codes(result))

    def test_mapping_outside_read_roots_is_configuration_error(self):
        with self.assertRaisesRegex(ValueError, 'outside allowed roots'):
            ReadPolicy(self.jobs, path_mappings={'/production': self.root / 'not-allowed'})
        with self.assertRaisesRegex(ValueError, 'filesystem root'):
            ReadPolicy(self.jobs, allowed_roots=['/'])

    def test_audit_makes_no_writes_and_does_not_read_environment_files(self):
        original_builtin, original_io = builtins.open, io.open
        def guard(original):
            def opening(file, mode='r', *args, **kwargs):
                self.assertFalse(any(flag in mode for flag in ('w', 'a', 'x', '+')))
                if isinstance(file, (str, Path)):
                    self.assertFalse(Path(file).name.startswith('.env'))
                return original(file, mode, *args, **kwargs)
            return opening
        before = {str(p.relative_to(self.root)): (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in self.root.rglob('*') if p.is_file()}
        with patch('builtins.open', side_effect=guard(original_builtin)), patch('io.open', side_effect=guard(original_io)):
            result = self.audit()
        after = {str(p.relative_to(self.root)): (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result['articles'][0]['status'], 'awaiting_semantic_review')

    def test_cli_prints_json_without_importing_business_code_or_creating_logs(self):
        project = Path(__file__).resolve().parents[1]
        script = project / 'scripts/audit_figure_artifacts.py'
        arguments = [str(script), '--jobs-root', str(self.jobs), '--allow-root', str(self.inputs)]
        code = (f'import runpy, sys; sys.argv = {arguments!r}; '
                f'module = runpy.run_path({str(script)!r}, run_name="audit_test"); '
                'status = module["main"](); '
                'assert "batch_processor" not in sys.modules; '
                'assert "utils.pdf_figure_extractor" not in sys.modules; '
                'assert "utils.logger" not in sys.modules; raise SystemExit(status)')
        command = [sys.executable, '-B', '-c', code]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report['scope'], 'offline_artifact_integrity_only')
        self.assertFalse((self.root / 'logs').exists())


if __name__ == '__main__':
    unittest.main()
