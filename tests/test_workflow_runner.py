import tempfile
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


if __name__ == "__main__":
    unittest.main()
