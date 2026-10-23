#!/usr/bin/env python3
"""
PersonalServer Jobs Scaling, Pagination & Safe Cancellation Test Suite
======================================================================
Tests covering:
1. Queued job cancellation before execution (never claimed by worker).
2. Running job cancellation with process termination.
3. Cancellation / completion race conditions handled atomically.
4. Repeated cancellation requests (idempotency support).
5. Cancelled jobs are never retried or swept into RECOVERING.
6. Unauthorized cancellation attempts rejected (HTTP 401).
7. Server-side pagination, total counts, and sorting (recent/oldest).
8. Multi-criteria filtering (status, workload type, node, search query).
9. Worker disconnect and cancellation behavior.
10. Unsupported or missing active process reporting integrity.
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

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths
from controller.controller import (
    ControllerHandler,
    load_jobs_db,
    save_jobs_db,
    sweep_expired_leases,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_CANCELLED,
    JOB_STATE_RECOVERING
)
from agent.job_executor import JobExecutor
from agent.simulated_worker import SimulatedWorker


class TestJobsCancellationAndPagination(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_jobs_cancel_test_"))
        cls.data_dir = cls.test_dir / "data"
        cls.config_dir = cls.test_dir / "config"
        cls.secrets_dir = cls.config_dir / "secrets"
        cls.runtime_dir = cls.test_dir / "runtime"
        cls.logs_dir = cls.test_dir / "logs"
        cls.storage_dir = cls.test_dir / "storage"

        for d in (cls.data_dir, cls.secrets_dir, cls.runtime_dir, cls.logs_dir, cls.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        cls.enrollment_token = "jobs-test-token-supersecret-999"
        (cls.secrets_dir / "enrollment.token").write_text(cls.enrollment_token + "\n", encoding="utf-8")

        cls.paths_ctx = override_controller_paths(
            data_dir=cls.data_dir,
            config_dir=cls.config_dir,
            secrets_dir=cls.secrets_dir,
            runtime_dir=cls.runtime_dir,
            logs_dir=cls.logs_dir,
            storage_dir=cls.storage_dir
        )
        cls.paths = cls.paths_ctx.__enter__()

        ControllerHandler.heartbeat_timeout = 3
        ControllerHandler.default_backoff_sec = 0.0
        cls.httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        cls.server_port = cls.httpd.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.server_port}"

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "httpd"):
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if hasattr(cls, "paths_ctx"):
            cls.paths_ctx.__exit__(None, None, None)
        if hasattr(cls, "test_dir") and cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _api_get(self, path, token=None):
        url = f"{self.controller_url}{path}"
        headers = {"User-Agent": "PersonalServer-Test/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return e.code, data

    def _api_post(self, path, payload, token=None):
        url = f"{self.controller_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "PersonalServer-Test/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return e.code, data

    # --------------------------------------------------------------------------
    # 1. Queued Job Cancellation
    # --------------------------------------------------------------------------
    def test_01_queued_cancellation_prevents_worker_claim(self):
        """A queued job cancelled before execution must never be leased to a worker."""
        worker = SimulatedWorker("q-worker", controller_url=self.controller_url, enrollment_token=self.enrollment_token)
        worker.register()
        worker.send_heartbeat()

        # Submit job
        st, sub = self._api_post("/jobs", {"type": "echo", "target": worker.node_id, "parameters": {"message": "hello"}}, token=self.enrollment_token)
        self.assertEqual(st, 200)
        job_id = sub["job_id"]

        # Cancel while QUEUED
        c_code, c_res = self._api_post(f"/jobs/{job_id}/cancel", {"reason": "Cancelled before start"}, token=self.enrollment_token)
        self.assertEqual(c_code, 200)
        self.assertEqual(c_res["status"], JOB_STATE_CANCELLED)

        # Worker polls for next job -> must receive None
        claimed = worker.poll_and_claim_job()
        self.assertIsNone(claimed)

        # Verify DB status
        _, job_info = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_info["job"]["status"], JOB_STATE_CANCELLED)
        self.assertEqual(job_info["job"]["cancel_reason"], "Cancelled before start")

    # --------------------------------------------------------------------------
    # 2. Running Job Active Cancellation
    # --------------------------------------------------------------------------
    def test_02_running_job_cancellation_and_process_termination(self):
        """Active running job cancellation stops process and transitions status."""
        job_id = "test-running-job-002"
        # Simulate an actively registered job in JobExecutor
        with JobExecutor._ACTIVE_JOBS_LOCK:
            JobExecutor._ACTIVE_JOBS[job_id] = {
                "proc": None,
                "type": "timeout-test",
                "container_name": None,
                "started_at": time.time(),
                "cancelled": False,
                "cancel_reason": None
            }

        # Put job in DB as RUNNING
        db = load_jobs_db()
        db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "type": "timeout-test",
            "target_node": "local",
            "status": JOB_STATE_RUNNING,
            "attempt": 1,
            "max_attempts": 3,
            "submitted_at": "2026-10-15T12:00:00Z"
        }
        save_jobs_db(db)

        # Cancel while RUNNING
        c_code, c_res = self._api_post(f"/jobs/{job_id}/cancel", {"reason": "Terminated during run"}, token=self.enrollment_token)
        self.assertEqual(c_code, 200)
        self.assertEqual(c_res["status"], JOB_STATE_CANCELLED)
        self.assertTrue(c_res["job"]["cancellation_requested"])

        # Check executor received cancellation flag
        with JobExecutor._ACTIVE_JOBS_LOCK:
            info = JobExecutor._ACTIVE_JOBS.get(job_id)
            self.assertTrue(info["cancelled"])
            self.assertEqual(info["cancel_reason"], "Terminated during run")
            # Clean up test registry
            JobExecutor._ACTIVE_JOBS.pop(job_id, None)

    # --------------------------------------------------------------------------
    # 3. Cancellation / Completion Race
    # --------------------------------------------------------------------------
    def test_03_cancellation_completion_race(self):
        """When worker submits result for an already cancelled job, result is acknowledged without overwriting CANCELLED."""
        worker = SimulatedWorker("race-worker", controller_url=self.controller_url, enrollment_token=self.enrollment_token)
        worker.register()
        worker.send_heartbeat()

        _, sub = self._api_post("/jobs", {"type": "echo", "target": worker.node_id}, token=self.enrollment_token)
        job_id = sub["job_id"]
        worker.poll_and_claim_job()

        # Cancel job while CLAIMED
        c_code, _ = self._api_post(f"/jobs/{job_id}/cancel", {"reason": "User cancelled"}, token=self.enrollment_token)
        self.assertEqual(c_code, 200)

        # Worker now submits result of the job it completed in background
        res = worker.submit_job_result(job_id, status=JOB_STATE_SUCCEEDED, stdout="Race stdout")
        # Controller returns duplicate/acknowledged
        self.assertTrue(res.get("duplicate") or res.get("status") == JOB_STATE_CANCELLED)

        # Job in DB MUST remain CANCELLED
        _, check = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(check["job"]["status"], JOB_STATE_CANCELLED)

    # --------------------------------------------------------------------------
    # 4. Repeated Cancellation Idempotency
    # --------------------------------------------------------------------------
    def test_04_repeated_cancellation_idempotency(self):
        """Repeated cancellation requests with idempotent=True return HTTP 200 with duplicate=True."""
        db = load_jobs_db()
        job_id = "test-idempotent-cancel-004"
        db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "type": "echo",
            "status": JOB_STATE_CANCELLED,
            "cancelled_at": "2026-10-15T12:00:00Z",
            "finished_at": "2026-10-15T12:00:00Z"
        }
        save_jobs_db(db)

        # Repeat cancellation with idempotent flag
        c_code, c_res = self._api_post(f"/jobs/{job_id}/cancel", {"idempotent": True}, token=self.enrollment_token)
        self.assertEqual(c_code, 200)
        self.assertTrue(c_res.get("duplicate"))
        self.assertEqual(c_res["status"], JOB_STATE_CANCELLED)

    # --------------------------------------------------------------------------
    # 5. Cancelled Jobs are Never Retried
    # --------------------------------------------------------------------------
    def test_05_cancelled_job_never_swept_into_recovering(self):
        """Sweep expired leases must never sweep CANCELLED jobs into RECOVERING."""
        db = load_jobs_db()
        job_id = "test-no-retry-005"
        db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "type": "echo",
            "status": JOB_STATE_CANCELLED,
            "attempt": 1,
            "max_attempts": 3,
            "lease_expires_at": time.time() - 100,  # Expired
            "retryable": True
        }
        save_jobs_db(db)

        # Run sweep
        sweep_expired_leases(db, {"nodes": {}}, timeout_seconds=1)

        # Status must still be CANCELLED, not RECOVERING
        self.assertEqual(db["jobs"][job_id]["status"], JOB_STATE_CANCELLED)

    # --------------------------------------------------------------------------
    # 6. Unauthorized Cancellation Rejected
    # --------------------------------------------------------------------------
    def test_06_unauthorized_cancellation_rejected(self):
        """POST /jobs/<id>/cancel without valid authorization token returns 401."""
        c_code, c_res = self._api_post("/jobs/any-job/cancel", {"reason": "hack"}, token="wrong-token-xyz")
        self.assertEqual(c_code, 401)
        self.assertIn("Unauthorized", c_res.get("error", ""))

    # --------------------------------------------------------------------------
    # 7. Server-Side Pagination and Sorting
    # --------------------------------------------------------------------------
    def test_07_server_side_pagination_and_sorting(self):
        """GET /jobs supports limit, page, total_count, total_pages, and sorting."""
        db = load_jobs_db()
        # Seed 5 jobs with distinct timestamps
        for i in range(1, 6):
            jid = f"job-paged-{i:02d}"
            db.setdefault("jobs", {})[jid] = {
                "job_id": jid,
                "type": "echo",
                "status": JOB_STATE_SUCCEEDED,
                "created_at": f"2026-10-15T10:0{i}:00Z",
                "result": {"duration_ms": i * 100}
            }
        save_jobs_db(db)

        # Fetch page 1 (limit 2) recent first
        st1, p1 = self._api_get("/jobs?search=job-paged&page=1&limit=2&sort=recent", token=self.enrollment_token)
        self.assertEqual(st1, 200)
        self.assertEqual(len(p1["jobs"]), 2)
        self.assertEqual(p1["total_count"], 5)
        self.assertEqual(p1["page"], 1)
        self.assertEqual(p1["total_pages"], 3)
        # Most recent first
        self.assertEqual(p1["jobs"][0]["job_id"], "job-paged-05")
        self.assertEqual(p1["jobs"][1]["job_id"], "job-paged-04")

        # Fetch page 2 (limit 2)
        st2, p2 = self._api_get("/jobs?search=job-paged&page=2&limit=2&sort=recent", token=self.enrollment_token)
        self.assertEqual(st2, 200)
        self.assertEqual(len(p2["jobs"]), 2)
        self.assertEqual(p2["jobs"][0]["job_id"], "job-paged-03")
        self.assertEqual(p2["jobs"][1]["job_id"], "job-paged-02")

    # --------------------------------------------------------------------------
    # 8. Multi-Criteria Filtering
    # --------------------------------------------------------------------------
    def test_08_filtering_by_status_type_and_search(self):
        """GET /jobs filters by status, type, and search keyword."""
        db = load_jobs_db()
        db.setdefault("jobs", {})["search-target-alpha"] = {
            "job_id": "search-target-alpha",
            "type": "health-check",
            "target_node": "node-alpha",
            "status": JOB_STATE_RUNNING
        }
        db.setdefault("jobs", {})["search-target-beta"] = {
            "job_id": "search-target-beta",
            "type": "system-info",
            "target_node": "node-beta",
            "status": JOB_STATE_FAILED
        }
        save_jobs_db(db)

        # Filter by status
        st, r_status = self._api_get("/jobs?status=RUNNING", token=self.enrollment_token)
        self.assertEqual(st, 200)
        self.assertTrue(all(j["status"] == "RUNNING" for j in r_status["jobs"]))

        # Filter by type
        st, r_type = self._api_get("/jobs?type=health-check", token=self.enrollment_token)
        self.assertEqual(st, 200)
        self.assertTrue(all(j["type"] == "health-check" for j in r_type["jobs"]))

        # Filter by search
        st, r_search = self._api_get("/jobs?search=target-alpha", token=self.enrollment_token)
        self.assertEqual(st, 200)
        self.assertEqual(len(r_search["jobs"]), 1)
        self.assertEqual(r_search["jobs"][0]["job_id"], "search-target-alpha")

    # --------------------------------------------------------------------------
    # 9. Worker Disconnect Handling
    # --------------------------------------------------------------------------
    def test_09_cancellation_persists_across_worker_disconnect(self):
        """Cancelling a job assigned to an offline worker keeps it CANCELLED permanently."""
        db = load_jobs_db()
        job_id = "test-worker-disc-009"
        db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "type": "echo",
            "target_node": "disconnected-node",
            "status": JOB_STATE_CLAIMED,
            "lease_expires_at": time.time() + 30
        }
        save_jobs_db(db)

        # Cancel the job
        c_code, _ = self._api_post(f"/jobs/{job_id}/cancel", {"reason": "Worker gone"}, token=self.enrollment_token)
        self.assertEqual(c_code, 200)

        # Sweeper runs with disconnected node
        sweep_expired_leases(db, {"nodes": {"disconnected-node": {"status": "OFFLINE"}}}, timeout_seconds=1)

        # Job must stay CANCELLED
        reloaded = load_jobs_db()
        self.assertEqual(reloaded["jobs"][job_id]["status"], JOB_STATE_CANCELLED)

    # --------------------------------------------------------------------------
    # 10. Unsupported Workload Termination Reporting
    # --------------------------------------------------------------------------
    def test_10_unsupported_or_not_found_process_reporting(self):
        """Terminating an unknown or non-running process returns truthful termination metadata."""
        res = JobExecutor.terminate_active_job("non-existent-job-xyz")
        self.assertTrue(res.get("supported"))
        self.assertFalse(res.get("found"))
        self.assertFalse(res.get("terminated"))
        self.assertIn("not actively executing", res.get("message", ""))


if __name__ == "__main__":
    unittest.main()
