import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import workflow_runner


class WorkflowRunnerTests(unittest.TestCase):
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
