#!/usr/bin/env python3
"""
Phase 11D — Docker Runtime & Real Application Deployment Tests
============================================================
Validates end-to-end containerized application lifecycle:
- Capability probing & Node 03 registration with container_runtime:docker
- Scheduler capability filtering (rejecting non-docker nodes)
- Application creation, port allocation, deployment to Docker runtime
- Live HTTP validation on allocated host port
- Log retrieval, stop, restart, deletion, and failure recovery
- Heterogeneous cluster integrity (Node 01/Node 02 remain non-docker)
"""

import sys
import os
import json
import time
import unittest
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scheduler.scheduler import ResourceScheduler
from controller.controller import load_nodes_db, load_apps_db
from agent.job_executor import JobExecutor, ALLOWLISTED_WORKLOADS


class TestPhase11DDockerRuntime(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.token_file = BASE_DIR / "config" / "secrets" / "enrollment.token"
        cls.token = cls.token_file.read_text().strip() if cls.token_file.exists() else ""
        cls.controller_url = "http://127.0.0.1:8000"

    @classmethod
    def _is_controller_running(cls):
        try:
            req = urllib.request.Request(f"{cls.controller_url}/health", headers={"User-Agent": "HealthCheck"})
            with urllib.request.urlopen(req, timeout=1) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _api_request(self, path, method="GET", data=None):
        if not self._is_controller_running():
            raise unittest.SkipTest(f"PersonalServer controller not running at {self.controller_url}. Skipping live cluster test.")
        url = f"{self.controller_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Phase11D-Tester/1.0"
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}

    def test_01_docker_engine_available_and_allowlist(self):
        """Verify Docker workload allowlist contains all runtime operations."""
        for workload in ["docker-deploy", "docker-stop", "docker-restart", "docker-remove", "docker-logs"]:
            self.assertIn(workload, ALLOWLISTED_WORKLOADS)
            self.assertTrue(JobExecutor.is_allowed(workload))

    def test_02_cluster_heterogeneity_and_capabilities(self):
        """Verify Node 01 & 02 are non-Docker while Node 03 has container_runtime:docker."""
        status, data = self._api_request("/nodes")
        self.assertEqual(status, 200)
        nodes = {n["node_id"]: n for n in data.get("nodes", [])}
        
        # Verify Node 01 (vivo-y31) is non-docker
        node01 = next((n for n in nodes.values() if "vivo" in n.get("name", "").lower()), None)
        if node01:
            self.assertFalse(node01.get("capabilities", {}).get("container_runtime:docker", False))
            self.assertEqual(node01.get("status"), "ONLINE")

        # Verify Node 02 (node-02) is non-docker
        node02 = next((n for n in nodes.values() if "node-02" in n.get("name", "").lower()), None)
        if node02:
            self.assertFalse(node02.get("capabilities", {}).get("container_runtime:docker", False))
            self.assertEqual(node02.get("status"), "ONLINE")

        # Verify Node 03 (Aki / Windows/Linux compute) has container_runtime:docker
        docker_nodes = [n for n in nodes.values() if n.get("capabilities", {}).get("container_runtime:docker") is True]
        self.assertGreaterEqual(len(docker_nodes), 1)
        self.assertEqual(docker_nodes[0].get("status"), "ONLINE")

    def test_03_scheduler_capability_filtering(self):
        """Verify scheduler filters out non-docker nodes when container_runtime:docker is required."""
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        test_cluster = {
            "nodes": {
                "server-f8b1485ecf458f74": {
                    "node_id": "server-f8b1485ecf458f74",
                    "name": "worker-docker-01",
                    "status": "ONLINE",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True, "container_runtime:docker": True},
                    "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 32}
                },
                "server-5387a86bf36116b1": {
                    "node_id": "server-5387a86bf36116b1",
                    "name": "vivo-y31",
                    "status": "ONLINE",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True},
                    "resources": {"cpu_cores": 8, "ram_mb": 5697, "storage_gb": 106}
                }
            }
        }
        reqs = {
            "capabilities": ["container_runtime:docker"],
            "min_cpu_cores": 1,
            "min_ram_mb": 128
        }
        decision = ResourceScheduler.select_node(reqs, test_cluster, timeout_seconds=60)
        self.assertIsNotNone(decision.get("selected_node"))
        selected_node_id = decision["selected_node"]
        self.assertEqual(selected_node_id, "server-f8b1485ecf458f74")

        # Verify selected node has docker capability
        node_record = test_cluster["nodes"][selected_node_id]
        self.assertTrue(node_record.get("capabilities", {}).get("container_runtime:docker"))

        # Verify non-docker nodes are in rejected dict with missing capability reason
        self.assertIn("server-5387a86bf36116b1", decision.get("rejected", {}))
        self.assertIn("container_runtime:docker", decision.get("rejected", {}).get("server-5387a86bf36116b1", ""))

    def test_04_docker_security_restrictions(self):
        """Verify job executor rejects volume mounts and unsafe parameters."""
        job = {
            "job_id": "test-sec-job",
            "type": "docker-deploy",
            "parameters": {
                "image": "personalserver/demo-app:v1",
                "volumes": ["/etc:/host_etc"],
                "host_port": 18099,
                "container_port": 8000
            }
        }
        res = JobExecutor.execute(job)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Host volume mounts are not allowed", res["stderr"])

    def test_05_application_lifecycle_and_http(self):
        """Test full app lifecycle: create, deploy, live HTTP 200, logs, stop, restart, delete."""
        app_name = f"phase11d-test-{int(time.time())}"
        create_payload = {
            "name": app_name,
            "image": "personalserver/demo-app:v1",
            "container_port": 8000,
            "env": {"TEST_VAR": "PersonalServerPhase11D"}
        }

        # 1. Create app
        status, app_res = self._api_request("/apps", method="POST", data=create_payload)
        self.assertEqual(status, 201)
        app_id = app_res["app_id"]
        host_port = app_res["app"]["host_port"]
        self.assertGreaterEqual(host_port, 18000)
        self.assertLessEqual(host_port, 18999)

        try:
            # 2. Deploy app
            status, dep_res = self._api_request(f"/apps/{app_id}/deploy", method="POST", data={})
            self.assertEqual(status, 200)
            self.assertEqual(dep_res["status"], "DEPLOYING")

            # 3. Poll for status == RUNNING
            max_wait = 15
            running = False
            for _ in range(max_wait):
                time.sleep(1)
                status, cur_app = self._api_request(f"/apps/{app_id}")
                if cur_app.get("app", {}).get("status") == "RUNNING":
                    running = True
                    break
            self.assertTrue(running, f"Application did not reach RUNNING status within {max_wait}s")

            # 4. Query live HTTP endpoint (poll until container HTTP server accepts connection)
            http_url = f"http://127.0.0.1:{host_port}/"
            live = False
            body = ""
            for _ in range(15):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(http_url, timeout=2) as http_resp:
                        if http_resp.status == 200:
                            body = http_resp.read().decode("utf-8")
                            live = True
                            break
                except Exception:
                    pass
            self.assertTrue(live, f"Container HTTP endpoint on port {host_port} failed to respond")
            self.assertIn("PersonalServer", body)
            self.assertIn("Application is running", body)

            # 5. Fetch logs via API
            status, logs_res = self._api_request(f"/apps/{app_id}/logs")
            self.assertEqual(status, 200)
            self.assertEqual(logs_res["status"], "RUNNING")
            self.assertIsNotNone(logs_res.get("logs"))

            # 6. Stop app
            status, stop_res = self._api_request(f"/apps/{app_id}/stop", method="POST", data={})
            self.assertEqual(status, 200)
            self.assertEqual(stop_res["status"], "STOPPED")

            # Poll until container is fully stopped (up to 15s)
            stopped = False
            for _ in range(15):
                time.sleep(1)
                try:
                    urllib.request.urlopen(http_url, timeout=1)
                except Exception:
                    stopped = True
                    break
            self.assertTrue(stopped, "Container HTTP endpoint did not shut down after stop")

            # 7. Restart app
            status, rest_res = self._api_request(f"/apps/{app_id}/restart", method="POST", data={})
            self.assertEqual(status, 200)
            self.assertEqual(rest_res["status"], "RUNNING")

            # Poll until HTTP endpoint responds again (up to 15s)
            restarted = False
            for _ in range(15):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(http_url, timeout=2) as http_resp:
                        if http_resp.status == 200:
                            restarted = True
                            break
                except Exception:
                    pass
            self.assertTrue(restarted, "Container HTTP endpoint did not resume after restart")

        finally:
            # 8. Clean up / delete app
            status, del_res = self._api_request(f"/apps/{app_id}", method="DELETE")
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
