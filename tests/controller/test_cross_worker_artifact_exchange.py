#!/usr/bin/env python3
"""
Tests for Secure Cross-Worker Artifact Exchange
===============================================
Comprehensive test suite verifying:
1. Path containment, traversal protection, symlink escape rejection in job sandbox.
2. Chunked streaming uploads, downloads, SHA-256 verification, and 64MB size limits.
3. Role & workflow based access control: cross-workflow artifact isolation (403 Forbidden).
4. Idempotent upload handling on worker retries (duplicate avoidance).
5. Workflow DAG artifact dependency resolution: Step B is queued only when Step A succeeds
   and its declared artifacts are verified and resolved.
6. Deterministic failure when upstream step succeeds but fails to produce declared output.
7. End-to-end simulated two-worker workflow execution (Worker 1 producing, Worker 2 consuming).
"""

import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib
from config.platform_config import override_controller_paths
from agent.job_executor import JobExecutor

_node_agent_mod = importlib.import_module("agent.node-agent")
_download_input_artifacts = getattr(_node_agent_mod, "_download_input_artifacts")
_upload_output_artifacts = getattr(_node_agent_mod, "_upload_output_artifacts")

from common.artifact_manager import (
    ArtifactManager,
    MAX_ARTIFACT_SIZE_BYTES,
    compute_file_sha256,
    generate_artifact_id,
    store_artifact_bytes,
    store_artifact_stream,
    validate_artifact_path,
    validate_job_output_path,
    verify_artifact_integrity,
)
from common.time_utils import get_current_iso_timestamp
from controller.constants import (
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
)
from controller.http.job_routes import (
    handle_job_get,
    handle_job_post,
)
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import load_artifacts_db, load_jobs_db, load_nodes_db, save_nodes_db
from controller.workflow_engine import (
    evaluate_workflow_progress,
    instantiate_workflow,
    progress_workflows,
)


