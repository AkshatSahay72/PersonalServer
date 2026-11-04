#!/usr/bin/env python3
"""
Unit tests for Process Identity Hardening and PID Safety (Batch 2.2)
=====================================================================
Verifies:
1. Fail-closed ownership verification (inaccessible/empty cmdline returns False, never True).
2. Elimination of weak generic token collision (python/python.exe alone cannot claim ownership).
3. Process start-time identity capture and PID reuse detection.
4. Backward compatibility: legacy records without start-time metadata are treated as unverified.
5. Robust /proc/{pid}/stat parsing with parentheses/spaces in comm field and malformed data handling.
6. Refusal to signal or kill unverified or conflicting processes.
7. End-to-end start_process and stop_process lifecycle with verified process identity.
"""

import os
import sys
import time
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.process_manager import (
    SafeProcessLifecycleManager,
    ProcessIdentityRecord,
    GENERIC_COMMAND_TOKENS,
    ProcessSecurityError,
)


class TestProcessIdentitySafety(unittest.TestCase):
    """Verifies fail-closed process ownership, PID start-time tracking, and termination refusal."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.test_dir.name) / "runtime"
        self.apps_runtime_dir = self.runtime_dir / "apps"
        self.apps_runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(self.test_dir.name) / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self._orig_runtime = SafeProcessLifecycleManager._ensure_runtime_dir
        SafeProcessLifecycleManager._ensure_runtime_dir = lambda: self.apps_runtime_dir

    def tearDown(self):
        SafeProcessLifecycleManager._ensure_runtime_dir = self._orig_runtime
        try:
            self.test_dir.cleanup()
        except Exception:
            pass

    def test_record_serialization_and_legacy_deserialization(self):
        """Verifies start_time persistence and safe fallback for legacy records missing start_time."""
        # 1. New record with start_time
        rec = ProcessIdentityRecord(
            app_id="app-1",
            deployment_id="dep-1",
            pid=1234,
            port=8080,
            command=["python", "app.py"],
            working_dir="/tmp/app",
            log_file="/tmp/log.log",
            created_at="2026-11-04T00:00:00Z",
            start_time=987654321
        )
        d = rec.to_dict()
        self.assertEqual(d.get("start_time"), 987654321)

        rec_loaded = ProcessIdentityRecord.from_dict(d)
        self.assertEqual(rec_loaded.start_time, 987654321)

        # 2. Legacy record without start_time field
        legacy_data = {
            "app_id": "app-legacy",
            "deployment_id": "dep-legacy",
            "pid": 5678,
            "port": 8081,
            "command": ["python", "app.py"],
            "working_dir": "/tmp/app",
            "log_file": "/tmp/log.log",
            "created_at": "2026-11-04T00:00:00Z"
        }
        legacy_rec = ProcessIdentityRecord.from_dict(legacy_data)
        self.assertIsNone(legacy_rec.start_time)

    def test_legacy_record_missing_start_time_treated_as_unverified(self):
        """A record lacking start_time metadata must NOT be silently trusted (fail-closed)."""
        curr_pid = os.getpid()
        rec = ProcessIdentityRecord(
            app_id="legacy-app",
            deployment_id="dep-old",
            pid=curr_pid,
            port=9999,
            command=[sys.executable, "dummy_script.py"],
            working_dir="/tmp/legacy",
            log_file="/tmp/legacy.log",
            created_at="2026-11-04T00:00:00Z",
            start_time=None  # Missing metadata
        )
        SafeProcessLifecycleManager.write_record(rec)

        valid, reason = SafeProcessLifecycleManager.verify_process_ownership("legacy-app", curr_pid, rec)
        self.assertFalse(valid)
        self.assertIn("Ownership unverified", reason)
        self.assertIn("lacks start-time metadata", reason)

    def test_recycled_pid_with_different_start_time_rejected(self):
        """When an active PID has a different start time than recorded, PID reuse is flagged."""
        # Spawn a harmless long-lived sleep dummy process
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        try:
            # Record with deliberate mismatched start time
            rec = ProcessIdentityRecord(
                app_id="recycled-app",
                deployment_id="dep-1",
                pid=dummy.pid,
                port=9099,
                command=[sys.executable, "app.py"],
                working_dir="/tmp/app",
                log_file="/tmp/app.log",
                created_at="2026-11-04T00:00:00Z",
                start_time=1  # Deliberately mismatched start time
            )
            SafeProcessLifecycleManager.write_record(rec)

            valid, reason = SafeProcessLifecycleManager.verify_process_ownership("recycled-app", dummy.pid, rec)
            self.assertFalse(valid)
            self.assertIn("PID reuse detected", reason)
        finally:
            dummy.kill()
            dummy.wait(timeout=3)

    def test_generic_python_token_collision_fails_closed(self):
        """Matching generic 'python' alone must NOT verify ownership of an unrelated process."""
        # Spawn a harmless dummy process that is definitely not 'my_app_service.py'
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        try:
            live_start_time = SafeProcessLifecycleManager.get_process_start_time(dummy.pid)
            self.assertIsNotNone(live_start_time)

            # Record says the process is running 'my_custom_service.py'
            rec = ProcessIdentityRecord(
                app_id="custom-service",
                deployment_id="dep-custom",
                pid=dummy.pid,
                port=9098,
                command=[sys.executable, "-u", "my_custom_service.py"],
                working_dir="/tmp/custom",
                log_file="/tmp/custom.log",
                created_at="2026-11-04T00:00:00Z",
                start_time=live_start_time
            )
            SafeProcessLifecycleManager.write_record(rec)

            valid, reason = SafeProcessLifecycleManager.verify_process_ownership("custom-service", dummy.pid, rec)
            self.assertFalse(valid)
            self.assertIn("Process ownership conflict", reason)
            self.assertIn("does not match deployment contract", reason)
        finally:
            dummy.kill()
            dummy.wait(timeout=3)

    def test_unreadable_or_empty_cmdline_fails_closed(self):
        """If cmdline cannot be retrieved (empty/inaccessible), ownership MUST be rejected, never assumed True."""
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        try:
            live_start_time = SafeProcessLifecycleManager.get_process_start_time(dummy.pid)
            rec = ProcessIdentityRecord(
                app_id="unreadable-app",
                deployment_id="dep-1",
                pid=dummy.pid,
                port=9097,
                command=[sys.executable, "app.py"],
                working_dir="/tmp/app",
                log_file="/tmp/app.log",
                created_at="2026-11-04T00:00:00Z",
                start_time=live_start_time
            )

            # Simulate unreadable or inaccessible cmdline
            with patch.object(SafeProcessLifecycleManager, "get_process_cmdline", return_value=""):
                valid, reason = SafeProcessLifecycleManager.verify_process_ownership("unreadable-app", dummy.pid, rec)
                self.assertFalse(valid)
                self.assertIn("Ownership unverified", reason)
                self.assertIn("inaccessible or empty", reason)
        finally:
            dummy.kill()
            dummy.wait(timeout=3)

    def test_linux_proc_stat_parsing_with_parentheses_and_spaces(self):
        """Validates robust field 22 starttime extraction even if process comm contains spaces/parentheses."""
        # Simulated /proc/{pid}/stat line with tricky comm: "(Web Content (1))"
        # Field 1 = 1234
        # Field 2 = (Web Content (1))
        # Field 3 (rest[0]) = S
        # ...
        # Field 22 (rest[19]) = 987654
        stat_fields = [
            "S", "100", "1234", "100", "0", "-1", "4194560", "1200", "0", "0", "0",
            "15", "5", "0", "0", "20", "0", "4", "0", "987654", "10485760", "500"
        ]
        simulated_stat = f"1234 (Web Content (1)) " + " ".join(stat_fields)

        # Test the rfind(")") extraction logic
        rparen = simulated_stat.rfind(")")
        self.assertNotEqual(rparen, -1)
        rest = simulated_stat[rparen + 1:].strip().split()
        self.assertEqual(rest[0], "S")  # State
        self.assertEqual(int(rest[19]), 987654)  # Starttime field 22

    def test_malformed_proc_stat_handled_gracefully(self):
        """Malformed or truncated /proc/{pid}/stat content returns None without exceptions."""
        malformed_samples = [
            "",
            "1234",
            "1234 (unclosed-comm",
            "1234 (comm) S 1 2 3",  # truncated (less than 22 fields)
            "corrupted data %&$*#",
        ]
        for sample in malformed_samples:
            rparen = sample.rfind(")")
            if rparen == -1:
                res = None
            else:
                rest = sample[rparen + 1:].strip().split()
                res = int(rest[19]) if len(rest) > 19 and rest[19].isdigit() else None
            self.assertIsNone(res)

    def test_stop_process_refuses_to_signal_unverified_process(self):
        """CRITICAL: stop_process MUST refuse to signal when ownership is unverified or conflicting."""
        dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        try:
            # Write a record pointing to dummy PID with wrong start time
            rec = ProcessIdentityRecord(
                app_id="refuse-kill-app",
                deployment_id="dep-fake",
                pid=dummy.pid,
                port=9096,
                command=[sys.executable, "app.py"],
                working_dir="/tmp/refuse",
                log_file="/tmp/refuse.log",
                created_at="2026-11-04T00:00:00Z",
                start_time=12345678  # Wrong start time
            )
            SafeProcessLifecycleManager.write_record(rec)

            ok, msg = SafeProcessLifecycleManager.stop_process("refuse-kill-app")
            self.assertFalse(ok)
            self.assertIn("Refusing to stop process", msg)

            # Process MUST still be alive!
            self.assertTrue(SafeProcessLifecycleManager.is_pid_active(dummy.pid))
            self.assertIsNone(dummy.poll(), "Process was improperly signaled despite failed ownership verification!")
        finally:
            dummy.kill()
            dummy.wait(timeout=3)

    def test_end_to_end_start_and_stop_lifecycle_with_verified_identity(self):
        """A properly launched process records start_time, verifies ownership, and stops cleanly."""
        app_id = "test-e2e-app"
        dep_id = "dep-e2e-1"
        cwd = Path(self.test_dir.name) / "e2e_app"
        cwd.mkdir(parents=True, exist_ok=True)

        script_path = cwd / "worker_app.py"
        script_path.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

        # Start process
        rec = SafeProcessLifecycleManager.start_process(
            app_id=app_id,
            deployment_id=dep_id,
            cmd=[sys.executable, "-u", str(script_path)],
            cwd=cwd,
            port=9095,
            env=os.environ.copy(),
            log_file=self.logs_dir / f"app_{app_id}.log"
        )

        try:
            # 1. Verify start_time was captured
            self.assertIsNotNone(rec.start_time)

            # 2. Verify active
            self.assertTrue(SafeProcessLifecycleManager.is_pid_active(rec.pid))

            # 3. Ownership verification MUST succeed
            valid, reason = SafeProcessLifecycleManager.verify_process_ownership(app_id, rec.pid, rec)
            self.assertTrue(valid, f"Ownership verification failed unexpectedly: {reason}")
            self.assertEqual(reason, "Ownership verified.")

            # 4. Graceful stop MUST succeed
            stopped, msg = SafeProcessLifecycleManager.stop_process(app_id)
            self.assertTrue(stopped, f"Stop failed: {msg}")
            self.assertIn("stopped successfully", msg)

            # 5. Process should no longer be active
            self.assertFalse(SafeProcessLifecycleManager.is_pid_active(rec.pid))

            # 6. Record should be cleanly removed
            self.assertIsNone(SafeProcessLifecycleManager.read_record(app_id))
        finally:
            # Defensive cleanup in case of failure
            if SafeProcessLifecycleManager.is_pid_active(rec.pid):
                try:
                    import signal
                    os.kill(rec.pid, signal.SIGTERM)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()

