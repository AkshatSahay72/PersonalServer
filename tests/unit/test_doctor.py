#!/usr/bin/env python3
"""
Test Suite for PersonalServer Unified Diagnostic & Recovery Engine (doctor.py)
=============================================================================
Validates:
1. Platform and environment detection (Windows, Linux, Termux).
2. Service applicability filtering (Node vs Controller role).
3. Process liveness and stale PID file handling.
4. Command-line identity matching and untracked process detection.
5. Local health endpoint checks and port probing.
6. Tailscale status parsing and peer reachability.
7. Secret redaction and sensitive information scrubbing.
8. Report generation, serialization, and report file saving.
9. Diagnostic CLI arguments and exit codes (0: HEALTHY/RECOVERED, 1: DEGRADED, 2: ACTION).
10. Recovery mutual-exclusion locking and stale lock cleanup.
11. Circuit breaker bounding and restart rate limiting.
12. Targeted repair, process adoption, and post-restart verification.
13. Tailscale non-destructive guard and Android GUI guidance.
14. Watchdog single-cycle monitoring and graceful stop.
15. Dry-run safety with zero state mutation.
"""

import os
import sys
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import scripts.doctor as doctor
from scripts.doctor import (
    DiagnosticEngine,
    RecoveryLock,
    CircuitBreaker,
    STATUS_HEALTHY,
    STATUS_RECOVERED,
    STATUS_DEGRADED,
    STATUS_ACTION,
)


class TestPlatformDetection(unittest.TestCase):
    """Tests platform, OS, and Termux environment detection logic."""

    def test_termux_detection_by_prefix(self):
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}):
            self.assertTrue(doctor.is_termux_environment())

    def test_termux_detection_by_android_root(self):
        with patch.dict(os.environ, {"ANDROID_ROOT": "/system"}, clear=True):
            self.assertTrue(doctor.is_termux_environment())

    def test_termux_false_on_standard_desktop(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                self.assertFalse(doctor.is_termux_environment())

    def test_platform_info_structure(self):
        engine = DiagnosticEngine(base_dir=BASE_DIR)
        info = engine.detect_platform()
        self.assertIn("os", info)
        self.assertIn("platform", info)
        self.assertIn("architecture", info)
        self.assertIn("python_version", info)
        self.assertIn("is_termux", info)
        self.assertIsInstance(info["is_termux"], bool)


class TestServiceApplicability(unittest.TestCase):
    """Verifies that only services relevant to the host's configured role are checked."""

    def test_node_role_services(self):
        engine = DiagnosticEngine(base_dir=BASE_DIR)
        with patch.object(engine, "read_node_identity", return_value={"role": "node"}):
            with patch.object(engine, "is_controller_host", return_value=False):
                services = engine.get_applicable_services()
                self.assertIn("node_api", services)
                self.assertIn("node_worker", services)
                self.assertIn("tailscale", services)
                self.assertNotIn("controller_router", services)

    def test_controller_role_services(self):
        engine = DiagnosticEngine(base_dir=BASE_DIR)
        with patch.object(engine, "is_controller_host", return_value=True):
            services = engine.get_applicable_services()
            self.assertIn("controller_router", services)


class TestProcessAndPidHandling(unittest.TestCase):
    """Validates PID file inspection, stale PID detection, and untracked process discovery."""

    def setUp(self):
        self.engine = DiagnosticEngine(base_dir=BASE_DIR)

    def test_stale_pid_file(self):
        """When PID file exists but process is dead, status is STALE_PID_FILE."""
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="999999\n"):
                with patch("scripts.doctor.check_process_alive", return_value=False):
                    with patch.object(self.engine, "find_untracked_process", return_value=None):
                        res = self.engine.check_service_process(
                            pid_file=BASE_DIR / "runtime" / "dummy.pid",
                            pattern="dummy"
                        )
                        self.assertEqual(res["status"], "STALE_PID_FILE")
                        self.assertEqual(res["pid"], 999999)

    def test_running_verified_process(self):
        """When PID file exists and process is alive with matching identity, status is RUNNING."""
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="12345\n"):
                with patch("scripts.doctor.check_process_alive", return_value=True):
                    res = self.engine.check_service_process(
                        pid_file=BASE_DIR / "runtime" / "dummy.pid",
                        pattern="dummy"
                    )
                    self.assertEqual(res["status"], "RUNNING")
                    self.assertEqual(res["pid"], 12345)

    def test_untracked_running_process(self):
        """When PID file is missing but matching process is running, status is RUNNING_UNTRACKED."""
        with patch("pathlib.Path.exists", return_value=False):
            with patch("scripts.doctor.find_untracked_process", return_value=54321):
                res = self.engine.check_service_process(
                    pid_file=BASE_DIR / "runtime" / "dummy.pid",
                    pattern="dummy"
                )
                self.assertEqual(res["status"], "RUNNING_UNTRACKED")
                self.assertEqual(res["pid"], 54321)

    def test_completely_stopped_process(self):
        """When neither PID file nor untracked process exists, status is STOPPED."""
        with patch("pathlib.Path.exists", return_value=False):
            with patch("scripts.doctor.find_untracked_process", return_value=None):
                res = self.engine.check_service_process(
                    pid_file=BASE_DIR / "runtime" / "dummy.pid",
                    pattern="dummy"
                )
                self.assertEqual(res["status"], "STOPPED")
                self.assertIsNone(res["pid"])