class TestArtifactSandboxSecurity(unittest.TestCase):
    """Test path containment, directory traversal, and symlink escape defenses."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ps_art_sec_")
        self.sandbox = Path(self.tmp_dir).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_valid_sandbox_output_path(self):
        """Clean relative path inside sandbox resolves safely."""
        test_file = self.sandbox / "result.txt"
        test_file.write_text("ok", encoding="utf-8")
        resolved = validate_job_output_path(self.sandbox, "result.txt")
        self.assertEqual(resolved, test_file)

    def test_02_path_traversal_attempts_rejected(self):
        """Parent directory traversal ('..') is strictly rejected."""
        with self.assertRaises(ValueError) as ctx:
            validate_job_output_path(self.sandbox, "../outside.txt")
        self.assertIn("Path traversal detected", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_job_output_path(self.sandbox, "sub/../../outside.txt")
        self.assertIn("Path traversal detected", str(ctx.exception))

    def test_03_symlink_escaping_sandbox_rejected(self):
        """Symlinks pointing to targets outside the job sandbox are detected and blocked."""
        outside_file = Path(self.tmp_dir).parent / "sensitive_host_file.txt"
        outside_file.write_text("secret_data", encoding="utf-8")
        try:
            link_path = self.sandbox / "evil_symlink.txt"
            try:
                os.symlink(str(outside_file), str(link_path))
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not supported in current environment/permissions.")

            with self.assertRaises(ValueError) as ctx:
                validate_job_output_path(self.sandbox, "evil_symlink.txt")
            self.assertIn("Symlink escape detected", str(ctx.exception))
        finally:
            if outside_file.exists():
                outside_file.unlink(missing_ok=True)

    def test_04_directory_output_rejected(self):
        """Directories declared as artifact output files are rejected."""
        sub_dir = self.sandbox / "sub_directory"
        sub_dir.mkdir()
        with self.assertRaises(ValueError) as ctx:
            validate_job_output_path(self.sandbox, "sub_directory")
        self.assertIn("is a directory, not a file", str(ctx.exception))


class TestArtifactHttpRoutesSecurity(unittest.TestCase):
    """Test HTTP upload, download, access control, size limits, and idempotency."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ps_art_http_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "admin-secret-token-12345"
        (self.secrets_dir / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir
        )
        self.paths = self.paths_ctx.__enter__()

        # Register two test nodes in nodes_db
        nodes_db = {
            "nodes": {
                "node_vivo": {
                    "node_id": "server-vivo-111",
                    "name": "Vivo Y31",
                    "auth_token": "token-vivo-111",
                    "status": "online"
                },
                "node_realme": {
                    "node_id": "server-realme-222",
                    "name": "Realme 5 Pro",
                    "auth_token": "token-realme-222",
                    "status": "online"
                },
                "node_attacker": {
                    "node_id": "server-unauth-333",
                    "name": "Attacker Node",
                    "auth_token": "token-attacker-333",
                    "status": "online"
                }
            }
        }
        save_nodes_db(nodes_db)

        # Mock handler
        self.handler = MagicMock()
        self.handler.current_correlation_id = "corr-art-test"
        self.handler.send_json = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = io.BytesIO()
        self.handler.headers = {}
        self.handler.heartbeat_timeout = 60
        self.handler.is_authenticated_admin.return_value = False
        self.handler.get_authenticated_node_id.return_value = "server-vivo-111"
        self.handler.is_authenticated_admin_or_node.return_value = True

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_upload_and_download_with_matching_checksum(self):
        """Worker uploads binary artifact via raw stream; controller persists and serves matching SHA-256."""
        payload = b"Sample artifact content generated by worker node 1."
        sha = hashlib.sha256(payload).hexdigest()

        self.handler.rfile = io.BytesIO(payload)
        self.handler.headers = {
            "Content-Length": str(len(payload)),
            "X-Artifact-Filename": "output.dat",
            "X-Artifact-Sha256": sha,
            "X-Job-Id": "job-101",
            "X-Workflow-Id": "wf-999"
        }

        handled = handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        self.assertTrue(handled)
        self.handler.send_json.assert_called_once()
        status_code = self.handler.send_json.call_args[0][0]
        resp_data = self.handler.send_json.call_args[0][1]
        self.assertEqual(status_code, 201)
        self.assertEqual(resp_data["status"], "UPLOADED")
        art_id = resp_data["artifact"]["artifact_id"]

        # Now download the artifact as Realme (part of same workflow)
        self.handler.reset_mock()
        self.handler.wfile = io.BytesIO()
        self.handler.get_authenticated_node_id.return_value = "server-realme-222"
        self.handler.parse_auth_token.return_value = "token-realme-222"

        # Populate jobs_db with job in same workflow for realme
        from controller.stores import load_jobs_db, save_jobs_db
        jobs_db = load_jobs_db()
        jobs_db["jobs"] = {
            "job-101": {"job_id": "job-101", "workflow_id": "wf-999", "assigned_node": "server-vivo-111"},
            "job-102": {"job_id": "job-102", "workflow_id": "wf-999", "assigned_node": "server-realme-222"}
        }
        save_jobs_db(jobs_db)

        dl_handled = handle_job_get(self.handler, f"/artifacts/{art_id}/download", query={})
        self.assertTrue(dl_handled)
        self.handler.send_response.assert_called_with(200)
        self.assertEqual(self.handler.wfile.getvalue(), payload)

    def test_02_rejection_of_checksum_mismatch(self):
        """Controller rejects artifact upload if declared SHA-256 does not match bytes."""
        payload = b"Tampered binary content."
        wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        self.handler.rfile = io.BytesIO(payload)
        self.handler.headers = {
            "Content-Length": str(len(payload)),
            "X-Artifact-Filename": "tampered.dat",
            "X-Artifact-Sha256": wrong_sha
        }

        handled = handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        self.assertTrue(handled)
        status_code = self.handler.send_json.call_args[0][0]
        self.assertEqual(status_code, 400)
        self.assertIn("Checksum mismatch", self.handler.send_json.call_args[0][1]["error"])

    def test_03_rejection_of_oversized_artifact(self):
        """Uploads exceeding the 64MB limit are rejected with 413 Payload Too Large."""
        oversized = MAX_ARTIFACT_SIZE_BYTES + 1024
        self.handler.headers = {
            "Content-Length": str(oversized),
            "X-Artifact-Filename": "huge.dat"
        }
        handled = handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        self.assertTrue(handled)
        status_code = self.handler.send_json.call_args[0][0]
        self.assertEqual(status_code, 413)

    def test_04_cross_workflow_unauthorized_access_blocked(self):
        """A worker node belonging to a different workflow receives 403 Forbidden when requesting another's artifact."""
        payload = b"Confidential workflow artifact."
        sha = hashlib.sha256(payload).hexdigest()

        # Upload under wf-private
        self.handler.rfile = io.BytesIO(payload)
        self.handler.headers = {
            "Content-Length": str(len(payload)),
            "X-Artifact-Filename": "private.dat",
            "X-Artifact-Sha256": sha,
            "X-Job-Id": "job-priv",
            "X-Workflow-Id": "wf-private"
        }
        handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        art_id = self.handler.send_json.call_args[0][1]["artifact"]["artifact_id"]

        # Attacker node attempts to download
        from controller.stores import load_jobs_db, save_jobs_db
        jobs_db = load_jobs_db()
        jobs_db["jobs"] = {
            "job-priv": {"job_id": "job-priv", "workflow_id": "wf-private", "assigned_node": "server-vivo-111"},
            "job-attacker": {"job_id": "job-attacker", "workflow_id": "wf-other", "assigned_node": "server-unauth-333"}
        }
        save_jobs_db(jobs_db)

        self.handler.reset_mock()
        self.handler.get_authenticated_node_id.return_value = "server-unauth-333"
        self.handler.parse_auth_token.return_value = "token-attacker-333"

        dl_handled = handle_job_get(self.handler, f"/artifacts/{art_id}/download", query={})
        self.assertTrue(dl_handled)
        self.handler.send_json.assert_called_once()
        status_code = self.handler.send_json.call_args[0][0]
        self.assertEqual(status_code, 403)
        self.assertIn("not authorized to access artifacts for workflow", self.handler.send_json.call_args[0][1]["error"])

    def test_05_idempotent_duplicate_upload_on_retry(self):
        """Worker retry upload with identical checksum returns 200 EXISTS and avoids duplicate DB records."""
        payload = b"Retry idempotent payload."
        sha = hashlib.sha256(payload).hexdigest()

        self.handler.rfile = io.BytesIO(payload)
        self.handler.headers = {
            "Content-Length": str(len(payload)),
            "X-Artifact-Filename": "retry.dat",
            "X-Artifact-Sha256": sha,
            "X-Job-Id": "job-retry-1",
            "X-Workflow-Id": "wf-retry"
        }

        # First upload: 201 UPLOADED
        handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 201)
        first_art_id = self.handler.send_json.call_args[0][1]["artifact"]["artifact_id"]

        # Second upload (retry): 200 EXISTS with same artifact ID
        self.handler.reset_mock()
        self.handler.rfile = io.BytesIO(payload)
        self.handler.headers = {
            "Content-Length": str(len(payload)),
            "X-Artifact-Filename": "retry.dat",
            "X-Artifact-Sha256": sha,
            "X-Job-Id": "job-retry-1",
            "X-Workflow-Id": "wf-retry"
        }
        handle_job_post(self.handler, "/artifacts/upload/raw", None, token="token-vivo-111", query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 200)
        resp_retry = self.handler.send_json.call_args[0][1]
        self.assertEqual(resp_retry["status"], "EXISTS")
        self.assertEqual(resp_retry["artifact"]["artifact_id"], first_art_id)

        # Confirm artifacts database only contains 1 artifact
        artifacts_db = load_artifacts_db()
        matching = [a for a in artifacts_db.get("artifacts", {}).values() if a.get("job_id") == "job-retry-1"]
        self.assertEqual(len(matching), 1)


