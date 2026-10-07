#!/usr/bin/env python3
"""
Test Suite for Phase 11B: Application Deployment Foundation (Docker Runtime)

Validates:
1. Docker capability discovery and isolation
2. Application model, CRUD APIs and safe persistence
3. Port allocator (18000-18999) collision avoidance and reuse
4. Scheduler capability filtering (rejecting non-Docker Android nodes cleanly)
5. Application lifecycle state transitions
6. Safe Docker operations in JobExecutor (argv-based, injection prevention, mount security)
7. API authentication and input validation security boundaries
8. Regression testing across existing cluster endpoints and workloads
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

CONTROLLER_URL = "http://127.0.0.1:8000"
ENROLLMENT_TOKEN_PATH = os.path.join(str(BASE_DIR), "config", "secrets", "enrollment.token")

def get_token():
    if os.path.exists(ENROLLMENT_TOKEN_PATH):
        with open(ENROLLMENT_TOKEN_PATH, "r") as f:
            return f.read().strip()
    return "dev-enrollment-token-insecure"

AUTH_HEADER = {"Authorization": f"Bearer {get_token()}"}

def http_req(path, method="GET", data=None, headers=None):
    url = f"{CONTROLLER_URL}{path}"
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode("utf-8")
            return response.status, json.loads(res_body) if res_body else {}
    except urllib.error.HTTPError as e:
        res_body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(res_body)
        except Exception:
            return e.code, {"error": res_body}

class Phase11BTests(unittest.TestCase):

    def test_01_api_authentication(self):
        """Verify that /apps endpoints reject unauthenticated access."""
        status, data = http_req("/apps", method="GET", headers={})
        self.assertEqual(status, 401, f"Expected 401 unauthenticated, got {status}: {data}")

        status, data = http_req("/apps", method="POST", data={"name": "test", "image": "nginx"}, headers={})
        self.assertEqual(status, 401, f"Expected 401 unauthenticated, got {status}: {data}")

    def test_02_input_validation(self):
        """Verify invalid application names, images, ports, and malicious characters are rejected."""
        # Invalid name with spaces/special characters
        status, data = http_req("/apps", method="POST", data={
            "name": "invalid name with spaces",
            "image": "nginx:latest",
            "container_port": 80
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 400, f"Expected 400 for invalid name, got {status}: {data}")

        # Invalid name with shell injection
        status, data = http_req("/apps", method="POST", data={
            "name": "app;rm -rf /",
            "image": "nginx:latest",
            "container_port": 80
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 400, f"Expected 400 for shell injection in name, got {status}: {data}")

        # Invalid port (< 1 or > 65535)
        status, data = http_req("/apps", method="POST", data={
            "name": "badport-app",
            "image": "nginx:latest",
            "container_port": 70000
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 400, f"Expected 400 for port > 65535, got {status}: {data}")

        # Invalid image name with command injection characters
        status, data = http_req("/apps", method="POST", data={
            "name": "badimage-app",
            "image": "nginx:latest | bash",
            "container_port": 80
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 400, f"Expected 400 for injection in image, got {status}: {data}")

    def test_03_app_crud_and_port_allocation(self):
        """Verify application creation, port allocator (18000-18999), listing, getting, and deletion."""
        # Create App 1
        status, res1 = http_req("/apps", method="POST", data={
            "name": "demo-app-1",
            "image": "python:3.11-slim",
            "container_port": 8080
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 201)
        app1 = res1.get("app", {})
        app1_id = app1.get("app_id")
        port1 = app1.get("port")
        self.assertTrue(18000 <= port1 <= 18999, f"Allocated port {port1} outside 18000-18999")
        self.assertEqual(app1.get("status"), "CREATED")

        # Create App 2 - should receive a distinct port
        status, res2 = http_req("/apps", method="POST", data={
            "name": "demo-app-2",
            "image": "nginx:alpine",
            "container_port": 80
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 201)
        app2 = res2.get("app", {})
        app2_id = app2.get("app_id")
        port2 = app2.get("port")
        self.assertTrue(18000 <= port2 <= 18999)
        self.assertNotEqual(port1, port2, f"Ports collided: {port1} vs {port2}")

        # List Apps
        status, list_res = http_req("/apps", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        app_ids = [a["app_id"] for a in list_res.get("apps", [])]
        self.assertIn(app1_id, app_ids)
        self.assertIn(app2_id, app_ids)

        # Get App 1
        status, get_res = http_req(f"/apps/{app1_id}", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        self.assertEqual(get_res.get("app", {}).get("name"), "demo-app-1")

        # Delete App 1 and App 2
        status, del1 = http_req(f"/apps/{app1_id}", method="DELETE", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        status, del2 = http_req(f"/apps/{app2_id}", method="DELETE", headers=AUTH_HEADER)
        self.assertEqual(status, 200)

        # Verify deleted
        status, get_del = http_req(f"/apps/{app1_id}", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 404)

    def test_04_scheduler_docker_capability_filtering(self):
        """
        Verify that attempting to schedule a Docker container on Android/non-docker nodes
        is cleanly rejected by the scheduler.
        """
        from datetime import datetime, timezone
        from scheduler.scheduler import ResourceScheduler
        android_only_cluster = {
            "nodes": {
                "server-5387a86bf36116b1": {
                    "node_id": "server-5387a86bf36116b1",
                    "name": "vivo-y31",
                    "status": "ONLINE",
                    "capabilities": {"compute": True, "storage": True, "network": True},
                    "last_seen": datetime.now(timezone.utc).isoformat()
                }
            }
        }
        decision = ResourceScheduler.select_node({"capabilities": ["container_runtime:docker"]}, android_only_cluster, timeout_seconds=60)
        self.assertIsNone(decision.get("selected_node"), "Android node should NOT have been selected for Docker workload")
        self.assertIn("container_runtime:docker", decision.get("rejected", {}).get("server-5387a86bf36116b1", ""))

    def test_05_job_executor_security_and_argument_safety(self):
        """
        Directly test JobExecutor safety for docker operations to ensure no shell injection
        or dangerous mounts can occur.
        """
        from agent.job_executor import JobExecutor

        # 1. Reject invalid docker container names
        bad_name_job = {
            "job_id": "test-sec-1",
            "type": "docker-stop",
            "parameters": {"container_name": "app; rm -rf /"}
        }
        res = JobExecutor.execute(bad_name_job)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Invalid container name", res.get("stderr", ""))

        # 2. Reject dangerous host mounts
        bad_mount_job = {
            "job_id": "test-sec-2",
            "type": "docker-deploy",
            "parameters": {
                "image": "python:3.11",
                "container_name": "test-sec-2",
                "volumes": ["/:/rootfs", "/var/run/docker.sock:/var/run/docker.sock"]
            }
        }
        res = JobExecutor.execute(bad_mount_job)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Host volume mounts are not allowed", res.get("stderr", ""))

        # 3. Reject invalid docker image with flags
        bad_img_job = {
            "job_id": "test-sec-3",
            "type": "docker-deploy",
            "parameters": {
                "image": "nginx --privileged --pid=host",
                "container_name": "test-sec-3"
            }
        }
        res = JobExecutor.execute(bad_img_job)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Invalid Docker Image", res.get("stderr", ""))

    def test_06_regression_existing_cluster_and_nodes(self):
        """Verify existing cluster nodes (vivo-y31 and node-02) remain ONLINE and untouched."""
        status, cluster_res = http_req("/cluster", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        nodes = cluster_res.get("nodes", [])
        online_nodes = [n for n in nodes if n.get("status") == "ONLINE"]
        self.assertGreaterEqual(len(online_nodes), 2, f"Expected >= 2 online nodes, got {len(online_nodes)}")
        
        node_names = [n.get("name") for n in online_nodes]
        self.assertIn("vivo-y31", node_names)
        self.assertIn("node-02", node_names)

        # Ensure Docker capability is correctly False on Android nodes
        for node in online_nodes:
            if node.get("name") in ["vivo-y31", "node-02"]:
                caps = node.get("capabilities", {})
                self.assertFalse(caps.get("container_runtime:docker", False), f"Node {node['name']} must not fake Docker support")

    def test_07_regression_standard_jobs(self):
        """Verify standard non-docker workloads continue to execute normally."""
        status, job_res = http_req("/jobs", method="POST", data={
            "type": "echo",
            "parameters": {"message": "Phase 11B Regression Test"},
            "timeout": 15
        }, headers=AUTH_HEADER)
        self.assertIn(status, [200, 201])
        job_id = job_res["job"]["job_id"]
        
        # Verify job is queued or processed
        status, job_check = http_req(f"/jobs/{job_id}", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        self.assertIn(job_check["job"]["status"], ["QUEUED", "CLAIMED", "RUNNING", "SUCCEEDED"])

    def test_08_duplicate_application_name_handling(self):
        """Verify duplicate application names return 409 Conflict."""
        status, res1 = http_req("/apps", method="POST", data={
            "name": "dup-test-app",
            "image": "python:3.11-slim",
            "container_port": 8000
        }, headers=AUTH_HEADER)
        self.assertEqual(status, 201)
        app_id = res1["app"]["app_id"]

        try:
            # Duplicate name creation attempt
            status, res2 = http_req("/apps", method="POST", data={
                "name": "dup-test-app",
                "image": "python:3.11-slim",
                "container_port": 8001
            }, headers=AUTH_HEADER)
            self.assertEqual(status, 409, f"Expected 409 Conflict, got {status}: {res2}")
            self.assertIn("already exists", res2.get("error", ""))
        finally:
            http_req(f"/apps/{app_id}", method="DELETE", headers=AUTH_HEADER)

    def test_09_log_truncation_safety(self):
        """Verify JobExecutor output truncation protects against oversized logs (> 64KB)."""
        from agent.job_executor import truncate_output, MAX_OUTPUT_BYTES

        oversized_text = "A" * (MAX_OUTPUT_BYTES + 5000)
        truncated = truncate_output(oversized_text)
        self.assertLessEqual(len(truncated.encode("utf-8")), MAX_OUTPUT_BYTES + 100)
        self.assertIn("[OUTPUT TRUNCATED]", truncated)

    def test_10_secret_isolation(self):
        """Verify secrets directory content is never exposed in /apps responses."""
        status, res = http_req("/apps", method="GET", headers=AUTH_HEADER)
        self.assertEqual(status, 200)
        raw_json = json.dumps(res)
        token = get_token()
        if len(token) > 8:
            self.assertNotIn(token, raw_json, "Enrollment secret token leaked in /apps output!")

if __name__ == "__main__":
    unittest.main()
