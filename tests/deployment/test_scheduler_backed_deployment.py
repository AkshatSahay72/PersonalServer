#!/usr/bin/env python3
"""
Unit and Integration Tests for D2: Scheduler-Backed Deployment Foundation
========================================================================
Verifies:
1. Canonical deployment job contract schema and portable specifications.
2. Canonical deployment state machine transitions (legal, illegal, monotonic).
3. Submission interface unification across Dashboard, CLI, REST API, and Webhooks.
4. Existing ResourceScheduler integration selecting capability-compatible Android nodes.
5. Android-native Python deployment execution via Node Agent JobExecutor contract.
6. SafeProcessLifecycleManager process ownership and PID tracking.
7. Durable persistence of job and deployment states in jobs.json and apps.json.
8. Laptop absence resilience (Android-native workloads deploy without Windows laptop or Docker).
9. Atomic health verification: failed deployments trigger automatic rollback and preserve active release.
10. Regression safety: unrelated scheduler workloads (echo, python-script, etc.) remain fully functional.
"""

import os
import sys
import time
import json
import socket
import shutil
import zipfile
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import get_controller_paths
from controller.deployment_job_contract import (
    build_canonical_deployment_job,
    generate_deployment_id,
    generate_deployment_job_id,
    DEPLOYMENT_JOB_TYPE,
    CANONICAL_JOB_SCHEMA_VERSION,
)
from controller.deployment_state_machine import (
    DeploymentStateMachine,
    DeploymentTransitionError,
    DEP_STATE_QUEUED,
    DEP_STATE_FETCHING,
    DEP_STATE_DETECTING,
    DEP_STATE_SCHEDULING,
    DEP_STATE_STARTING,
    DEP_STATE_HEALTH_CHECK,
    DEP_STATE_ACTIVATING,
    DEP_STATE_SUCCEEDED,
    DEP_STATE_FAILED,
)
from controller.deployment_manager import DeploymentManager, DeploymentError
from controller.stores import load_apps_db, save_apps_db, load_nodes_db, save_nodes_db, load_jobs_db, save_jobs_db
from scheduler.scheduler import ResourceScheduler
from agent.job_executor import JobExecutor, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED
from common.process_manager import SafeProcessLifecycleManager
from common.artifact_manager import compute_file_sha256


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSchedulerBackedDeploymentFoundation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_d2_test_"))
        cls.mock_data_dir = cls.test_dir / "data"
        cls.mock_data_dir.mkdir(parents=True, exist_ok=True)

        cls.orig_paths = get_controller_paths()
        cls.mock_apps_file = cls.mock_data_dir / "apps.json"
        cls.mock_backup_apps_file = cls.mock_data_dir / "apps.json.bak"
        cls.mock_nodes_file = cls.mock_data_dir / "nodes.json"
        cls.mock_backup_nodes_file = cls.mock_data_dir / "nodes.json.bak"
        cls.mock_jobs_file = cls.mock_data_dir / "jobs.json"
        cls.mock_backup_jobs_file = cls.mock_data_dir / "jobs.json.bak"

        cls.orig_paths.apps_file = cls.mock_apps_file
        cls.orig_paths.backup_apps_file = cls.mock_backup_apps_file
        cls.orig_paths.nodes_file = cls.mock_nodes_file
        cls.orig_paths.backup_nodes_file = cls.mock_backup_nodes_file
        cls.orig_paths.jobs_file = cls.mock_jobs_file
        cls.orig_paths.backup_jobs_file = cls.mock_backup_jobs_file

        # Realistic cluster representation:
        # Vivo Y31 (ARM64 Android Controller/Compute, python3 ready, no Docker)
        # Realme 5 Pro (ARM64 Android Compute, python3 ready, no Docker)
        # Aki Laptop (x86_64 Windows Build Worker with Docker)
        cls.cluster_nodes = {
            "nodes": {
                "server-android-vivoy31": {
                    "node_id": "server-android-vivoy31",
                    "name": "Vivo Y31",
                    "role": "controller",
                    "platform": "termux",
                    "os": "Linux",
                    "architecture": "aarch64",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "compute": True, "storage": True},
                    "resources": {"cpu_cores": 8, "ram_mb": 5697, "storage_gb": 106},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 8,
                            "cpu_percent": 10.0,
                            "memory_details": {"total_mb": 5697.0, "available_mb": 1500.0, "used_percent": 73.0},
                            "storage_details": {"total_gb": 106.0, "available_gb": 40.0, "used_percent": 62.0}
                        }
                    }
                },
                "server-android-realme5pro": {
                    "node_id": "server-android-realme5pro",
                    "name": "Realme 5 Pro",
                    "role": "compute",
                    "platform": "termux",
                    "os": "Linux",
                    "architecture": "aarch64",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "compute": True, "storage": True},
                    "resources": {"cpu_cores": 8, "ram_mb": 7500, "storage_gb": 64},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 8,
                            "cpu_percent": 5.0,
                            "memory_details": {"total_mb": 7500.0, "available_mb": 3500.0, "used_percent": 53.0},
                            "storage_details": {"total_gb": 64.0, "available_gb": 25.0, "used_percent": 60.0}
                        }
                    }
                },
                "server-win-laptop": {
                    "node_id": "server-win-laptop",
                    "name": "Aki Windows Laptop",
                    "role": "compute",
                    "platform": "windows",
                    "os": "Windows",
                    "architecture": "x86_64",
                    "status": "OFFLINE",  # Simulating laptop offline / disconnected
                    "last_seen": time.time() - 3600,
                    "capabilities": {"container_runtime:docker": True, "docker": True, "python3": True},
                    "resources": {"cpu_cores": 12, "ram_mb": 24000, "storage_gb": 512}
                }
            }
        }

    def setUp(self):
        # Refresh heartbeats
        for n in self.cluster_nodes["nodes"].values():
            if n["status"] == "ONLINE":
                n["last_seen"] = time.time()
        with open(self.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(self.cluster_nodes, f)
        with open(self.mock_apps_file, "w", encoding="utf-8") as f:
            json.dump({"apps": {}}, f)
        with open(self.mock_jobs_file, "w", encoding="utf-8") as f:
            json.dump({"jobs": {}}, f)

    @classmethod
    def tearDownClass(cls):
        try:
            SafeProcessLifecycleManager.stop_process("test-d2-app")
            SafeProcessLifecycleManager.stop_process("test-d2-app-broken")
        except Exception:
            pass
        if cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _create_test_artifact(self, app_id: str, version: str = "v1", is_healthy: bool = True) -> tuple[Path, str]:
        pkg = self.test_dir / f"pkg_{app_id}_{version}"
        pkg.mkdir(parents=True, exist_ok=True)
        py_file = pkg / "app.py"

        code = (
            "import http.server, socketserver, os, json\n"
            f"VERSION = '{version}'\n"
            f"HEALTHY = {str(is_healthy)}\n"
            "class Svr(http.server.HTTPServer):\n"
            "    allow_reuse_address = True\n"
            "class H(http.server.SimpleHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        if self.path in ('/health', '/api/health'):\n"
            "            if not HEALTHY:\n"
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
            "port = int(os.environ.get('PORT', 8090))\n"
            "Svr(('127.0.0.1', port), H).serve_forever()\n"
        )
        py_file.write_text(code, encoding="utf-8")

        zip_p = self.test_dir / f"{app_id}_{version}.zip"
        with zipfile.ZipFile(zip_p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(py_file, arcname="app.py")

        sha, _ = compute_file_sha256(zip_p)
        return zip_p, sha

    def test_01_canonical_job_schema_contract(self):
        """Canonical deployment job adheres to versioned D2 contract and does not leak absolute paths."""
        job = build_canonical_deployment_job(
            app_id="app-exambuddy",
            deployment_id="dep-1234",
            project_type="python",
            artifact_reference="/tmp/local/path/exambuddy-v1.zip",
            artifact_sha256="abc123def456",
            runtime_requirements={"port": 18000, "env": {"ENV_VAR": "value"}},
            health_check={"path": "/health", "timeout_seconds": 15},
            requested_by="dashboard"
        )

        self.assertEqual(job["type"], DEPLOYMENT_JOB_TYPE)
        self.assertEqual(job["schema_version"], CANONICAL_JOB_SCHEMA_VERSION)
        self.assertEqual(job["app_id"], "app-exambuddy")
        self.assertEqual(job["deployment_id"], "dep-1234")
        self.assertEqual(job["status"], "QUEUED")
        self.assertIn("python3", job["requirements"]["capabilities"])
        # Portable artifact reference check (no leading /tmp/local/path/)
        self.assertEqual(job["parameters"]["artifact_reference"], "exambuddy-v1.zip")
        self.assertNotIn("/tmp/local/path", job["parameters"]["artifact_reference"])

    def test_02_deployment_state_machine_legal_and_illegal_transitions(self):
        """State machine validates transitions, enforces monotonic history, and rejects invalid jumps."""
        app_id = "test-sm-app"
        dep_id = "dep-sm-01"

        db = load_apps_db()
        db["apps"][app_id] = {
            "app_id": app_id,
            "name": "SM App",
            "status": "CREATED",
            "deployments": [{
                "deployment_id": dep_id,
                "status": DEP_STATE_QUEUED,
                "history": []
            }]
        }
        save_apps_db(db)

        # Legal progression
        d1 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FETCHING, reason="Fetching git source")
        self.assertEqual(d1["status"], DEP_STATE_FETCHING)

        d2 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_DETECTING)
        self.assertEqual(d2["status"], DEP_STATE_DETECTING)

        d3 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SCHEDULING)
        self.assertEqual(d3["status"], DEP_STATE_SCHEDULING)

        d4 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_STARTING)
        self.assertEqual(d4["status"], DEP_STATE_STARTING)

        d5 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_HEALTH_CHECK)
        self.assertEqual(d5["status"], DEP_STATE_HEALTH_CHECK)

        d6 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_ACTIVATING)
        self.assertEqual(d6["status"], DEP_STATE_ACTIVATING)

        d7 = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SUCCEEDED)
        self.assertEqual(d7["status"], DEP_STATE_SUCCEEDED)

        # Illegal jump: terminal SUCCEEDED cannot jump backwards to QUEUED
        with self.assertRaises(DeploymentTransitionError):
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_QUEUED)

        # Idempotent no-op
        d_same = DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SUCCEEDED)
        self.assertEqual(d_same["status"], DEP_STATE_SUCCEEDED)

        # Verify history is recorded
        db_after = load_apps_db()
        dep = db_after["apps"][app_id]["deployments"][0]
        self.assertGreaterEqual(len(dep["history"]), 6)

    def test_03_scheduler_integration_and_laptop_independence(self):
        """Native Python deployment selects Android node despite Windows laptop being OFFLINE."""
        app_id = "test-d2-app"
        port = get_free_port()
        manifest = {
            "app_id": app_id,
            "name": "d2-app",
            "public_path": "/d2-app",
            "runtime": "python",
            "port": port,
            "health_check": {"path": "/health", "timeout_seconds": 3.0, "interval_seconds": 0.2}
        }
        DeploymentManager.register_manifest(manifest)

        zip_v1, sha_v1 = self._create_test_artifact(app_id, "v1", is_healthy=True)

        # Submit deployment via canonical interface (without node override)
        sub_res = DeploymentManager.submit_deployment(
            app_id=app_id,
            commit="commit-d2-01",
            branch="main",
            trigger="api",
            artifact_path=zip_v1,
            artifact_sha256=sha_v1
        )

        # Verify scheduler selected an Android node (Realme 5 Pro or Vivo Y31), NOT the offline laptop
        assigned_node = sub_res["assigned_node"]
        self.assertIn(assigned_node, ("server-android-realme5pro", "server-android-vivoy31"))
        self.assertNotEqual(assigned_node, "server-win-laptop")

        # Verify job is durably persisted in jobs_db
        jobs_db = load_jobs_db()
        job_id = sub_res["job_id"]
        self.assertIn(job_id, jobs_db["jobs"])
        job_rec = jobs_db["jobs"][job_id]
        self.assertEqual(job_rec["type"], DEPLOYMENT_JOB_TYPE)
        self.assertEqual(job_rec["assigned_node"], assigned_node)
        self.assertIn("python3", job_rec["requirements"]["capabilities"])

    def test_04_end_to_end_native_execution_and_health_activation(self):
        """Full slice: Scheduler -> JobExecutor -> ProcessManager -> Health Check -> Release Activation."""
        app_id = "test-d2-app"
        port = get_free_port()
        manifest = {
            "app_id": app_id,
            "name": "d2-app",
            "public_path": "/d2-app",
            "runtime": "python",
            "port": port,
            "health_check": {"path": "/health", "timeout_seconds": 4.0, "interval_seconds": 0.2}
        }
        DeploymentManager.register_manifest(manifest)

        zip_v1, sha_v1 = self._create_test_artifact(app_id, "v1", is_healthy=True)

        res = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_v1,
            artifact_sha256=sha_v1,
            port_override=port,
            commit="sha-vertical-slice-1",
            branch="main",
            trigger="dashboard"
        )

        self.assertEqual(res["status"], "SUCCESS")
        dep_id = res["deployment_id"]

        # Verify SafeProcessLifecycleManager recorded process ownership
        proc_rec = SafeProcessLifecycleManager.read_record(app_id)
        self.assertIsNotNone(proc_rec)
        self.assertEqual(proc_rec.deployment_id, dep_id)
        self.assertEqual(proc_rec.port, port)
        self.assertTrue(SafeProcessLifecycleManager.is_pid_active(proc_rec.pid))

        # Verify active release in apps_db
        apps_db = load_apps_db()
        app = apps_db["apps"][app_id]
        self.assertEqual(app["status"], "RUNNING")
        self.assertEqual(app["active_deployment_id"], dep_id)
        self.assertEqual(app["port"], port)

        # Clean up process
        SafeProcessLifecycleManager.stop_process(app_id)

    def test_05_failed_health_check_triggers_rollback_preserving_prior_healthy_release(self):
        """Broken release fails health check, rolls back, and leaves previous healthy release active."""
        app_id = "test-d2-app-broken"
        port = get_free_port()
        manifest = {
            "app_id": app_id,
            "name": "d2-app-broken",
            "public_path": "/d2-app-broken",
            "runtime": "python",
            "port": port,
            "health_check": {"path": "/health", "timeout_seconds": 2.0, "interval_seconds": 0.2}
        }
        DeploymentManager.register_manifest(manifest)

        # 1. Deploy healthy v1
        zip_v1, sha_v1 = self._create_test_artifact(app_id, "v1", is_healthy=True)
        res_v1 = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_v1,
            artifact_sha256=sha_v1,
            port_override=port,
            commit="v1-ok"
        )
        self.assertEqual(res_v1["status"], "SUCCESS")
        v1_dep_id = res_v1["deployment_id"]

        # 2. Deploy broken v2 (returns HTTP 500 on /health)
        zip_v2, sha_v2 = self._create_test_artifact(app_id, "v2", is_healthy=False)
        res_v2 = DeploymentManager.deploy_application(
            app_id=app_id,
            artifact_path=zip_v2,
            artifact_sha256=sha_v2,
            port_override=port,
            commit="v2-broken",
            auto_rollback_on_failure=True
        )

        self.assertEqual(res_v2["status"], "ROLLED_BACK")
        self.assertEqual(res_v2["active_deployment_id"], v1_dep_id)

        # Verify apps_db still points to healthy v1
        apps_db = load_apps_db()
        app = apps_db["apps"][app_id]
        self.assertEqual(app["active_deployment_id"], v1_dep_id)
        self.assertEqual(app["status"], "RUNNING")

        SafeProcessLifecycleManager.stop_process(app_id)

    def test_06_unrelated_scheduler_workloads_remain_functional(self):
        """Standard scheduler workloads (echo, python-script, health-check) execute normally via JobExecutor."""
        job_echo = {
            "job_id": "test-job-echo-01",
            "type": "echo",
            "parameters": {"message": "D2 regression check OK"}
        }
        res_echo = JobExecutor.execute(job_echo)
        self.assertEqual(res_echo["status"], JOB_STATE_SUCCEEDED)
        self.assertIn("D2 regression check OK", res_echo["stdout"])

        job_py = {
            "job_id": "test-job-py-01",
            "type": "python-script",
            "parameters": {"code": "print(2 + 2)"}
        }
        res_py = JobExecutor.execute(job_py)
        self.assertEqual(res_py["status"], JOB_STATE_SUCCEEDED)
        self.assertIn("4", res_py["stdout"])


if __name__ == "__main__":
    unittest.main()