class TestWorkflowDAGArtifactProgression(unittest.TestCase):
    """Test DAG input/output dependency binding and progression."""

    def test_01_dependent_step_binds_upstream_artifact(self):
        """Step B with inputs successfully binds Step A's output artifact when Step A succeeds."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Pipeline with Artifacts",
            "steps": {
                "stepA": {
                    "type": "python-script",
                    "outputs": ["model.bin"]
                },
                "stepB": {
                    "type": "python-script",
                    "depends_on": ["stepA"],
                    "inputs": [{"step": "stepA", "name": "model.bin"}]
                }
            }
        }
        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)
        a_job = wf_rec["steps"]["stepA"]["job_id"]
        b_job = wf_rec["steps"]["stepB"]["job_id"]

        # Step B initially WAITING_FOR_DEPENDENCY
        self.assertEqual(jobs_db["jobs"][b_job]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # Step A succeeds and attaches its artifact
        jobs_db["jobs"][a_job]["status"] = JOB_STATE_SUCCEEDED
        jobs_db["jobs"][a_job]["artifacts"] = [{
            "artifact_id": "art-model-123",
            "name": "model.bin",
            "filename": "model.bin",
            "sha256": "abcdef123456",
            "size_bytes": 1024,
            "job_id": a_job
        }]

        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}
        changed = progress_workflows(workflows_db, jobs_db)

        self.assertTrue(changed)
        # Step B must now be QUEUED and have input_artifacts bound!
        self.assertEqual(jobs_db["jobs"][b_job]["status"], JOB_STATE_QUEUED)
        self.assertIn("input_artifacts", jobs_db["jobs"][b_job])
        input_art = jobs_db["jobs"][b_job]["input_artifacts"][0]
        self.assertEqual(input_art["artifact_id"], "art-model-123")
        self.assertEqual(input_art["name"], "model.bin")
        self.assertEqual(input_art["sha256"], "abcdef123456")

    def test_02_missing_upstream_artifact_fails_dependent_step(self):
        """If Step A succeeds but failed to produce the declared artifact, Step B fails deterministically."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Missing Artifact Pipeline",
            "steps": {
                "stepA": {"type": "python-script", "outputs": ["metrics.json"]},
                "stepB": {"type": "python-script", "depends_on": ["stepA"], "inputs": [{"step": "stepA", "name": "metrics.json"}]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        a_job = wf_rec["steps"]["stepA"]["job_id"]
        b_job = wf_rec["steps"]["stepB"]["job_id"]

        # Step A succeeds with empty artifacts list
        jobs_db["jobs"][a_job]["status"] = JOB_STATE_SUCCEEDED
        jobs_db["jobs"][a_job]["artifacts"] = []

        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}
        progress_workflows(workflows_db, jobs_db)

        # Step B must transition to FAILED with clear diagnostic reason
        self.assertEqual(jobs_db["jobs"][b_job]["status"], JOB_STATE_FAILED)
        self.assertIn("was not produced", jobs_db["jobs"][b_job]["retry_reason"])


class TestEndToEndCrossWorkerExecution(unittest.TestCase):
    """Test full execution of two simulated workers communicating through controller."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ps_e2e_art_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "admin-secret-e2e"
        (self.secrets_dir / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir
        )
        self.paths = self.paths_ctx.__enter__()

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_worker_a_produces_and_worker_b_consumes_artifact(self):
        """Worker 1 produces output.txt; Controller stores it; Worker 2 downloads into its sandbox and reads it."""
        # 1. Simulate Worker 1 executing a python script that writes output.txt
        worker1_sandbox = Path(self.tmp_dir) / "worker1_sandbox"
        worker1_sandbox.mkdir()

        job_a = {
            "job_id": "job-step-a",
            "type": "python-script",
            "parameters": {
                "code": "with open('message_from_node1.txt', 'w') as f: f.write('HELLO_FROM_NODE_1_SUCCESS')\nprint('STEP_A_DONE')",
                "outputs": ["message_from_node1.txt"]
            }
        }

        res_a = JobExecutor.execute(job_a, sandbox_dir=worker1_sandbox)
        self.assertEqual(res_a["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(len(res_a["declared_outputs"]), 1)
        output_info = res_a["declared_outputs"][0]
        self.assertEqual(output_info["name"], "message_from_node1.txt")
        self.assertTrue(Path(output_info["path"]).exists())

        # 2. Persist artifact to controller storage directly via store_artifact_bytes
        art_record = store_artifact_bytes(
            storage_root=self.paths.artifacts_dir,
            artifact_id=generate_artifact_id(),
            filename=output_info["filename"],
            content_bytes=Path(output_info["path"]).read_bytes(),
            expected_sha256=output_info["sha256"],
            job_id="job-step-a",
            workflow_id="wf-cross-node"
        )
        art_record["name"] = output_info["name"]

        # 3. Setup Worker 2 job with input_artifacts referencing Worker 1's artifact
        worker2_sandbox = Path(self.tmp_dir) / "worker2_sandbox"
        worker2_sandbox.mkdir()

        # Simulate Worker 2 downloading the artifact into its sandbox
        dest_in_worker2 = worker2_sandbox / "message_from_node1.txt"
        shutil.copy2(art_record["path"], str(dest_in_worker2))

        # Worker 2 executes script that reads Worker 1's artifact
        job_b = {
            "job_id": "job-step-b",
            "type": "python-script",
            "parameters": {
                "code": "with open('message_from_node1.txt') as f: msg = f.read().strip()\nassert msg == 'HELLO_FROM_NODE_1_SUCCESS'\nprint(f'CONSUMED: {msg}')"
            }
        }

        res_b = JobExecutor.execute(job_b, sandbox_dir=worker2_sandbox)
        self.assertEqual(res_b["status"], JOB_STATE_SUCCEEDED)
        self.assertIn("CONSUMED: HELLO_FROM_NODE_1_SUCCESS", res_b["stdout"])

    def test_02_download_input_artifacts_checksum_mismatch(self):
        """Worker fails and deletes temp file if downloaded content does not match expected SHA-256."""
        sandbox = Path(self.tmp_dir) / "sb_chk"
        sandbox.mkdir()

        corrupted_content = b"corrupted-data"
        class MockResp:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, n=65536):
                nonlocal corrupted_content
                data = corrupted_content
                corrupted_content = b""
                return data

        with patch("urllib.request.urlopen", return_value=MockResp()):
            input_artifacts = [{
                "artifact_id": "art-123",
                "name": "data.bin",
                "sha256": "0" * 64
            }]
            ok, err = _download_input_artifacts("http://127.0.0.1:8000", "dummy-token", input_artifacts, sandbox)
            self.assertFalse(ok)
            self.assertIn("Checksum mismatch", err)
            # Ensure no corrupted file left in sandbox
            self.assertFalse((sandbox / "data.bin").exists())
            self.assertEqual(len(list(sandbox.glob("*.tmp*"))), 0)

    def test_03_download_input_artifacts_path_traversal_blocked(self):
        """Worker rejects input artifact destination attempting directory traversal."""
        sandbox = Path(self.tmp_dir) / "sb_trav"
        sandbox.mkdir()

        input_artifacts = [{
            "artifact_id": "art-123",
            "name": "../escape.txt",
            "sha256": "abc"
        }]
        ok, err = _download_input_artifacts("http://127.0.0.1:8000", "dummy-token", input_artifacts, sandbox)
        self.assertFalse(ok)
        self.assertIn("Invalid destination path", err)

    def test_04_download_input_artifacts_oversized_blocked(self):
        """Worker terminates download and cleans up if stream exceeds MAX_ARTIFACT_SIZE_BYTES."""
        sandbox = Path(self.tmp_dir) / "sb_oversize"
        sandbox.mkdir()

        class GiantMockResp:
            def __init__(self):
                self.delivered = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, n=65536):
                # Return chunks until exceeding 64MB limit
                if self.delivered > MAX_ARTIFACT_SIZE_BYTES + 1024:
                    return b""
                chunk = b"X" * 65536
                self.delivered += len(chunk)
                return chunk

        with patch("urllib.request.urlopen", return_value=GiantMockResp()):
            input_artifacts = [{
                "artifact_id": "art-giant",
                "name": "giant.bin",
                "sha256": "abc"
            }]
            ok, err = _download_input_artifacts("http://127.0.0.1:8000", "dummy-token", input_artifacts, sandbox)
            self.assertFalse(ok)
            self.assertIn("exceeded maximum size limit", err)
            self.assertFalse((sandbox / "giant.bin").exists())
            self.assertEqual(len(list(sandbox.glob("*.tmp*"))), 0)


if __name__ == "__main__":
    unittest.main()

