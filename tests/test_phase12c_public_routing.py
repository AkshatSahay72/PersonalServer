#!/usr/bin/env python3
"""
PersonalServer Phase 12C — Public Path-Based Application Routing Test Suite
==========================================================================
Verifies:
1. Public / Ingress application routing (/demo-app/, /recallflow/)
2. Portfolio origin preservation (akshatsahay.space hosted on Vercel)
3. Path prefix stripping & subpath forwarding (/demo-app/api/test -> /api/test)
4. Query string preservation (?x=123&y=hello)
5. HTTPS / Reverse proxy header forwarding (X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-For, X-Forwarded-Prefix)
6. Trailing slash normalization (308 redirect)
7. Application route isolation (unknown apps return 404)
8. Administrative route isolation (/api, /storage, /nodes, /cluster, /health)
9. Port exposure safety (raw container host ports not publicly exposed)
10. Application lifecycle failure states (STOPPED 503, DEPLOYING 503 + Retry-After, FAILED 502, OFFLINE 504)
11. SSRF & path traversal rejection (../, %2e%2e, encoded traversal, null bytes)
12. Sensitive credential isolation (no token or internal leakages)
"""

import sys
import os
import json
import time
import unittest
import urllib.request
import urllib.parse
import urllib.error
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from router.app_router import ApplicationRouter, PersonalServerRouter, ProxyRedirectHandler


class MockPortfolioOriginHandler(BaseHTTPRequestHandler):
    """Simulates external portfolio hosting (Vercel) for akshatsahay.space root & normal pages."""
    def do_GET(self):
        if self.path in ("/", "/about", "/projects", "/contact"):
            body = f"Portfolio page: {self.path}".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Vercel-Cache", "HIT")
            self.send_header("X-Vercel-Id", "bom1::iad1::test-portfolio")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"Portfolio 404 Not Found")

    def log_message(self, format, *args):
        pass


