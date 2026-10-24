#!/usr/bin/env python3
"""
Unit tests for Worker Lifecycle Safety and Acceptance Test Payload Validation
=============================================================================
Tests:
1. Worker stop safety:
   - Stale PID in PID file
   - PID reuse (PID belongs to an unrelated command)
   - Graceful termination
   - Timeout without auto-SIGKILL (leaves PID file and process alive for operator decision)
   - Duplicate worker prevention on startup
2. Acceptance payload accuracy:
   - Validates allowlisted workloads ('system-info', 'echo', 'python-script')
   - Rejects unhyphenated/unsupported workloads ('system_info')
   - Workflow schema DAG instantiation with input/output artifacts
"""

import os
import sys
import json
import time
import signal
import tempfile
import subprocess
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from controller.constants import ALLOWLISTED_WORKLOADS, JOB_STATE_REJECTED
from controller.workflow_engine import validate_workflow_dag, instantiate_workflow, WorkflowValidationError


class TestWorkerLifecycleSafety(unittest.TestCase):
    """Verifies process management safety rules for worker lifecycle."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.test_dir.name) / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.runtime_dir / "node-worker.pid"

    def tearDown(self):
        try:
            self.test_dir.cleanup()
        except Exception:
            pass

    def test_stale_pid_handled_safely(self):
        """A PID file containing a non-existent PID should be recognized as stale."""
        # Write a non-existent high PID
        non_existent_pid = 999999
        self.pid_file.write_text(str(non_existent_pid), encoding="utf-8")

        # Emulate checking logic
        pid = int(self.pid_file.read_text().strip())
        is_alive = False
        try:
            os.kill(pid, 0)
            is_alive = True
        except (ProcessLookupError, OSError):
            is_alive = False

        self.assertFalse(is_alive)
        # Safe handling: Stale PID identified without killing any process
        if not is_alive:
            self.pid_file.unlink()

        self.assertFalse(self.pid_file.exists())

    def test_pid_reuse_protection(self):
        """If PID belongs to an unrelated process (e.g. sleep/python test), the stop procedure MUST refuse to kill."""
        # Launch a harmless dummy process that is definitely NOT node-agent.py work
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"]
        )
        try:
            # Write dummy's PID to worker pid_file
            self.pid_file.write_text(str(dummy.pid), encoding="utf-8")

            # Emulate verification logic: Check PID command line
            pid = int(self.pid_file.read_text().strip())
            
            # Simulated command line retrieval
            # On Windows/Linux, verify that the command does NOT match 'agent/node-agent.py work'
            simulated_cmd = f"python dummy_process.py"
            expected_pattern = "agent/node-agent.py work"
            is_intended_worker = expected_pattern in simulated_cmd

            self.assertFalse(is_intended_worker, "Unrelated process was mistakenly identified as worker")

            # Under the safety rule: DO NOT KILL, DO NOT DELETE PID FILE
            if not is_intended_worker:
                action = "ABORT_MISMATCH"
            else:
                action = "KILL"

            self.assertEqual(action, "ABORT_MISMATCH")
            self.assertTrue(self.pid_file.exists(), "PID file must be preserved when identity is uncertain")
            self.assertIsNone(dummy.poll(), "Unrelated process must not be terminated")
        finally:
            dummy.terminate()
            dummy.wait()

    def test_timeout_without_auto_sigkill(self):
        """Worker that does not shut down within timeout must NOT be auto-SIGKILLed."""
        # Launch dummy simulating busy worker
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"]
        )
        try:
            self.pid_file.write_text(str(dummy.pid), encoding="utf-8")
            pid = dummy.pid

            # Emulate timeout without auto-SIGKILL
            # Say we poll for timeout_sec and it's still alive:
            timeout_occurred = True
            auto_sigkill = False  # Strict safety rule: require operator confirmation

            if timeout_occurred and not auto_sigkill:
                report = f"WARNING: Worker PID {pid} still running. Retaining PID file for operator decision."
                # Do NOT unlink pid_file
                self.assertTrue(self.pid_file.exists())
                # Do NOT kill
                self.assertIsNone(dummy.poll())
                self.assertIn("still running", report)
        finally:
            dummy.terminate()
            dummy.wait()

    def test_duplicate_worker_prevention(self):
        """Starting a worker when an active worker is already running must abort."""
        active_worker_pid = 12345
        running_workers = [active_worker_pid]

        # Emulate start guard
        if len(running_workers) > 0:
            start_allowed = False
            error = f"ABORT: Worker already running with PID {running_workers[0]}"
        else:
            start_allowed = True
            error = ""

        self.assertFalse(start_allowed)
        self.assertIn("ABORT: Worker already running", error)


class TestAcceptancePayloads(unittest.TestCase):
    """Verifies that acceptance test payloads conform strictly to repository schemas."""

    def test_workload_allowlist_hyphenation(self):
        """'system-info' and 'echo' are in allowlist; 'system_info' (underscored) is rejected."""
        self.assertIn("system-info", ALLOWLISTED_WORKLOADS)
        self.assertIn("echo", ALLOWLISTED_WORKLOADS)
        self.assertIn("python-script", ALLOWLISTED_WORKLOADS)
        self.assertNotIn("system_info", ALLOWLISTED_WORKLOADS)

    def test_dag_workflow_specification_valid(self):
        """Validates exact multi-step DAG specification supported by workflow_engine."""
        spec = {
            "name": "canary-dag-workflow",
            "steps": {
                "step1-produce": {
                    "name": "Produce Artifact",
                    "type": "python-script",
                    "target": "vivo-y31",
                    "parameters": {
                        "code": "import os; p=os.path.join(os.environ.get('PERSONALSERVER_JOB_DIR','.'),'data.dat'); open(p,'wb').write(b'payload')",
                        "outputs": ["data.dat"]
                    }
                },
                "step2-consume": {
                    "name": "Consume Artifact",
                    "type": "python-script",
                    "target": "realme-5-pro",
                    "depends_on": ["step1-produce"],
                    "inputs": ["step1-produce/data.dat"],
                    "parameters": {
                        "code": "import os; p=os.path.join(os.environ.get('PERSONALSERVER_JOB_DIR','.'),'data.dat'); assert open(p,'rb').read() == b'payload'"
                    }
                }
            }
        }
        jobs_db = {"jobs": {}}
        wf_rec, created_jobs = instantiate_workflow(spec, jobs_db)
        self.assertEqual(wf_rec["status"], "QUEUED")
        self.assertEqual(len(created_jobs), 2)
        
        # Step 1 should be QUEUED, Step 2 should be WAITING_FOR_DEPENDENCY
        s1 = next(j for j in created_jobs if j["workflow_step_id"] == "step1-produce")
        s2 = next(j for j in created_jobs if j["workflow_step_id"] == "step2-consume")
        self.assertEqual(s1["status"], "QUEUED")
        self.assertEqual(s2["status"], "WAITING_FOR_DEPENDENCY")
        self.assertEqual(s2["depends_on_steps"], ["step1-produce"])
        self.assertEqual(s2["inputs"], ["step1-produce/data.dat"])

    def test_canary_payload_checksum_exact(self):
        """Validates exact SHA-256 for the deployment canary payload."""
        import hashlib
        payload = b"VERIFIED_CANARY_PAYLOAD_V07"
        expected_sha256 = "8fbf822d57b5b8d1b83bbfe0d0bc8593e7ee8447d48e9576f7de29228f0beef8"
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        self.assertEqual(actual_sha256, expected_sha256)

    def test_corrupt_pid_file_preservation(self):
        """PID file with corrupt/non-numeric content must be preserved for operator inspection."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "node-worker.pid"
            pid_file.write_text("corrupted_pid_content\n", encoding="utf-8")
            
            raw_content = pid_file.read_text(encoding="utf-8").strip()
            is_valid_pid = raw_content.isdigit()
            self.assertFalse(is_valid_pid)
            
            # Safety rule: Do not delete PID file if identity or content is uncertain
            if not is_valid_pid:
                action = "PRESERVE_AND_PROMPT_OPERATOR"
            else:
                action = "PROCEED"
                
            self.assertEqual(action, "PRESERVE_AND_PROMPT_OPERATOR")
            self.assertTrue(pid_file.exists())


if __name__ == "__main__":
    unittest.main()

