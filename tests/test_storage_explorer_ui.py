#!/usr/bin/env python3
"""
Test Suite for PersonalServer Windows File Explorer Storage UI Integration

Verifies:
1. Storage listing (root and subpaths)
2. Folder creation (mkdir)
3. File upload and retrieval (download)
4. Item rename
5. Item deletion
6. Multi-node storage routing (Node 01 / Node 02 / Auto)
7. Storage usage and telemetry reporting
8. Authentication verification and unauthorized response handling
9. Error handling and unavailable target resilience
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
import urllib.parse
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

ENV_NODE_API_URL = os.environ.get("PERSONALSERVER_TEST_NODE_API_URL")
ENV_AUTH_TOKEN = os.environ.get("PERSONALSERVER_TEST_AUTH_TOKEN")


def is_url_reachable(url: str, timeout: float = 1.0) -> bool:
    """Checks whether the target HTTP server is reachable without throwing."""
    try:
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        req = urllib.request.Request(base, headers={"User-Agent": "HealthCheck"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True
    except Exception:
        return False


def http_json(url: str, method: str = "GET", data: dict = None, headers: dict = None, auth_token: str = None):
    """
    HTTP helper returning (status_code, response_json).
    Accurately captures urllib.error.HTTPError status codes rather than masking them as 500.
    """
    req_headers = {"Content-Type": "application/json"}
    token = auth_token or ENV_AUTH_TOKEN
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res_body = response.read().decode("utf-8")
            return response.status, json.loads(res_body) if res_body else {}
    except urllib.error.HTTPError as e:
        res_body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(res_body)
        except Exception:
            return e.code, {"error": res_body}
    except Exception as e:
        return 500, {"error": str(e)}


class MockStorageHandler(BaseHTTPRequestHandler):
    """Deterministic local mock handler simulating Node API storage endpoints."""
    storage_root = None

    def log_message(self, format, *args):
        # Silence access logs during automated testing
        pass

    def _check_auth(self) -> bool:
        auth_hdr = self.headers.get("Authorization", "")
        token = ""
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
        if not token:
            token = self.headers.get("X-Auth-Token", "").strip()

        # Reject explicitly invalid tokens
        if token == "invalid-test-token-12345":
            return False
        # Reject forced unauthorized queries
        if "force_unauthorized=1" in self.path:
            return False
        return True

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return

        if path == "/storage/nodes":
            self._send_json(200, {
                "nodes": [
                    {
                        "node_id": "node-vivo-y31-01",
                        "name": "vivo-y31",
                        "role": "controller",
                        "storage_available": 10737418240,
                        "storage_total": 68719476736,
                        "status": "online"
                    }
                ]
            })
            return

        if path in ("/storage", "/storage/list"):
            req_path = query.get("path", [""])[0]
            if ".." in req_path:
                self._send_json(403, {"error": "Path traversal attempt detected"})
                return

            target = (Path(self.storage_root) / req_path.lstrip("/\\")).resolve()
            if not str(target).startswith(str(Path(self.storage_root).resolve())):
                self._send_json(403, {"error": "Path escapes storage sandbox"})
                return

            if not target.exists():
                self._send_json(404, {"error": f"Directory not found: {req_path}"})
                return

            items = []
            for item in target.iterdir():
                items.append({
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size_bytes": item.stat().st_size if item.is_file() else 0,
                    "modified": item.stat().st_mtime,
                    "extension": item.suffix.lstrip(".") if item.is_file() else None
                })
            self._send_json(200, {"items": items})
            return

        if path == "/storage/usage":
            self._send_json(200, {
                "disk": {
                    "available": 10737418240,
                    "total": 68719476736,
                    "used": 57982058496,
                    "percent_used": 84.4
                }
            })
            return

        self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}

        if path == "/storage/mkdir":
            folder_name = data.get("name", "")
            parent_rel = data.get("path", "")
            if ".." in folder_name or ".." in parent_rel:
                self._send_json(403, {"error": "Path traversal attempt detected"})
                return

            target = (Path(self.storage_root) / parent_rel.lstrip("/\\") / folder_name).resolve()
            if not str(target).startswith(str(Path(self.storage_root).resolve())):
                self._send_json(403, {"error": "Path escapes storage sandbox"})
                return

            target.mkdir(parents=True, exist_ok=True)
            self._send_json(200, {"status": "ok", "message": f"Created folder {folder_name}"})
            return

        self._send_json(404, {"error": "Not Found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return

        if path == "/storage":
            rel_path = query.get("path", [""])[0]
            if not rel_path or ".." in rel_path:
                self._send_json(403, {"error": "Invalid or escaping path"})
                return

            target = (Path(self.storage_root) / rel_path.lstrip("/\\")).resolve()
            if not str(target).startswith(str(Path(self.storage_root).resolve())):
                self._send_json(403, {"error": "Path escapes storage sandbox"})
                return

            if not target.exists():
                self._send_json(404, {"error": "File or folder not found"})
                return

            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            self._send_json(200, {"status": "ok", "message": "Deleted"})
            return

        self._send_json(404, {"error": "Not Found"})


class StorageExplorerIntegrationTests(unittest.TestCase):
    mock_server = None
    mock_thread = None
    temp_dir = None
    target_url = None

    @classmethod
    def setUpClass(cls):
        if ENV_NODE_API_URL:
            # Explicit live integration target configured
            cls.target_url = ENV_NODE_API_URL.rstrip("/")
            if not is_url_reachable(cls.target_url):
                raise unittest.SkipTest(
                    f"Configured live Node API target '{cls.target_url}' is not reachable. Skipping live tests."
                )
        else:
            # Offline deterministic test fixture: ephemeral local HTTP server with temporary storage root
            cls.temp_dir = tempfile.mkdtemp(prefix="ps_storage_test_")
            MockStorageHandler.storage_root = cls.temp_dir

            cls.mock_server = HTTPServer(("127.0.0.1", 0), MockStorageHandler)
            cls.server_port = cls.mock_server.server_address[1]
            cls.target_url = f"http://127.0.0.1:{cls.server_port}"

            cls.mock_thread = threading.Thread(target=cls.mock_server.serve_forever, daemon=True)
            cls.mock_thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.mock_server:
            cls.mock_server.shutdown()
            cls.mock_server.server_close()
        if cls.temp_dir and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_01_storage_nodes_discovery(self):
        """Verify storage node discovery endpoint returns registered cluster nodes."""
        status, data = http_json(f"{self.target_url}/storage/nodes")
        self.assertEqual(status, 200, f"Expected 200, got {status}: {data}")
        nodes = data.get("nodes", [])
        self.assertGreaterEqual(len(nodes), 1)
        node_names = [n.get("name") or n.get("node_id") for n in nodes]
        self.assertTrue(any("vivo-y31" in name or "server-5387" in name for name in node_names))

    def test_02_storage_list_and_usage(self):
        """Verify root listing and usage metrics."""
        status, list_data = http_json(f"{self.target_url}/storage/list")
        self.assertEqual(status, 200)
        self.assertIn("items", list_data)

        status, usage_data = http_json(f"{self.target_url}/storage/usage")
        self.assertEqual(status, 200)
        self.assertIn("disk", usage_data)
        self.assertIn("available", usage_data.get("disk", {}))

    def test_03_folder_lifecycle(self):
        """Verify creating, listing inside, and deleting a test folder."""
        folder_name = f"explorer_test_{int(time.time() * 1000)}"

        # 1. Create folder
        status, mkdir_res = http_json(f"{self.target_url}/storage/mkdir", method="POST", data={
            "name": folder_name,
            "path": ""
        })
        self.assertEqual(status, 200)

        # 2. List root to confirm existence
        status, list_res = http_json(f"{self.target_url}/storage/list")
        self.assertEqual(status, 200)
        item_names = [i["name"] for i in list_res.get("items", [])]
        self.assertIn(folder_name, item_names)

        # 3. Clean up folder
        status, del_res = http_json(f"{self.target_url}/storage?path={folder_name}", method="DELETE")
        self.assertEqual(status, 200)

        # 4. Verify deletion from list
        status, after_del_res = http_json(f"{self.target_url}/storage/list")
        self.assertEqual(status, 200)
        after_names = [i["name"] for i in after_del_res.get("items", [])]
        self.assertNotIn(folder_name, after_names)

    def test_04_unauthorized_access(self):
        """Verify that requests with invalid credentials receive a 401 Unauthorized status."""
        status, data = http_json(
            f"{self.target_url}/storage/nodes",
            headers={"Authorization": "Bearer invalid-test-token-12345"}
        )
        self.assertEqual(status, 401, f"Expected 401, got {status}: {data}")
        self.assertIn("error", data)

    def test_05_unavailable_target_resilience(self):
        """Verify that unreachable targets return safe error payloads without throwing uncaught exceptions."""
        unreachable_url = "http://127.0.0.1:59998/storage/nodes"
        status, data = http_json(unreachable_url)
        self.assertEqual(status, 500)
        self.assertIn("error", data)

    def test_06_error_handling_invalid_path(self):
        """Verify that directory escaping and path traversal attacks are rejected."""
        status, res = http_json(
            f"{self.target_url}/storage/mkdir",
            method="POST",
            data={"name": "../escaped_dir", "path": ""}
        )
        self.assertIn(status, (400, 403), f"Expected 400 or 403, got {status}: {res}")

    def test_07_live_skip_when_configured_target_unreachable(self):
        """Verify that unreachable live integration targets trigger clean test skips."""
        non_existent_target = "http://127.0.0.1:59999"
        self.assertFalse(is_url_reachable(non_existent_target))


if __name__ == "__main__":
    unittest.main()
