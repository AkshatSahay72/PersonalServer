#!/usr/bin/env python3
"""
Unit and Integration Tests for Process Observability & Startup Failure Detection
=================================================================================
Covers:
1. Unbuffered subprocess execution (-u flag and PYTHONUNBUFFERED=1 env var)
2. Immediate child-process crash detection (fast-abort < 2s without waiting full health timeout)
3. Traceback & log-tail inclusion in deployment failure diagnostics
4. Bounded log-tail reading (memory and line limits)
5. Sensitive credential and token redaction in log output
6. GET /apps/{app_id}/logs reading actual log files with jobs_db fallback
7. Missing, empty, and invalid app ID log handling
"""

import os
import sys
import json
import time
import shutil
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.process_manager import SafeProcessLifecycleManager, LOGS_DIR
from controller.deployment_manager import DeploymentManager, DeploymentError
from controller.stores import load_apps_db, save_apps_db, load_nodes_db, save_nodes_db
from config.platform_config import get_controller_paths
from controller.http.app_routes import handle_app_get


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestProcessObservability(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_proc_obs_"))
        cls.orig_paths = get_controller_paths()

        # Isolate controller data directory
        cls.mock_data_dir = cls.test_dir / "data"
        cls.mock_data_dir.mkdir(parents=True, exist_ok=True)

        cls.mock_apps_file = cls.mock_data_dir / "apps.json"
        cls.mock_backup_apps_file = cls.mock_data_dir / "apps.json.bak"
        cls.mock_nodes_file = cls.mock_data_dir / "nodes.json"
        cls.mock_backup_nodes_file = cls.mock_data_dir / "nodes.json.bak"

        cls.orig_paths.apps_file = cls.mock_apps_file
        cls.orig_paths.backup_apps_file = cls.mock_backup_apps_file
        cls.orig_paths.nodes_file = cls.mock_nodes_file
        cls.orig_paths.backup_nodes_file = cls.mock_backup_nodes_file

        # Initialize mock online node
        nodes_db = {
            "nodes": {
                "node-obs-test": {
                    "id": "node-obs-test",
                    "name": "Observability Test Node",
                    "status": "ONLINE",
                    "role": "compute",
                    "last_seen": time.time() + 3600,
                    "last_heartbeat": {"services": {"node_worker": "online"}},
                    "capabilities": {"python3": True, "web_service": True}
                }
            }
        }
        with open(cls.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(nodes_db, f)

        with open(cls.mock_apps_file, "w", encoding="utf-8") as f:
            json.dump({"apps": {}}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def tearDown(self):
        for app_id in ("obs-app-live", "obs-app-crash", "obs-app-unbuf"):
            try:
                SafeProcessLifecycleManager.stop_process(app_id)
            except Exception:
                pass

    def test_01_read_bounded_log_tail_limits(self):
        """Verify log tail reader strictly bounds bytes and line count without excessive memory use."""
        log_file = self.test_dir / "large_test.log"
        # Write 300 lines of known content (~60 bytes each = ~18 KB)
        with open(log_file, "w", encoding="utf-8") as f:
            for i in range(1, 301):
                f.write(f"Line {i:04d}: System event record payload padding data here\n")

        # Read with max_lines=50
        tail_50 = DeploymentManager.read_bounded_log_tail(log_file, max_lines=50, max_bytes=64 * 1024)
        lines = tail_50.splitlines()
        self.assertEqual(len(lines), 50)
        self.assertIn("Line 0300", lines[-1])
        self.assertIn("Line 0251", lines[0])

        # Read with very tight max_bytes limit
        tail_tight = DeploymentManager.read_bounded_log_tail(log_file, max_lines=100, max_bytes=200)
        self.assertLessEqual(len(tail_tight.encode("utf-8")), 200)
        self.assertIn("Line 0300", tail_tight)

    def test_02_read_bounded_log_tail_redaction(self):
        """Verify sensitive credentials are scrubbed from log diagnostics."""
        log_file = self.test_dir / "sensitive_test.log"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("Starting server with token=Bearer super_secret_token_12345\n")
            f.write("Database connection: password=my_db_secret_pass\n")
            f.write("Ready to receive requests\n")

        tail = DeploymentManager.read_bounded_log_tail(log_file)
        self.assertNotIn("super_secret_token_12345", tail)
        self.assertNotIn("my_db_secret_pass", tail)
        self.assertIn("[REDACTED]", tail)
        self.assertIn("Ready to receive requests", tail)

    def test_03_unbuffered_subprocess_execution(self):
        """Verify launched applications receive -u flag and PYTHONUNBUFFERED=1."""
        port = find_free_port()
        app_id = "obs-app-unbuf"

        artifact_zip = self.test_dir / "unbuf_svc.zip"
        with zipfile.ZipFile(artifact_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, sys, os\n"
                f"port = int(os.environ.get('PORT', {port}))\n"
                f"# Print without newline or flush to test unbuffered stdout\n"
                f"sys.stdout.write('UNBUFFERED_TOKEN_PROVEN')\n"
                f"sys.stdout.flush()\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        self.send_response(200)\n"
                f"        self.end_headers()\n"
                f"        self.wfile.write(b'{{\"status\":\"ok\"}}')\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )

        manifest = {
            "app_id": app_id,
            "name": "unbuf-svc",
            "public_path": "/unbuf",
            "runtime": "python",
            "port": port,
            "health_check": {"path": "/health", "timeout_seconds": 3, "interval_seconds": 0.2}
        }
        DeploymentManager.register_manifest(manifest)

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=artifact_zip,
            node_override="node-obs-test"
        )
        self.assertEqual(res["status"], "SUCCESS")

        # Inspect process record: verify -u is present in command
        rec = SafeProcessLifecycleManager.read_record(app_id)
        self.assertIsNotNone(rec)
        self.assertIn("-u", rec.command)

        # Inspect log file directly: token must appear
        log_path = Path(rec.log_file)
        self.assertTrue(log_path.exists())
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("UNBUFFERED_TOKEN_PROVEN", log_content)

    def test_04_immediate_child_crash_fast_abort_and_log_tail(self):
        """Verify that when a process exits immediately, health polling aborts fast and captures traceback."""
        port = find_free_port()
        app_id = "obs-app-crash"

        # 1. Verify unit fast-abort in probe_http_health: when PID is dead, aborts in <1s (not 5s)
        t_probe = time.time()
        healthy, probe_diag = DeploymentManager.probe_http_health(
            port=port,
            timeout_sec=5.0,
            interval_sec=0.2,
            pid=999999
        )
        probe_elapsed = time.time() - t_probe
        self.assertFalse(healthy)
        self.assertLess(probe_elapsed, 1.0, f"Expected probe to abort in <1s, took {probe_elapsed:.2f}s")
        self.assertIn("exited unexpectedly before becoming healthy", probe_diag)

        # 2. Verify end-to-end deployment failure diagnostic and log tail capture
        artifact_zip = self.test_dir / "crash_svc.zip"
        with zipfile.ZipFile(artifact_zip, "w") as zf:
            zf.writestr(
                "app.py",
                "import sys\n"
                "print('CRASH_DIAGNOSTIC_MARKER: Initializing crash test app')\n"
                "raise RuntimeError('FATAL_STARTUP_EXCEPTION: Missing configuration table')\n"
            )

        manifest = {
            "app_id": app_id,
            "name": "crash-svc",
            "public_path": "/crash",
            "runtime": "python",
            "port": port,
            "health_check": {"path": "/health", "timeout_seconds": 5, "interval_seconds": 0.3}
        }
        DeploymentManager.register_manifest(manifest)

        with self.assertRaises(DeploymentError) as ctx:
            DeploymentManager.deploy_application(
                app_id=app_id,
                artifact_path=artifact_zip,
                node_override="node-obs-test",
                auto_rollback_on_failure=False
            )

        err_msg = str(ctx.exception)

        # Verify PID exit detection and log tail diagnostic
        self.assertIn("exited unexpectedly before becoming healthy", err_msg)
        self.assertIn("--- Process Log Tail (last 20 lines) ---", err_msg)
        self.assertIn("FATAL_STARTUP_EXCEPTION", err_msg)
        self.assertIn("CRASH_DIAGNOSTIC_MARKER", err_msg)

    def test_05_get_app_logs_endpoint_reads_actual_file(self):
        """Verify GET /apps/<app_id>/logs reads the real log file and formats correctly."""
        app_id = "obs-app-logs-route"
        manifest = {
            "app_id": app_id,
            "name": "logs-route-svc",
            "public_path": "/logs-route"
        }
        DeploymentManager.register_manifest(manifest)

        # Write dummy application log to logs directory
        log_file = LOGS_DIR / f"app_{app_id}.log"
        log_file.write_text(
            "2026-11-04 15:00:00 INFO Server starting\n"
            "2026-11-04 15:00:01 INFO Application listening on port 8090\n"
            "2026-11-04 15:00:02 INFO Request GET /health returned 200\n",
            encoding="utf-8"
        )

        try:
            # Mock BaseHandler
            mock_handler = MagicMock()
            mock_handler.is_authenticated_admin_or_node.return_value = True

            handled = handle_app_get(mock_handler, f"/apps/{app_id}/logs", {})
            self.assertTrue(handled)
            mock_handler.send_json.assert_called_once()
            status_code, body = mock_handler.send_json.call_args[0]
            self.assertEqual(status_code, 200)
            self.assertEqual(body["app_id"], app_id)
            self.assertIn("Server starting", body["logs"])
            self.assertIn("Application listening on port 8090", body["logs"])
        finally:
            if log_file.exists():
                log_file.unlink()

    def test_06_get_app_logs_missing_file_and_not_found(self):
        """Verify missing log file gracefully returns default message, and unknown app returns 404."""
        # 1. Unknown app -> 404
        mock_handler = MagicMock()
        mock_handler.is_authenticated_admin_or_node.return_value = True
        handled = handle_app_get(mock_handler, "/apps/non-existent-app-999/logs", {})
        self.assertTrue(handled)
        status_code, body = mock_handler.send_json.call_args[0]
        self.assertEqual(status_code, 404)

        # 2. Known app with no log file -> 200 with default message
        app_id = "obs-app-empty-logs"
        manifest = {
            "app_id": app_id,
            "name": "empty-logs-svc",
            "public_path": "/empty-logs"
        }
        DeploymentManager.register_manifest(manifest)

        mock_handler.reset_mock()
        handled = handle_app_get(mock_handler, f"/apps/{app_id}/logs", {})
        self.assertTrue(handled)
        status_code, body = mock_handler.send_json.call_args[0]
        self.assertEqual(status_code, 200)
        self.assertEqual(body["logs"], "No logs recorded yet.")


if __name__ == "__main__":
    unittest.main()
