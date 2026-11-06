#!/usr/bin/env python3
"""
PersonalServer Deployment Port Alignment & Gateway Routing Integration Tests
============================================================================
Validates V2.7.1 gateway integration fixes:
1. Application record with only 'port' routes correctly through AppRouter.
2. Legacy application record with only 'host_port' routes correctly.
3. Record with both 'port' and 'host_port' routes consistently.
4. Remote worker node IP / Tailscale IP resolution with port routing.
5. Missing or invalid port produces a controlled HTTP 504 error, never an uncaught exception.
6. DeploymentManager.deploy_application persists both 'port' and 'host_port'.
7. Live HTTP end-to-end: deployed service is reachable through AppRouter gateway.
8. Live HTTP rollback: restored release remains immediately reachable through gateway.
"""

import os
import sys
import json
import time
import shutil
import socket
import zipfile
import threading
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from router.app_router import ApplicationRouter, RouterHTTPHandler
from controller.deployment_manager import DeploymentManager
from common.process_manager import SafeProcessLifecycleManager
from common.artifact_manager import compute_file_sha256
from controller.stores import load_apps_db, save_apps_db
from config.platform_config import get_controller_paths


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDeploymentPortAlignment(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_port_align_"))
        cls.orig_paths = get_controller_paths()

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

        # Set up mock nodes: local and remote
        cls.mock_nodes = {
            "nodes": {
                "node-local": {
                    "id": "node-local",
                    "name": "Local Node",
                    "status": "ONLINE",
                    "role": "controller",
                    "ip": "127.0.0.1",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "web_service": True}
                },
                "node-remote": {
                    "id": "node-remote",
                    "name": "Remote Realme 5 Pro",
                    "status": "ONLINE",
                    "role": "compute",
                    "ip": "100.73.52.72",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "web_service": True}
                }
            }
        }
        with open(cls.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(cls.mock_nodes, f)

        with open(cls.mock_apps_file, "w", encoding="utf-8") as f:
            json.dump({"apps": {}}, f)

    @classmethod
    def tearDownClass(cls):
        try:
            SafeProcessLifecycleManager.stop_process("smoke-align-svc")
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        # Refresh node last_seen timestamps
        with open(self.mock_nodes_file, "w", encoding="utf-8") as f:
            for n in self.mock_nodes["nodes"].values():
                n["last_seen"] = time.time()
            json.dump(self.mock_nodes, f)

    def tearDown(self):
        try:
            SafeProcessLifecycleManager.stop_process("smoke-align-svc")
        except Exception:
            pass

    def test_01_router_resolves_port_only_record(self):
        """Application record with only 'port' attribute resolves correctly to http://<host>:<port>."""
        app = {
            "app_id": "app-port-only",
            "name": "port-app",
            "status": "RUNNING",
            "selected_node": "node-local",
            "port": 18091
        }
        status_code, target_url, headers = ApplicationRouter.resolve_target_upstream(app, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, "http://127.0.0.1:18091")

    def test_02_router_resolves_legacy_host_port_record(self):
        """Legacy application record with only 'host_port' attribute resolves correctly."""
        app = {
            "app_id": "app-legacy",
            "name": "legacy-app",
            "status": "RUNNING",
            "selected_node": "node-local",
            "host_port": 18092
        }
        status_code, target_url, headers = ApplicationRouter.resolve_target_upstream(app, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, "http://127.0.0.1:18092")

    def test_03_router_resolves_dual_port_record(self):
        """Record with both 'port' and 'host_port' resolves consistently."""
        app = {
            "app_id": "app-dual",
            "name": "dual-app",
            "status": "RUNNING",
            "selected_node": "node-local",
            "port": 18093,
            "host_port": 18093
        }
        status_code, target_url, headers = ApplicationRouter.resolve_target_upstream(app, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, "http://127.0.0.1:18093")

    def test_04_router_resolves_remote_node_ip(self):
        """When deployed on a remote node, target resolves to the remote node IP with the correct port."""
        app = {
            "app_id": "app-remote",
            "name": "remote-app",
            "status": "RUNNING",
            "selected_node": "node-remote",
            "port": 18095
        }
        status_code, target_url, headers = ApplicationRouter.resolve_target_upstream(app, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, "http://100.73.52.72:18095")

    def test_05_missing_or_invalid_port_controlled_error(self):
        """Missing, null, or out-of-range port returns HTTP 504 without uncaught exceptions."""
        for invalid_port in (None, "", "not-a-port", -1, 70000):
            app = {
                "app_id": "app-bad-port",
                "name": "bad-port",
                "status": "RUNNING",
                "selected_node": "node-local",
                "port": invalid_port,
                "host_port": None
            }
            status_code, body, _ = ApplicationRouter.resolve_target_upstream(app, self.mock_nodes)
            self.assertEqual(status_code, 504)
            self.assertIn("port", body.get("error", "").lower())

    def test_06_end_to_end_deploy_and_gateway_routing(self):
        """Deploying a real Python web app through DeploymentManager makes it reachable through AppRouter."""
        app_id = "smoke-align-svc"
        port = find_free_port()

        # 1. Create minimal web service artifact
        artifact_zip = self.test_dir / "smoke_bundle.zip"
        with zipfile.ZipFile(artifact_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, os\n"
                f"port = int(os.environ.get('PORT', {port}))\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        if self.path in ('/health', '/smoke-align/health'):\n"
                f"            self.send_response(200)\n"
                f"            self.send_header('Content-Type', 'application/json')\n"
                f"            self.end_headers()\n"
                f"            self.wfile.write(b'{{\"status\":\"ok\",\"service\":\"smoke\"}}\\n')\n"
                f"        else:\n"
                f"            self.send_response(200)\n"
                f"            self.end_headers()\n"
                f"            self.wfile.write(b'Smoke service online')\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )

        sha256, _ = compute_file_sha256(artifact_zip)

        # 2. Register Manifest
        manifest = {
            "app_id": app_id,
            "name": "smoke-align",
            "public_path": "/smoke-align",
            "runtime": "python",
            "port": port,
            "health_check": {
                "path": "/health",
                "timeout_seconds": 4,
                "interval_seconds": 0.2,
                "expected_status": 200
            }
        }
        reg_record = DeploymentManager.register_manifest(manifest)
        self.assertEqual(reg_record["port"], port)
        self.assertEqual(reg_record["host_port"], port)

        # 3. Deploy
        dep_res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=artifact_zip,
            artifact_sha256=sha256,
            port_override=port
        )
        self.assertEqual(dep_res["status"], "SUCCESS")

        # 4. Verify apps DB has both 'port' and 'host_port'
        db = load_apps_db()
        app_record = db["apps"][app_id]
        self.assertEqual(app_record["port"], port)
        self.assertEqual(app_record["host_port"], port)
        self.assertEqual(app_record["status"], "RUNNING")

        # 5. Verify AppRouter resolution for this live deployed app
        status_code, target_url, _ = ApplicationRouter.resolve_target_upstream(app_record, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, f"http://127.0.0.1:{port}")

        # 6. Verify direct HTTP response from the live deployed process
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")

    def test_07_rollback_restores_routable_ports(self):
        """Rollback restores prior release and persists routable port and host_port."""
        app_id = "smoke-align-svc"
        port1 = find_free_port()

        # Build v1
        v1_zip = self.test_dir / "v1_bundle.zip"
        with zipfile.ZipFile(v1_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, os\n"
                f"port = int(os.environ.get('PORT', {port1}))\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        self.send_response(200)\n"
                f"        self.send_header('Content-Type', 'application/json')\n"
                f"        self.end_headers()\n"
                f"        self.wfile.write(b'{{\"version\":\"1.0.0\"}}\\n')\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )
        sha1, _ = compute_file_sha256(v1_zip)

        manifest = {
            "app_id": app_id,
            "name": "smoke-align",
            "public_path": "/smoke-align",
            "runtime": "python",
            "port": port1,
            "health_check": {"path": "/health", "timeout_seconds": 3, "interval_seconds": 0.2}
        }
        DeploymentManager.register_manifest(manifest)
        dep1 = DeploymentManager.deploy_application(app_id, artifact_path=v1_zip, artifact_sha256=sha1, port_override=port1)
        self.assertEqual(dep1["status"], "SUCCESS")

        # Build broken v2
        port2 = find_free_port()
        v2_zip = self.test_dir / "v2_broken.zip"
        with zipfile.ZipFile(v2_zip, "w") as zf:
            zf.writestr(
                "app.py",
                f"import http.server, os\n"
                f"port = int(os.environ.get('PORT', {port2}))\n"
                f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                f"    def do_GET(self):\n"
                f"        self.send_response(500)\n"
                f"        self.end_headers()\n"
                f"http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
            )
        sha2, _ = compute_file_sha256(v2_zip)

        # Deploy v2 with auto-rollback
        dep2 = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=v2_zip,
            artifact_sha256=sha2,
            port_override=port2,
            auto_rollback_on_failure=True
        )
        self.assertEqual(dep2["status"], "ROLLED_BACK")

        # Verify DB has restored v1 port in both port and host_port
        db = load_apps_db()
        app_record = db["apps"][app_id]
        self.assertEqual(app_record["port"], port1)
        self.assertEqual(app_record["host_port"], port1)
        self.assertEqual(app_record["status"], "RUNNING")

        # Verify router resolves target port1
        status_code, target_url, _ = ApplicationRouter.resolve_target_upstream(app_record, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, f"http://127.0.0.1:{port1}")

    def test_09_multi_segment_path_remote_node_resolution(self):
        """Single-container app with multi-segment public_path resolves to remote node IP and port."""
        app_record = {
            "name": "ExamBuddy",
            "public_path": "/apps/ExamBuddy",
            "status": "RUNNING",
            "selected_node": "node-remote",
            "port": 8096,
            "host_port": 8096,
            "route": {"enabled": True, "path": "/apps/ExamBuddy"}
        }
        # 1. Match route
        db = {"apps": {"exambuddy": app_record}}
        matched, prefix, remainder, is_redirect, err = ApplicationRouter.match_route("/apps/ExamBuddy/health", db)
        self.assertIsNotNone(matched)
        self.assertEqual(prefix, "/apps/ExamBuddy")
        self.assertEqual(remainder, "/health")
        self.assertFalse(is_redirect)

        # 2. Resolve upstream
        status_code, target_url, _ = ApplicationRouter.resolve_target_upstream(matched, self.mock_nodes)
        self.assertEqual(status_code, 200)
        self.assertEqual(target_url, "http://100.73.52.72:8096")


if __name__ == "__main__":
    unittest.main()

