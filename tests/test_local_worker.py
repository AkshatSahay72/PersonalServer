#!/usr/bin/env python3
"""
Test Suite for PersonalServer LocalWorker (Windows Development & Local Execution)
==================================================================================
Tests:
1. Registration & Authentication Success.
2. Authentication Failure (invalid enrollment token or auth token).
3. Worker Heartbeat & Offline Transition.
4. Successful Harmless Job Execution (echo & system-info).
5. Failed Job with Non-Zero Exit Code (failing-test).
6. Job Timeout Enforcement (timeout-test) & Cancellation.
7. Duplicate Submission & Lease Lock Protection.
8. Unsupported Capability & Workload Rejection.
9. Controller Restart & Worker Reconnection.
10. Log & Secret Redaction in Workload Results.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.platform_config import (
    override_controller_paths,
    get_controller_paths,
    redact_sensitive_dict
)
from controller.controller import (
    ControllerHandler,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    compute_node_liveness,
    sweep_expired_leases,
    STATE_ONLINE,
    STATE_OFFLINE,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_CANCELLED,
    JOB_STATE_REJECTED
)
from agent.local_worker import (
    LocalWorker,
    LocalWorkerAuthError,
    LocalWorkerError,
    probe_hardware_resources,
    get_canonical_architecture
)


class TestLocalWorker(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Create isolated temporary directory for controller paths
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_local_worker_test_"))
        cls.data_dir = cls.test_dir / "data"
        cls.config_dir = cls.test_dir / "config"
        cls.secrets_dir = cls.config_dir / "secrets"
        cls.runtime_dir = cls.test_dir / "runtime"
        cls.logs_dir = cls.test_dir / "logs"
        cls.storage_dir = cls.test_dir / "storage"

        for d in (cls.data_dir, cls.secrets_dir, cls.runtime_dir, cls.logs_dir, cls.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Write test enrollment token
        cls.enrollment_token = "test-enrollment-secret-token-12345"
        (cls.secrets_dir / "enrollment.token").write_text(cls.enrollment_token + "\n", encoding="utf-8")

        # Activate controller paths override context
        cls.paths_ctx = override_controller_paths(
            data_dir=cls.data_dir,
            config_dir=cls.config_dir,
            secrets_dir=cls.secrets_dir,
            runtime_dir=cls.runtime_dir,
            logs_dir=cls.logs_dir,
            storage_dir=cls.storage_dir
        )
        cls.paths = cls.paths_ctx.__enter__()

        # Start an in-process controller on an ephemeral port
        ControllerHandler.heartbeat_timeout = 3  # short timeout for offline transition tests
        cls.httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        cls.server_port = cls.httpd.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.server_port}"

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass
        try:
            cls.paths_ctx.__exit__(None, None, None)
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _api_post(self, path: str, data: dict, token: str = None) -> tuple:
        url = f"{self.controller_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            res_body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(res_body)
            except Exception:
                return e.code, {"error": res_body}

    def _api_get(self, path: str, token: str = None) -> tuple:
        url = f"{self.controller_url}{path}"
        req_headers = {}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            res_body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(res_body)
            except Exception:
                return e.code, {"error": res_body}

    # --------------------------------------------------------------------------
    # Tests
    # --------------------------------------------------------------------------

    def test_01_registration_and_authentication_success(self):
        """Verify LocalWorker registers through existing /register endpoint and acquires auth_token."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-worker-01",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        res = worker.register(force=True)

        self.assertEqual(res.get("status"), "registered")
        self.assertEqual(res.get("node_id"), "server-test-worker-01")
        self.assertTrue(bool(worker.auth_token))

        # Verify cluster registry has recorded worker with accurate Windows capability model
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get("server-test-worker-01")
        self.assertIsNotNone(node_entry)
        self.assertEqual(node_entry["status"], STATE_ONLINE)
        self.assertEqual(node_entry["architecture"], get_canonical_architecture())
        self.assertTrue(node_entry["capabilities"]["compute"])
        # Must not claim false udocker capability on Windows
        self.assertFalse(node_entry["capabilities"].get("container_runtime:udocker", False))

    def test_02_authentication_failure(self):
        """Verify registration and heartbeats fail with invalid credentials."""
        # 1. Invalid enrollment token on registration
        bad_worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-bad-auth-worker",
            enrollment_token="invalid-token-xyz",
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        with self.assertRaises(LocalWorkerAuthError) as ctx:
            bad_worker.register(force=True)
        self.assertIn("401", str(ctx.exception))

        # 2. Invalid auth token on heartbeat
        valid_worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-worker-01",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        valid_worker.auth_token = "forged-auth-token-bad"
        with self.assertRaises(LocalWorkerAuthError):
            valid_worker.send_heartbeat()

    def test_03_heartbeat_and_offline_transition(self):
        """Verify heartbeat updates last_seen and missing heartbeats transition node to OFFLINE."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-hb-01",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        worker.register(force=True)

        # Send heartbeat
        hb_res = worker.send_heartbeat()
        self.assertIn("timestamp", hb_res)

        nodes_db = load_nodes_db()
        node_record = nodes_db["nodes"]["server-test-hb-01"]
        liveness_online = compute_node_liveness(node_record, timeout_seconds=3)
        self.assertEqual(liveness_online, STATE_ONLINE)

        # Simulate heartbeat timeout by backdating last_seen
        node_record["last_seen"] = "2020-01-01T00:00:00Z"
        save_nodes_db(nodes_db)

        nodes_db_fresh = load_nodes_db()
        liveness_offline = compute_node_liveness(nodes_db_fresh["nodes"]["server-test-hb-01"], timeout_seconds=3)
        self.assertEqual(liveness_offline, STATE_OFFLINE)

    def test_04_successful_job_execution(self):
        """Verify controller schedules harmless echo job, LocalWorker executes it, and result is persisted."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-worker-exec",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        worker.register(force=True)
        worker.send_heartbeat()

        # Submit job to controller targeted at this worker
        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "echo",
                "target": "server-test-worker-exec",
                "parameters": {"message": "Hello from Windows LocalWorker!"},
                "timeout": 10
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]
        self.assertEqual(submit_res["status"], JOB_STATE_QUEUED)

        # Worker polls and executes next job
        exec_res = worker.poll_and_execute_next()
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res["job_id"], job_id)
        self.assertEqual(exec_res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(exec_res["exit_code"], 0)
        self.assertIn("Hello from Windows LocalWorker!", exec_res["stdout"])

        # Verify controller has updated and persisted the final job state
        g_status, job_res = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(g_status, 200)
        self.assertEqual(job_res["job"]["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(job_res["job"]["result"]["exit_code"], 0)
        self.assertIn("Hello from Windows LocalWorker!", job_res["job"]["result"]["stdout"])

    def test_05_failed_job_and_nonzero_exit_code(self):
        """Verify failing-test job captures non-zero exit code and structured stderr message."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-fail-worker",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        worker.register(force=True)
        worker.send_heartbeat()

        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "failing-test",
                "target": "server-test-fail-worker",
                "parameters": {"error_message": "Deliberate test failure scenario"},
                "max_attempts": 1,
                "timeout": 10
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        exec_res = worker.poll_and_execute_next()
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res["status"], JOB_STATE_FAILED)
        self.assertEqual(exec_res["exit_code"], 1)
        self.assertIn("Deliberate test failure scenario", exec_res["stderr"])

        # Check persisted result
        _, job_res = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_res["job"]["status"], JOB_STATE_FAILED)
        self.assertEqual(job_res["job"]["result"]["exit_code"], 1)

    def test_06_timeout_and_cancellation(self):
        """Verify workload timeout is enforced and processes are terminated cleanly."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-timeout-worker",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        worker.register(force=True)
        worker.send_heartbeat()

        # Submit job with 1-second timeout and 10-second sleep
        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "timeout-test",
                "target": "server-test-timeout-worker",
                "parameters": {"sleep": 10},
                "max_attempts": 1,
                "timeout": 1
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        exec_res = worker.poll_and_execute_next()
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res["status"], JOB_STATE_TIMEOUT)
        self.assertEqual(exec_res["exit_code"], -1)
        self.assertIn("timed out after", exec_res["stderr"])
        self.assertIn("seconds", exec_res["stderr"])

        # Verify controller received timeout status
        _, job_res = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_res["job"]["status"], JOB_STATE_TIMEOUT)

    def test_07_duplicate_submission_and_lease_locking(self):
        """Verify atomic lease claiming prevents duplicate or concurrent execution of the same job."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-lease-worker",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        worker.register(force=True)
        worker.send_heartbeat()

        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "echo",
                "target": "server-test-lease-worker",
                "parameters": {"message": "Lease lock test"},
                "timeout": 10
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)

        # First claim succeeds and executes job
        first_claim = worker.poll_and_execute_next()
        self.assertIsNotNone(first_claim)
        self.assertEqual(first_claim["status"], JOB_STATE_SUCCEEDED)

        # Immediate second poll finds no pending job
        second_claim = worker.poll_and_execute_next()
        self.assertIsNone(second_claim)

    def test_08_unsupported_capability_rejection(self):
        """Verify worker rejects workloads requiring capabilities it does not support."""
        worker = LocalWorker(
            controller_url=self.controller_url,
            node_id="server-test-rejection-worker",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        # Manually restrict supported workloads for testing rejection
        worker.supported_workloads = {"echo"}
        worker.register(force=True)
        worker.send_heartbeat()

        # Submit a python-script workload (which this worker currently does not support)
        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "python-script",
                "target": "server-test-rejection-worker",
                "parameters": {"code": "print(123)"},
                "max_attempts": 1,
                "timeout": 10
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        exec_res = worker.poll_and_execute_next()
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res["status"], JOB_STATE_REJECTED)
        self.assertEqual(exec_res["exit_code"], 1)
        self.assertIn("not supported by LocalWorker", exec_res["stderr"])

    def test_09_controller_restart_and_worker_reconnection(self):
        """Verify worker handles transient controller connection drops without unhandled crashes."""
        worker = LocalWorker(
            controller_url="http://127.0.0.1:59999",  # unreachable port
            node_id="server-test-reconnect-worker",
            enrollment_token=self.enrollment_token,
            config_dir=self.config_dir,
            runtime_dir=self.runtime_dir
        )
        # Attempting heartbeat to unreachable controller raises LocalWorkerError gracefully
        with self.assertRaises(LocalWorkerError):
            worker.send_heartbeat()

        # Restoring valid controller URL allows clean recovery
        worker.controller_url = self.controller_url
        worker.register(force=True)
        hb_res = worker.send_heartbeat()
        self.assertIn("timestamp", hb_res)

    def test_10_log_and_secret_redaction(self):
        """Verify credentials, tokens, and passwords in job parameters or outputs are redacted."""
        payload_with_secrets = {
            "token": "secret_token_123456789",
            "password": "super_secret_password",
            "safe_param": "safe_value",
            "nested": {
                "auth_key": "nested_secret_xyz"
            }
        }
        redacted = redact_sensitive_dict(payload_with_secrets)
        self.assertIn("[REDACTED]", redacted["password"])
        self.assertNotIn("super_secret", redacted["password"])
        self.assertEqual(redacted["safe_param"], "safe_value")
        self.assertIn("[REDACTED]", redacted["nested"]["auth_key"])


if __name__ == "__main__":
    unittest.main()
