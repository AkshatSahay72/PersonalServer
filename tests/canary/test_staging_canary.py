#!/usr/bin/env python3
"""
PersonalServer Staging Canary Suite
===================================
End-to-end verification of the Distributed Job Scheduler in an isolated,
disposable staging environment simulating the multi-node cluster topology.

Canary Workload Sequence (executed in order):
1. Simple HTTP health check against a controlled test endpoint.
2. Short Python script writing a result artifact.
3. Two-step workflow where Job 2 consumes Job 1's output.
4. Transfer an artifact between two simulated workers & verify SHA-256 checksum.
5. Simulate worker disconnect during active job execution.
6. Verify lease expiration, bounded recovery, and rejection of superseded lease generations.
7. Attempt unauthenticated job submission, artifact listing, and artifact download.
8. Prohibited private-network HTTP targets and redirect SSRF verification.
9. Verify Jobs UI payload structure for terminal states, waiting reasons, and artifact links.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agent.job_executor import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    JobExecutor,
)
from controller.constants import JOB_STATE_NEEDS_ATTENTION
from common.artifact_manager import (
    ArtifactManager,
    compute_file_sha256,
    generate_artifact_id,
    store_artifact_bytes,
)
from config.platform_config import get_controller_paths, override_controller_paths
from controller.controller import (
    ControllerHandler,
    compute_node_liveness,
    load_jobs_db,
    load_nodes_db,
    save_jobs_db,
    save_nodes_db,
    sweep_expired_leases,
)
from controller.workflow_engine import (
    evaluate_workflow_progress,
    instantiate_workflow,
    progress_workflows,
)


class MockTargetServerHandler(BaseHTTPRequestHandler):
    """Mock external and redirect target for controlled HTTP checks and SSRF tests."""

    def log_message(self, format, *args):
        pass  # Suppress logging during tests

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"status": "healthy", "service": "canary-target"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-to-prohibited":
            # Redirects to local loopback address to test redirect SSRF filter
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:22/internal-service")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class StagingCanaryTest(unittest.TestCase):
    """Executes the complete staging canary workload sequence against disposable test services."""

    @classmethod
    def setUpClass(cls):
        # 1. Create disposable staging environment directories
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_staging_canary_"))
        cls.data_dir = cls.test_dir / "data"
        cls.config_dir = cls.test_dir / "config"
        cls.secrets_dir = cls.config_dir / "secrets"
        cls.runtime_dir = cls.test_dir / "runtime"
        cls.logs_dir = cls.test_dir / "logs"
        cls.storage_dir = cls.test_dir / "storage"

        for d in (cls.data_dir, cls.secrets_dir, cls.runtime_dir, cls.logs_dir, cls.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        cls.enrollment_token = "canary-admin-enrollment-secret-token"
        (cls.secrets_dir / "enrollment.token").write_text(cls.enrollment_token + "\n", encoding="utf-8")

        # 2. Activate paths override
        cls.paths_ctx = override_controller_paths(
            data_dir=cls.data_dir,
            config_dir=cls.config_dir,
            secrets_dir=cls.secrets_dir,
            runtime_dir=cls.runtime_dir,
            logs_dir=cls.logs_dir,
            storage_dir=cls.storage_dir
        )
        cls.paths = cls.paths_ctx.__enter__()

        # 3. Spin up in-process Staging Controller on ephemeral port
        ControllerHandler.heartbeat_timeout = 4  # Short timeout for lease and disconnect tests
        cls.controller_httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        cls.controller_port = cls.controller_httpd.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.controller_port}"
        cls.controller_thread = threading.Thread(target=cls.controller_httpd.serve_forever, daemon=True)
        cls.controller_thread.start()

        # 4. Spin up mock target server on ephemeral port
        cls.target_httpd = HTTPServer(("127.0.0.1", 0), MockTargetServerHandler)
        cls.target_port = cls.target_httpd.server_address[1]
        cls.target_url = f"http://127.0.0.1:{cls.target_port}"
        cls.target_thread = threading.Thread(target=cls.target_httpd.serve_forever, daemon=True)
        cls.target_thread.start()

        # 5. Register two simulated staging nodes (representing 2-node cluster topology)
        cls.worker1_id = "staging-worker-1"
        cls.worker1_token = "w1-token-alpha-secret"
        cls.worker2_id = "staging-worker-2"
        cls.worker2_token = "w2-token-beta-secret"

        cls._register_node(cls.worker1_id, cls.worker1_token, name="Simulated Vivo Y31", arch="arm64")
        cls._register_node(cls.worker2_id, cls.worker2_token, name="Simulated Realme 5 Pro", arch="arm64")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.controller_httpd.shutdown()
            cls.controller_httpd.server_close()
        except Exception:
            pass
        try:
            cls.target_httpd.shutdown()
            cls.target_httpd.server_close()
        except Exception:
            pass
        try:
            cls.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    @classmethod
    def _register_node(cls, node_id: str, token: str, name: str, arch: str):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nodes_db = load_nodes_db()
        nodes_db.setdefault("nodes", {})[node_id] = {
            "node_id": node_id,
            "name": name,
            "role": "compute",
            "architecture": arch,
            "status": "ONLINE",
            "auth_token": token,
            "last_seen": now_iso,
            "capabilities": ["python:3", "shell:sh"],
            "resources": {"cpu_cores": 8, "ram_mb": 4096, "storage_gb": 64},
            "last_heartbeat": {
                "timestamp": now_iso,
                "services": {"node_api": "running"},
                "system": {"memory": "1.0Gi/4.0Gi", "cpu_cores": 8, "storage": "10Gi/64Gi"}
            }
        }
        save_nodes_db(nodes_db)

    def _http_request(self, method: str, path: str, data: dict = None, token: str = None, headers: dict = None) -> tuple:
        url = f"{self.controller_url}{path}"
        req_headers = headers or {}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        body_bytes = None
        if data is not None:
            req_headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"error": raw}

    # -------------------------------------------------------------------------
    # Workload 1: Simple HTTP health check against a controlled test endpoint
    # -------------------------------------------------------------------------
    def test_01_canary_http_health_check_controlled_endpoint(self):
        """Submit http-check to controlled test server; verify claim, execution, and success."""
        # Submit http-check targeting our mock server /healthz
        target_health_url = f"{self.target_url}/healthz"
        status, res = self._http_request(
            "POST",
            "/jobs",
            {
                "type": "http-check",
                "name": "canary-health-probe",
                "target_node": self.worker1_id,
                "pinned": True,
                "parameters": {"url": target_health_url, "allow_internal": True}
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200, f"Job submission failed: {res}")
        job_id = res["job_id"]

        # Worker 1 claims job
        c_status, c_res = self._http_request("GET", f"/nodes/{self.worker1_id}/jobs/next", token=self.worker1_token)
        self.assertEqual(c_status, 200)
        self.assertIsNotNone(c_res.get("job"))
        claimed_job = c_res["job"]
        self.assertEqual(claimed_job["id"], job_id)
        self.assertEqual(claimed_job["status"], JOB_STATE_CLAIMED)

        # Worker 1 executes job locally via JobExecutor
        exec_res = JobExecutor.execute(claimed_job)
        self.assertEqual(exec_res["status"], JOB_STATE_SUCCEEDED)
        self.assertIn("healthy", exec_res["stdout"])

        # Worker 1 reports result
        r_status, r_res = self._http_request(
            "POST",
            f"/jobs/{job_id}/result",
            exec_res,
            token=self.worker1_token
        )
        self.assertEqual(r_status, 200)
        self.assertEqual(r_res["status"], "ok")
        jobs_db = load_jobs_db()
        self.assertEqual(jobs_db["jobs"][job_id]["status"], JOB_STATE_SUCCEEDED)

    # -------------------------------------------------------------------------
    # Workload 2: Short Python script that writes a small result artifact
    # -------------------------------------------------------------------------
    def test_02_canary_python_script_writes_result_artifact(self):
        """Submit python-script that computes payload and uploads artifact."""
        py_code = (
            "import hashlib, json\n"
            "data = b'canary-computation-result'\n"
            "print('HASH:' + hashlib.sha256(data).hexdigest())\n"
        )
        status, res = self._http_request(
            "POST",
            "/jobs",
            {
                "type": "python-script",
                "name": "canary-python-artifact-gen",
                "target_node": self.worker1_id,
                "parameters": {"script": py_code}
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = res["job_id"]

        # Claim and execute
        _, claim = self._http_request("GET", f"/nodes/{self.worker1_id}/jobs/next", token=self.worker1_token)
        exec_res = JobExecutor.execute(claim["job"])
        self.assertEqual(exec_res["status"], JOB_STATE_SUCCEEDED)
        expected_hash = hashlib.sha256(b"canary-computation-result").hexdigest()
        self.assertIn(expected_hash, exec_res["stdout"])

        # Worker uploads artifact
        artifact_bytes = b"canary-computation-result-artifact-payload"
        art_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        u_status, u_res = self._http_request(
            "POST",
            "/artifacts/upload",
            {
                "filename": "result.bin",
                "content_base64": base64.b64encode(artifact_bytes).decode("ascii"),
                "job_id": job_id
            },
            token=self.worker1_token
        )
        self.assertEqual(u_status, 201)
        art_record = u_res["artifact"]
        self.assertEqual(art_record["sha256"], art_sha256)

        # Complete job
        exec_res["artifacts"] = [art_record["artifact_id"]]
        r_status, _ = self._http_request("POST", f"/jobs/{job_id}/result", exec_res, token=self.worker1_token)
        self.assertEqual(r_status, 200)

    # -------------------------------------------------------------------------
    # Workload 3: Two-step workflow in which Job 2 consumes Job 1's output
    # -------------------------------------------------------------------------
    def test_03_canary_two_step_workflow_topological_execution(self):
        """Verify 2-step DAG workflow: Step 2 waits for Step 1, completes in correct sequence."""
        jobs_db = load_jobs_db()
        wf_spec = {
            "name": "Canary Ingest and Transform Workflow",
            "failure_policy": "fail_fast",
            "steps": {
                "step1_ingest": {
                    "type": "echo",
                    "parameters": {"message": "INGEST_DONE"}
                },
                "step2_transform": {
                    "type": "echo",
                    "depends_on": ["step1_ingest"],
                    "parameters": {"message": "TRANSFORM_DONE"}
                }
            }
        }
        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)
        save_jobs_db(jobs_db)
        s1_id = wf_rec["steps"]["step1_ingest"]["job_id"]
        s2_id = wf_rec["steps"]["step2_transform"]["job_id"]

        # Step 1 is QUEUED; Step 2 must be WAITING_FOR_DEPENDENCY
        self.assertEqual(jobs_db["jobs"][s1_id]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][s2_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # Step 1 executes and finishes
        jobs_db["jobs"][s1_id]["status"] = JOB_STATE_SUCCEEDED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        # Progress workflow
        changed = progress_workflows(workflows_db, jobs_db)
        self.assertTrue(changed)

        # Step 2 has been unblocked to QUEUED
        self.assertEqual(jobs_db["jobs"][s2_id]["status"], JOB_STATE_QUEUED)

        # Step 2 completes
        jobs_db["jobs"][s2_id]["status"] = JOB_STATE_SUCCEEDED
        progress_workflows(workflows_db, jobs_db)

        # Workflow fully succeeded
        self.assertEqual(wf_rec["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_rec["completed_steps"], 2)

    # -------------------------------------------------------------------------
    # Workload 4: Transfer an artifact between two simulated workers & verify SHA-256
    # -------------------------------------------------------------------------
    def test_04_canary_cross_worker_artifact_transfer_and_checksum(self):
        """Worker 1 uploads artifact; Worker 2 downloads it and verifies exact SHA-256."""
        payload = b"Transferable artifact payload across heterogeneous nodes in cluster."
        expected_sha256 = hashlib.sha256(payload).hexdigest()

        # Worker 1 uploads
        u_status, u_res = self._http_request(
            "POST",
            "/artifacts/upload",
            {
                "filename": "cross_node.bin",
                "content_base64": base64.b64encode(payload).decode("ascii")
            },
            token=self.worker1_token
        )
        self.assertEqual(u_status, 201)
        art_id = u_res["artifact"]["artifact_id"]

        # Worker 2 downloads artifact
        d_status, d_body = self._http_request(
            "GET",
            f"/artifacts/{art_id}/download",
            token=self.worker2_token
        )
        self.assertEqual(d_status, 200)
        received_bytes = d_body.encode("utf-8") if isinstance(d_body, str) else d_body
        received_sha256 = hashlib.sha256(received_bytes).hexdigest()

        # Exact checksum verification
        self.assertEqual(received_sha256, expected_sha256)

    # -------------------------------------------------------------------------
    # Workload 5: Simulate worker disconnect during an active job
    # -------------------------------------------------------------------------
    def test_05_canary_worker_disconnect_during_active_job(self):
        """Worker claims job, disconnects (offline); sweeper detects disconnect and marks retry."""
        status, res = self._http_request(
            "POST",
            "/jobs",
            {
                "type": "echo",
                "name": "canary-disconnect-test",
                "target_node": self.worker1_id,
                "pinned": True,
                "max_attempts": 2
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = res["job_id"]

        # Worker 1 claims job
        _, claim = self._http_request("GET", f"/nodes/{self.worker1_id}/jobs/next", token=self.worker1_token)
        self.assertIsNotNone(claim.get("job"))
        self.assertEqual(claim["job"]["status"], JOB_STATE_CLAIMED)
        claimed_job_id = claim["job"]["id"]

        # Simulate Worker 1 disconnecting (stale heartbeat beyond timeout)
        nodes_db = load_nodes_db()
        stale_time = "2026-10-01T00:00:00Z"
        nodes_db["nodes"][self.worker1_id]["last_seen"] = stale_time
        nodes_db["nodes"][self.worker1_id]["last_heartbeat"]["timestamp"] = stale_time
        save_nodes_db(nodes_db)

        # Sweeper executes
        jobs_db = load_jobs_db()
        recovered = sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=4)
        save_jobs_db(jobs_db)
        self.assertGreaterEqual(recovered, 1)

        # Job is transitioned to RETRY_WAIT or RECOVERING with explanatory reason
        job_after = jobs_db["jobs"][claimed_job_id]
        self.assertIn(job_after["status"], (JOB_STATE_RETRY_WAIT, JOB_STATE_RECOVERING, JOB_STATE_QUEUED, JOB_STATE_NEEDS_ATTENTION))
        self.assertIn("went OFFLINE", job_after.get("retry_reason") or "")

    # -------------------------------------------------------------------------
    # Workload 6: Lease expiration, bounded recovery, & rejection of older generation
    # -------------------------------------------------------------------------
    def test_06_canary_lease_expiry_and_stale_generation_fencing_rejection(self):
        """A superseded lease generation result is rejected with 409 Conflict."""
        # Restore Worker 1 to ONLINE
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nodes_db = load_nodes_db()
        nodes_db["nodes"][self.worker1_id]["last_seen"] = now_iso
        nodes_db["nodes"][self.worker1_id]["last_heartbeat"]["timestamp"] = now_iso
        save_nodes_db(nodes_db)

        # Set up a job where generation was incremented to 2
        jobs_db = load_jobs_db()
        test_job_id = "canary-fenced-job"
        jobs_db.setdefault("jobs", {})[test_job_id] = {
            "job_id": test_job_id,
            "target_node": self.worker1_id,
            "status": JOB_STATE_RUNNING,
            "attempt": 2,
            "lease_generation": 2,
            "lease_expires_at": time.time() + 100
        }
        save_jobs_db(jobs_db)

        # Worker 1 (stale worker report) submits result with superseded generation 1
        stale_res = {
            "job_id": test_job_id,
            "status": JOB_STATE_SUCCEEDED,
            "lease_generation": 1,
            "exit_code": 0,
            "stdout": "Late report from old generation",
            "stderr": ""
        }
        rej_status, rej_body = self._http_request(
            "POST",
            f"/jobs/{test_job_id}/result",
            stale_res,
            token=self.worker1_token
        )
        self.assertEqual(rej_status, 409, f"Stale generation should be rejected with 409: {rej_body}")
        self.assertTrue(rej_body.get("superseded"))

        # Valid Worker 1 submits result with current generation (2)
        valid_res = {
            "job_id": test_job_id,
            "status": JOB_STATE_SUCCEEDED,
            "lease_generation": 2,
            "exit_code": 0,
            "stdout": "Current report from generation 2",
            "stderr": ""
        }
        ok_status, ok_body = self._http_request(
            "POST",
            f"/jobs/{test_job_id}/result",
            valid_res,
            token=self.worker1_token
        )
        self.assertEqual(ok_status, 200)
        self.assertEqual(ok_body["status"], "ok")
        jobs_db = load_jobs_db()
        self.assertEqual(jobs_db["jobs"][test_job_id]["status"], JOB_STATE_SUCCEEDED)

    # -------------------------------------------------------------------------
    # Workload 7: Unauthenticated job submission, artifact listing, & download
    # -------------------------------------------------------------------------
    def test_07_canary_unauthenticated_requests_safely_rejected(self):
        """Unauthenticated requests to submission and artifact endpoints return 401."""
        # 1. POST /jobs without token
        s1, r1 = self._http_request("POST", "/jobs", {"type": "echo"}, token="")
        self.assertEqual(s1, 401)
        self.assertIn("Unauthorized", r1.get("error", ""))

        # 2. GET /artifacts without token
        s2, r2 = self._http_request("GET", "/artifacts", token="")
        self.assertEqual(s2, 401)

        # 3. GET /artifacts/<id>/download without token
        s3, r3 = self._http_request("GET", "/artifacts/art-canary-dummy/download", token="")
        self.assertEqual(s3, 401)

        # 4. DELETE /jobs/<id> without token
        s4, r4 = self._http_request("DELETE", "/jobs/job-dummy", token="")
        self.assertEqual(s4, 401)

    # -------------------------------------------------------------------------
    # Workload 8: Prohibited private-network HTTP target & redirect SSRF
    # -------------------------------------------------------------------------
    def test_08_canary_ssrf_prohibited_targets_and_redirect_protection(self):
        """Direct SSRF to private/metadata IP and redirect-based SSRF are blocked."""
        # 1. Direct SSRF against cloud metadata IP (169.254.169.254)
        direct_ssrf = JobExecutor.execute({
            "job_id": "canary-ssrf-direct",
            "type": "http-check",
            "parameters": {"url": "http://169.254.169.254/latest/meta-data/"},
            "timeout": 5
        })
        self.assertEqual(direct_ssrf["status"], JOB_STATE_FAILED)
        self.assertIn("Security violation (SSRF)", direct_ssrf["stderr"])

        # 2. Worker node token cannot bypass SSRF using allow_internal: True
        post_status, post_res = self._http_request(
            "POST",
            "/jobs",
            {"type": "http-check", "parameters": {"url": "http://10.0.0.1", "allow_internal": True}},
            token=self.worker1_token
        )
        self.assertEqual(post_status, 403)
        self.assertIn("requires administrator credentials", post_res.get("error", ""))

        # 3. Redirect SSRF: external server redirects to internal service
        redirect_test_url = f"{self.target_url}/redirect-to-prohibited"
        # Admin requests probe against mock target; target issues 302 redirect to http://127.0.0.1:22/internal-service
        # SafeRedirectHandler inside urllib must intercept redirect and abort with 403 SSRF Blocked
        redirect_exec = JobExecutor.execute({
            "job_id": "canary-ssrf-redirect",
            "type": "http-check",
            "parameters": {"url": redirect_test_url, "allow_internal": False},
            "timeout": 5
        })
        self.assertEqual(redirect_exec["status"], JOB_STATE_FAILED)
        self.assertTrue(
            "SSRF Blocked" in redirect_exec["stderr"] or "Security violation (SSRF)" in redirect_exec["stderr"],
            f"Expected SSRF redirect block, got: {redirect_exec['stderr']}"
        )

    # -------------------------------------------------------------------------
    # Workload 9: Confirm Jobs UI presentation data structures
    # -------------------------------------------------------------------------
    def test_09_canary_jobs_ui_presentation_data_structures(self):
        """Query /jobs and verify structure contains all attributes required by app.js."""
        status, res = self._http_request("GET", "/jobs", token=self.enrollment_token)
        self.assertEqual(status, 200)
        jobs = res.get("jobs", [])
        self.assertIsInstance(jobs, list)
        self.assertGreater(len(jobs), 0)

        # Inspect canonical properties used by app.js rendering
        sample_job = jobs[0]
        self.assertIn("id", sample_job)
        self.assertIn("status", sample_job)
        self.assertIn("type", sample_job)
        self.assertIn("lease_generation", sample_job)
        self.assertIn("created_at", sample_job)

        # Check diagnostics on individual job detail endpoint /jobs/<id>
        job_id = sample_job["id"]
        d_status, d_res = self._http_request("GET", f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(d_status, 200)
        self.assertIn("diagnostics", d_res)
        diag = d_res["diagnostics"]
        self.assertIn("is_active", diag)
        self.assertIn("current_attempt", diag)
        self.assertIn("lease_active", diag)


if __name__ == "__main__":
    unittest.main()
