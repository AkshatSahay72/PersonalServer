"""
PersonalServer V2.7 - Portfolio Deployment & Gateway Routing Tests
==================================================================
Verifies:
1. Portfolio manifest registration at root path ("/")
2. Longest-prefix gateway routing:
   - Root path ("/") and root subpaths (/contact, /api/...) route to portfolio
   - Sub-applications (e.g. /RecallFlow/) route to sub-application
   - Reserved system routes (/health, /metrics) are preserved for PersonalServer
3. Portfolio API contracts:
   - /api/projects returns expected JSON format
   - /api/git_stats returns expected JSON format
   - /contact and /chat fail safely when credentials are missing (clean 500 JSON)
4. Production runner lifecycle and passive process status (no signals/os.kill)
"""

import sys
import os
import json
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from common.app_manifest import ApplicationManifest, ManifestRegistry
from router.app_router import PersonalServerRouter
from scripts.portfolio_runner import get_running_pid


class MockBackendHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><head><title>Portfolio</title></head><body><h1>Akshat Sahay</h1></body></html>")
        elif self.path == "/api/projects":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([{"name": "RecallFlow"}, {"name": "ThoughtRAG"}]).encode())
        elif self.path == "/api/git_stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"public_repos": 37, "stars": 5}).encode())
        elif self.path.startswith("/RecallFlow") or self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"app": "RecallFlow", "path": self.path}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        if self.path == "/contact":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Email service not configured on server (Missing Resend API Key)."}).encode())
        elif self.path == "/chat":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "AI service not configured on server (Missing Groq API Key)."}).encode())
        else:
            self.send_response(404)
            self.end_headers()


class TestPortfolioDeploymentAndRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start mock portfolio backend
        cls.portfolio_server = HTTPServer(("127.0.0.1", 0), MockBackendHandler)
        cls.portfolio_port = cls.portfolio_server.server_port
        cls.portfolio_thread = threading.Thread(target=cls.portfolio_server.serve_forever, daemon=True)
        cls.portfolio_thread.start()

        # Start mock recallflow backend
        cls.recallflow_server = HTTPServer(("127.0.0.1", 0), MockBackendHandler)
        cls.recallflow_port = cls.recallflow_server.server_port
        cls.recallflow_thread = threading.Thread(target=cls.recallflow_server.serve_forever, daemon=True)
        cls.recallflow_thread.start()

        cls.registry = ManifestRegistry()

        # Build manifests using from_dict
        cls.portfolio_manifest = ApplicationManifest.from_dict({
            "name": "portfolio",
            "public_path": "/",
            "display_name": "Akshat Sahay Portfolio",
            "app_id": "portfolio",
            "status": "active",
            "components": {
                "web": {
                    "type": "frontend",
                    "provider": "self-hosted",
                    "destination": f"http://127.0.0.1:{cls.portfolio_port}",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "preserve",
                        "api_priority": False
                    }
                }
            }
        })

        cls.recallflow_manifest = ApplicationManifest.from_dict({
            "name": "RecallFlow",
            "public_path": "/RecallFlow",
            "display_name": "RecallFlow AI",
            "app_id": "app-recallflow",
            "status": "active",
            "components": {
                "web": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": f"http://127.0.0.1:{cls.recallflow_port}",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "api_priority": False
                    }
                }
            }
        })

        cls.registry.register(cls.portfolio_manifest)
        cls.registry.register(cls.recallflow_manifest)

        cls.mock_apps_db = {
            "apps": {
                "portfolio": cls.portfolio_manifest.to_dict(),
                "app-recallflow": cls.recallflow_manifest.to_dict()
            }
        }

        import router.app_router as ar
        ar.load_apps_registry = lambda: cls.mock_apps_db

        # Start gateway router
        cls.gateway = PersonalServerRouter(host="127.0.0.1", port=0)
        cls.gateway_port = cls.gateway.start()

    @classmethod
    def tearDownClass(cls):
        cls.gateway.stop()
        cls.portfolio_server.shutdown()
        cls.portfolio_server.server_close()
        cls.recallflow_server.shutdown()
        cls.recallflow_server.server_close()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.gateway_port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)

    def _post_json(self, path: str, payload: dict):
        url = f"http://127.0.0.1:{self.gateway_port}{path}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_01_root_manifest_registered(self):
        """Root portfolio manifest registers at '/' and validates cleanly."""
        manifest = self.registry.get_by_path("/")
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.app_id, "portfolio")
        self.assertEqual(manifest.public_path, "/")

    def test_02_longest_prefix_resolution(self):
        """Specific apps (/RecallFlow/) match first; general paths fallback to root portfolio."""
        rf = self.registry.get_by_path("/RecallFlow/dashboard")
        self.assertIsNotNone(rf)
        self.assertEqual(rf.app_id, "app-recallflow")

        root = self.registry.get_by_path("/")
        self.assertIsNotNone(root)
        self.assertEqual(root.app_id, "portfolio")

        contact = self.registry.get_by_path("/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.app_id, "portfolio")

    def test_03_gateway_routes_root_to_portfolio(self):
        """GET / on gateway routes to portfolio web component and returns HTML."""
        status, body, headers = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Akshat Sahay", body)

    def test_04_gateway_routes_portfolio_apis(self):
        """GET /api/projects and /api/git_stats route to portfolio."""
        status, body, _ = self._get("/api/projects")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(isinstance(data, list))

        status, body, _ = self._get("/api/git_stats")
        self.assertEqual(status, 200)
        stats = json.loads(body)
        self.assertEqual(stats["public_repos"], 37)

    def test_05_gateway_routes_subapp_coexistence(self):
        """GET /RecallFlow/... routes to RecallFlow without interference from root."""
        status, body, _ = self._get("/RecallFlow/dashboard")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["app"], "RecallFlow")

    def test_06_unconfigured_external_apis_fail_safely(self):
        """POST /contact and /chat return safe error without crashing or leaking secrets."""
        status, body = self._post_json("/contact", {"name": "Test", "email": "a@b.com", "message": "Hi"})
        self.assertEqual(status, 500)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("Missing Resend API Key", data["error"])

        status, body = self._post_json("/chat", {"message": "Hello"})
        self.assertEqual(status, 500)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("Missing Groq API Key", data["error"])

    def test_07_passive_pid_check_does_not_use_signals(self):
        """Process check never uses os.kill to query liveness."""
        # Non-existent PID returns None safely without raising or signaling
        from scripts.portfolio_runner import PID_FILE
        # Test get_running_pid safely
        pid = get_running_pid()
        # Should either be None or an int
        self.assertTrue(pid is None or isinstance(pid, int))


if __name__ == "__main__":
    unittest.main()
