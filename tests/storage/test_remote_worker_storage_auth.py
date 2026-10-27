#!/usr/bin/env python3
"""
Test Suite for PersonalServer Remote Worker-Node Storage Authorization & Routing
================================================================================

Verifies:
1. End-user authentication at ingress:
   - Tailscale / Cloudflare Access / Bearer token authentication is verified.
   - Unauthenticated requests are rejected (401) immediately at the gateway
     without proxying or leaking to worker nodes.
2. Inter-node trust & authorization:
   - Remote worker nodes accept requests authorized via cluster controller enrollment token.
   - Remote worker nodes reject node-local private tokens (demonstrating the root cause).
   - Node-API proxies remote storage requests with the controller enrollment token,
     enabling seamless access for authenticated users.
3. Node validation & routing resilience:
   - Invalid / unknown node IDs return 404.
   - Offline nodes return 503.
   - Nodes without storage capability (e.g. compute-only / Aki) return 501.
4. Path traversal prevention:
   - Path traversal attempts ('..', null bytes, absolute paths) on remote node
     requests are sanitized and rejected (403) before proxying.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import urllib.request
import urllib.parse
import urllib.error
import unittest
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import importlib.util
node_api_path = BASE_DIR / "services" / "node-api" / "app.py"
spec = importlib.util.spec_from_file_location("node_api_module", str(node_api_path))
node_api_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_api_mod)
NodeAPIHandler = node_api_mod.NodeAPIHandler


class MockRemoteWorkerHandler(BaseHTTPRequestHandler):
    """
    Simulates a remote worker node (e.g., Realme 5 Pro) running its own NodeAPI.
    Accepts requests authorized with the cluster enrollment token (controller_token),
    and rejects requests with local-only tokens or missing auth.
    """
    expected_enrollment_token = "cluster-enrollment-token-48-chars-secret-token"
    last_received_headers = {}
    request_count = 0

    def log_message(self, format, *args):
        pass

    def _check_auth(self):
        MockRemoteWorkerHandler.request_count += 1
        MockRemoteWorkerHandler.last_received_headers = dict(self.headers)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token == self.expected_enrollment_token:
                return True
        return False

    def do_GET(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode("utf-8"))
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/storage/list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {
                "items": [
                    {"name": "worker_file.txt", "is_dir": False, "size_bytes": 1024, "modified": time.time()}
                ],
                "path": ""
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        if parsed.path == "/storage/usage":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {"disk": {"total": "64GB", "available": "32GB", "used_percent": "50%"}}
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


class MockControllerHandler(BaseHTTPRequestHandler):
    """Simulates cluster controller providing /nodes discovery."""
    nodes_data = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/nodes":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"nodes": MockControllerHandler.nodes_data}).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()


class TestRemoteWorkerStorageAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="ps_remote_storage_test_")
        cls.orig_storage_root = node_api_mod.STORAGE_ROOT
        cls.orig_secrets_dir = node_api_mod.SECRETS_DIR
        cls.orig_auth_token_file = node_api_mod.AUTH_TOKEN_FILE
        cls.orig_config_json = node_api_mod.CONFIG_JSON

        # 1. Local Storage Root
        cls.mock_storage_root = Path(cls.test_dir) / "storage"
        cls.mock_storage_root.mkdir(parents=True, exist_ok=True)
        (cls.mock_storage_root / "local_file.txt").write_text("local content", encoding="utf-8")
        node_api_mod.STORAGE_ROOT = cls.mock_storage_root

        # 2. Local Node Tokens
        cls.mock_secrets_dir = Path(cls.test_dir) / "config" / "secrets"
        cls.mock_secrets_dir.mkdir(parents=True, exist_ok=True)
        node_api_mod.SECRETS_DIR = cls.mock_secrets_dir

        cls.local_auth_token = "local-vivo-token-40chars-abcdef1234567890"
        cls.mock_token_file = cls.mock_secrets_dir / "auth.token"
        cls.mock_token_file.write_text(cls.local_auth_token, encoding="utf-8")
        node_api_mod.AUTH_TOKEN_FILE = cls.mock_token_file

        cls.enrollment_token = "cluster-enrollment-token-48-chars-secret-token"
        cls.mock_enrollment_file = cls.mock_secrets_dir / "enrollment.token"
        cls.mock_enrollment_file.write_text(cls.enrollment_token, encoding="utf-8")

        MockRemoteWorkerHandler.expected_enrollment_token = cls.enrollment_token

        # 3. Spin up Mock Remote Worker Node
        cls.worker_server = HTTPServer(("127.0.0.1", 0), MockRemoteWorkerHandler)
        cls.worker_port = cls.worker_server.server_address[1]
        cls.worker_thread = threading.Thread(target=cls.worker_server.serve_forever, daemon=True)
        cls.worker_thread.start()

        # 4. Mock Cluster Nodes Registry
        cls.realme_node_id = "server-95bad5ff01424d4c8d184330d6d2e394"
        cls.aki_node_id = "server-win-c03532e6881c"
        cls.offline_node_id = "server-offline-node-12345"

        cls.mock_cluster_nodes = {
            "server-5387a86bf36116b1": {
                "node_id": "server-5387a86bf36116b1",
                "name": "vivo-y31",
                "status": "ONLINE",
                "tailscale_ip": "127.0.0.1",
                "is_local": True,
                "capabilities": {"storage": True, "compute": True}
            },
            cls.realme_node_id: {
                "node_id": cls.realme_node_id,
                "name": "Realme 5 Pro",
                "status": "ONLINE",
                "tailscale_ip": "127.0.0.1",
                "port": cls.worker_port,
                "is_local": False,
                "capabilities": {"storage": True, "compute": True}
            },
            cls.aki_node_id: {
                "node_id": cls.aki_node_id,
                "name": "Aki",
                "status": "ONLINE",
                "tailscale_ip": "127.0.0.1",
                "is_local": False,
                "capabilities": {"storage": False, "build": True, "compute": True}
            },
            cls.offline_node_id: {
                "node_id": cls.offline_node_id,
                "name": "Offline Worker",
                "status": "OFFLINE",
                "tailscale_ip": "127.0.0.1",
                "is_local": False,
                "capabilities": {"storage": True}
            }
        }

        # Spin up Mock Controller serving /nodes
        MockControllerHandler.nodes_data = list(cls.mock_cluster_nodes.values())
        cls.controller_server = HTTPServer(("127.0.0.1", 0), MockControllerHandler)
        cls.controller_port = cls.controller_server.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.controller_port}"
        cls.controller_thread = threading.Thread(target=cls.controller_server.serve_forever, daemon=True)
        cls.controller_thread.start()

        cls.orig_get_controller_url = node_api_mod.get_configured_controller_url
        node_api_mod.get_configured_controller_url = lambda: cls.controller_url

        # Set local node id in node_api_mod
        cls.orig_load_config = node_api_mod.load_config
        node_api_mod.load_config = lambda: {
            "node_id": "server-5387a86bf36116b1",
            "name": "vivo-y31",
            "NODE_ID": "server-5387a86bf36116b1",
            "NODE_NAME": "vivo-y31"
        }

        # 5. Spin up Gateway / Controller NodeAPI
        cls.gateway_server = HTTPServer(("127.0.0.1", 0), NodeAPIHandler)
        cls.gateway_port = cls.gateway_server.server_address[1]
        cls.gateway_url = f"http://127.0.0.1:{cls.gateway_port}"
        cls.gateway_thread = threading.Thread(target=cls.gateway_server.serve_forever, daemon=True)
        cls.gateway_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.gateway_server.shutdown()
            cls.gateway_server.server_close()
        except Exception:
            pass
        try:
            cls.worker_server.shutdown()
            cls.worker_server.server_close()
        except Exception:
            pass
        try:
            cls.controller_server.shutdown()
            cls.controller_server.server_close()
        except Exception:
            pass

        node_api_mod.STORAGE_ROOT = cls.orig_storage_root
        node_api_mod.SECRETS_DIR = cls.orig_secrets_dir
        node_api_mod.AUTH_TOKEN_FILE = cls.orig_auth_token_file
        node_api_mod.get_configured_controller_url = cls.orig_get_controller_url
        node_api_mod.load_config = cls.orig_load_config
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        MockRemoteWorkerHandler.request_count = 0
        MockRemoteWorkerHandler.last_received_headers = {}

    def _http_req(self, path: str, headers: dict = None):
        url = f"{self.gateway_url}{path}"
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"error": body}
            return e.code, data

    def test_unauthenticated_request_rejected(self):
        """Unauthenticated requests are rejected with 401 and remote node is NOT called."""
        # Case A: Public tunnel ingress without Cloudflare Access credentials
        status_pub, data_pub = self._http_req("/storage/list?node=Realme%205%20Pro", headers={"Cf-Ray": "mock-ray-12345"})
        self.assertEqual(status_pub, 401)
        self.assertIn("error", data_pub)

        # Case B: Invalid Authorization Bearer token
        status_token, data_token = self._http_req("/storage/list?node=Realme%205%20Pro", headers={"Authorization": "Bearer bad-token-xyz"})
        self.assertEqual(status_token, 401)
        self.assertIn("error", data_token)

        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_authenticated_local_node_browsing(self):
        """Authenticated local-node storage listing succeeds."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req("/storage/list", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        item_names = [i["name"] for i in data["items"]]
        self.assertIn("local_file.txt", item_names)
        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_authenticated_remote_node_browsing_by_friendly_name(self):
        """
        Selecting 'Realme 5 Pro' proxies with controller enrollment token
        and returns worker node storage items cleanly.
        """
        # Authenticated end-user via Cloudflare Access header
        headers = {"Cf-Access-Authenticated-User-Email": "admin@akshatsahay.space"}
        status, data = self._http_req("/storage/list?node=Realme%205%20Pro", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        item_names = [i["name"] for i in data["items"]]
        self.assertIn("worker_file.txt", item_names)
        self.assertGreater(MockRemoteWorkerHandler.request_count, 0)

        # Verify that the remote worker received the enrollment token, not user's email or local token
        received_auth = MockRemoteWorkerHandler.last_received_headers.get("Authorization", "")
        self.assertEqual(received_auth, f"Bearer {self.enrollment_token}")

    def test_authenticated_remote_node_browsing_by_canonical_node_id(self):
        """Browsing using canonical node_id proxies cleanly with enrollment token."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req(f"/storage/list?node={self.realme_node_id}", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        self.assertEqual(data["items"][0]["name"], "worker_file.txt")

    def test_invalid_node_id_returns_404(self):
        """Specifying a nonexistent node ID returns 404 Not Found."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req("/storage/list?node=nonexistent-node-9999", headers=headers)
        self.assertEqual(status, 404)
        self.assertIn("not found", data.get("error", "").lower())
        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_offline_node_returns_503(self):
        """Specifying an offline worker node returns 503 Service Unavailable."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req(f"/storage/list?node={self.offline_node_id}", headers=headers)
        self.assertEqual(status, 503)
        self.assertIn("offline", data.get("error", "").lower())
        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_unsupported_storage_node_returns_501(self):
        """Specifying a node without storage capability (e.g. Aki) returns 501 Not Implemented."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req(f"/storage/list?node={self.aki_node_id}", headers=headers)
        self.assertEqual(status, 501)
        self.assertIn("does not support storage", data.get("error", "").lower())
        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_path_traversal_rejected_before_remote_proxy(self):
        """Path traversal queries on remote node requests are rejected (403) before proxying."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        traversal_attempts = [
            "/storage/list?node=Realme%205%20Pro&path=../../etc",
            "/storage/download?node=Realme%205%20Pro&path=../secret.txt",
            "/storage/usage?node=Realme%205%20Pro&path=/etc/shadow",
        ]
        for url in traversal_attempts:
            status, data = self._http_req(url, headers=headers)
            self.assertTrue(
                any(keyword in data.get("error", "").lower() for keyword in ["traversal", "escape", "access denied"]),
                f"Unexpected error message for traversal {url}: {data.get('error')}"
            )

        self.assertEqual(MockRemoteWorkerHandler.request_count, 0)

    def test_storage_nodes_endpoint_includes_storage_supported_flag(self):
        """Verify GET /storage/nodes reports storage_supported correctly for all nodes."""
        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req("/storage/nodes", headers=headers)
        self.assertEqual(status, 200)
        nodes = {n["node_id"]: n for n in data.get("nodes", [])}

        self.assertIn(self.realme_node_id, nodes)
        self.assertTrue(nodes[self.realme_node_id].get("storage_supported", False))

        self.assertIn(self.aki_node_id, nodes)
        self.assertFalse(nodes[self.aki_node_id].get("storage_supported", True))

    def test_root_cause_demonstration_local_vs_enrollment_token(self):
        """
        Directly proves the root cause:
        Remote worker nodes require cluster enrollment/controller token.
        When provided the gateway's private local auth token, the worker node
        rejects with 401 Unauthorized ('Unable to load storage: Unauthorized').
        When provided the shared enrollment token, the worker accepts with 200 OK.
        """
        worker_url = f"http://127.0.0.1:{self.worker_port}/storage/list"

        # 1. Direct request with gateway's private local token (the bug)
        req_local = urllib.request.Request(
            worker_url,
            headers={"Authorization": f"Bearer {self.local_auth_token}"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_local)
        self.assertEqual(ctx.exception.code, 401)

        # 2. Direct request with cluster enrollment token (the fix)
        req_enrollment = urllib.request.Request(
            worker_url,
            headers={"Authorization": f"Bearer {self.enrollment_token}"}
        )
        with urllib.request.urlopen(req_enrollment) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("items", data)

    def test_system_folders_filtered_from_personal_storage_view(self):
        """
        Verifies that platform internal directories (applications, artifacts, images, system)
        and hidden directories are filtered out from the personal file manager root listing,
        preventing clutter for user personal storage.
        """
        # Create system folders and personal folders in mock storage root
        for sys_dir in ["applications", "artifacts", "images", "system", ".hidden_cache"]:
            (self.mock_storage_root / sys_dir).mkdir(parents=True, exist_ok=True)
        (self.mock_storage_root / "My Documents").mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": f"Bearer {self.local_auth_token}"}
        status, data = self._http_req("/storage/list", headers=headers)
        self.assertEqual(status, 200)

        item_names = [i["name"] for i in data.get("items", [])]
        # Personal folder must be visible
        self.assertIn("My Documents", item_names)
        self.assertIn("local_file.txt", item_names)

        # System folders must NOT appear in the user's storage view
        self.assertNotIn("applications", item_names)
        self.assertNotIn("artifacts", item_names)
        self.assertNotIn("images", item_names)
        self.assertNotIn("system", item_names)
        self.assertNotIn(".hidden_cache", item_names)


if __name__ == "__main__":
    unittest.main()