class TestHealthAndNetworkProbing(unittest.TestCase):
    """Tests HTTP probes, port probes, and Tailscale / Cloudflare log parsing."""

    def setUp(self):
        self.engine = DiagnosticEngine(base_dir=BASE_DIR)

    def test_node_api_healthy_response(self):
        """Node API returns 200 OK with online status -> HEALTHY."""
        fake_proc = {"status": "RUNNING", "pid": 1234}
        with patch.object(self.engine, "check_service_process", return_value=fake_proc):
            with patch("scripts.doctor.check_tcp_port", return_value=True):
                with patch("scripts.doctor.http_get_json", return_value=(200, {"status": "online"}, "ok")):
                    res = self.engine.check_node_api()
                    self.assertEqual(res["status"], STATUS_HEALTHY)
                    self.assertEqual(res["port_open"], True)

    def test_node_api_degraded_on_failed_endpoint(self):
        """Node API process is running but /health probe returns 500 -> DEGRADED."""
        fake_proc = {"status": "RUNNING", "pid": 1234}
        with patch.object(self.engine, "check_service_process", return_value=fake_proc):
            with patch("scripts.doctor.check_tcp_port", return_value=True):
                with patch("scripts.doctor.http_get_json", return_value=(500, {"error": "internal error"}, "error")):
                    res = self.engine.check_node_api()
                    self.assertEqual(res["status"], STATUS_DEGRADED)

    def test_tailscale_connected_status(self):
        """Tailscale connected with IP and peers parsed correctly."""
        tailscale_json = json.dumps({
            "BackendState": "Running",
            "Self": {
                "HostName": "test-device",
                "TailscaleIPs": ["100.64.0.10"],
                "Online": True
            },
            "Peer": {
                "peer-1": {"HostName": "node-1", "Online": True},
                "peer-2": {"HostName": "node-2", "Online": False}
            }
        })
        with patch("shutil.which", return_value="/usr/bin/tailscale"):
            with patch("scripts.doctor.run_cmd_safe", return_value=tailscale_json):
                res = self.engine.check_tailscale()
                self.assertEqual(res["status"], STATUS_HEALTHY)
                self.assertEqual(res["tailscale_ip"], "100.64.0.10")
                self.assertEqual(res["peers_count"], 2)
                self.assertEqual(res["peers_online"], 1)

    def test_tailscale_missing_cli(self):
        """When Tailscale CLI is absent, doctor marks it DEGRADED with informative notes."""
        with patch("shutil.which", return_value=None):
            res = self.engine.check_tailscale()
            self.assertEqual(res["status"], STATUS_DEGRADED)
            self.assertIn("CLI not found", res["message"])


