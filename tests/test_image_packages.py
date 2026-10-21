"""Immutable packages: no network, no approval inferred from extraction output."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from utils import image_packages as ip
from utils.job_loader import Job
from utils.pdf_figure_extractor import Figure


class ImagePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.article_root = self.root / 'articles'
        self.directory = self.article_root / 'paper'
        self.directory.mkdir(parents=True)
        self.pdf = self.root / 'input.pdf'
        self.pdf.write_bytes(b'%PDF test source identity')
        (self.directory / 'article.md').write_text('# Article\n\n[图片:Figure 1 Experiment]\n\n[图片:Figure 2 Result]', encoding='utf-8')
        (self.directory / 'meta.json').write_text('{}')
        self.job = Job('paper', str(self.pdf), 'template', 'product', line='aav')
        self.figures = []
        for number, color in [('1', 'blue'), ('2', 'red')]:
            path = self.root / f'figure-{number}.png'
            image = Image.new('RGB', (400, 400), 'white')
            ImageDraw.Draw(image).ellipse((20, 20, 380, 380), fill=color)
            image.save(path)
            self.figures.append(Figure(number, False, f'Figure {number} Experiment', 1,
                                       str(path), 400, 400, (50, 50, 450, 450)))
        self.patches = [patch.object(ip, 'ARTICLE_CONTENT_DIR', self.article_root),
                        patch('utils.blog_task_lock.LOCK_DIR', self.root / 'locks'),
                        patch('batch_processor._resolve_job_figures', return_value=(self.figures, self.root)),
                        patch.object(ip, '_render_pages', side_effect=self.render_pages)]
        for item in self.patches:
            item.start()
        self.confirmations = dict.fromkeys(ip.CONFIRMATIONS, True)
        self.scope = {'title': 'Source paper', 'doi': '', 'evidence': 'Title and figures verified against source PDF', 'pages': [1]}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def render_pages(self, source, directory):
        (directory / 'pages').mkdir()
        target = directory / 'pages/page-1.png'
        Image.new('RGB', (600, 800), 'white').save(target)
        return [{**ip._asset_record(target, directory, image=True), 'page': 1, 'pdf_width': 600, 'pdf_height': 800}]

    def build(self):
        result = ip.build_package(self.job, job_pk=7)
        self.assertEqual(result['status'], 'needs_review', result)
        return result

    def approve(self):
        candidate = self.build()
        result = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)
        self.assertEqual(result['status'], 'approved', result)
        return result

    def test_candidate_cannot_publish_and_approval_binds_scope_into_new_id(self):
        candidate = self.build()
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)
        self.assertNotEqual(approved['bundle_id'], candidate['bundle_id'])
        package = ip.require_approved_package(self.job, approved['bundle_id'])
        self.assertEqual(package.bundle_id, approved['bundle_id'])
        self.assertEqual(len(package.figures), 2)
        self.assertEqual(package.source_markdown_sha256, ip._sha(self.directory / 'article.md'))
        with Image.open(package.cover_path) as cover:
            self.assertEqual(cover.size, (900, 383))
        self.assertEqual(ip.get_package(self.job, candidate['bundle_id'])['status'], 'superseded')

    def test_legacy_binding_requires_explicit_source_confirmation_without_rewriting_meta(self):
        candidate = self.build()
        self.assertEqual(candidate['manifest']['source']['binding'], 'legacy_unbound')
        confirmations = {**self.confirmations, 'source': False}
        with self.assertRaises(ip.ImagePackageError):
            ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', confirmations, self.scope)
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)
        self.assertTrue(approved['approval']['legacy_source_confirmed'])
        self.assertEqual(json.loads((self.directory / 'meta.json').read_text()), {})

    def test_current_pdf_or_markdown_change_invalidates_package(self):
        approved = self.approve()
        self.pdf.write_bytes(b'%PDF another source')
        self.assertEqual(ip.get_package(self.job)['status'], 'stale')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job, approved['bundle_id'])
        self.pdf.write_bytes(b'%PDF test source identity')
        (self.directory / 'article.md').write_text('Changed source text')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)

    def test_generation_source_mismatch_blocks_before_extraction(self):
        (self.directory / 'meta.json').write_text(json.dumps({'source_pdf_sha256': 'wrong'}))
        with patch('batch_processor._resolve_job_figures') as extract:
            result = ip.build_package(self.job)
        self.assertEqual(result['status'], 'blocked')
        extract.assert_not_called()

    def test_approved_assets_and_manifest_are_checked_again_before_publication(self):
        approved = self.approve()
        package = ip.require_approved_package(self.job)
        image = Path(package.figures[0].image_path)
        original = image.read_bytes()
        image.write_bytes(b'not a valid image')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)
        image.write_bytes(original)
        manifest_path = package.directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['figures'][0]['label'] = '3'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job, approved['bundle_id'])

    def test_review_record_cannot_be_edited_without_invalidating_approval(self):
        self.approve()
        state = ip._state(self.job)
        record = ip._root(self.job) / 'reviews' / f"{state['approval_id']}.json"
        value = json.loads(record.read_text())
        value['reviewer'] = 'another-reviewer'
        record.write_text(json.dumps(value))
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)

    def test_revocation_stops_publication_without_removing_assets(self):
        approved = self.approve()
        package = ip.require_approved_package(self.job)
        revoked = ip.revoke_package(self.job, approved['bundle_id'], 'reviewer', 'incorrect panel')
        self.assertEqual(revoked['status'], 'revoked')
        self.assertTrue(Path(package.figures[0].image_path).is_file())
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)

    def test_new_candidate_or_failed_build_never_falls_back_to_old_approval(self):
        approved = self.approve()
        candidate = self.build()
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job, approved['bundle_id'])
        with self.assertRaises(ip.ImagePackageError):
            ip.approve_package(self.job, approved['bundle_id'], 'reviewer', self.confirmations, self.scope)
        with patch.object(ip, '_render_pages', side_effect=RuntimeError('interrupted')):
            failed = ip.build_package(self.job)
        self.assertEqual(failed['status'], 'blocked')
        self.assertEqual(ip.get_package(self.job)['status'], 'blocked')
        with self.assertRaises(ip.ImagePackageError):
            ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)

    def test_missing_or_duplicate_figures_cannot_be_approved(self):
        with patch('batch_processor._resolve_job_figures', return_value=(self.figures[:1], self.root)):
            self.assertEqual(ip.build_package(self.job)['status'], 'blocked')
        self.figures[1].image_path = self.figures[0].image_path
        self.assertEqual(ip.build_package(self.job)['status'], 'blocked')

    def test_no_figure_article_is_blocked_by_default(self):
        (self.directory / 'article.md').write_text('# A text-only article')
        result = ip.build_package(self.job)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['bundle_id'], '')

    def test_unsafe_crop_reason_is_preserved_without_approval(self):
        with patch('batch_processor._resolve_job_figures', side_effect=ip.UnsafeFigureCrop('同一图号对应多个区域')):
            result = ip.build_package(self.job)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], '同一图号对应多个区域')
        self.assertEqual(ip.get_package(self.job)['reason'], result['reason'])
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)

    def test_unexpected_build_failure_has_private_trace_and_public_reference(self):
        with patch('batch_processor._resolve_job_figures', side_effect=RuntimeError('private-path-secret')), self.assertLogs(ip.logger, 'ERROR') as logs:
            result = ip.build_package(self.job)
        self.assertNotIn('private-path-secret', result['reason'])
        self.assertRegex(result['reason'], r'诊断编号 [0-9a-f]{12}')
        self.assertIn('private-path-secret', '\n'.join(logs.output))
        self.assertEqual(result['status'], 'blocked')

    def test_crop_provenance_is_required_even_for_manual_candidates(self):
        self.figures[0].bbox = None
        self.assertEqual(ip.build_package(self.job)['status'], 'blocked')

    def test_scope_must_include_figure_pages_and_exclude_other_paper_regions(self):
        candidate = self.build()
        invalid_scopes = [{**self.scope, 'pages': [2]}, {**self.scope, 'evidence': ''},
                          {**self.scope, 'regions': [{'page': 1, 'bbox': [0, 0, 40, 40]}]},
                          {**self.scope, 'exclude_regions': [{'page': 1, 'bbox': [100, 100, 200, 200]}]}]
        for scope in invalid_scopes:
            with self.subTest(scope=scope), self.assertRaises(ip.ImagePackageError):
                ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, scope)

    def test_source_changes_during_build_do_not_create_approvable_package(self):
        def change_source(*args):
            result = self.render_pages(*args)
            self.pdf.write_bytes(b'replaced during processing')
            return result
        with patch.object(ip, '_render_pages', side_effect=change_source):
            result = ip.build_package(self.job)
        self.assertEqual(result['status'], 'blocked')

    def test_staging_directories_are_not_published_or_discovered(self):
        self.approve()
        observed = []
        def during_render(*args):
            observed.append(ip.get_package(self.job)['status'])
            raise RuntimeError('stop before commit')
        with patch.object(ip, '_render_pages', side_effect=during_render):
            self.assertEqual(ip.build_package(self.job)['status'], 'blocked')
        self.assertEqual(observed, ['building'])
        self.assertFalse(list(ip._root(self.job).glob('.build-*')))
        with self.assertRaises(ip.ImagePackageError):
            ip.get_package_directory(self.job, '.build-incomplete')

    def test_asset_symlink_and_job_path_escape_are_rejected(self):
        self.approve()
        package = ip.require_approved_package(self.job)
        asset = Path(package.figures[0].image_path)
        external = self.root / 'outside.png'
        external.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(external)
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)
        unsafe = Job('../escape', str(self.pdf), 't', 'p')
        with self.assertRaises(ip.ImagePackageError):
            ip.get_package(unsafe)

    def test_explicit_old_bundle_never_switches_to_current(self):
        approved = self.approve()
        self.build()
        with self.assertRaisesRegex(ip.ImagePackageError, '禁止切换'):
            ip.require_approved_package(self.job, approved['bundle_id'])

    def test_expected_job_pk_accepts_only_exact_database_revision(self):
        approved = self.approve()
        package = ip.require_approved_package(self.job, approved['bundle_id'], expected_job_pk=7)
        self.assertEqual(package.manifest['job_pk'], 7)
        for other in (8, '7', 7.0, True):
            with self.subTest(other=other), self.assertRaisesRegex(ip.ImagePackageError, '数据库文章版本'):
                ip.require_approved_package(self.job, approved['bundle_id'], expected_job_pk=other)

    def test_expected_job_pk_rejects_missing_manifest_revision(self):
        candidate = ip.build_package(self.job)
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)
        self.assertIsNone(approved['manifest']['job_pk'])
        with self.assertRaisesRegex(ip.ImagePackageError, '数据库文章版本'):
            ip.require_approved_package(self.job, approved['bundle_id'], expected_job_pk=7)

    def test_expected_job_pk_rejects_non_integer_manifest_revision(self):
        candidate = ip.build_package(self.job, job_pk=7.0)
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope)
        with self.assertRaisesRegex(ip.ImagePackageError, '数据库文章版本'):
            ip.require_approved_package(self.job, approved['bundle_id'], expected_job_pk=7)

    def test_publish_read_does_not_call_extractor(self):
        self.approve()
        with patch('batch_processor._resolve_job_figures', side_effect=AssertionError('must not extract')):
            ip.require_approved_package(self.job)

    def test_review_requires_exact_booleans_and_named_author(self):
        candidate = self.build()
        with self.assertRaises(ip.ImagePackageError):
            ip.approve_package(self.job, candidate['bundle_id'], '', self.confirmations, self.scope)
        with self.assertRaises(ip.ImagePackageError):
            ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', dict.fromkeys(ip.CONFIRMATIONS, 'true'), self.scope)

    def test_single_confirmation_is_bound_to_bundle_without_fabricated_scope_or_checks(self):
        candidate = self.build()
        approved = ip.confirm_package(self.job, candidate['bundle_id'], 'human', True, expected_job_pk=7)
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(approved['bundle_id'], candidate['bundle_id'])
        self.assertEqual(approved['approval']['confirmation_mode'], ip.SINGLE_CONFIRMATION_MODE)
        self.assertIs(approved['approval']['images_correct'], True)
        self.assertIsNone(approved['manifest']['paper_scope'])
        for field in ('paper_scope', 'confirmations', 'figure_confirmations'):
            self.assertNotIn(field, approved['approval'])
        self.assertEqual(ip.require_approved_package(self.job, approved['bundle_id'], expected_job_pk=7).bundle_id,
                         candidate['bundle_id'])
        again = ip.confirm_package(self.job, candidate['bundle_id'], 'other-human', True, expected_job_pk=7)
        self.assertEqual(again['approval'], approved['approval'])

    def test_single_confirmation_rejects_missing_decision_wrong_job_and_changed_source(self):
        candidate = self.build()
        for decision in (False, None, 1, 'true'):
            with self.subTest(decision=decision), self.assertRaises(ip.ImagePackageError):
                ip.confirm_package(self.job, candidate['bundle_id'], 'human', decision, expected_job_pk=7)
        for job_pk in (8, None, True, '7', 7.0):
            with self.subTest(job_pk=job_pk), self.assertRaises(ip.ImagePackageError):
                ip.confirm_package(self.job, candidate['bundle_id'], 'human', True, expected_job_pk=job_pk)
        with self.assertRaises(ip.ImagePackageError):
            ip.confirm_package(self.job, candidate['bundle_id'], '', True, expected_job_pk=7)
        self.pdf.write_bytes(b'changed PDF')
        with self.assertRaises(ip.ImagePackageError):
            ip.confirm_package(self.job, candidate['bundle_id'], 'human', True, expected_job_pk=7)

    def test_single_confirmation_still_invalidates_on_asset_change_and_revocation(self):
        candidate = self.build()
        approved = ip.confirm_package(self.job, candidate['bundle_id'], 'human', True, expected_job_pk=7)
        revoked = ip.revoke_package(self.job, approved['bundle_id'], 'human')
        self.assertEqual(revoked['status'], 'revoked')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)
        ip.confirm_package(self.job, candidate['bundle_id'], 'human', True, expected_job_pk=7)
        package = ip.require_approved_package(self.job)
        Path(package.figures[0].image_path).write_bytes(b'changed image')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(self.job)

    def test_per_figure_confirmations_match_package_and_are_preserved(self):
        candidate = self.build()
        invalid_values = [{}, {'main:1': True}, {'main:1': True, 'main:2': False},
                          {'main:1': True, 'main:2': 'true'}, {'main:1': True, 'extended:2': True},
                          {'main:1': True, 'main:2': True, 'main:3': True}]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ip.ImagePackageError):
                ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope,
                                   figure_confirmations=value)
        figures = {'main:1': True, 'main:2': True}
        approved = ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, self.scope,
                                      figure_confirmations=figures)
        self.assertEqual(approved['approval']['figure_confirmations'], figures)
        self.assertEqual(ip.require_approved_package(self.job).bundle_id, approved['bundle_id'])

    def test_review_asset_reads_only_requested_registered_file(self):
        candidate = self.build()
        path = candidate['manifest']['figures'][0]['path']
        original_sha = ip._sha
        with patch('PIL.Image.open', side_effect=AssertionError('must not decode whole package')), \
                patch.object(ip, '_sha', wraps=original_sha) as hashed:
            result = ip.resolve_review_asset(self.job, candidate['bundle_id'], path)
        self.assertEqual(hashed.call_count, 1)
        self.assertEqual(hashed.call_args.args, (result,))
        self.assertEqual(result.name, Path(path).name)
        self.assertEqual(ip.get_package(self.job)['status'], 'needs_review')
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], '../state.json')
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], 'manifest.json')
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], 'article.md')
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], 'source.md')

    def test_requested_review_asset_hash_is_always_checked(self):
        candidate = self.build()
        path = candidate['manifest']['pages'][0]['path']
        (Path(candidate['directory']) / path).write_bytes(b'corrupt-preview')
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], path)

    def test_publication_hashes_but_does_not_redecode_all_preview_pages(self):
        self.approve()
        original_open = Image.open
        def guard(path, *args, **kwargs):
            self.assertNotIn('/pages/', str(path))
            return original_open(path, *args, **kwargs)
        with patch('PIL.Image.open', side_effect=guard):
            ip.require_approved_package(self.job)

    def test_extracts_copied_source_in_isolated_directory(self):
        observed = []

        def extract(snapshot_job, keys, *, figures_dir):
            self.assertNotEqual(Path(snapshot_job.pdf), self.pdf)
            self.assertEqual(Path(snapshot_job.pdf).name, 'source.pdf')
            self.assertEqual(Path(snapshot_job.pdf).read_bytes(), self.pdf.read_bytes())
            self.assertEqual(keys, {('1', False), ('2', False)})
            self.assertTrue(figures_dir.is_relative_to(ip._root(self.job)))
            self.assertTrue(figures_dir.parent.name.startswith('.build-'))
            # A mutable input is not used once its copy has been taken.
            original = self.pdf.read_bytes()
            self.pdf.write_bytes(b'changed outside workflow')
            self.assertEqual(Path(snapshot_job.pdf).read_bytes(), original)
            self.pdf.write_bytes(original)
            observed.append(figures_dir)
            return self.figures, figures_dir

        with patch('batch_processor._resolve_job_figures', side_effect=extract):
            self.build()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse((self.directory / 'figures').exists())

    def test_changed_business_line_invalidates_package_and_review_assets(self):
        approved = self.approve()
        changed_job = replace(self.job, line='solidex')
        self.assertEqual(ip.get_package(changed_job)['status'], 'blocked')
        with self.assertRaises(ip.ImagePackageError):
            ip.require_approved_package(changed_job)
        with self.assertRaises(ip.ImagePackageError):
            ip.resolve_review_asset(changed_job, approved['bundle_id'], 'source.pdf')

    def test_non_numeric_scope_bounds_return_domain_error_without_approval(self):
        candidate = self.build()
        for coordinate in ('invalid', None, {}, float('nan'), float('inf'), True):
            scope = {**self.scope, 'regions': [{'page': 1, 'bbox': [0, coordinate, 500, 600]}]}
            with self.subTest(coordinate=coordinate), self.assertRaises(ip.ImagePackageError):
                ip.approve_package(self.job, candidate['bundle_id'], 'reviewer', self.confirmations, scope)
        self.assertEqual(ip.get_package(self.job)['status'], 'needs_review')

    def test_review_asset_read_holds_shared_job_lock(self):
        from contextlib import contextmanager

        candidate = self.build()
        depth = 0
        original_sha = ip._sha

        @contextmanager
        def checked_lock(job_id, namespace):
            nonlocal depth
            self.assertEqual((job_id, namespace), ('paper', 'image_job'))
            depth += 1
            try:
                yield
            finally:
                depth -= 1

        def sha_under_lock(path):
            self.assertGreater(depth, 0)
            return original_sha(path)

        with patch.object(ip, 'version_lock', side_effect=checked_lock), \
                patch.object(ip, '_sha', side_effect=sha_under_lock):
            ip.resolve_review_asset(self.job, candidate['bundle_id'], 'source.pdf')
        self.assertEqual(depth, 0)


if __name__ == '__main__':
    unittest.main()
