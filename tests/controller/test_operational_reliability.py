#!/usr/bin/env python3
"""
PersonalServer V1.7 Operational Reliability Test Suite
======================================================
Validates controller-authoritative lease validity, worker clock skew protection,
interrupted raw artifact upload rejection, cleanup of unpromoted staging artifacts,
and idempotent safe upload retries.
"""

import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from common.artifact_manager import (
    ArtifactManager,
    cleanup_orphan_staging_artifacts,
    compute_file_sha256,
    store_artifact_stream,
    verify_artifact_integrity,
)
from common.time_utils import get_current_iso_timestamp
from config.platform_config import override_controller_paths
from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    STATE_ONLINE,
)
from controller.http.job_routes import handle_job_post
from controller.lease_sweeper import (
    reconcile_startup_recovery,
    sweep_expired_leases,
)
from controller.stores import (
    load_artifacts_db,
    load_jobs_db,
    load_nodes_db,
    save_artifacts_db,
    save_jobs_db,
    save_nodes_db,
)


class TestOperationalReliability(unittest.TestCase):
    """Integration test suite for V1.7 operational reliability."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_ctrl_reliability_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "enrollment-token-reliability-test"
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

        # Register a test node
        self.node_token = "tok-worker-1"
        now_iso = get_current_iso_timestamp()
        nodes_db = {
            "nodes": {
                "node-1": {
                    "node_id": "node-1",
                    "name": "worker-alpha",
                    "auth_token": self.node_token,
                    "status": "ONLINE",
                    "last_seen": now_iso,
                    "last_heartbeat": now_iso,
                    "capabilities": ["python3", "cpu:4"],
                    "resources": {"cpu_percent": 10.0, "ram_free_mb": 2048}
                }
            }
        }
        save_nodes_db(nodes_db, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)
        save_artifacts_db({"artifacts": {}}, paths=self.paths)

    def tearDown(self):
        self.paths_ctx.__exit__(None, None, None)

    def _make_mock_handler(self, headers=None, rfile=None):
        handler = MagicMock()
        handler.headers = headers or {}
        handler.rfile = rfile or io.BytesIO(b"")
        handler.heartbeat_timeout = 30
        handler.default_backoff_sec = 0.5
        handler.is_authenticated_admin.return_value = False
        handler.get_authenticated_node_id.return_value = "node-1"
        handler.is_authenticated_admin_or_node.return_value = True

        responses = []

        def send_json(code, data):
            responses.append((code, data))

        handler.send_json = send_json
        handler.responses = responses
        return handler

    def test_authoritative_lease_expiration_on_result_submission(self):
        """
        Verify that if the controller's clock indicates a lease has expired,
        a worker's result submission is rejected with 409 Conflict (lease_expired: True)
        even if the background sweeper thread has not ticked yet.
        """
        jobs_db = load_jobs_db(paths=self.paths)
        now_epoch = time.time()
        job_id = "job-lease-expire-test"
        jobs_db["jobs"][job_id] = {
            "job_id": job_id,
            "status": JOB_STATE_RUNNING,
            "state": JOB_STATE_RUNNING,
            "assigned_node": "node-1",
            "target_node": "node-1",
            "attempt": 1,
            "max_attempts": 3,
            "lease_generation": 1,
            "lease_expires_at": now_epoch - 10.0,  # Expired 10s ago on controller's clock
            "claimed_at": now_epoch - 40.0,
            "retryable": True,
            "target": "auto"
        }
        save_jobs_db(jobs_db, paths=self.paths)

        handler = self._make_mock_handler()
        body = {
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0,
            "stdout": "late output",
            "attempt": 1,
            "lease_generation": 1
        }

        handled = handle_job_post(
            handler=handler,
            path=f"/jobs/{job_id}/result",
            body=body,
            token=self.node_token,
            query={}
        )
        self.assertTrue(handled)
        self.assertEqual(len(handler.responses), 1)
        code, resp = handler.responses[0]
        self.assertEqual(code, 409)
        self.assertTrue(resp.get("lease_expired"))
        self.assertIn("expired", resp.get("error", "").lower())

        # Verify job immediately transitioned into recovery retry state with incremented attempt
        reloaded_db = load_jobs_db(paths=self.paths)
        updated_job = reloaded_db["jobs"][job_id]
        self.assertIn(updated_job["status"], (JOB_STATE_RETRY_WAIT, JOB_STATE_RECOVERING))
        self.assertEqual(updated_job["attempt"], 2)
        self.assertGreater(updated_job["lease_generation"], 1)

    def test_authoritative_lease_expiration_on_renew_and_status(self):
        """
        Verify that expired leases cannot be renewed or updated to RUNNING by a worker
        whose lease has lapsed on the controller's clock.
        """
        jobs_db = load_jobs_db(paths=self.paths)
        now_epoch = time.time()
        job_id = "job-renew-expire-test"
        jobs_db["jobs"][job_id] = {
            "job_id": job_id,
            "status": JOB_STATE_RUNNING,
            "state": JOB_STATE_RUNNING,
            "assigned_node": "node-1",
            "target_node": "node-1",
            "attempt": 1,
            "max_attempts": 3,
            "lease_generation": 1,
            "lease_expires_at": now_epoch - 5.0,  # Expired
            "retryable": True,
            "target": "auto"
        }
        save_jobs_db(jobs_db, paths=self.paths)

        # 1. Attempt to renew expired lease
        handler1 = self._make_mock_handler()
        handled = handle_job_post(
            handler=handler1,
            path=f"/jobs/{job_id}/lease/renew",
            body={"extension_sec": 60, "lease_generation": 1},
            token=self.node_token,
            query={}
        )
        self.assertTrue(handled)
        code, resp = handler1.responses[0]
        self.assertEqual(code, 409)
        self.assertTrue(resp.get("lease_expired"))

        # 2. Attempt to update status on expired lease
        job_id_2 = "job-status-expire-test"
        jobs_db["jobs"][job_id_2] = {
            "job_id": job_id_2,
            "status": JOB_STATE_ASSIGNED,
            "state": JOB_STATE_ASSIGNED,
            "assigned_node": "node-1",
            "target_node": "node-1",
            "attempt": 1,
            "max_attempts": 3,
            "lease_generation": 1,
            "lease_expires_at": now_epoch - 5.0,
            "retryable": True,
            "target": "auto"
        }
        save_jobs_db(jobs_db, paths=self.paths)

        handler2 = self._make_mock_handler()
        handled = handle_job_post(
            handler=handler2,
            path=f"/jobs/{job_id_2}/status",
            body={"status": "RUNNING", "lease_generation": 1},
            token=self.node_token,
            query={}
        )
        self.assertTrue(handled)
        code, resp = handler2.responses[0]
        self.assertEqual(code, 409)
        self.assertTrue(resp.get("lease_expired"))

    def test_interrupted_raw_artifact_upload_rejected_and_cleaned(self):
        """
        Verify that an interrupted raw artifact upload (premature EOF before total_bytes_expected)
        is rejected with 400 Bad Request, leaves no complete file or metadata on disk,
        cleans up staging temp files, and is not recorded in artifacts.json.
        """
        # Truncated payload: Content-Length promises 100 bytes, but stream sends only 35 bytes
        full_content = b"A" * 100
        truncated_content = full_content[:35]
        stream = io.BytesIO(truncated_content)

        headers = {
            "Content-Length": "100",
            "Content-Type": "text/plain",
            "X-Artifact-Filename": "partial_test.txt"
        }
        handler = self._make_mock_handler(headers=headers, rfile=stream)

        handled = handle_job_post(
            handler=handler,
            path="/artifacts/upload/raw",
            body=None,
            token=self.node_token,
            query={"filename": ["partial_test.txt"]}
        )
        self.assertTrue(handled)
        self.assertEqual(len(handler.responses), 1)
        code, resp = handler.responses[0]
        self.assertEqual(code, 400)
        self.assertIn("incomplete artifact stream", resp.get("error", "").lower())

        # Verify not recorded in database
        artifacts_db = load_artifacts_db(paths=self.paths)
        self.assertEqual(len(artifacts_db.get("artifacts", {})), 0)

        # Verify no orphan .tmp files or unpromoted target files exist in artifacts directory
        artifacts_dir = self.paths.artifacts_dir
        if artifacts_dir.exists():
            remaining_tmp = list(artifacts_dir.rglob("*.tmp"))
            self.assertEqual(len(remaining_tmp), 0)
            target_files = list(artifacts_dir.rglob("partial_test.txt"))
            self.assertEqual(len(target_files), 0)

    def test_safe_retry_after_interrupted_upload(self):
        """
        Verify that after an interrupted upload fails, retrying the upload with the complete
        data payload and expected checksum succeeds cleanly, writes metadata, and registers
        the artifact. Subsequent retries with identical checksum return 200 EXISTS idempotently.
        """
        full_content = b"Complete and verified artifact payload for V1.7 reliability testing."
        full_sha256 = hashlib.sha256(full_content).hexdigest()
        full_length = len(full_content)

        # Step 1: Failed interrupted upload
        interrupted_stream = io.BytesIO(full_content[:20])
        handler1 = self._make_mock_handler(
            headers={"Content-Length": str(full_length), "X-Artifact-Filename": "reliable_data.bin"},
            rfile=interrupted_stream
        )
        handle_job_post(
            handler=handler1,
            path="/artifacts/upload/raw",
            body=None,
            token=self.node_token,
            query={"filename": ["reliable_data.bin"], "sha256": [full_sha256]}
        )
        self.assertEqual(handler1.responses[0][0], 400)

        # Step 2: Retry with full payload and matching checksum
        retry_stream = io.BytesIO(full_content)
        handler2 = self._make_mock_handler(
            headers={"Content-Length": str(full_length), "X-Artifact-Filename": "reliable_data.bin"},
            rfile=retry_stream
        )
        handle_job_post(
            handler=handler2,
            path="/artifacts/upload/raw",
            body=None,
            token=self.node_token,
            query={"filename": ["reliable_data.bin"], "sha256": [full_sha256]}
        )
        self.assertEqual(handler2.responses[0][0], 201)
        art_record = handler2.responses[0][1].get("artifact")
        self.assertIsNotNone(art_record)
        self.assertEqual(art_record["sha256"], full_sha256)
        self.assertEqual(art_record["size_bytes"], full_length)

        # Verify disk integrity
        self.assertTrue(verify_artifact_integrity(art_record))

        # Step 3: Duplicate retry returns 200 EXISTS idempotently
        idempotent_stream = io.BytesIO(full_content)
        handler3 = self._make_mock_handler(
            headers={"Content-Length": str(full_length), "X-Artifact-Filename": "reliable_data.bin"},
            rfile=idempotent_stream
        )
        handle_job_post(
            handler=handler3,
            path="/artifacts/upload/raw",
            body=None,
            token=self.node_token,
            query={"filename": ["reliable_data.bin"], "sha256": [full_sha256]}
        )
        self.assertEqual(handler3.responses[0][0], 200)
        self.assertEqual(handler3.responses[0][1].get("status"), "EXISTS")

    def test_startup_recovery_cleans_crashed_orphan_staging_artifacts(self):
        """
        Verify that startup recovery sweeps orphan .tmp files and incomplete artifact directories
        left over from unexpected controller crashes during streaming uploads.
        """
        artifacts_dir = self.paths.artifacts_dir
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 1. Create a simulated crashed streaming upload folder with a leftover .tmp file
        orphan_dir = artifacts_dir / "art-crashed-upload"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan_tmp = orphan_dir / "partial.1234abcd.tmp"
        orphan_tmp.write_bytes(b"crashed temp data")

        # 2. Create a valid artifact alongside it
        valid_dir = artifacts_dir / "art-valid-artifact"
        valid_dir.mkdir(parents=True, exist_ok=True)
        valid_file = valid_dir / "valid.txt"
        valid_file.write_text("valid persisted content", encoding="utf-8")
        valid_sha, valid_size = compute_file_sha256(valid_file)
        valid_meta = {
            "artifact_id": "art-valid-artifact",
            "name": "valid.txt",
            "filename": "valid.txt",
            "path": str(valid_file),
            "size_bytes": valid_size,
            "sha256": valid_sha
        }
        (valid_dir / "metadata.json").write_text(json.dumps(valid_meta), encoding="utf-8")

        # Run startup reconciliation
        summary = reconcile_startup_recovery(paths=self.paths)

        # Verify orphan staging artifacts were cleaned
        self.assertGreaterEqual(summary.get("staging_artifacts_cleaned", 0), 1)
        self.assertFalse(orphan_tmp.exists())
        self.assertFalse(orphan_dir.exists())

        # Verify valid artifact is completely untouched and intact
        self.assertTrue(valid_file.exists())
        self.assertTrue(verify_artifact_integrity(valid_meta))


if __name__ == "__main__":
    unittest.main()