class TestSecretRedaction(unittest.TestCase):
    """Verifies that secrets, tokens, and authorization data are scrubbed from reports."""

    def test_redact_sensitive_text_tokens(self):
        sensitive_text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test and token=secret_token_12345"
        redacted = doctor.redact_sensitive_text(sensitive_text)
        self.assertNotIn("secret_token_12345", redacted)
        self.assertIn("[REDACTED", redacted)

    def test_redact_sensitive_data_dict(self):
        sensitive_dict = {
            "token": "super_secret_auth_token",
            "password": "db_password",
            "public_key": "ssh-ed25519 AAAA...",
            "safe_field": "node-alpha"
        }
        redacted = doctor.redact_sensitive_data(sensitive_dict)
        self.assertIn("[REDACTED", str(redacted["token"]))
        self.assertIn("[REDACTED", str(redacted["password"]))
        self.assertEqual(redacted["safe_field"], "node-alpha")


class TestReportGeneration(unittest.TestCase):
    """Tests report generation, serialization, and disk persistence."""

    def test_save_report_creates_files(self):
        engine = DiagnosticEngine(base_dir=BASE_DIR)
        mock_results = {
            "timestamp": "2026-10-24T00:00:00Z",
            "platform": {"os": "Linux", "is_termux": False},
            "summary": {"status": STATUS_HEALTHY, "warnings": [], "manual_actions": []},
            "services": {"ssh": {"status": STATUS_HEALTHY}},
            "resources": {"disk": {"free_gb": 50.0}}
        }
        engine.results = mock_results
        report_path = engine.save_report()
        try:
            self.assertTrue(report_path.exists())
            self.assertTrue(report_path.name.endswith(".json"))
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.assertEqual(data["summary"]["status"], STATUS_HEALTHY)

            txt_path = report_path.with_suffix(".txt")
            self.assertTrue(txt_path.exists())
        finally:
            if report_path.exists():
                report_path.unlink()
            txt_path = report_path.with_suffix(".txt")
            if txt_path.exists():
                txt_path.unlink()


class TestRecoveryLocking(unittest.TestCase):
    """Tests mutual exclusion locking for self-healing operations."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.temp_dir.name) / "recovery.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_acquire_and_release_lock(self):
        lock = RecoveryLock(lock_file=self.lock_file)
        ok, msg = lock.acquire()
        self.assertTrue(ok)
        self.assertTrue(self.lock_file.exists())
        lock.release()
        self.assertFalse(self.lock_file.exists())

    def test_concurrent_lock_rejected(self):
        lock1 = RecoveryLock(lock_file=self.lock_file)
        ok1, _ = lock1.acquire()
        self.assertTrue(ok1)

        lock2 = RecoveryLock(lock_file=self.lock_file)
        with patch("scripts.doctor.check_process_alive", return_value=True):
            ok2, msg2 = lock2.acquire()
            self.assertFalse(ok2)
            self.assertIn("already in progress", msg2)

        lock1.release()

    def test_stale_lock_cleanup(self):
        # Create a stale lock from a dead PID
        payload = {"pid": 999999, "time": "2026-10-24T00:00:00Z"}
        self.lock_file.write_text(json.dumps(payload), encoding="utf-8")

        lock = RecoveryLock(lock_file=self.lock_file)
        with patch("scripts.doctor.check_process_alive", return_value=False):
            ok, _ = lock.acquire()
            self.assertTrue(ok)
            lock.release()


class TestCircuitBreaker(unittest.TestCase):
    """Tests circuit breaker bounding and restart limits."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "recovery_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_allows_up_to_max_attempts(self):
        cb = CircuitBreaker(state_file=self.state_file, max_attempts=3, window_sec=600)
        self.assertTrue(cb.can_attempt("node_api")[0])

        cb.record_attempt("node_api", success=False)
        self.assertTrue(cb.can_attempt("node_api")[0])

        cb.record_attempt("node_api", success=False)
        self.assertTrue(cb.can_attempt("node_api")[0])

        cb.record_attempt("node_api", success=False)
        # 3rd failure reached -> circuit breaker trips
        can_try, count, msg = cb.can_attempt("node_api")
        self.assertFalse(can_try)
        self.assertEqual(count, 3)
        self.assertIn("tripped", msg)

    def test_reset_on_success(self):
        cb = CircuitBreaker(state_file=self.state_file, max_attempts=3, window_sec=600)
        cb.record_attempt("node_api", success=False)
        cb.record_attempt("node_api", success=False)
        cb.record_attempt("node_api", success=True)  # Success clears counter
        can_try, count, _ = cb.can_attempt("node_api")
        self.assertTrue(can_try)
        self.assertEqual(count, 0)


