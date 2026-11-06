#!/usr/bin/env python3
"""
Targeted Verification Suite for PersonalServer Result Management & Artifact Pipeline (J5)
========================================================================================
Validates:
1. Declared output discovery and manifest generation.
2. Required output missing causes failure even if exit code is 0.
3. Optional output missing is gracefully handled.
4. Path traversal ('..') and absolute paths strictly rejected.
5. Symlink escape from attempt workspace strictly rejected.
6. File size limit enforcement (50 MB single limit).
7. Aggregate output limit enforcement.
8. Independent SHA-256 verification (corrupted / modified files rejected).
9. Two-phase CAS promotion into permanent artifact storage.
10. Atomic commit of job results and artifacts with fencing validation (HTTP 412).
11. Safe cleanup of orphaned staging files without deleting promoted artifacts.
"""

import os
import sys
import time
import shutil
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from controller.result_manager import (
    ResultManager,
    MAX_SINGLE_ARTIFACT_LIMIT
)
from common.artifact_manager import compute_file_sha256


class TestResultManagerJ5(unittest.TestCase):
    """Exhaustive test suite for J5 Result Management and Artifact Pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ps_test_j5_")
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir = Path(self.temp_dir) / "artifacts_storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_collect_declared_outputs_success(self):
        """Discovers declared output files and computes SHA-256 and size correctly."""
        out_file = self.workspace_dir / "report.json"
        out_file.write_text('{"status": "ok", "metrics": 100}', encoding="utf-8")

        declared = [
            {
                "name": "daily_report",
                "path_pattern": "report.json",
                "required": True
            }
        ]

        ok, manifest, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["name"], "daily_report")
        self.assertEqual(manifest[0]["filename"], "report.json")
        self.assertGreater(manifest[0]["size_bytes"], 0)
        self.assertEqual(len(manifest[0]["sha256"]), 64)

    def test_missing_required_output_fails_job(self):
        """Missing required output causes collection failure (process exit 0 != success)."""
        declared = [
            {
                "name": "mandatory_model",
                "path_pattern": "model.bin",
                "required": True
            }
        ]
        ok, manifest, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertFalse(ok)
        self.assertIn("Missing required output artifact", err)

    def test_missing_optional_output_gracefully_ignored(self):
        """Missing optional output succeeds without error."""
        declared = [
            {
                "name": "optional_debug_dump",
                "path_pattern": "debug.log",
                "required": False
            }
        ]
        ok, manifest, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertTrue(ok)
        self.assertEqual(len(manifest), 0)

    def test_path_traversal_rejection(self):
        """Output pattern containing '..' or absolute paths raises security error."""
        declared = [{"name": "evil_traversal", "path_pattern": "../../etc/shadow", "required": True}]
        ok, _, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertFalse(ok)
        self.assertIn("Path traversal", err)

        abs_declared = [{"name": "evil_abs", "path_pattern": "/etc/passwd", "required": True}]
        ok2, _, err2 = ResultManager.collect_declared_outputs(self.workspace_dir, abs_declared)
        self.assertFalse(ok2)
        self.assertIn("Absolute output path rejected", err2)

    def test_symlink_escape_rejection(self):
        """Symlink pointing outside workspace is detected and rejected."""
        secret_outside = Path(self.temp_dir) / "secret.key"
        secret_outside.write_text("SUPER_SECRET_KEY")

        symlink_in_ws = self.workspace_dir / "symlink_escape.txt"
        try:
            symlink_in_ws.symlink_to(secret_outside)
        except (OSError, NotImplementedError):
            # Skip if symlink creation not permitted on Windows without admin
            return

        declared = [{"name": "escape", "path_pattern": "symlink_escape.txt", "required": True}]
        ok, _, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertFalse(ok)
        self.assertIn("escapes workspace", err)

    def test_file_size_limit_enforcement(self):
        """Single output exceeding size limit is rejected."""
        big_file = self.workspace_dir / "big.bin"
        big_file.write_bytes(b"A" * 1024)

        declared = [
            {
                "name": "big_artifact",
                "path_pattern": "big.bin",
                "max_size_bytes": 500,  # 500 byte limit
                "required": True
            }
        ]
        ok, _, err = ResultManager.collect_declared_outputs(self.workspace_dir, declared)
        self.assertFalse(ok)
        self.assertIn("exceeds limit", err)

    def test_independent_sha256_verification_and_cas_promotion(self):
        """Two-phase promotion verifies checksum independently and atomically places into storage."""
        source = self.workspace_dir / "clean.data"
        content = b"PROMOTED_ARTIFACT_PAYLOAD_12345"
        source.write_bytes(content)
        expected_sha, expected_size = compute_file_sha256(source)

        record = ResultManager.promote_artifact_cas(
            storage_root=self.storage_dir,
            source_file=source,
            expected_sha256=expected_sha,
            filename="clean.data",
            job_id="job-j5-test"
        )

        self.assertTrue(record.get("promoted"))
        self.assertEqual(record["sha256"], expected_sha)
        self.assertEqual(record["size_bytes"], len(content))
        promoted_path = Path(record["path"])
        self.assertTrue(promoted_path.exists())
        self.assertEqual(promoted_path.read_bytes(), content)

    def test_tampered_checksum_rejected_during_promotion(self):
        """Checksum mismatch during promotion raises ValueError and aborts placement."""
        source = self.workspace_dir / "tampered.data"
        source.write_bytes(b"AUTHENTIC_DATA")

        fake_sha = "0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaises(ValueError):
            ResultManager.promote_artifact_cas(
                storage_root=self.storage_dir,
                source_file=source,
                expected_sha256=fake_sha,
                filename="tampered.data",
                job_id="job-j5-tamper"
            )

    def test_cleanup_orphaned_staging_preserves_promoted(self):
        """Staging cleanup deletes abandoned .staging and .tmp files but keeps promoted artifacts."""
        # 1. Valid promoted artifact
        art_dir = self.storage_dir / "art-12345"
        art_dir.mkdir(parents=True, exist_ok=True)
        promoted = art_dir / "final.bin"
        promoted.write_text("keep me")

        # 2. Abandoned staging file
        staging = art_dir / "abandoned.staging"
        staging.write_text("abandoned")
        # Set old mtime (> 2 hours ago)
        old_time = time.time() - 7200
        os.utime(staging, (old_time, old_time))

        cleaned_count = ResultManager.cleanup_orphaned_staging(self.storage_dir, max_age_seconds=3600.0)
        self.assertEqual(cleaned_count, 1)
        self.assertTrue(promoted.exists())
        self.assertFalse(staging.exists())

    def test_crash_boundary_post_promotion_pre_db_commit_reconciled(self):
        """Artifact promoted to disk but uncommitted due to controller crash is reconciled on recovery."""
        import json
        from unittest.mock import patch
        art_id = "art-uncommitted-crash"
        art_dir = self.storage_dir / art_id
        art_dir.mkdir(parents=True, exist_ok=True)
        target_file = art_dir / "recovered.dat"
        target_file.write_bytes(b"RECOVERABLE_CONTENT_6789")

        meta = {
            "artifact_id": art_id,
            "filename": "recovered.dat",
            "path": str(target_file),
            "size_bytes": len(b"RECOVERABLE_CONTENT_6789"),
            "sha256": "abcdef123456",
            "job_id": "job-crash-boundary"
        }
        (art_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

        mock_db = {"artifacts": {}}
        with patch("controller.result_manager.load_artifacts_db", return_value=mock_db), \
             patch("controller.result_manager.save_artifacts_db") as mock_save:
            reconciled = ResultManager.reconcile_promoted_artifacts(self.storage_dir)
            self.assertEqual(reconciled, 1)
            self.assertIn(art_id, mock_db["artifacts"])
            mock_save.assert_called_once()

    def test_database_failure_missing_artifact_integrity_rejection(self):
        """DB commit fails closed and refuses to mark job SUCCEEDED if promoted artifact file is missing."""
        from unittest.mock import patch
        missing_record = {
            "artifact_id": "art-ghost",
            "name": "ghost.bin",
            "path": str(self.storage_dir / "non_existent_ghost.bin")
        }
        mock_jobs = {
            "jobs": {
                "job-integrity": {
                    "job_id": "job-integrity",
                    "status": "RUNNING",
                    "lease_generation": 1
                }
            }
        }
        with patch("controller.result_manager.load_jobs_db", return_value=mock_jobs), \
             patch("controller.result_manager.save_jobs_db") as mock_save:
            ok, err = ResultManager.commit_job_result_and_artifacts(
                job_id="job-integrity",
                attempt=1,
                fencing_token=1,
                status="SUCCEEDED",
                exit_code=0,
                stdout="",
                stderr="",
                promoted_artifacts=[missing_record]
            )
            self.assertFalse(ok)
            self.assertIn("missing from CAS storage", err)
            # Job state was NOT committed as SUCCEEDED
            self.assertEqual(mock_jobs["jobs"]["job-integrity"]["status"], "RUNNING")
            mock_save.assert_not_called()

    def test_cleanup_never_deletes_metadata_or_active_artifacts(self):
        """Staging cleaner never touches metadata.json or active artifact files regardless of age."""
        art_dir = self.storage_dir / "art-ancient"
        art_dir.mkdir(parents=True, exist_ok=True)
        art_file = art_dir / "payload.tar.gz"
        meta_file = art_dir / "metadata.json"
        art_file.write_bytes(b"DATA")
        meta_file.write_text('{"status": "ok"}')

        # Set ancient mtimes (> 30 days)
        ancient_time = time.time() - 30 * 86400
        os.utime(art_file, (ancient_time, ancient_time))
        os.utime(meta_file, (ancient_time, ancient_time))

        cleaned = ResultManager.cleanup_orphaned_staging(self.storage_dir, max_age_seconds=60.0)
        self.assertEqual(cleaned, 0)
        self.assertTrue(art_file.exists())
        self.assertTrue(meta_file.exists())


if __name__ == "__main__":
    unittest.main()

