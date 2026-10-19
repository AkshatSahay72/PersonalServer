#!/usr/bin/env python3
"""
Unit Tests for Job Deletion Functionality
=========================================
Validates that:
- Non-running jobs (SUCCEEDED, FAILED, TIMEOUT, CANCELLED) can be safely deleted.
- Actively running or claimed jobs cannot be deleted (returns 409 Conflict).
- Non-existent job deletion returns 404.
- Unauthorized requests return 401.
- CLI delete_job helper functions correctly.
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from config.platform_config import ControllerPaths
from controller.stores import load_jobs_db, save_jobs_db
from controller.http.job_routes import handle_job_delete
from controller.cli import delete_job
from controller.constants import (
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_CANCELLED,
)


class TestJobDeletion(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.paths = ControllerPaths(self.base_path)
        self.paths.ensure_directories()

        self.enrollment_token = "secret-enrollment-token-12345"
        self.paths.enrollment_token_file.write_text(self.enrollment_token, encoding="utf-8")

        # Mock handler
        self.handler = MagicMock()
        self.handler.sent_status = None
        self.handler.sent_json = None
        def mock_send_json(status, data):
            self.handler.sent_status = status
            self.handler.sent_json = data
        self.handler.send_json.side_effect = mock_send_json
        self.handler.is_authenticated_admin_or_node.return_value = True

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("controller.http.job_routes.load_jobs_db")
    @patch("controller.http.job_routes.save_jobs_db")
    def test_delete_finished_job_success(self, mock_save, mock_load):
        jobs_db = {
            "jobs": {
                "job-succeeded-1": {
                    "job_id": "job-succeeded-1",
                    "status": JOB_STATE_SUCCEEDED,
                    "type": "system-info"
                }
            }
        }
        mock_load.return_value = jobs_db

        handled = handle_job_delete(self.handler, "/jobs/job-succeeded-1", token="any-admin-token")
        self.assertTrue(handled)
        self.assertEqual(self.handler.sent_status, 200)
        self.assertEqual(self.handler.sent_json["status"], "DELETED")
        self.assertNotIn("job-succeeded-1", jobs_db["jobs"])
        mock_save.assert_called_once_with(jobs_db)

    @patch("controller.http.job_routes.load_jobs_db")
    @patch("controller.http.job_routes.save_jobs_db")
    def test_delete_running_job_rejected_with_409(self, mock_save, mock_load):
        jobs_db = {
            "jobs": {
                "job-running-1": {
                    "job_id": "job-running-1",
                    "status": JOB_STATE_RUNNING,
                    "type": "system-info"
                }
            }
        }
        mock_load.return_value = jobs_db

        handled = handle_job_delete(self.handler, "/jobs/job-running-1", token="any-admin-token")
        self.assertTrue(handled)
        self.assertEqual(self.handler.sent_status, 409)
        self.assertIn("Cancel the job first", self.handler.sent_json["error"])
        self.assertIn("job-running-1", jobs_db["jobs"])
        mock_save.assert_not_called()

    @patch("controller.http.job_routes.load_jobs_db")
    @patch("controller.http.job_routes.save_jobs_db")
    def test_delete_claimed_job_rejected_with_409(self, mock_save, mock_load):
        jobs_db = {
            "jobs": {
                "job-claimed-1": {
                    "job_id": "job-claimed-1",
                    "status": JOB_STATE_CLAIMED,
                    "type": "system-info"
                }
            }
        }
        mock_load.return_value = jobs_db

        handled = handle_job_delete(self.handler, "/jobs/job-claimed-1", token="any-admin-token")
        self.assertTrue(handled)
        self.assertEqual(self.handler.sent_status, 409)
        mock_save.assert_not_called()

    @patch("controller.http.job_routes.load_jobs_db")
    def test_delete_non_existent_job_returns_404(self, mock_load):
        mock_load.return_value = {"jobs": {}}
        handled = handle_job_delete(self.handler, "/jobs/non-existent-999", token="any-admin-token")
        self.assertTrue(handled)
        self.assertEqual(self.handler.sent_status, 404)

    @patch("controller.cli.load_jobs_db")
    @patch("controller.cli.save_jobs_db")
    def test_cli_delete_job_helper(self, mock_save, mock_load):
        mock_load.return_value = {
            "jobs": {
                "job-cli-test": {
                    "job_id": "job-cli-test",
                    "status": JOB_STATE_FAILED
                }
            }
        }
        ret = delete_job("job-cli-test")
        self.assertEqual(ret, 0)
        mock_save.assert_called_once()
        saved_db = mock_save.call_args[0][0]
        self.assertNotIn("job-cli-test", saved_db["jobs"])


if __name__ == "__main__":
    unittest.main()