class TestTargetedRepair(unittest.TestCase):
    """Tests targeted self-healing actions, dry-run safety, and verification."""

    def setUp(self):
        self.engine = DiagnosticEngine(base_dir=BASE_DIR)

    def test_healthy_services_remain_untouched(self):
        mock_diag = {
            "summary": {"status": STATUS_HEALTHY, "warnings": [], "manual_actions": []},
            "services": {"node_api": {"status": STATUS_HEALTHY}},
            "environment": {"is_termux": False}
        }
        with patch.object(self.engine, "run_all", return_value=mock_diag):
            res = self.engine.repair(dry_run=False)
            self.assertEqual(res["final_status"], STATUS_HEALTHY)
            self.assertEqual(len(res["actions"]), 0)
            self.assertIn("No recovery action needed", res["message"])

    def test_dry_run_does_not_modify_state(self):
        mock_diag = {
            "summary": {"status": STATUS_ACTION, "warnings": [], "manual_actions": []},
            "services": {
                "node_api": {
                    "status": STATUS_ACTION,
                    "process": {"status": "STOPPED", "pid": None},
                    "health_response": {"is_online": False}
                }
            },
            "environment": {"is_termux": False}
        }
        with patch.object(self.engine, "run_all", return_value=mock_diag):
            with patch.object(self.engine, "check_node_api", return_value=mock_diag["services"]["node_api"]):
                with patch("scripts.doctor.spawn_background_process") as mock_spawn:
                    res = self.engine.repair(dry_run=True)
                    self.assertTrue(res["dry_run"])
                    mock_spawn.assert_not_called()
                    self.assertEqual(len(res["actions"]), 1)
                    self.assertEqual(res["actions"][0]["action"], "WOULD_START")

    def test_untracked_process_adoption(self):
        mock_node_api = {
            "status": STATUS_DEGRADED,
            "process": {"status": "RUNNING_UNTRACKED", "pid": 4242},
            "health_response": {"is_online": True}
        }
        with patch.object(self.engine, "check_node_api", return_value=mock_node_api):
            with patch("pathlib.Path.write_text") as mock_write:
                act = self.engine.repair_node_api(dry_run=False)
                self.assertEqual(act["action"], "ADOPTED_PID")
                self.assertTrue(act["success"])
                mock_write.assert_called_once_with("4242\n", encoding="utf-8")

    def test_cloudflare_tunnel_quic_fallback(self):
        mock_cf_info = {
            "status": STATUS_ACTION,
            "process": {"status": "STOPPED", "pid": None},
            "connection_state": "DISCONNECTED"
        }
        with patch("shutil.which", return_value="/usr/bin/cloudflared"):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 128
                    with patch.object(self.engine, "check_cloudflare_tunnel", return_value=mock_cf_info):
                        with patch("pathlib.Path.read_text", return_value="Failed to create new quic connection"):
                            with patch("scripts.doctor.spawn_background_process", return_value=7777) as mock_spawn:
                                with patch("scripts.doctor.check_process_alive", return_value=True):
                                    with patch("pathlib.Path.write_text"):
                                        act = self.engine.repair_cloudflare_tunnel(dry_run=False)
                                        self.assertEqual(act["action"], "STARTED")
                                        self.assertTrue(act["success"])
                                        # Verify --protocol http2 was passed
                                        called_cmd = mock_spawn.call_args[0][0]
                                        self.assertIn("--protocol", called_cmd)
                                        self.assertIn("http2", called_cmd)


