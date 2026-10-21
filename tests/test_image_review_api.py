"""Review boundary and recovery tests: no extraction, remote calls or worker launch."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

import app as appmod
import db.database as dbmod
from db.database import DatabaseManager, JobStatus
from utils.image_packages import ImagePackageError
from utils.job_loader import Job


class ImageReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = DatabaseManager(database_url=f"sqlite:///{self.root / 'review.db'}")
        task = self.db.get_or_create_task('test')
        job = self.db.upsert_job(task.id, 'paper', pdf_path='source.pdf', template_id='t',
                                 product_id='p', status=JobStatus.GENERATED)
        self.db.upsert_article(job.id, title='Paper', content_dir=str(self.root))
        self.job_pk = job.id
        self.source_job = Job('paper', 'source.pdf', 't', 'p', line='aav')
        self.patches = [
            patch.object(dbmod, '_instance', self.db),
            patch('utils.panel_runner.list_sources', return_value=[
                {'line_id': 'aav', 'pdfs': [{'job_id': 'paper'}]}]),
            patch('utils.blog_pipeline._build_source_job', return_value=self.source_job),
            patch('socket.socket.connect', side_effect=AssertionError('network forbidden')),
        ]
        for item in self.patches:
            item.start()
        application = appmod.create_app(testing=False)
        application.config['TRUST_PROXY_IDENTITY'] = True  # Simulate the authenticated gateway in these boundary tests.
        self.client = application.test_client()
        self.headers = {'X-GM-LAB-Username': 'hqq'}
        self.bundle_id = 'a' * 64
        self.final_id = 'b' * 64
        self.package = {'bundle_id': self.bundle_id, 'status': 'needs_review', 'manifest': {
            'job_pk': self.job_pk,
            'source': {'binding': 'verified', 'pdf': {'path': 'source.pdf', 'sha256': 'pdfhash'}},
            'figures': [{'path': 'figures/1.jpg', 'label': '1', 'is_extended': False,
                         'page': 2, 'width': 400, 'height': 300}],
            'pages': [{'path': 'pages/2.png', 'page': 2, 'width': 600, 'height': 800}],
            'cover': {'path': 'cover.jpg', 'width': 900, 'height': 383},
        }}
        self.body = {'package_id': self.bundle_id,
                     'confirmations': dict.fromkeys(('source', 'number', 'completeness', 'cover'), True),
                     'figure_confirmations': {'main:1': True},
                     'paper_scope': {'title': 'Original paper', 'evidence': 'Title and DOI checked', 'pages': [2]}}
        self.api = '/api/image-review/paper'

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.db.engine.dispose()
        self.temporary.cleanup()

    def test_authenticated_review_page_has_one_button_and_no_form_or_implicit_approval(self):
        response = self.client.get('/image-review/paper', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('图片正确，确认并发布', text)
        self.assertEqual(text.count('<button '), 1)
        self.assertNotIn('<input', text)
        self.assertNotIn('<textarea', text)
        self.assertNotIn('仅确认配图', text)

    def test_direct_cookie_can_view_but_cannot_approve_continue_or_revoke(self):
        self.client.get('/aav')
        page = self.client.get('/image-review/paper').get_data(as_text=True)
        self.assertIn('/auth/start?next=/image-review/paper', page)
        self.assertIn('默认用户名不代表已登录', page)
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package') as approve, patch(
                'utils.image_packages.revoke_package') as revoke, patch(
                'utils.workflow_runner.start_workflow') as workflow:
            response = self.client.get(self.api)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.get_json()['can_approve'])
            for action in ('confirm', 'approve', 'continue', 'revoke'):
                with self.subTest(action=action):
                    self.assertEqual(self.client.post(f'{self.api}/{action}', json=self.body).status_code, 401)
            approve.assert_not_called()
            revoke.assert_not_called()
            workflow.assert_not_called()

    def test_review_run_status_is_scoped_and_never_kicks_publication(self):
        state = {'run_id': '123456789a', 'stage': 'images', 'status': 'failed',
                 'owner_line': 'aav', 'errors': ['blocked'], 'pid': 10, 'private': 'secret'}
        with patch('utils.workflow_runner.review_workflow_status', return_value=state) as read, patch('utils.auto_blog.kick') as kick:
            response = self.client.get(f'{self.api}/workflow/123456789a', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['status'], 'failed')
            self.assertNotIn('private', response.get_json())
            self.assertNotIn('pid', response.get_json())
            read.assert_called_once_with('paper', '123456789a')
            kick.assert_not_called()
        with patch('utils.workflow_runner.review_workflow_status', return_value={**state, 'owner_line': 'solidex'}):
            self.assertEqual(self.client.get(f'{self.api}/workflow/123456789a', headers=self.headers).status_code, 404)

    def test_review_publish_progress_exposes_exact_bundle_without_waiting_for_image_lock(self):
        state = {'run_id': '123456789a', 'stage': 'review_publish', 'status': 'running',
                 'owner_line': 'aav', 'bundle_id': self.bundle_id}
        with patch('utils.workflow_runner.review_workflow_status', return_value=state), patch(
                'utils.image_packages.get_package', side_effect=AssertionError('must not wait for publisher lock')):
            response = self.client.get(f'{self.api}/workflow/123456789a', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['bundle_id'], self.bundle_id)

    def test_every_route_checks_business_line_before_any_core_or_worker_call(self):
        with patch('utils.image_packages.get_package') as get, patch(
                'utils.image_packages.approve_package') as approve, patch(
                'utils.image_packages.resolve_review_asset') as asset, patch(
                'utils.workflow_runner.start_workflow') as workflow:
            routes = [('GET', self.api), ('GET', '/image-review/paper'),
                      ('GET', f'{self.api}/workflow/123456789a'),
                      ('GET', f'{self.api}/assets/{self.bundle_id}/cover.jpg')]
            routes += [('POST', f'{self.api}/{action}') for action in ('prepare', 'confirm', 'approve', 'continue', 'revoke')]
            for method, route in routes:
                with self.subTest(route=route):
                    self.assertEqual(self.client.open(route, method=method, json=self.body,
                        headers={'X-GM-LAB-Username': 'jhh'}).status_code, 403)
            for mock in (get, approve, asset, workflow):
                mock.assert_not_called()

    def test_payload_only_exposes_whitelisted_metadata_and_local_asset_urls(self):
        with patch('utils.image_packages.get_package', return_value={**self.package, 'directory': '/private/path'}):
            response = self.client.get(self.api, headers=self.headers)
        data = response.get_json()
        self.assertTrue(data['can_approve'])
        self.assertEqual(data['figures'][0]['key'], 'main:1')
        self.assertIn(f'/assets/{self.bundle_id}/figures/1.jpg', data['figures'][0]['url'])
        self.assertNotIn('/private/path', response.get_data(as_text=True))

    def test_prepare_is_asynchronous_and_never_approves(self):
        with patch('utils.workflow_runner.start_workflow', return_value={'ok': True, 'run_id': 'prep'}) as workflow, patch(
                'utils.image_packages.approve_package') as approve:
            response = self.client.post(f'{self.api}/prepare', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        workflow.assert_called_once_with('images', [{'job_id': 'paper', 'lang': 'zh'}],
                                        owner_line='aav', owner_username='hqq')
        approve.assert_not_called()

    def test_single_confirmation_needs_no_paper_fields_and_always_continues_pinned_bundle(self):
        with patch('utils.image_packages.confirm_package', return_value={'bundle_id': self.bundle_id, 'status': 'approved'}) as confirm, patch(
                'utils.image_packages.require_approved_package') as require, patch(
                'utils.workflow_runner.start_workflow', return_value={'ok': True, 'run_id': 'publish'}) as workflow:
            response = self.client.post(f'{self.api}/confirm', headers=self.headers,
                json={'package_id': self.bundle_id, 'images_correct': True, 'reviewer': 'forged'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['approved'])
        self.assertTrue(response.get_json()['continuation']['ok'])
        confirm.assert_called_once_with(self.source_job, self.bundle_id, 'hqq', True, expected_job_pk=self.job_pk)
        require.assert_called_once_with(self.source_job, self.bundle_id, expected_job_pk=self.job_pk)
        workflow.assert_called_once_with('review_publish', [{'job_id': 'paper', 'lang': 'zh',
            'expected_package_id': self.bundle_id}], owner_line='aav', owner_username='hqq')

    def test_single_confirmation_requires_boolean_decision_and_rejects_stale_packages(self):
        for value in (False, None, 1, 'true'):
            with self.subTest(value=value), patch('utils.image_packages.confirm_package') as confirm:
                response = self.client.post(f'{self.api}/confirm', headers=self.headers,
                    json={'package_id': self.bundle_id, 'images_correct': value})
                self.assertEqual(response.status_code, 400)
                confirm.assert_not_called()
        with patch('utils.image_packages.confirm_package', side_effect=ImagePackageError('图片包已过期')), patch(
                'utils.workflow_runner.start_workflow') as workflow:
            response = self.client.post(f'{self.api}/confirm', headers=self.headers,
                json={'package_id': self.bundle_id, 'images_correct': True})
        self.assertEqual(response.status_code, 409)
        workflow.assert_not_called()

    def test_single_confirmation_keeps_saved_decision_when_worker_cannot_start(self):
        with patch('utils.image_packages.confirm_package', return_value={'bundle_id': self.bundle_id, 'status': 'approved'}), patch(
                'utils.image_packages.require_approved_package'), patch(
                'utils.workflow_runner.start_workflow', side_effect=RuntimeError('private-error')):
            response = self.client.post(f'{self.api}/confirm', headers=self.headers,
                json={'package_id': self.bundle_id, 'images_correct': True})
        self.assertTrue(response.get_json()['approved'])
        self.assertFalse(response.get_json()['continuation']['ok'])
        self.assertNotIn('private-error', response.get_data(as_text=True))

    def test_prepare_worker_error_is_recoverable_json_without_secret(self):
        with patch('utils.workflow_runner.start_workflow', side_effect=RuntimeError('private-token')):
            response = self.client.post(f'{self.api}/prepare', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()['ok'])
        self.assertNotIn('private-token', response.get_data(as_text=True))

    def test_all_global_confirmations_must_be_boolean_true(self):
        for value in (None, False, 'true', 1):
            with self.subTest(value=value), patch('utils.image_packages.approve_package') as approve:
                body = deepcopy(self.body)
                body['confirmations']['source'] = value
                response = self.client.post(f'{self.api}/approve', json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                approve.assert_not_called()

    def test_per_figure_confirmation_is_required_on_server(self):
        for confirmations in (None, {}, {'main:1': False}, {'main:1': 'true'},
                              {'main:1': True, 'main:2': True}):
            with self.subTest(confirmations=confirmations), patch(
                    'utils.image_packages.get_package', return_value=self.package), patch(
                    'utils.image_packages.approve_package') as approve:
                response = self.client.post(f'{self.api}/approve', headers=self.headers,
                    json={**self.body, 'figure_confirmations': confirmations})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['field'], 'figures')
                approve.assert_not_called()

    def test_approval_uses_real_header_identity_and_final_bundle_for_continuation(self):
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package', return_value={'bundle_id': self.final_id, 'status': 'approved'}) as approve, patch(
                'utils.workflow_runner.start_workflow', return_value={'ok': True, 'run_id': 'publish'}) as workflow:
            response = self.client.post(f'{self.api}/approve', headers=self.headers,
                json={**self.body, 'reviewer': 'forged', 'continue': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['bundle_id'], self.final_id)
        self.assertEqual(approve.call_args.args[2], 'hqq')
        self.assertEqual(approve.call_args.kwargs['figure_confirmations'], {'main:1': True})
        workflow.assert_called_once_with('review_publish', [{'job_id': 'paper', 'lang': 'zh',
            'expected_package_id': self.final_id}], owner_line='aav', owner_username='hqq')

    def test_approval_only_does_not_launch_worker(self):
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package', return_value={'bundle_id': self.final_id, 'status': 'approved'}), patch(
                'utils.workflow_runner.start_workflow') as workflow:
            response = self.client.post(f'{self.api}/approve', headers=self.headers, json=self.body)
        self.assertTrue(response.get_json()['approved'])
        workflow.assert_not_called()

    def test_approval_rejects_old_or_unbound_database_record(self):
        for job_pk in (self.job_pk + 1, None, True, str(self.job_pk)):
            with self.subTest(job_pk=job_pk), patch('utils.image_packages.get_package', return_value={
                    **self.package, 'manifest': {**self.package['manifest'], 'job_pk': job_pk}}), patch(
                    'utils.image_packages.approve_package') as approve, patch(
                    'utils.workflow_runner.start_workflow') as workflow:
                response = self.client.post(f'{self.api}/approve', headers=self.headers,
                                           json={**self.body, 'continue': True})
                self.assertEqual(response.status_code, 409)
                self.assertIn('当前文章记录', response.get_json()['error'])
                approve.assert_not_called()
                workflow.assert_not_called()

    def test_worker_start_failure_does_not_erase_saved_approval(self):
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package', return_value={'bundle_id': self.final_id, 'status': 'approved'}), patch(
                'utils.workflow_runner.start_workflow', side_effect=RuntimeError('private-worker-error')):
            response = self.client.post(f'{self.api}/approve', headers=self.headers,
                                       json={**self.body, 'continue': True})
        data = response.get_json()
        self.assertTrue(data['approved'])
        self.assertFalse(data['continuation']['ok'])
        self.assertIn('无需重新审核', data['continuation']['error'])
        self.assertNotIn('private-worker-error', response.get_data(as_text=True))

    def test_stale_approval_is_rejected_and_cannot_launch_worker(self):
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package', side_effect=ImagePackageError('候选图片包已过期')), patch(
                'utils.workflow_runner.start_workflow') as workflow:
            response = self.client.post(f'{self.api}/approve', headers=self.headers,
                                       json={**self.body, 'continue': True})
        self.assertEqual(response.status_code, 409)
        self.assertIn('过期', response.get_json()['error'])
        workflow.assert_not_called()

    def test_post_approval_stale_readback_is_not_reported_as_success(self):
        with patch('utils.image_packages.get_package', return_value=self.package), patch(
                'utils.image_packages.approve_package', return_value={'bundle_id': self.final_id, 'status': 'stale', 'reason': '来源已变化'}), patch(
                'utils.workflow_runner.start_workflow') as workflow:
            response = self.client.post(f'{self.api}/approve', headers=self.headers,
                                       json={**self.body, 'continue': True})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['approved'])
        self.assertFalse(response.get_json()['ok'])
        workflow.assert_not_called()

    def test_continue_requires_current_approved_state(self):
        for status in ('needs_review', 'stale', 'superseded', 'revoked', 'blocked'):
            with self.subTest(status=status), patch('utils.image_packages.get_package', return_value={**self.package, 'status': status}), patch(
                    'utils.workflow_runner.start_workflow') as workflow:
                response = self.client.post(f'{self.api}/continue', headers=self.headers, json=self.body)
                self.assertEqual(response.status_code, 409)
                workflow.assert_not_called()

    def test_continue_current_approved_package_uses_async_pinned_task(self):
        with patch('utils.image_packages.get_package', return_value={**self.package, 'status': 'approved'}), patch(
                'utils.workflow_runner.start_workflow', return_value={'ok': True, 'run_id': 'continue'}) as workflow, patch(
                'utils.image_packages.require_approved_package') as require:
            response = self.client.post(f'{self.api}/continue', headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(workflow.call_args.args[1][0]['expected_package_id'], self.bundle_id)
        require.assert_called_once_with(self.source_job, self.bundle_id, expected_job_pk=self.job_pk)

    def test_continue_rejects_approved_package_for_old_or_missing_database_record(self):
        for job_pk in (self.job_pk + 1, None):
            with self.subTest(job_pk=job_pk), patch('utils.image_packages.get_package', return_value={
                    **self.package, 'status': 'approved', 'manifest': {**self.package['manifest'], 'job_pk': job_pk}}), patch(
                    'utils.image_packages.require_approved_package', side_effect=ImagePackageError('图片包记录不匹配')) as require, patch(
                    'utils.workflow_runner.start_workflow') as workflow:
                response = self.client.post(f'{self.api}/continue', headers=self.headers, json=self.body)
                self.assertEqual(response.status_code, 409)
                require.assert_called_once_with(self.source_job, self.bundle_id, expected_job_pk=self.job_pk)
                workflow.assert_not_called()

    def test_revoke_records_real_identity(self):
        with patch('utils.image_packages.revoke_package', return_value={'bundle_id': self.bundle_id, 'status': 'revoked'}) as revoke:
            response = self.client.post(f'{self.api}/revoke', headers=self.headers,
                                       json={**self.body, 'reason': 'Wrong panel'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(revoke.call_args.args[2], 'hqq')
        self.assertEqual(revoke.call_args.kwargs['note'], 'Wrong panel')

    def test_asset_is_no_store_and_checked_by_core(self):
        asset = self.root / 'image.png'
        Image.new('RGB', (30, 30), 'blue').save(asset)
        with patch('utils.image_packages.resolve_review_asset', return_value=asset) as resolve:
            response = self.client.get(f'{self.api}/assets/{self.bundle_id}/figures/1.png', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        resolve.assert_called_once_with(self.source_job, self.bundle_id, 'figures/1.png')
        response.close()

    def test_non_preview_files_and_corrupt_assets_are_not_served(self):
        with patch('utils.image_packages.resolve_review_asset', side_effect=ImagePackageError('asset mismatch')) as resolve:
            for relative in ('manifest.json', 'article.md', 'source.md', 'pages/2.png'):
                with self.subTest(relative=relative):
                    self.assertEqual(self.client.get(f'{self.api}/assets/{self.bundle_id}/{relative}',
                        headers=self.headers).status_code, 404)
            resolve.assert_called_once_with(self.source_job, self.bundle_id, 'pages/2.png')

    def test_malformed_json_objects_and_package_ids_are_rejected(self):
        with patch('utils.image_packages.approve_package') as approve:
            for body in ([], 'text', {'package_id': '../state'}, {'package_id': 3}):
                with self.subTest(body=body):
                    self.assertEqual(self.client.post(f'{self.api}/approve', headers=self.headers,
                        json=body).status_code, 400)
            approve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
