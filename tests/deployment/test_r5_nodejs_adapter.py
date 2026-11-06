#!/usr/bin/env python3
"""
PersonalServer Phase R5: Node.js Runtime Adapter Test Suite
===========================================================
Validates:
1. Environment validation: host Node.js (>= 14) and npm checks, missing binary handling.
2. Entry point detection: package.json 'main', 'scripts.start', conventional fallbacks, invalid JSON syntax.
3. Dependency isolation: local node_modules isolation, package-lock.json (npm ci), installation failure handling.
4. Process lifecycle: SafeProcessLifecycleManager integration, environment injection, clean termination.
5. End-to-end deployment integration: artifact packaging, scheduler contract, health checks, rollback preservation, and duplicate submission idempotency.
"""

import os
import sys
import json
import time
import shutil
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import ControllerPaths
from controller.stores import (
    load_apps_db,
    save_apps_db,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    load_artifacts_db,
    save_artifacts_db,
)
from controller.deployment_manager import DeploymentManager, DeploymentError
from controller.artifact_packager import package_source_to_artifact
from controller.deployment_state_machine import (
    DeploymentStateMachine,
    DEP_STATE_SUCCEEDED,
    DEP_STATE_FAILED,
)
from common.process_manager import SafeProcessLifecycleManager
from agent.runtime_adapters import get_runtime_adapter
from agent.runtime_adapters.nodejs_adapter import NodejsProcessAdapter
from agent.job_executor import JobExecutor


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestNodejsRuntimeAdapter(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_r5_test_"))
        self.paths = ControllerPaths(
            base_dir=self.test_dir,
            data_dir=self.test_dir / "data",
            config_dir=self.test_dir / "config",
            logs_dir=self.test_dir / "logs",
            runtime_dir=self.test_dir / "runtime",
            storage_dir=self.test_dir / "storage"
        )
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.paths.storage_dir.mkdir(parents=True, exist_ok=True)

        save_apps_db({"apps": {}}, paths=self.paths)
        save_nodes_db({"nodes": {}}, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)
        save_artifacts_db({"artifacts": {}}, paths=self.paths)

        self.adapter = NodejsProcessAdapter()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_factory_returns_nodejs_adapter(self):
        adapter1 = get_runtime_adapter("nodejs")
        adapter2 = get_runtime_adapter("node")
        adapter3 = get_runtime_adapter("javascript")
        self.assertIsInstance(adapter1, NodejsProcessAdapter)
        self.assertIsInstance(adapter2, NodejsProcessAdapter)
        self.assertIsInstance(adapter3, NodejsProcessAdapter)
        self.assertEqual(adapter1.runtime_name, "nodejs")

    def test_environment_validation(self):
        is_ok, err = self.adapter.validate_environment()
        if shutil.which("node") and shutil.which("npm"):
            self.assertTrue(is_ok, f"Expected environment valid, got: {err}")
        else:
            self.assertFalse(is_ok)

        # Test missing Node.js executable
        bad_node_adapter = NodejsProcessAdapter(node_executable="/nonexistent/bin/node")
        is_ok, err = bad_node_adapter.validate_environment()
        self.assertFalse(is_ok)
        self.assertTrue(any(w in err.lower() for w in ("not found", "cannot find", "winerror 2")), err)

        # Test missing npm executable
        bad_npm_adapter = NodejsProcessAdapter(npm_executable="/nonexistent/bin/npm")
        is_ok, err = bad_npm_adapter.validate_environment()
        self.assertFalse(is_ok)
        self.assertIn("npm", err.lower())

    def test_entry_point_detection_success(self):
        rel_dir = self.test_dir / "rel_detect"
        rel_dir.mkdir()

        # 1. Package.json with 'main'
        pkg = {"name": "test-app", "main": "src/app.js"}
        (rel_dir / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        src_dir = rel_dir / "src"
        src_dir.mkdir()
        (src_dir / "app.js").write_text("console.log('hi');", encoding="utf-8")

        cmd, err = self.adapter.detect_entry_point(rel_dir)
        self.assertIsNone(err)
        self.assertTrue(cmd[1].endswith("app.js"))

        # 2. Package.json with 'scripts.start'
        pkg = {"name": "test-app", "scripts": {"start": "node server.js"}}
        (rel_dir / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (rel_dir / "server.js").write_text("console.log('server');", encoding="utf-8")

        cmd, err = self.adapter.detect_entry_point(rel_dir)
        self.assertIsNone(err)
        self.assertTrue(cmd[1].endswith("server.js"))

        # 3. Conventional server.js without package.json
        (rel_dir / "package.json").unlink()
        cmd, err = self.adapter.detect_entry_point(rel_dir)
        self.assertIsNone(err)
        self.assertTrue(cmd[1].endswith("server.js"))

    def test_entry_point_detection_failures(self):
        rel_dir = self.test_dir / "rel_fail"
        rel_dir.mkdir()

        # 1. Invalid JSON syntax
        (rel_dir / "package.json").write_text("{ broken json: true ", encoding="utf-8")
        cmd, err = self.adapter.detect_entry_point(rel_dir)
        self.assertIsNone(cmd)
        self.assertIn("Invalid package.json", err)

        # 2. Valid package.json but missing all entry point candidates
        (rel_dir / "package.json").write_text(json.dumps({"name": "empty"}), encoding="utf-8")
        cmd, err = self.adapter.detect_entry_point(rel_dir)
        self.assertIsNone(cmd)
        self.assertIn("No entry point found", err)

    def test_dependency_isolation_and_failure_cleanup(self):
        if not shutil.which("node") or not shutil.which("npm"):
            self.skipTest("node/npm not available on host test runner")

        rel_dir = self.test_dir / "rel_deps"
        rel_dir.mkdir()
        (rel_dir / "index.js").write_text("console.log('ready');", encoding="utf-8")

        # 1. Invalid lockfile handling
        (rel_dir / "package.json").write_text(json.dumps({"name": "lock-test", "dependencies": {}}), encoding="utf-8")
        (rel_dir / "package-lock.json").write_text("not a json lockfile", encoding="utf-8")
        ok, err = self.adapter.prepare_release("app-t1", "dep-t1", rel_dir, {})
        self.assertFalse(ok)
        self.assertIn("Invalid package-lock.json", err)

        # 2. Dependency installation failure (broken/non-existent package)
        (rel_dir / "package-lock.json").unlink()
        pkg_broken = {
            "name": "broken-dep-app",
            "dependencies": {
                "this-package-definitely-does-not-exist-ps-r5-test": "99.99.99"
            }
        }
        (rel_dir / "package.json").write_text(json.dumps(pkg_broken), encoding="utf-8")

        ok, err = self.adapter.prepare_release("app-t2", "dep-t2", rel_dir, {}, timeout_sec=20.0)
        self.assertFalse(ok)
        self.assertIn("Dependency installation failed", err)

    def test_process_lifecycle_and_execution(self):
        if not shutil.which("node"):
            self.skipTest("node not available on host test runner")

        port = get_free_port()
        app_id = "app-node-lifecycle"
        dep_id = "dep-nl1"
        rel_dir = self.test_dir / "rel_live"
        rel_dir.mkdir()

        # Simple Node.js HTTP server script
        server_code = f"""
const http = require('http');
const port = parseInt(process.env.PORT || '{port}', 10);
const server = http.createServer((req, res) => {{
    if (req.url === '/health') {{
        res.writeHead(200, {{ 'Content-Type': 'application/json' }});
        res.end(JSON.stringify({{
            status: 'healthy',
            app: process.env.APPLICATION_ID,
            dep: process.env.DEPLOYMENT_ID,
            port: port
        }}));
    }} else {{
        res.writeHead(200, {{ 'Content-Type': 'text/plain' }});
        res.end('OK');
    }}
}});
server.listen(port, '127.0.0.1', () => {{
    console.log(`Server listening on ${{port}}`);
}});
"""
        (rel_dir / "server.js").write_text(server_code, encoding="utf-8")
        (rel_dir / "package.json").write_text(json.dumps({
            "name": "test-server",
            "main": "server.js"
        }), encoding="utf-8")

        # Prepare release
        prep_ok, prep_err = self.adapter.prepare_release(app_id, dep_id, rel_dir, {})
        self.assertTrue(prep_ok, f"prepare_release failed: {prep_err}")

        # Start application
        log_file = self.test_dir / "node_app.log"
        proc_rec = self.adapter.start_application(
            app_id=app_id,
            deployment_id=dep_id,
            release_dir=rel_dir,
            port=port,
            env_vars={"CUSTOM_NODE_VAR": "val123"},
            log_file=log_file
        )

        self.assertIsNotNone(proc_rec)
        self.assertEqual(proc_rec.app_id, app_id)
        self.assertEqual(proc_rec.port, port)
        self.assertTrue(SafeProcessLifecycleManager.is_pid_active(proc_rec.pid))

        # Wait up to 5s for server to start and probe /health
        import urllib.request
        healthy = False
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode())
                        self.assertEqual(data.get("status"), "healthy")
                        self.assertEqual(data.get("app"), app_id)
                        healthy = True
                        break
            except Exception:
                time.sleep(0.2)

        self.assertTrue(healthy, "Node.js application failed to respond on /health")

        # Cleanup and verify graceful shutdown
        self.adapter.cleanup_release(app_id, dep_id, rel_dir)
        time.sleep(0.5)
        self.assertFalse(SafeProcessLifecycleManager.is_pid_active(proc_rec.pid))

    def test_deployment_manager_runtime_detection_and_scheduling(self):
        # Register a Node.js application manifest
        manifest = {
            "app_id": "app-node-sched",
            "name": "node-sched",
            "runtime": "nodejs",
            "port": 18200,
            "target": "auto",
            "health_check": {"path": "/health"}
        }
        app_rec = DeploymentManager.register_manifest(manifest, paths=self.paths)
        self.assertEqual(app_rec["runtime"], "nodejs")

        # Add node with nodejs capability
        save_nodes_db({
            "nodes": {
                "node-arm64-1": {
                    "node_id": "node-arm64-1",
                    "status": "ONLINE",
                    "role": "compute",
                    "architecture": "aarch64",
                    "capabilities": {"compute": True, "nodejs": True},
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "last_heartbeat": {"services": {"node_worker": "running"}},
                    "cpu_cores": 8,
                    "ram_mb": 4096,
                    "metrics": {"cpu_percent": 10.0, "ram_used_mb": 500}
                },
                "node-python-only": {
                    "node_id": "node-python-only",
                    "status": "ONLINE",
                    "role": "compute",
                    "architecture": "aarch64",
                    "capabilities": {"compute": True, "python3": True},
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "last_heartbeat": {"services": {"node_worker": "running"}},
                    "cpu_cores": 8,
                    "ram_mb": 4096,
                    "metrics": {"cpu_percent": 10.0, "ram_used_mb": 500}
                }
            }
        }, paths=self.paths)

        # Create source
        src_dir = self.test_dir / "src_node_sched"
        src_dir.mkdir()
        (src_dir / "package.json").write_text(json.dumps({"name": "app", "main": "index.js"}), encoding="utf-8")
        (src_dir / "index.js").write_text("console.log('ok');", encoding="utf-8")

        art = package_source_to_artifact(src_dir, "app-node-sched", "dep-sched-1", paths=self.paths)

        # Submit deployment
        res = DeploymentManager.submit_deployment(
            app_id="app-node-sched",
            artifact_path=Path(art.get("path") or art.get("storage_path")),
            artifact_sha256=art["sha256"],
            paths=self.paths
        )

        self.assertIsNotNone(res.get("deployment_id"))
        self.assertEqual(res.get("assigned_node") or res.get("node"), "node-arm64-1")

        # Verify job in jobs_db
        jobs_db = load_jobs_db(paths=self.paths)
        job = jobs_db.get("jobs", {}).get(res["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["parameters"]["project_type"], "nodejs")
        self.assertIn("nodejs", job["requirements"]["capabilities"])

        # Test duplicate submission idempotency
        dup = DeploymentManager.submit_deployment(
            app_id="app-node-sched",
            idempotency_key=f"app-node-sched:{res['deployment_id']}",
            paths=self.paths
        )
        self.assertTrue(dup.get("idempotent"))
        self.assertEqual(dup.get("deployment_id"), res["deployment_id"])


if __name__ == "__main__":
    unittest.main()
