#!/usr/bin/env python3
"""
Tests for Job Submission Route Handlers
=======================================
Comprehensive tests for job submission (POST /jobs), request validation,
parameter/artifact normalization, target scheduling, capability matching,
privileged safeguards, and legacy handler delegation.
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from config.platform_config import override_controller_paths
from controller.constants import (
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_REJECTED,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_REMOVED,
)
from controller.ha_helper import get_ha_manager
from controller.http.job_routes import handle_job_post
from controller.http.job_submission_routes import handle_job_submission_post
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import (
    load_jobs_db,
    save_artifacts_db,
    save_jobs_db,
    save_nodes_db,
    save_workflows_db,
)


class TestJobSubmissionRoutes(unittest.TestCase):
    """Tests the extracted job submission route handlers."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_test_job_sub_"))
        self.data_dir = self.test_dir / "data"
        self.config_dir = self.test_dir / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = self.test_dir / "runtime"
        self.logs_dir = self.test_dir / "logs"
        self.storage_dir = self.test_dir / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir,
        )
        self.paths = self.paths_ctx.__enter__()
        get_ha_manager(paths=self.paths, role="leader", force_reload=True)

        self.admin_token = get_or_create_enrollment_token()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_nodes_db({
            "nodes": {
                "node-alpha": {
                    "node_id": "node-alpha",
                    "name": "Node Alpha",
                    "status": STATE_ONLINE,
                    "last_seen": now_iso,
                    "capabilities": ["build", "python", "docker"],
                    "resources": {"ram_free_mb": 4096, "cpu_usage_pct": 10},
                },
                "node-beta": {
                    "node_id": "node-beta",
                    "name": "Node Beta",
                    "status": STATE_ONLINE,
                    "last_seen": now_iso,
                    "capabilities": ["python"],
                    "resources": {"ram_free_mb": 2048, "cpu_usage_pct": 20},
                },
                "node-offline": {
                    "node_id": "node-offline",
                    "name": "Node Offline",
                    "status": STATE_OFFLINE,
                    "last_seen": "2020-01-01T00:00:00Z",
                    "capabilities": [],
                },
                "node-removed": {
                    "node_id": "node-removed",
                    "name": "Node Removed",
                    "status": STATE_REMOVED,
                    "last_seen": now_iso,
                    "capabilities": [],
                },
            }
        })
        save_workflows_db({"workflows": {}})
        save_artifacts_db({
            "artifacts": {
                "art-123456": {
                    "artifact_id": "art-123456",
                    "name": "bundle.tar.gz",
                    "filename": "bundle.tar.gz",
                    "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                    "size_bytes": 1024,
                    "job_id": "job-source-01",
                }
            }
        })
        save_jobs_db({"jobs": {}})

        # Create mock HTTP handler
        self.handler = MagicMock()
        self.handler.current_correlation_id = None
        self.handler.heartbeat_timeout = 60
        self.handler.is_authenticated_admin_or_node.return_value = True
        self.response_status = None
        self.response_json = None

        def _send_json(status: int, data: dict):
            self.response_status = status
            self.response_json = data

        self.handler.send_json.side_effect = _send_json

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            get_ha_manager(force_reload=True)
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_missing_body_returns_400(self):
        """Missing request body returns 400 Bad Request."""
        handled = handle_job_submission_post(self.handler, "/jobs", None, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertIn("Missing JSON request body", self.response_json.get("error", ""))

    def test_02_unauthorized_submission_returns_401(self):
        """Unauthenticated caller without admin or node auth is rejected with 401."""
        self.handler.is_authenticated_admin_or_node.return_value = False
        body = {"type": "python-script", "parameters": {"code": "print(1)"}}
        handled = handle_job_submission_post(self.handler, "/jobs", body, "invalid-token", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 401)
        self.assertIn("Unauthorized", self.response_json.get("error", ""))

    def test_03_successful_submission_auto_target(self):
        """Submitting a valid job with target=auto queues the job and returns 200."""
        body = {
            "type": "python-script",
            "target": "auto",
            "name": "auto-test-job",
            "parameters": {"code": "print('hello from auto')"},
            "timeout": 45,
            "max_attempts": 2,
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_QUEUED)
        self.assertTrue(self.response_json.get("job_id", "").startswith("job-"))
        job = self.response_json.get("job", {})
        self.assertEqual(job.get("name"), "auto-test-job")
        self.assertEqual(job.get("timeout"), 45)
        self.assertEqual(job.get("max_attempts"), 2)

        # Verify persisted in jobs_db
        jobs_db = load_jobs_db()
        self.assertIn(job["job_id"], jobs_db.get("jobs", {}))

    def test_04_workload_allowlist_rejection(self):
        """Jobs with unallowlisted workload types are rejected with 400."""
        body = {"type": "unregistered_dangerous_type", "target": "auto"}
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_REJECTED)
        self.assertIn("safe allowlist", self.response_json.get("error", ""))

    def test_05_code_script_args_normalization(self):
        """Top-level code, script, and args are normalized into parameters."""
        body = {
            "type": "python-script",
            "target": "auto",
            "code": "print('normalized')",
            "script": "task.py",
            "args": ["--verbose", "1"],
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        params = self.response_json["job"]["parameters"]
        self.assertEqual(params.get("code"), "print('normalized')")
        self.assertEqual(params.get("script"), "task.py")
        self.assertEqual(params.get("args"), ["--verbose", "1"])

    def test_06_output_normalization(self):
        """Output declarations as strings or dicts are normalized into canonical structure."""
        body = {
            "type": "python-script",
            "target": "auto",
            "outputs": [
                "output_data.csv",
                {"name": "report.pdf", "path": "custom/report.pdf"},
            ],
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        outputs = self.response_json["job"]["outputs"]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["name"], "output_data.csv")
        self.assertEqual(outputs[0]["filename"], "output_data.csv")
        self.assertEqual(outputs[1]["name"], "report.pdf")
        self.assertEqual(outputs[1]["path"], "custom/report.pdf")

    def test_07_input_artifacts_resolution_success(self):
        """Input artifacts are resolved and validated against cluster artifact database."""
        body = {
            "type": "python-script",
            "target": "auto",
            "inputs": ["art-123456"],
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        input_arts = self.response_json["job"]["input_artifacts"]
        self.assertEqual(len(input_arts), 1)
        self.assertEqual(input_arts[0]["artifact_id"], "art-123456")
        self.assertEqual(input_arts[0]["sha256"], "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890")

    def test_08_input_artifacts_missing_returns_400(self):
        """Referencing a nonexistent input artifact rejects job submission with 400."""
        body = {
            "type": "python-script",
            "target": "auto",
            "inputs": ["art-nonexistent"],
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_REJECTED)
        self.assertIn("not found in cluster artifact database", self.response_json.get("error", ""))

    def test_09_input_artifacts_invalid_spec_returns_400(self):
        """Invalid artifact specification rejects job submission with 400."""
        body = {
            "type": "python-script",
            "target": "auto",
            "inputs": [{"invalid_key": "no_id"}],
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_REJECTED)
        self.assertIn("missing required 'artifact_id'", self.response_json.get("error", ""))

    def test_10_explicit_target_node_success(self):
        """Explicitly targeting an online node selects and assigns that node."""
        body = {
            "type": "python-script",
            "target": "node-alpha",
            "parameters": {"code": "print(1)"},
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json.get("target_node"), "node-alpha")
        self.assertEqual(self.response_json["scheduler"]["mode"], "explicit")

    def test_11_explicit_target_nonexistent_node_returns_400(self):
        """Explicit target node that does not exist in the cluster is rejected with 400."""
        body = {"type": "python-script", "target": "node-ghost"}
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_REJECTED)
        self.assertIn("does not exist in cluster", self.response_json.get("error", ""))

    def test_12_explicit_target_removed_node_returns_400(self):
        """Explicit target node marked REMOVED is rejected with 400."""
        body = {"type": "python-script", "target": "node-removed"}
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_REJECTED)
        self.assertIn("was removed from the cluster", self.response_json.get("error", ""))

    def test_13_explicit_target_offline_node(self):
        """Offline node without pin rejects; with pinned=True queues as NEEDS_ATTENTION."""
        # A: Unpinned offline node
        body_unpinned = {"type": "python-script", "target": "node-offline"}
        handled = handle_job_submission_post(self.handler, "/jobs", body_unpinned, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 400)
        self.assertIn("currently OFFLINE", self.response_json.get("error", ""))

        # B: Pinned offline node
        body_pinned = {"type": "python-script", "target": "node-offline", "pinned": True}
        handled = handle_job_submission_post(self.handler, "/jobs", body_pinned, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json.get("status"), JOB_STATE_NEEDS_ATTENTION)
        self.assertIn("awaiting reconnection", self.response_json.get("waiting_reason", ""))

    def test_14_privileged_and_allow_internal_require_admin(self):
        """Non-admin callers cannot submit allow_internal or privileged workloads."""
        # Non-admin submission of allow_internal
        body_internal = {"type": "python-script", "parameters": {"allow_internal": True}}
        handled = handle_job_submission_post(self.handler, "/jobs", body_internal, "node-worker-token", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 403)
        self.assertIn("'allow_internal' parameter requires administrator credentials", self.response_json.get("error", ""))

        # Non-admin submission of privileged
        body_priv = {"type": "python-script", "requirements": {"privileged": True}}
        handled = handle_job_submission_post(self.handler, "/jobs", body_priv, "node-worker-token", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 403)
        self.assertIn("Privileged workloads require administrator credentials", self.response_json.get("error", ""))

        # Admin submission of both succeeds
        body_admin = {"type": "python-script", "parameters": {"allow_internal": True}, "requirements": {"privileged": True}}
        handled = handle_job_submission_post(self.handler, "/jobs", body_admin, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)

    def test_15_capability_target_syntax(self):
        """Targeting capability:<cap> parses capabilities and uses capability-aware scheduling."""
        body = {
            "type": "python-script",
            "target": "capability:build",
            "parameters": {"code": "print('building')"},
        }
        handled = handle_job_submission_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        job = self.response_json["job"]
        self.assertIn("build", job["requirements"]["capabilities"])
        self.assertEqual(job["target_node"], "node-alpha")  # Node alpha has "build"
        self.assertEqual(self.response_json["scheduler"]["mode"], "capability-aware")

    def test_16_legacy_delegation_via_handle_job_post(self):
        """Directly calling handle_job_post with POST /jobs delegates to the submission handler."""
        body = {"type": "python-script", "target": "node-beta", "parameters": {"code": "print('legacy')"}}
        handled = handle_job_post(self.handler, "/jobs", body, self.admin_token, {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json.get("target_node"), "node-beta")

    def test_17_unhandled_path_returns_false(self):
        """Non-matching paths return False without modifying response."""
        handled = handle_job_submission_post(self.handler, "/jobs/metrics", {}, self.admin_token, {})
        self.assertFalse(handled)
        self.assertIsNone(self.response_status)


if __name__ == "__main__":
    unittest.main()
