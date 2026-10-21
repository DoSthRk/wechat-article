import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import workflow_runner


class WorkflowRunnerTests(unittest.TestCase):
    def test_single_button_reuses_exact_successful_bundle_but_retries_failures(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(workflow_runner, 'RUN_DIR', Path(temp)), patch.object(
                workflow_runner, 'start_workflow', return_value={'ok': True, 'run_id': 'new'}) as start:
            run_id, bundle = '123456789a', 'a'*64
            path = Path(temp) / f'{run_id}.state.json'
            state = {'run_id': run_id, 'stage': 'review_publish', 'status': 'done', 'owner_line': 'aav', 'failed': 0}
            workflow_runner._atomic_write(path, state)
            (Path(temp) / f'{run_id}.selections.json').write_text(json.dumps([
                {'job_id': 'paper', 'lang': 'zh', 'expected_package_id': bundle}]))
            result = workflow_runner.start_review_publication('paper', bundle, owner_line='aav', owner_username='human')
            self.assertTrue(result['reused'])
            self.assertEqual(result['run_id'], run_id)
            start.assert_not_called()
            workflow_runner.start_review_publication('paper', 'b'*64, owner_line='aav', owner_username='human')
            self.assertEqual(start.call_count, 1)
            workflow_runner._atomic_write(path, {**state, 'status': 'failed', 'failed': 1})
            workflow_runner.start_review_publication('paper', bundle, owner_line='aav', owner_username='human')
            self.assertEqual(start.call_count, 2)
            workflow_runner.start_review_publication('other', bundle, owner_line='aav', owner_username='human')
            self.assertEqual(start.call_count, 3)

    def test_review_status_reads_exact_run_and_binds_job(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(workflow_runner, 'RUN_DIR', Path(temp)):
            run_id = '123456789a'
            state = {'run_id': run_id, 'stage': 'images', 'status': 'failed', 'errors': ['blocked']}
            workflow_runner._atomic_write(Path(temp) / f'{run_id}.state.json', state)
            selection = Path(temp) / f'{run_id}.selections.json'
            selection.write_text(json.dumps([{'job_id': 'paper', 'lang': 'zh'}]))
            workflow_runner._atomic_write(Path(temp) / 'newer.state.json', {'stage': 'images', 'status': 'done'})
            self.assertEqual(workflow_runner.review_workflow_status('paper', run_id), state)
            for job_id, identifier in [('other', run_id), ('paper', '../' + run_id), ('paper', '0'*10)]:
                self.assertEqual(workflow_runner.review_workflow_status(job_id, identifier), {})
            selection.write_text(json.dumps([{'job_id': 'paper', 'lang': 'zh'}, {'job_id': 'other', 'lang': 'zh'}]))
            self.assertEqual(workflow_runner.review_workflow_status('paper', run_id), {})

    def test_review_status_detects_interrupted_worker_without_writing_or_kicking(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(workflow_runner, 'RUN_DIR', Path(temp)):
            run_id = '123456789a'
            state = {'run_id': run_id, 'stage': 'images', 'status': 'running', 'pid': 42, 'started_at': 1}
            path = Path(temp) / f'{run_id}.state.json'
            workflow_runner._atomic_write(path, state)
            (Path(temp) / f'{run_id}.selections.json').write_text(json.dumps([{'job_id': 'paper', 'lang': 'zh'}]))
            with patch.object(workflow_runner, '_pid_running', return_value=False):
                self.assertEqual(workflow_runner.review_workflow_status('paper', run_id)['status'], 'failed')
            self.assertEqual(json.loads(path.read_text()), state)
            with patch.object(workflow_runner, '_pid_running', return_value=True):
                self.assertEqual(workflow_runner.review_workflow_status('paper', run_id)['status'], 'running')

    def test_normalize_keeps_supported_unique_languages(self):
        items = workflow_runner._normalize("translate", [
            {"job_id": "paper-1", "lang": "en"},
            {"job_id": "paper-1", "lang": "en"},
            {"job_id": "paper-1", "lang": "zh"},
            {"job_id": "paper-2", "lang": "ja"},
        ])
        self.assertEqual(items, [
            {"job_id": "paper-1", "lang": "en"},
            {"job_id": "paper-2", "lang": "ja"},
        ])

    def test_start_workflow_persists_state_and_launches_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            process = Mock(pid=12345)
            with patch.object(workflow_runner, "RUN_DIR", run_dir), patch.object(
                workflow_runner.subprocess, "Popen", return_value=process,
            ) as popen:
                result = workflow_runner.start_workflow(
                    "publish", [{"job_id": "paper-1", "lang": "zh"}],
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(len(list(run_dir.glob("*.state.json"))), 1)
            self.assertTrue((run_dir / "publish.lock").is_file())
            command = popen.call_args.args[0]
            self.assertIn("workflow_worker.py", command)

    def test_publish_normalization_preserves_explicit_force(self):
        items = workflow_runner._normalize("publish", [
            {"job_id": "paper-1", "lang": "en", "force": True},
            {"job_id": "paper-2", "lang": "ja", "force": False},
        ])
        self.assertEqual(items, [
            {"job_id": "paper-1", "lang": "en", "force": True},
            {"job_id": "paper-2", "lang": "ja"},
        ])

    def test_image_only_repair_flag_is_preserved_only_for_publish(self):
        item = {'job_id': 'paper', 'lang': 'en', 'repair_images': True}
        self.assertEqual(workflow_runner._normalize('publish', [item]), [item])
        self.assertEqual(workflow_runner._normalize('translate', [item]), [{'job_id': 'paper', 'lang': 'en'}])

    def test_explicit_cover_and_wechat_repair_flags(self):
        blog = {'job_id': 'p1', 'lang': 'en', 'repair_images': True, 'repair_cover': True}
        wechat = {'job_id': 'p2', 'lang': 'zh', 'repair_wechat_images': True, 'repair_cover': True}
        self.assertEqual(workflow_runner._normalize('publish', [blog, wechat]), [blog, wechat])
        self.assertEqual(workflow_runner._normalize('publish', [{'job_id': 'p3', 'lang': 'zh', 'repair_cover': True}]),
                         [{'job_id': 'p3', 'lang': 'zh'}])

    def test_worker_repairs_only_selected_language_without_republishing_or_translating(self):
        import workflow_worker
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selections, state, lock = [root / name for name in ('selections.json', 'state.json', 'publish.lock')]
            selections.write_text(json.dumps([{'job_id': 'paper', 'lang': 'zh', 'repair_images': True}]))
            state.write_text(json.dumps({'completed': 0, 'failed': 0, 'errors': []}))
            lock.touch()
            workflow = Mock()
            argv = ['worker', '--stage', 'publish', '--selections', str(selections), '--state', str(state), '--lock', str(lock)]
            with patch('sys.argv', argv), patch.object(workflow_worker, 'get_db_manager'), patch.object(
                    workflow_worker, 'BlogWorkflow', return_value=workflow), patch(
                    'utils.blog_image_repair.repair_images', return_value={'verified': True}) as repair:
                self.assertEqual(workflow_worker.main(), 0)
                repair.assert_called_once_with(workflow, 'paper', 'zh')
                workflow.publish.assert_not_called()
                workflow.translate.assert_not_called()
                result = json.loads(state.read_text())
                self.assertEqual(result['status'], 'done')
                self.assertEqual(result['results'], [{'verified': True}])


if __name__ == "__main__":
    unittest.main()
