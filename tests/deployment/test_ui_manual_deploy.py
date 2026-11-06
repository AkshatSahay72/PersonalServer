#!/usr/bin/env python3
"""
Test Suite for PersonalServer Manual UI Deployment & Release Rollback
=====================================================================
Covers Phase 2 requirements:
1. Manual deployment with custom branch and exact commit SHA.
2. Provenance recording (commit, branch, trigger, node, artifact checksum).
3. Explicit node selection policy (node_override).
4. Release rollback preserving previous healthy releases.
5. Failed health check auto-rollback without destroying prior healthy release.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.artifact_manager import compute_file_sha256
from common.process_manager import SafeProcessLifecycleManager
from config.platform_config import get_controller_paths
from controller.deployment_manager import DeploymentError, DeploymentManager
from controller.stores import load_apps_db, save_apps_db


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestUIManualDeploy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_ui_deploy_"))
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

        nodes_db = {
            "nodes": {
                "server-android-realme5pro": {
                    "id": "server-android-realme5pro",
                    "hostname": "Realme 5 Pro",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "web_service": True}
                },
                "server-android-vivoy31": {
                    "id": "server-android-vivoy31",
                    "hostname": "Vivo Y31",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "web_service": True}
                }
            }
        }
        with open(cls.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(nodes_db, f)

        with open(cls.mock_apps_file, "w", encoding="utf-8") as f:
            json.dump({"apps": {}}, f)

    def setUp(self):
        # Keep mock nodes fresh for scheduler timeout checks
        with open(self.mock_nodes_file, "r", encoding="utf-8") as f:
            nodes_db = json.load(f)
        for n in nodes_db.get("nodes", {}).values():
            n["last_seen"] = time.time()
        with open(self.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(nodes_db, f)

    @classmethod
    def tearDownClass(cls):
        try:
            SafeProcessLifecycleManager.stop_process("test-manual-app")
        except Exception:
            pass
        if cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _create_app_artifact(self, app_id: str, version_tag: str, is_healthy: bool = True) -> tuple[Path, str]:
        pkg_dir = self.test_dir / f"pkg_{version_tag}"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        code_file = pkg_dir / "app.py"

        code = (
            "import http.server, socketserver, os, json\n"
            f"VERSION = '{version_tag}'\n"
            f"IS_HEALTHY = {str(is_healthy)}\n"
            "class ReusableServer(http.server.HTTPServer):\n"
            "    allow_reuse_address = True\n"
            "class Handler(http.server.SimpleHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        if self.path in ('/health', '/api/health'):\n"
            "            if not IS_HEALTHY:\n"
            "                self.send_response(500)\n"
            "                self.end_headers()\n"
            "                return\n"
            "            self.send_response(200)\n"
            "            self.send_header('Content-Type', 'application/json')\n"
            "            self.end_headers()\n"
            "            self.wfile.write(json.dumps({'status': 'healthy', 'version': VERSION}).encode('utf-8'))\n"
            "        else:\n"
            "            self.send_response(200)\n"
            "            self.end_headers()\n"
            "            self.wfile.write(b'OK')\n"
            "port = int(os.environ.get('PORT', 8095))\n"
            "ReusableServer(('127.0.0.1', port), Handler).serve_forever()\n"
        )
        code_file.write_text(code, encoding="utf-8")

        zip_path = self.test_dir / f"{app_id}_{version_tag}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(code_file, arcname="app.py")

        sha256_digest, _ = compute_file_sha256(zip_path)
        return zip_path, sha256_digest

    def test_01_manual_deploy_with_exact_commit_and_node(self):
        """Operator can deploy an application with exact commit SHA and targeted node policy."""
        app_id = "test-manual-app"
        port = find_free_port()
        manifest = {
            "app_id": app_id,
            "name": "manual-app",
            "public_path": "/manual-app",
            "runtime": "python",
            "port": port,
            "health_check": {
                "path": "/health",
                "timeout_seconds": 4.0,
                "interval_seconds": 0.2,
                "expected_status": 200
            }
        }
        DeploymentManager.register_manifest(manifest)

        zip_v1, sha_v1 = self._create_app_artifact(app_id, "v1", is_healthy=True)
        exact_commit = "9a7f3c1b82d4e6501f2e3a4b5c6d7e8f9a0b1c2d"

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_v1,
            artifact_sha256=sha_v1,
            port_override=port,
            node_override="server-android-realme5pro",
            commit=exact_commit,
            branch="main",
            trigger="manual_ui"
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["commit"], exact_commit)
        self.assertEqual(res["branch"], "main")
        self.assertEqual(res["trigger"], "manual_ui")
        self.assertEqual(res["node"], "server-android-realme5pro")
        self.assertEqual(res["artifact_sha256"], sha_v1)

        db = load_apps_db()
        app = db["apps"][app_id]
        self.assertEqual(app["status"], "RUNNING")
        self.assertEqual(len(app["deployments"]), 1)
        dep0 = app["deployments"][0]
        self.assertEqual(dep0["commit"], exact_commit)
        self.assertEqual(dep0["trigger"], "manual_ui")
        self.assertEqual(dep0["node"], "server-android-realme5pro")

    def test_02_deploy_v2_and_rollback_through_history(self):
        """Deploying a second release preserves the first, and rollback restores the first."""
        app_id = "test-manual-app"
        db = load_apps_db()
        v1_dep_id = db["apps"][app_id]["active_deployment_id"]
        port = db["apps"][app_id]["port"]

        zip_v2, sha_v2 = self._create_app_artifact(app_id, "v2", is_healthy=True)
        v2_commit = "b4e8c1a93f5d720468e21a3b4c5d6e7f8a9b0c1e"

        res_v2 = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_v2,
            artifact_sha256=sha_v2,
            port_override=port,
            node_override="server-android-vivoy31",
            commit=v2_commit,
            branch="feature/v2",
            trigger="manual_ui"
        )
        self.assertEqual(res_v2["status"], "SUCCESS")
        v2_dep_id = res_v2["deployment_id"]

        db = load_apps_db()
        app = db["apps"][app_id]
        self.assertEqual(app["active_deployment_id"], v2_dep_id)
        self.assertEqual(len(app["deployments"]), 2)

        # Rollback to v1 release
        rb_res = DeploymentManager.rollback_application(app_id, target_deployment_id=v1_dep_id)
        self.assertEqual(rb_res["status"], "ROLLED_BACK")
        self.assertEqual(rb_res["restored_deployment_id"], v1_dep_id)

        db_after = load_apps_db()
        self.assertEqual(db_after["apps"][app_id]["active_deployment_id"], v1_dep_id)
        self.assertEqual(db_after["apps"][app_id]["status"], "RUNNING")

    def test_03_broken_deploy_auto_rolls_back_without_destroying_v1(self):
        """When a new deployment fails health check, it automatically rolls back to the prior healthy release."""
        app_id = "test-manual-app"
        db = load_apps_db()
        healthy_dep_id = db["apps"][app_id]["active_deployment_id"]
        port = db["apps"][app_id]["port"]

        zip_broken, sha_broken = self._create_app_artifact(app_id, "v3_broken", is_healthy=False)

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_broken,
            artifact_sha256=sha_broken,
            port_override=port,
            commit="broken_sha_1234",
            branch="broken-branch",
            trigger="manual_ui",
            auto_rollback_on_failure=True
        )

        self.assertEqual(res["status"], "ROLLED_BACK")
        self.assertEqual(res["active_deployment_id"], healthy_dep_id)

        db_after = load_apps_db()
        app = db_after["apps"][app_id]
        self.assertEqual(app["status"], "RUNNING")
        self.assertEqual(app["active_deployment_id"], healthy_dep_id)


if __name__ == "__main__":
    unittest.main()