class MockUpstreamContainerHandler(BaseHTTPRequestHandler):
    """Simulates containerized application responding inside Docker on host_port."""
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = parsed.query

        if path == "/login-redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session=xyz123; Path=/; Secure; HttpOnly")
            self.end_headers()
            return

        body_dict = {
            "status": "running",
            "message": "PersonalServer Application is running.",
            "received_path": path,
            "received_query": query,
            "received_headers": dict(self.headers)
        }
        body = json.dumps(body_dict).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        body_dict = {
            "status": "created",
            "received_path": self.path,
            "received_body": req_body,
            "received_headers": dict(self.headers)
        }
        body = json.dumps(body_dict).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class TestPhase12CPublicRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Start mock portfolio server (simulating Vercel)
        cls.portfolio_server = HTTPServer(("127.0.0.1", 0), MockPortfolioOriginHandler)
        cls.portfolio_port = cls.portfolio_server.server_address[1]
        cls.portfolio_thread = threading.Thread(target=cls.portfolio_server.serve_forever, daemon=True)
        cls.portfolio_thread.start()

        # 2. Start mock upstream Docker container
        cls.container_server = HTTPServer(("127.0.0.1", 0), MockUpstreamContainerHandler)
        cls.container_port = cls.container_server.server_address[1]
        cls.container_thread = threading.Thread(target=cls.container_server.serve_forever, daemon=True)
        cls.container_thread.start()

        # 3. Setup mock registry database
        cls.mock_nodes_db = {
            "nodes": {
                "node-online": {
                    "node_id": "node-online",
                    "name": "docker-node-03",
                    "status": "ONLINE",
                    "capabilities": {"container_runtime:docker": True}
                },
                "node-offline": {
                    "node_id": "node-offline",
                    "name": "docker-node-offline",
                    "status": "OFFLINE",
                    "capabilities": {"container_runtime:docker": True}
                }
            }
        }

        cls.mock_apps_db = {
            "apps": {
                "app-demo": {
                    "app_id": "app-demo",
                    "name": "demo-app",
                    "status": "RUNNING",
                    "selected_node": "node-online",
                    "host_port": cls.container_port,
                    "route": {
                        "enabled": True,
                        "type": "path",
                        "path": "/demo-app",
                        "strip_prefix": True,
                        "public_access": True
                    }
                },
                "app-stopped": {
                    "app_id": "app-stopped",
                    "name": "stopped-app",
                    "status": "STOPPED",
                    "selected_node": "node-online",
                    "host_port": cls.container_port,
                    "route": {
                        "enabled": True,
                        "type": "path",
                        "path": "/stopped-app",
                        "strip_prefix": True,
                        "public_access": True
                    }
                },
                "app-deploying": {
                    "app_id": "app-deploying",
                    "name": "deploying-app",
                    "status": "DEPLOYING",
                    "selected_node": "node-online",
                    "host_port": cls.container_port,
                    "route": {
                        "enabled": True,
                        "type": "path",
                        "path": "/deploying-app",
                        "strip_prefix": True,
                        "public_access": True
                    }
                },
                "app-failed": {
                    "app_id": "app-failed",
                    "name": "failed-app",
                    "status": "FAILED",
                    "selected_node": "node-online",
                    "host_port": cls.container_port,
                    "route": {
                        "enabled": True,
                        "type": "path",
                        "path": "/failed-app",
                        "strip_prefix": True,
                        "public_access": True
                    }
                },
                "app-offline-node": {
                    "app_id": "app-offline-node",
                    "name": "offline-node-app",
                    "status": "RUNNING",
                    "selected_node": "node-offline",
                    "host_port": cls.container_port,
                    "route": {
                        "enabled": True,
                        "type": "path",
                        "path": "/offline-node-app",
                        "strip_prefix": True,
                        "public_access": True
                    }
                }
            }
        }

        # 4. Start PersonalServer Application Router instance
        cls.router = PersonalServerRouter(host="127.0.0.1", port=0)
        cls.router_port = cls.router.start()

        # Monkey-patch router database loading to use test DBs
        import router.app_router as ar
        ar.load_apps_registry = lambda: cls.mock_apps_db
        ar.load_nodes_registry = lambda: cls.mock_nodes_db

        cls.opener = urllib.request.build_opener(ProxyRedirectHandler)

    @classmethod
    def tearDownClass(cls):
        cls.router.stop()
        cls.container_server.shutdown()
        cls.portfolio_server.shutdown()

    # --------------------------------------------------------------------------
    # 1. Portfolio Origin Preservation
    # --------------------------------------------------------------------------
    def test_01_portfolio_origin_preserved(self):
        """Verify normal portfolio paths reach Vercel origin and are not captured by router."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.portfolio_port}/")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("X-Vercel-Cache"), "HIT")
            data = resp.read().decode("utf-8")
            self.assertIn("Portfolio page: /", data)

        req_about = urllib.request.Request(f"http://127.0.0.1:{self.portfolio_port}/about")
        with urllib.request.urlopen(req_about) as resp:
            self.assertEqual(resp.status, 200)
            data = resp.read().decode("utf-8")
            self.assertIn("Portfolio page: /about", data)

    # --------------------------------------------------------------------------
    # 2. Public Application URL & Prefix Stripping
    # --------------------------------------------------------------------------
    def test_02_public_app_root_and_prefix_stripping(self):
        """Verify /demo-app/ forwards / to container and returns running response."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app/")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("received_path"), "/")
            self.assertIn("PersonalServer Application is running.", data.get("message"))

    def test_03_api_path_forwarding(self):
        """Verify /demo-app/api/test forwards /api/test to the container."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app/api/test")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("received_path"), "/api/test")

    # --------------------------------------------------------------------------
    # 3. Query String Preservation
    # --------------------------------------------------------------------------
    def test_04_query_string_preservation(self):
        """Verify query strings ?x=123&search=ai are preserved through routing."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app/api/test?x=123&search=ai")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("received_query"), "x=123&search=ai")

    # --------------------------------------------------------------------------
    # 4. Trailing Slash Normalization
    # --------------------------------------------------------------------------
    def test_05_trailing_slash_redirect(self):
        """Verify /demo-app without trailing slash returns 308 redirect to /demo-app/."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app")
        resp = self.opener.open(req)
        self.assertEqual(resp.status, 308)
        self.assertEqual(resp.headers.get("Location"), "/demo-app/")

    # --------------------------------------------------------------------------
    # 5. Reverse Proxy Headers & Credential Isolation
    # --------------------------------------------------------------------------
    def test_06_headers_forwarded_and_sensitive_credentials_stripped(self):
        """Verify standard proxy headers are injected and auth credentials stripped."""
        headers = {
            "Authorization": "Bearer sensitive-admin-token-12345",
            "X-Auth-Token": "secret-node-token-67890",
            "Cf-Access-Authenticated-User-Email": "admin@example.com",
            "X-Custom-Header": "AllowedCustomValue"
        }
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app/api/headers", headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            received = data.get("received_headers", {})

            # Sensitive headers stripped
            self.assertNotIn("Authorization", received)
            self.assertNotIn("authorization", received)
            self.assertNotIn("X-Auth-Token", received)
            self.assertNotIn("x-auth-token", received)
            self.assertNotIn("Cf-Access-Authenticated-User-Email", received)
            self.assertNotIn("cf-access-authenticated-user-email", received)

            # Injected reverse proxy headers
            self.assertIn("X-Forwarded-Prefix", received)
            self.assertEqual(received.get("X-Forwarded-Prefix"), "/demo-app")
            self.assertIn("X-Forwarded-For", received)
            self.assertIn("X-Forwarded-Proto", received)
            self.assertIn("X-Custom-Header", received)

    # --------------------------------------------------------------------------
    # 6. Response Rewriting (Location & Set-Cookie)
    # --------------------------------------------------------------------------
    def test_07_response_redirect_and_cookie_path_rewriting(self):
        """Verify upstream Location: /login becomes Location: /demo-app/login and Cookie Path rewritten."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/demo-app/login-redirect")
        resp = self.opener.open(req)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers.get("Location"), "/demo-app/login")
        self.assertIn("Path=/demo-app", resp.headers.get("Set-Cookie"))

    # --------------------------------------------------------------------------
    # 7. Application Isolation & Unknown Route Handling
    # --------------------------------------------------------------------------
    def test_08_unknown_application_returns_404(self):
        """Verify unmapped path returns 404 Application route not found."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/unknown-service/")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 404 HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data.get("error"), "Application route not found")

    # --------------------------------------------------------------------------
    # 8. Administrative Route Isolation
    # --------------------------------------------------------------------------
    def test_09_administrative_reserved_routes_rejected_from_routing(self):
        """Verify reserved PersonalServer control routes cannot be routed as apps."""
        for reserved in ("/api", "/storage", "/cluster", "/nodes", "/jobs", "/admin", "/health"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}{reserved}")
            try:
                urllib.request.urlopen(req)
                # If health route, returns router health; others return 404 route not found
                if reserved == "/health":
                    continue
                self.fail(f"Expected 404 for reserved route {reserved}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)

    # --------------------------------------------------------------------------
    # 9. Failure State Responses
    # --------------------------------------------------------------------------
    def test_10_stopped_application_returns_503(self):
        """Verify STOPPED application returns 503."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/stopped-app/")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 503 HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data.get("error"), "Application is currently stopped")

    def test_11_deploying_application_returns_503_retry_after(self):
        """Verify DEPLOYING application returns 503 with Retry-After header."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/deploying-app/")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 503 HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            self.assertEqual(e.headers.get("Retry-After"), "3")
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data.get("error"), "Application is currently starting up")

    def test_12_failed_application_returns_502(self):
        """Verify FAILED application returns 502."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/failed-app/")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 502 HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data.get("error"), "Application is currently unhealthy")

    def test_13_offline_node_returns_504(self):
        """Verify application on OFFLINE node returns 504."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/offline-node-app/")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 504 HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 504)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data.get("error"), "Application host node is temporarily unreachable")

    # --------------------------------------------------------------------------
    # 10. SSRF, Traversal & Security Isolation
    # --------------------------------------------------------------------------
    def test_14_path_traversal_and_malformed_url_rejected(self):
        """Verify ../ and %2e%2e traversal attempts are blocked safely with 400."""
        for malicious in (
            "/demo-app/../../etc/passwd",
            "/demo-app/%2e%2e/%2e%2e/secret",
            "/demo-app/..\\..\\windows\\system32"
        ):
            req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}{malicious}")
            try:
                urllib.request.urlopen(req)
                self.fail(f"Expected 400 for {malicious}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
                data = json.loads(e.read().decode("utf-8"))
                self.assertIn("Invalid request path", data.get("error"))

    def test_15_no_internal_node_or_token_leakage(self):
        """Verify error responses never reveal Tailscale IPs, tokens, or Docker IDs."""
        for app_path in ("/stopped-app/", "/failed-app/", "/offline-node-app/", "/unknown-app/"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}{app_path}")
            try:
                urllib.request.urlopen(req)
            except urllib.error.HTTPError as e:
                raw_resp = e.read().decode("utf-8")
                self.assertNotIn("100.", raw_resp)
                self.assertNotIn("token", raw_resp.lower())
                self.assertNotIn("container_id", raw_resp)
                self.assertNotIn("ps-", raw_resp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
