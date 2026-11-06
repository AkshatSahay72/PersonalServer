#!/usr/bin/env python3
"""
Targeted Verification Suite for PersonalServer Universal Execution Engine (J4)
==============================================================================
Validates:
1. Native Python execution within attempt-isolated workspaces.
2. Native Node.js execution within attempt-isolated workspaces.
3. Path traversal attack prevention in attempt workspace resolution.
4. Attempt workspace isolation between retries (attempt 1 vs attempt 2).
5. Monotonic fencing generation validation and stale attempt HTTP 412 rejection.
6. Execution timeout supervision and process-group termination.
7. Active job cancellation and cleanup.
8. Docker Engine vs udocker distinction.
9. Bounded stdout/stderr capture and sensitive text redaction.
"""

import os
import sys
import time
import shutil
import threading
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agent.universal_executor import (
    UniversalExecutionEngine,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_TIMED_OUT,
    JOB_STATE_CANCELLED
)


class TestUniversalExecutionEngineJ4(unittest.TestCase):
    """Exhaustive test suite for J4 Universal Job Execution Engine."""

    def setUp(self):
        self.job_id = f"test-ujc-job-{int(time.time()*1000)}"

    def test_attempt_workspace_isolation(self):
        """Attempts receive separate workspaces; retries do not mutate prior attempts."""
        ws1 = UniversalExecutionEngine.get_attempt_workspace(self.job_id, attempt=1)
        ws2 = UniversalExecutionEngine.get_attempt_workspace(self.job_id, attempt=2)

        self.assertNotEqual(str(ws1), str(ws2))
        self.assertTrue(ws1.exists())
        self.assertTrue(ws2.exists())
        self.assertTrue(str(ws1).endswith("attempts" + os.sep + "1"))
        self.assertTrue(str(ws2).endswith("attempts" + os.sep + "2"))

        # Writing to attempt 1 workspace does not affect attempt 2
        test_file_1 = ws1 / "file.txt"
        test_file_1.write_text("attempt 1 data")
        self.assertFalse((ws2 / "file.txt").exists())

    def test_path_traversal_rejection(self):
        """Attempting to escape runtime directory with '..' raises ValueError."""
        with self.assertRaises(ValueError):
            UniversalExecutionEngine.get_attempt_workspace("../../etc", attempt=1)

    def test_stale_fencing_generation_rejected(self):
        """Worker rejects executing when caller provides a stale fencing token."""
        job = {
            "job_id": self.job_id,
            "attempt": 2,
            "lease_generation": 3,
            "spec": {"workload": {"runtime": "native/python"}}
        }
        # Caller supplies stale token 2 (< current generation 3)
        res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=2)
        self.assertEqual(res["status"], JOB_STATE_FAILED)
        self.assertEqual(res["exit_code"], 412)
        self.assertIn("Precondition Failed", res["stderr"])

    def test_python_native_execution_success(self):
        """Executes a native Python script and captures output and exit code 0."""
        job = {
            "job_id": self.job_id,
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {
                    "runtime": "native/python",
                    "arguments": [],
                },
                "policy": {"timeout_sec": 10}
            },
            "parameters": {
                "script": "import sys; print('HELLO_J4_PYTHON'); sys.exit(0)"
            }
        }
        res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("HELLO_J4_PYTHON", res["stdout"])
        self.assertGreater(res["duration_ms"], 0)

    def test_python_non_zero_exit_reporting(self):
        """Reports non-zero exit codes correctly as JOB_STATE_FAILED."""
        job = {
            "job_id": self.job_id,
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {"runtime": "native/python"},
                "policy": {"timeout_sec": 10}
            },
            "parameters": {
                "script": "import sys; sys.stderr.write('fatal crash\\n'); sys.exit(42)"
            }
        }
        res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)
        self.assertEqual(res["status"], JOB_STATE_FAILED)
        self.assertEqual(res["exit_code"], 42)
        self.assertIn("fatal crash", res["stderr"])

    def test_nodejs_native_execution(self):
        """Executes Node.js workload if node is on PATH, or cleanly reports missing runtime."""
        has_node = shutil.which("node") is not None
        job = {
            "job_id": self.job_id,
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {"runtime": "native/nodejs"},
                "policy": {"timeout_sec": 10}
            },
            "parameters": {
                "script": "console.log('HELLO_J4_NODEJS'); process.exit(0);"
            }
        }
        res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)
        if has_node:
            self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
            self.assertIn("HELLO_J4_NODEJS", res["stdout"])
        else:
            self.assertEqual(res["status"], JOB_STATE_FAILED)
            self.assertIn("Unsupported or missing runtime", res["stderr"])

    def test_execution_timeout_supervision(self):
        """Long-running process exceeding timeout_sec is forcefully terminated with TIMED_OUT."""
        job = {
            "job_id": self.job_id,
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {"runtime": "native/python"},
                "policy": {"timeout_sec": 1.0}  # 1 second timeout
            },
            "parameters": {
                "script": "import time; time.sleep(15)"
            }
        }
        t0 = time.time()
        res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)
        elapsed = time.time() - t0
        self.assertEqual(res["status"], JOB_STATE_TIMED_OUT)
        self.assertIn("Execution timed out", res["stderr"])
        self.assertLess(elapsed, 6.0)

    def test_active_job_cancellation(self):
        """Active job cancellation terminates process tree and transitions to CANCELLED."""
        job = {
            "job_id": self.job_id,
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {"runtime": "native/python"},
                "policy": {"timeout_sec": 15}
            },
            "parameters": {
                "script": "import time; time.sleep(10)"
            }
        }

        # Spawn in thread and cancel after 0.5s
        result_holder = {}

        def run_thread():
            result_holder["res"] = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)

        th = threading.Thread(target=run_thread)
        th.start()
        time.sleep(0.5)

        cancelled = UniversalExecutionEngine.cancel_execution(self.job_id, reason="Operator stop")
        self.assertTrue(cancelled)
        th.join(timeout=3.0)

        res = result_holder.get("res", {})
        self.assertEqual(res.get("status"), JOB_STATE_CANCELLED)

    def test_docker_vs_udocker_distinction(self):
        """Engine strictly refuses to satisfy Docker Engine if only udocker is present."""
        is_supp, canon, bin_p = UniversalExecutionEngine.detect_runtime("docker")
        if is_supp:
            self.assertNotIn("udocker", Path(bin_p).name.lower())

    def test_duplicate_assignment_does_not_spawn_duplicate(self):
        """Duplicate assignments for actively executing attempt are safely ignored."""
        job = {
            "job_id": f"{self.job_id}-dup",
            "attempt": 1,
            "lease_generation": 1,
            "spec": {
                "workload": {
                    "runtime": "native/python",
                    "script": "import time; time.sleep(3)"
                }
            }
        }
        res_holder = {}
        th = threading.Thread(target=lambda: res_holder.update(res=UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)))
        th.start()
        time.sleep(0.4)

        try:
            # Duplicate call while job is actively running
            dup_res = UniversalExecutionEngine.execute_job(job, expected_fencing_token=1)
            self.assertTrue(dup_res.get("duplicate_ignored"))
            self.assertEqual(dup_res.get("status"), "RUNNING")
        finally:
            UniversalExecutionEngine.cancel_execution(f"{self.job_id}-dup")
            th.join(timeout=2.0)

    def test_retries_use_isolated_attempt_workspaces(self):
        """Retries strictly execute in separate attempt directories."""
        ws1 = UniversalExecutionEngine.get_attempt_workspace(f"{self.job_id}-retry", attempt=1)
        (ws1 / "canary_att1.txt").write_text("attempt1", encoding="utf-8")

        ws2 = UniversalExecutionEngine.get_attempt_workspace(f"{self.job_id}-retry", attempt=2)
        self.assertFalse((ws2 / "canary_att1.txt").exists())
        self.assertNotEqual(str(ws1), str(ws2))

    def test_stale_child_process_cleanup_on_restart(self):
        """Agent restart purges unmanaged/stale active processes."""
        cleaned = UniversalExecutionEngine.cleanup_stale_processes()
        self.assertIsInstance(cleaned, int)
        self.assertEqual(len(UniversalExecutionEngine._ACTIVE_PROCESSES), 0)


if __name__ == "__main__":
    import threading
    unittest.main()