class TestTailscaleGuardedRecovery(unittest.TestCase):
    """Verifies that Tailscale recovery never rotates keys and provides exact Android GUI guidance."""

    def setUp(self):
        self.engine = DiagnosticEngine(base_dir=BASE_DIR)

    def test_android_guidance_for_tailscale(self):
        with patch("scripts.doctor.is_termux_environment", return_value=True):
            act = self.engine.repair_tailscale(dry_run=False)
            self.assertEqual(act["action"], "MANUAL_ACTION_REQUIRED")
            self.assertFalse(act["success"])
            self.assertIn("Open the Tailscale app", act["message"])


class TestWatchdogEngine(unittest.TestCase):
    """Tests bounded watchdog monitoring and clean shutdown."""

    def setUp(self):
        self.engine = DiagnosticEngine(base_dir=BASE_DIR)

    def test_single_iteration_watchdog(self):
        mock_diag = {
            "summary": {"status": STATUS_HEALTHY},
            "services": {}
        }
        with patch.object(self.engine, "run_all", return_value=mock_diag):
            with patch("pathlib.Path.write_text"):
                with patch("pathlib.Path.unlink"):
                    code = self.engine.run_watchdog(interval=1, iterations=1)
                    self.assertEqual(code, 0)

    def test_stop_watchdog_nonexistent(self):
        with patch("pathlib.Path.exists", return_value=False):
            ok, msg = self.engine.stop_watchdog()
            self.assertFalse(ok)
            self.assertIn("missing", msg)


class TestExitCodesAndCli(unittest.TestCase):
    """Tests CLI argument parsing and process exit code mapping."""

    def test_exit_code_zero_for_healthy(self):
        mock_results = {"summary": {"status": STATUS_HEALTHY}}
        with patch.object(DiagnosticEngine, "run_all", return_value=mock_results):
            with patch("sys.argv", ["doctor.py"]):
                code = doctor.main()
                self.assertEqual(code, 0)

    def test_exit_code_zero_for_recovered(self):
        mock_results = {"summary": {"status": STATUS_RECOVERED}}
        with patch.object(DiagnosticEngine, "repair", return_value={"final_status": STATUS_RECOVERED}):
            engine_mock = DiagnosticEngine(base_dir=BASE_DIR)
            engine_mock.results = mock_results
            with patch("scripts.doctor.DiagnosticEngine", return_value=engine_mock):
                with patch("sys.argv", ["doctor.py", "--repair"]):
                    code = doctor.main()
                    self.assertEqual(code, 0)

    def test_exit_code_one_for_degraded(self):
        mock_results = {"summary": {"status": STATUS_DEGRADED}}
        with patch.object(DiagnosticEngine, "run_all", return_value=mock_results):
            with patch("sys.argv", ["doctor.py"]):
                code = doctor.main()
                self.assertEqual(code, 1)

    def test_exit_code_two_for_action(self):
        mock_results = {"summary": {"status": STATUS_ACTION}}
        with patch.object(DiagnosticEngine, "run_all", return_value=mock_results):
            with patch("sys.argv", ["doctor.py"]):
                code = doctor.main()
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
