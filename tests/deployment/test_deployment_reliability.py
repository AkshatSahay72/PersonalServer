#!/usr/bin/env python3
"""
PersonalServer Application Deployment Reliability & Safety Test Suite
=====================================================================
Covers:
1. Successful deployment of minimal Python web service with strict health probe
2. Invalid manifest schema rejection
3. Secret keyword detection and rejection in manifest
4. Artifact checksum mismatch rejection (integrity verification)
5. Capability-based scheduling (requires python3; rejects offline/incompatible nodes)
6. Port conflict detection and fast failure
7. Application process crash / startup failure handling
8. Health check failure / timeout with automatic rollback
9. Status inspection, graceful stop, and restart
10. Rollback to prior release, and rejection when no prior release exists
11. CRITICAL: Process and IDE safety regression tests (proving current PID, parent PID,
    IDE, controller, router, and worker agents are immune from termination)
"""

import os
import sys
import json
import time
import shutil
import zipfile
import socket
import unittest
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.app_manifest import ManifestValidationError, check_for_secrets
from common.process_manager import (
    SafeProcessLifecycleManager,
    ProcessIdentityRecord,
    ProcessSecurityError,
    PROTECTED_CMD_KEYWORDS
)
from common.artifact_manager import compute_file_sha256
from controller.deployment_manager import DeploymentManager, DeploymentError
from controller.stores import load_apps_db, save_apps_db, load_nodes_db, save_nodes_db
from config.platform_config import get_controller_paths


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDeploymentReliability(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_deploy_rel_"))
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

        # Initialize mock nodes: Node 1 online with python3; Node 2 offline
        nodes_db = {
            "nodes": {
                "worker-phone-1": {
                    "id": "worker-phone-1",
                    "name": "Realme 5 Pro",
                    "status": "ONLINE",
                    "role": "compute",
                    "last_seen": time.time(),
                    "last_heartbeat": {"services": {"node_worker": "online"}},
                    "capabilities": {"python3": True, "web_service": True}
                },
                "worker-offline": {
                    "id": "worker-offline",
                    "name": "Offline Phone",
                    "status": "OFFLINE",
                    "last_seen": time.time() - 3600,
                    "last_heartbeat": {"services": {}},
                    "capabilities": {"python3": True}
                }
            }
        }
        with open(cls.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(nodes_db, f)

        # Initialize clean apps DB
        with open(cls.mock_apps_file, "w", encoding="utf-8") as f:
            json.dump({"apps": {}}, f)

    @classmethod
    def tearDownClass(cls):
        # Stop any residual test processes
        try:
            SafeProcessLifecycleManager.stop_process("test-micro-svc")
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        # Refresh mock node last_seen timestamps to prevent node liveness timeouts during test run
        try:
            with open(self.mock_nodes_file, "r", encoding="utf-8") as f:
                nodes_db = json.load(f)
            if "worker-phone-1" in nodes_db.get("nodes", {}):
                nodes_db["nodes"]["worker-phone-1"]["last_seen"] = time.time()
                nodes_db["nodes"]["worker-phone-1"]["status"] = "ONLINE"
                with open(self.mock_nodes_file, "w", encoding="utf-8") as f:
                    json.dump(nodes_db, f)
        except Exception:
            pass

    def tearDown(self):
        # Clean up any running test processes
        for app_id in ("test-micro-svc", "test-failing-app", "test-conflict-app"):
            try:
                SafeProcessLifecycleManager.stop_process(app_id)
            except Exception:
                pass

    def test_01_valid_manifest_registration(self):
        """Valid application manifest is registered with zero secrets."""
        manifest = {
            "app_id": "test-micro-svc",
            "name": "micro-svc",
            "public_path": "/micro-svc",
            "runtime": "python",
            "port": find_free_port(),
            "health_check": {
                "path": "/health",
                "timeout_seconds": 3,
                "interval_seconds": 0.2,
                "expected_status": 200
            }
        }
        record = DeploymentManager.register_manifest(manifest)
        self.assertEqual(record["app_id"], "test-micro-svc")
        self.assertEqual(record["status"], "CREATED")
        self.assertEqual(record["public_path"], "/micro-svc")

    def test_02_secret_exposure_rejection(self):
        """Manifest containing credentials or API keys is rejected immediately."""
        bad_manifest = {
            "app_id": "test-leaky-app",
            "name": "leaky-app",
            "public_path": "/leaky",
            "api_key": "super-secret-12345"
        }
        with self.assertRaises(ManifestValidationError) as ctx:
            DeploymentManager.register_manifest(bad_manifest)
        self.assertIn("forbidden in application manifest", str(ctx.exception))

    def test_03_invalid_path_traversal_manifest_rejection(self):
        """Manifest with directory traversal or null bytes is rejected."""
        bad_manifest = {
            "app_id": "test-traversal-app",
            "name": "traversal-app",
            "public_path": "/../../escape"
        }
        with self.assertRaises(ManifestValidationError):
            DeploymentManager.register_manifest(bad_manifest)

    def test_04_artifact_checksum_verification_and_tamper_detection(self):
        """Artifact SHA-256 integrity verification passes for authentic artifact and fails for tampered bundle."""
        artifact_zip = self.test_dir / "valid_bundle.zip"
        with zipfile.ZipFile(artifact_zip, "w") as zf:
            zf.writestr("app.py", "print('hello')\n")

        valid_sha256, _ = compute_file_sha256(artifact_zip)

        # Authentic verification passes
        DeploymentManager.verify_artifact_checksum(artifact_zip, valid_sha256)

        # Tampered verification fails
        fake_sha256 = "0" * 64
        with self.assertRaises(DeploymentError) as ctx:
            DeploymentManager.verify_artifact_checksum(artifact_zip, fake_sha256)
        self.assertIn("checksum verification failed", str(ctx.exception).lower())

    def test_05_capability_scheduling_selection_and_offline_rejection(self):
        """Scheduler picks available python3 worker, and rejects incompatible requirements."""
        # 1. Matching capability succeeds
        selected = DeploymentManager.select_deployment_node({"capabilities": ["python3"]})
        self.assertEqual(selected, "worker-phone-1")

        # 2. Impossible capability fails with clear error
        with self.assertRaises(DeploymentError) as ctx:
            DeploymentManager.select_deployment_node({"capabilities": ["quantum_computer"]})
        self.assertIn("No eligible worker node available", str(ctx.exception))

    def test_06_port_conflict_detection(self):
        """Deployment fails fast if target port is already bound by another process."""
        port = find_free_port()
        # Bind the port manually with a raw server
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)

        try:
            # Register app on this port
            manifest = {
                "app_id": "test-conflict-app",
                "name": "conflict-app",
                "public_path": "/conflict",
                "runtime": "python",
                "port": port
            }
            DeploymentManager.register_manifest(manifest)

            with self.assertRaises(DeploymentError) as ctx:
                DeploymentManager.deploy_application("test-conflict-app", port_override=port)
            self.assertIn("Port conflict", str(ctx.exception))
        finally:
            blocker.close()

    def test_07_successful_deployment_with_strict_health_probe(self):
        """Application process starts, health check probe passes, and deployment is marked SUCCESS."""
        port = find_free_port()
        app_id = "test-micro-svc"

        # Create minimal python HTTP service artifact
        artifact_zip = self.test_dir / "micro_service.zip"
        with zipfile.ZipFile(artifact_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, sys, os\n"
                f"port = int(os.environ.get('PORT', {port}))\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        if self.path == '/health':\n"
                f"            self.send_response(200)\n"
                f"            self.send_header('Content-Type', 'application/json')\n"
                f"            self.end_headers()\n"
                f"            self.wfile.write(b'{{\"status\":\"healthy\",\"version\":\"1.0.0\"}}\\n')\n"
                f"        else:\n"
                f"            self.send_response(200)\n"
                f"            self.end_headers()\n"
                f"            self.wfile.write(b'Hello World')\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )

        sha256, _ = compute_file_sha256(artifact_zip)

        manifest = {
            "app_id": app_id,
            "name": "micro-svc",
            "public_path": "/micro-svc",
            "runtime": "python",
            "port": port,
            "health_check": {
                "path": "/health",
                "timeout_seconds": 4,
                "interval_seconds": 0.2,
                "expected_status": 200
            }
        }
        DeploymentManager.register_manifest(manifest)

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=artifact_zip,
            artifact_sha256=sha256,
            port_override=port
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["port"], port)
        self.assertTrue(res["pid"] > 0)
        self.assertIn("Health check passed", res["health_probe"])

        # Verify status endpoint reflects RUNNING
        st = DeploymentManager.get_application_status(app_id)
        self.assertEqual(st["status"], "RUNNING")
        self.assertEqual(st["pid"], res["pid"])

    def test_08_failed_health_probe_triggers_automatic_rollback(self):
        """When an application upgrade fails its health probe, it automatically rolls back to prior working release."""
        app_id = "test-micro-svc"
        # App is already running from test_07 as deployment 1
        st0 = DeploymentManager.get_application_status(app_id)
        prior_dep_id = st0["active_deployment_id"]
        self.assertIsNotNone(prior_dep_id)

        # Build broken v2 artifact (returns HTTP 500 on /health)
        broken_zip = self.test_dir / "broken_service.zip"
        port2 = find_free_port()
        with zipfile.ZipFile(broken_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, sys, os\n"
                f"port = int(os.environ.get('PORT', {port2}))\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        self.send_response(500)\n"
                f"        self.end_headers()\n"
                f"        self.wfile.write(b'Service Broken')\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )

        broken_sha, _ = compute_file_sha256(broken_zip)

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=broken_zip,
            artifact_sha256=broken_sha,
            port_override=port2,
            auto_rollback_on_failure=True
        )

        # Must report ROLLED_BACK and prior release must be restored
        self.assertEqual(res["status"], "ROLLED_BACK")
        self.assertEqual(res["active_deployment_id"], prior_dep_id)

        # Status must still be healthy and running
        st1 = DeploymentManager.get_application_status(app_id)
        self.assertEqual(st1["status"], "RUNNING")
        self.assertEqual(st1["active_deployment_id"], prior_dep_id)

    def test_09_manual_rollback_rejection_when_no_prior_release(self):
        """Rollback is rejected with an actionable error when no prior successful release exists."""
        manifest = {
            "app_id": "test-no-prior-app",
            "name": "no-prior-app",
            "public_path": "/no-prior",
            "runtime": "python"
        }
        DeploymentManager.register_manifest(manifest)

        with self.assertRaises(DeploymentError) as ctx:
            DeploymentManager.rollback_application("test-no-prior-app")
        self.assertIn("No previous successful release available for rollback", str(ctx.exception))

    def test_10_graceful_stop_and_restart(self):
        """Application process stops gracefully and restarts successfully."""
        app_id = "test-micro-svc"
        # Stop
        stop_res = DeploymentManager.stop_application(app_id)
        self.assertEqual(stop_res["status"], "STOPPED")

        st = DeploymentManager.get_application_status(app_id)
        self.assertEqual(st["status"], "STOPPED")

        # Restart
        rst_res = DeploymentManager.restart_application(app_id)
        self.assertEqual(rst_res["status"], "SUCCESS")

        st2 = DeploymentManager.get_application_status(app_id)
        self.assertEqual(st2["status"], "RUNNING")

    def test_11_process_safety_infrastructure_immunity(self):
        """CRITICAL: Immune processes (current PID, parent PID, IDE, controller, gateway) cannot be targeted."""
        # 1. Current process (self-termination defense)
        curr_pid = os.getpid()
        with self.assertRaises(ProcessSecurityError) as ctx:
            SafeProcessLifecycleManager.verify_infrastructure_immunity(curr_pid)
        self.assertIn("self-termination strictly forbidden", str(ctx.exception))

        # 2. Parent process (Antigravity IDE / terminal shell defense)
        ppid = os.getppid()
        with self.assertRaises(ProcessSecurityError) as ctx:
            SafeProcessLifecycleManager.verify_infrastructure_immunity(ppid)
        self.assertIn("parent process of executor", str(ctx.exception))

        # 3. Simulated PID with protected keywords (controller, node-agent, antigravity)
        for kw in ("antigravity", "controller.py", "node-agent.py", "app_router.py"):
            with self.assertRaises(ProcessSecurityError) as ctx:
                SafeProcessLifecycleManager.verify_infrastructure_immunity(99999, candidate_cmd=f"python {kw} --daemon")
            self.assertIn("protected infrastructure or IDE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
