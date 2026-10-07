#!/usr/bin/env python3
"""
Test Suite for Phase 12B: PersonalServer Application Router
===========================================================
Validates:
1. Route registration & validation in controller
2. Duplicate route rejection
3. Invalid route syntax rejection
4. Reserved system route rejection
5. Unknown route 404 response
6. Stopped application 503 response
7. Failed application 502 response
8. Deploying application 503 response with Retry-After header
9. Offline node 504 response
10. Prefix stripping and path forwarding
11. Trailing slash normalization redirect
12. Location redirect header rewriting
13. Set-Cookie Path rewriting
14. SSRF protection (no arbitrary upstreams)
15. Directory traversal (../) rejection
16. Encoded traversal (%2e%2e, %00) rejection
17. Host header injection rejection
18. Port tampering rejection
19. Internal API isolation from router
20. Sensitive authentication header isolation
"""

import sys
import os
import json
import time
import socket
import unittest
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from router.app_router import ApplicationRouter, PersonalServerRouter, load_apps_registry, load_nodes_registry
from controller.controller import load_apps_db, load_nodes_db, save_apps_db


class MockContainerHandler(BaseHTTPRequestHandler):
    """Mock container backend to verify header forwarding and response rewriting."""
    last_request_path = ""
    last_headers = {}

    def do_GET(self):
        MockContainerHandler.last_request_path = self.path
        MockContainerHandler.last_headers = dict(self.headers)

        if self.path == "/redirect-me":
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.send_header("Set-Cookie", "session_id=secret123; Path=/; HttpOnly")
            self.end_headers()
            return

        if self.path == "/echo-headers":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(dict(self.headers)).encode())
            return

        body = b"Mock container response: OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class TestPhase12ApplicationRouting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.token_file = BASE_DIR / "config" / "secrets" / "enrollment.token"
        cls.token = cls.token_file.read_text().strip() if cls.token_file.exists() else ""
        cls.controller_url = "http://127.0.0.1:8000"
        cls.router_url = "http://127.0.0.1:8088"

        # Start a mock backend container on port 18090
        cls.mock_backend_port = 18090
        cls.mock_backend = HTTPServer(("127.0.0.1", cls.mock_backend_port), MockContainerHandler)
        cls.mock_thread = threading.Thread(target=cls.mock_backend.serve_forever, daemon=True)
        cls.mock_thread.start()

    def _api_post(self, path, data):
        req = urllib.request.Request(
            f"{self.controller_url}{path}",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _router_get(self, path, headers=None):
        req = urllib.request.Request(
            f"{self.router_url}{path}",
            headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            content = e.read().decode()
            try:
                data = json.loads(content)
            except Exception:
                data = content
            return e.code, e.headers, data

    def test_01_valid_route_registration(self):
        """1. Verify valid route registration on an application."""
        status, res = self._api_post("/apps", {
            "name": "test-valid-app",
            "image": "personalserver/demo-app:v1",
            "container_port": 8000,
            "route": {
                "enabled": True,
                "type": "path",
                "path": "/thoughtrag",
                "strip_prefix": True
            }
        })
        self.assertEqual(status, 201)
        self.assertEqual(res["app"]["route"]["path"], "/thoughtrag")
        self.assertTrue(res["app"]["route"]["strip_prefix"])

        # Clean up app
        app_id = res["app_id"]
        del_req = urllib.request.Request(
            f"{self.controller_url}/apps/{app_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="DELETE"
        )
        urllib.request.urlopen(del_req)

    def test_02_duplicate_route_rejection(self):
        """2. Verify duplicate route paths are rejected across applications."""
        # Create App 1
        status1, res1 = self._api_post("/apps", {
            "name": "app-route-1",
            "image": "personalserver/demo-app:v1",
            "route": {"path": "/sharedroute"}
        })
        self.assertEqual(status1, 201)

        # Attempt to create App 2 with same route
        status2, res2 = self._api_post("/apps", {
            "name": "app-route-2",
            "image": "personalserver/demo-app:v1",
            "route": {"path": "/sharedroute"}
        })
        self.assertEqual(status2, 400)
        self.assertIn("already registered", res2.get("error", ""))

        # Clean up
        app1_id = res1["app_id"]
        urllib.request.urlopen(urllib.request.Request(f"{self.controller_url}/apps/{app1_id}", headers={"Authorization": f"Bearer {self.token}"}, method="DELETE"))

    def test_03_invalid_route_syntax_rejection(self):
        """3. Reject invalid route syntax (spaces, missing leading slash, symbols)."""
        bad_paths = ["thoughtrag", "/thought rag", "/thought/rag", "/@app", "/app$name"]
        for bad_p in bad_paths:
            status, res = self._api_post("/apps", {
                "name": f"bad-route-{int(time.time()*1000)%10000}",
                "image": "personalserver/demo-app:v1",
                "route": {"path": bad_p}
            })
            self.assertEqual(status, 400, f"Expected 400 for bad path: {bad_p}")
            self.assertIn("Invalid route path", res.get("error", ""))

    def test_04_reserved_route_rejection(self):
        """4. Reject registration of reserved PersonalServer system paths."""
        reserved = ["/", "/api", "/static", "/storage", "/health", "/status", "/cluster", "/nodes", "/jobs", "/admin"]
        for res_p in reserved:
            status, res = self._api_post("/apps", {
                "name": f"res-app-{int(time.time()*1000)%10000}",
                "image": "personalserver/demo-app:v1",
                "route": {"path": res_p}
            })
            self.assertEqual(status, 400, f"Expected 400 for reserved path: {res_p}")
            self.assertIn("reserved", res.get("error", ""))

    def test_05_unknown_route_returns_404(self):
        """5. Querying an unregistered route returns clean HTTP 404 JSON."""
        status, headers, body = self._router_get("/nonexistent-route-xyz/")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("status"), 404)
        self.assertEqual(body.get("error"), "Application route not found")

    def test_06_stopped_application_returns_503(self):
        """6. Stopped application returns HTTP 503."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-stopped-test",
            "name": "stopped-app",
            "status": "STOPPED",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": 18090,
            "route": {"enabled": True, "path": "/stoppedapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-stopped-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/stoppedapp/")
            self.assertEqual(status, 503)
            self.assertIn("stopped", body.get("error", "").lower())
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-stopped-test", None)
            save_apps_db(apps_db)

    def test_07_failed_application_returns_502(self):
        """7. Failed application returns HTTP 502."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-failed-test",
            "name": "failed-app",
            "status": "FAILED",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": 18090,
            "route": {"enabled": True, "path": "/failedapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-failed-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/failedapp/")
            self.assertEqual(status, 502)
            self.assertIn("unhealthy", body.get("error", "").lower())
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-failed-test", None)
            save_apps_db(apps_db)

    def test_08_deploying_application_returns_503_retry_after(self):
        """8. Deploying application returns HTTP 503 with Retry-After header."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-deploying-test",
            "name": "deploying-app",
            "status": "DEPLOYING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": 18090,
            "route": {"enabled": True, "path": "/deployingapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-deploying-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/deployingapp/")
            self.assertEqual(status, 503)
            self.assertEqual(headers.get("Retry-After"), "3")
            self.assertIn("starting up", body.get("error", "").lower())
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-deploying-test", None)
            save_apps_db(apps_db)

    def test_09_offline_node_returns_504(self):
        """9. App on an offline/unreachable node returns HTTP 504."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-offline-node-test",
            "name": "offline-node-app",
            "status": "RUNNING",
            "selected_node": "server-nonexistent-offline",
            "host_port": 18090,
            "route": {"enabled": True, "path": "/offlinenodeapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-offline-node-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/offlinenodeapp/")
            self.assertEqual(status, 504)
            self.assertIn("unreachable", body.get("error", "").lower())
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-offline-node-test", None)
            save_apps_db(apps_db)

    def test_10_prefix_stripping_and_forwarding(self):
        """10. Verify prefix stripping forwards clean internal path to container."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-prefix-test",
            "name": "prefix-app",
            "status": "RUNNING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": self.mock_backend_port,
            "route": {"enabled": True, "path": "/prefixtest", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-prefix-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/prefixtest/api/v1/users?limit=10")
            self.assertEqual(status, 200)
            self.assertEqual(MockContainerHandler.last_request_path, "/api/v1/users?limit=10")
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-prefix-test", None)
            save_apps_db(apps_db)

    def test_11_trailing_slash_redirect(self):
        """11. Verify GET /app without trailing slash returns 308 redirect to /app/."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_308(self, req, fp, code, msg, headers):
                return fp
        
        opener = urllib.request.build_opener(NoRedirect)
        res = opener.open(f"{self.router_url}/recallflow")
        self.assertEqual(res.status, 308)
        self.assertEqual(res.headers.get("Location"), "/recallflow/")

    def test_12_location_header_rewriting(self):
        """12. Upstream Location: /dashboard is rewritten to /routedapp/dashboard."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-rewrite-test",
            "name": "rewrite-app",
            "status": "RUNNING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": self.mock_backend_port,
            "route": {"enabled": True, "path": "/routedapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-rewrite-test"] = test_app
        save_apps_db(apps_db)

        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, req, fp, code, msg, headers):
                    return fp
            opener = urllib.request.build_opener(NoRedirect)
            res = opener.open(f"{self.router_url}/routedapp/redirect-me")
            self.assertEqual(res.status, 302)
            self.assertEqual(res.headers.get("Location"), "/routedapp/dashboard")
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-rewrite-test", None)
            save_apps_db(apps_db)

    def test_13_cookie_path_rewriting(self):
        """13. Upstream Set-Cookie Path=/ is rewritten to Path=/routedapp."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-cookie-test",
            "name": "cookie-app",
            "status": "RUNNING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": self.mock_backend_port,
            "route": {"enabled": True, "path": "/routedapp", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-cookie-test"] = test_app
        save_apps_db(apps_db)

        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, req, fp, code, msg, headers):
                    return fp
            opener = urllib.request.build_opener(NoRedirect)
            res = opener.open(f"{self.router_url}/routedapp/redirect-me")
            cookie_hdr = res.headers.get("Set-Cookie", "")
            self.assertIn("Path=/routedapp", cookie_hdr)
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-cookie-test", None)
            save_apps_db(apps_db)

    def test_14_ssrf_protection_no_arbitrary_upstream(self):
        """14. Router only forwards to verified registry targets (no arbitrary SSRF)."""
        # Testing route resolution fails for unlisted target
        status, headers, body = self._router_get("/http://169.254.169.254/latest/meta-data/")
        self.assertIn(status, [400, 404])

    def test_15_dot_dot_traversal_rejection(self):
        """15. Path traversal attempts (../) are rejected with HTTP 400."""
        status, headers, body = self._router_get("/recallflow/../../etc/passwd")
        self.assertEqual(status, 400)
        self.assertIn("Invalid request path", str(body))

    def test_16_encoded_traversal_rejection(self):
        """16. Encoded traversals (%2e%2e, %00) are rejected with HTTP 400."""
        bad_paths = [
            "/recallflow/%2e%2e%2f%2e%2e%2fetc/passwd",
            "/recallflow/test%00admin"
        ]
        for bp in bad_paths:
            status, headers, body = self._router_get(bp)
            self.assertEqual(status, 400)

    def test_17_arbitrary_host_injection_rejected(self):
        """17. Host header injection does not divert upstream destination."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-host-inj-test",
            "name": "host-inj-app",
            "status": "RUNNING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": self.mock_backend_port,
            "route": {"enabled": True, "path": "/hostinj", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-host-inj-test"] = test_app
        save_apps_db(apps_db)

        try:
            status, headers, body = self._router_get("/hostinj/echo-headers", headers={"Host": "malicious-site.com"})
            self.assertEqual(status, 200)
            # Backend receives forwarded Host header but routing remained bound to mock_backend_port
            backend_received = json.loads(body)
            self.assertEqual(backend_received.get("X-Forwarded-Host"), "malicious-site.com")
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-host-inj-test", None)
            save_apps_db(apps_db)

    def test_18_arbitrary_port_injection_rejected(self):
        """18. Port tampering in path or headers cannot redirect destination."""
        status, headers, body = self._router_get("/recallflow:8000/api")
        self.assertIn(status, [400, 404])

    def test_19_internal_api_access_isolation(self):
        """19. Router does not expose internal controller endpoints under application routes."""
        status, headers, body = self._router_get("/api/cluster")
        self.assertEqual(status, 404)

    def test_20_auth_header_isolation(self):
        """20. Internal admin auth headers (Bearer token) are stripped before forwarding to container."""
        apps_db = load_apps_db()
        test_app = {
            "app_id": "app-auth-strip-test",
            "name": "auth-strip-app",
            "status": "RUNNING",
            "selected_node": "server-f8b1485ecf458f74",
            "host_port": self.mock_backend_port,
            "route": {"enabled": True, "path": "/authstrip", "strip_prefix": True}
        }
        apps_db.setdefault("apps", {})["app-auth-strip-test"] = test_app
        save_apps_db(apps_db)

        try:
            secret_token = "PersonalServerAdminSecret12345"
            status, headers, body = self._router_get("/authstrip/echo-headers", headers={
                "Authorization": f"Bearer {secret_token}",
                "X-Auth-Token": secret_token,
                "Cf-Access-Authenticated-User-Email": "admin@example.com",
                "User-Agent": "PublicClient/1.0"
            })
            self.assertEqual(status, 200)
            received = json.loads(body)
            self.assertNotIn("Authorization", received)
            self.assertNotIn("X-Auth-Token", received)
            self.assertNotIn("Cf-Access-Authenticated-User-Email", received)
            self.assertEqual(received.get("User-Agent"), "PublicClient/1.0")
        finally:
            apps_db = load_apps_db()
            apps_db.get("apps", {}).pop("app-auth-strip-test", None)
            save_apps_db(apps_db)


if __name__ == "__main__":
    unittest.main()
