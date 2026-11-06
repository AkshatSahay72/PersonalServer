#!/usr/bin/env python3
"""
PersonalServer Multi-Application Path Routing Acceptance Test Suite (V2.4)
==========================================================================
Verifies live HTTP gateway behavior for concurrent multi-application hosting:
1. Concurrent registration of multiple applications (RecallFlow, MemoVault)
2. Frontend route resolution to independent frontend destinations
3. API route resolution to independent backend destinations with API priority
4. Strict cross-app route isolation (RecallFlow traffic never hits MemoVault)
5. API failure isolation (API 404/500 JSON errors are preserved, never converted to HTML)
6. Trailing slash normalization and query string preservation
7. Upstream Location redirect header and Set-Cookie Path rewriting
8. Rejection of directory traversal and path attacks
9. Independent lifecycle updates without modifying adjacent applications
"""

import sys
import json
import unittest
import urllib.request
import urllib.parse
import urllib.error
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from router.app_router import PersonalServerRouter, ProxyRedirectHandler
from common.app_manifest import ApplicationManifest, ManifestRegistry, RouteConflictError


class MockComponentServerHandler(BaseHTTPRequestHandler):
    """
    Simulates external frontend (Vercel) or backend API (Render) HTTP endpoints.
    Records received requests and returns designated component responses.
    """
    server_type: str = "generic"
    component_name: str = "generic"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = parsed.query

        # Simulate backend error endpoint
        if path == "/api/does-not-exist":
            body = json.dumps({"error": "Resource not found", "component": self.component_name}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Simulate backend internal server error
        if path == "/api/fault":
            body = json.dumps({"error": "Internal database error", "code": 500}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Simulate backend auth redirect
        if path == "/api/login-redirect":
            self.send_response(302)
            self.send_header("Location", "/api/auth/login")
            self.send_header("Set-Cookie", "session_token=test1234; Path=/; Secure; HttpOnly")
            self.end_headers()
            return

        # Normal response
        is_frontend = (self.server_type == "frontend")
        content_type = "text/html; charset=utf-8" if is_frontend else "application/json"

        body_dict = {
            "server_type": self.server_type,
            "component_name": self.component_name,
            "received_path": path,
            "received_query": query,
            "host_header": self.headers.get("Host", ""),
            "forwarded_prefix": self.headers.get("X-Forwarded-Prefix", ""),
            "forwarded_host": self.headers.get("X-Forwarded-Host", "")
        }

        if is_frontend:
            body = f"<html><body><h1>{self.component_name} Frontend</h1><p>Path: {path}</p></body></html>".encode("utf-8")
        else:
            body = json.dumps(body_dict).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        req_data = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""

        if self.path == "/api/fail":
            body = json.dumps({"error": "Validation failed", "status": 422}).encode("utf-8")
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({
            "component_name": self.component_name,
            "received_path": self.path,
            "posted_data": req_data
        }).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def make_mock_server(server_type: str, component_name: str):
    handler = type(
        f"Mock_{component_name}_{server_type}",
        (MockComponentServerHandler,),
        {"server_type": server_type, "component_name": component_name}
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


class TestMultiApplicationPathRouting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 1. Start mock servers for RecallFlow
        cls.rf_front_srv, cls.rf_front_port, _ = make_mock_server("frontend", "RecallFlow")
        cls.rf_back_srv, cls.rf_back_port, _ = make_mock_server("backend", "RecallFlow")

        # 2. Start mock servers for MemoVault
        cls.mv_front_srv, cls.mv_front_port, _ = make_mock_server("frontend", "MemoVault")
        cls.mv_back_srv, cls.mv_back_port, _ = make_mock_server("backend", "MemoVault")

        # 3. Create mock manifest registry
        cls.registry = ManifestRegistry()

        cls.rf_manifest = {
            "name": "RecallFlow",
            "public_path": "/RecallFlow",
            "display_name": "RecallFlow AI",
            "status": "active",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": f"http://127.0.0.1:{cls.rf_front_port}",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "spa_fallback": True,
                        "assets_prefix": "/assets"
                    }
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": f"http://127.0.0.1:{cls.rf_back_port}",
                    "routing": {
                        "path_prefix": "/api",
                        "strip_mode": "strip_app",  # /RecallFlow/api/notes -> /api/notes
                        "api_priority": True
                    }
                }
            }
        }

        cls.mv_manifest = {
            "name": "MemoVault",
            "public_path": "/MemoVault",
            "display_name": "MemoVault Knowledge",
            "status": "active",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": f"http://127.0.0.1:{cls.mv_front_port}",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "spa_fallback": True
                    }
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": f"http://127.0.0.1:{cls.mv_back_port}",
                    "routing": {
                        "path_prefix": "/api",
                        "strip_mode": "strip_app",
                        "api_priority": True
                    }
                }
            }
        }

        cls.registry.register(cls.rf_manifest)
        cls.registry.register(cls.mv_manifest)

        # 4. Mock apps_db for PersonalServerRouter
        cls.mock_apps_db = {
            "apps": {
                "app-recallflow": cls.registry.get_by_name("RecallFlow").to_dict(),
                "app-memovault": cls.registry.get_by_name("MemoVault").to_dict()
            }
        }

        # 5. Start PersonalServerRouter
        cls.router = PersonalServerRouter(host="127.0.0.1", port=0)
        cls.router_port = cls.router.start()

        # Monkey-patch router loading
        import router.app_router as ar
        ar.load_apps_registry = lambda: cls.mock_apps_db

        cls.opener = urllib.request.build_opener(ProxyRedirectHandler)

    @classmethod
    def tearDownClass(cls):
        cls.router.stop()
        cls.rf_front_srv.shutdown()
        cls.rf_back_srv.shutdown()
        cls.mv_front_srv.shutdown()
        cls.mv_back_srv.shutdown()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.router_port}{path}"
        req = urllib.request.Request(url, headers={"X-Forwarded-Proto": "https", "Host": "akshatsahay.space"})
        try:
            with self.opener.open(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _post(self, path: str, data: dict):
        url = f"http://127.0.0.1:{self.router_port}{path}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https", "Host": "akshatsahay.space"},
            method="POST"
        )
        try:
            with self.opener.open(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # --------------------------------------------------------------------------
    # Check 1: Trailing Slash Normalization
    # --------------------------------------------------------------------------
    def test_01_trailing_slash_normalization(self):
        """GET /RecallFlow without slash redirects to /RecallFlow/ (308)."""
        status, headers, body = self._get("/RecallFlow")
        self.assertEqual(status, 308)
        self.assertEqual(headers.get("Location"), "/RecallFlow/")

        status_mv, headers_mv, _ = self._get("/MemoVault")
        self.assertEqual(status_mv, 308)
        self.assertEqual(headers_mv.get("Location"), "/MemoVault/")

    # --------------------------------------------------------------------------
    # Check 2: Frontend Route Resolution
    # --------------------------------------------------------------------------
    def test_02_frontend_route_resolution(self):
        """Frontend routes resolve to independent frontend destinations."""
        # RecallFlow root
        status, headers, body = self._get("/RecallFlow/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("RecallFlow Frontend", body.decode("utf-8"))

        # RecallFlow client-side subroute
        status, headers, body = self._get("/RecallFlow/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Path: /dashboard", body.decode("utf-8"))

        # MemoVault frontend
        status_mv, headers_mv, body_mv = self._get("/MemoVault/")
        self.assertEqual(status_mv, 200)
        self.assertIn("MemoVault Frontend", body_mv.decode("utf-8"))

    # --------------------------------------------------------------------------
    # Check 3: API Route Precedence and Backend Resolution
    # --------------------------------------------------------------------------
    def test_03_api_route_precedence_and_backend_resolution(self):
        """Routes under /app/api/* route to backend, not frontend, with prefix stripped."""
        status, headers, body = self._get("/RecallFlow/api/notes")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("component_name"), "RecallFlow")
        self.assertEqual(data.get("server_type"), "backend")
        self.assertEqual(data.get("received_path"), "/api/notes")

        # MemoVault API
        status_mv, headers_mv, body_mv = self._get("/MemoVault/api/items")
        self.assertEqual(status_mv, 200)
        data_mv = json.loads(body_mv.decode("utf-8"))
        self.assertEqual(data_mv.get("component_name"), "MemoVault")
        self.assertEqual(data_mv.get("server_type"), "backend")
        self.assertEqual(data_mv.get("received_path"), "/api/items")

    # --------------------------------------------------------------------------
    # Check 4: Cross-Application Route Isolation
    # --------------------------------------------------------------------------
    def test_04_cross_application_route_isolation(self):
        """A route for RecallFlow NEVER resolves to MemoVault's backend or frontend."""
        status, headers, body = self._get("/RecallFlow/api/status")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("component_name"), "RecallFlow")
        self.assertNotEqual(data.get("component_name"), "MemoVault")

        status_mv, headers_mv, body_mv = self._get("/MemoVault/api/status")
        data_mv = json.loads(body_mv.decode("utf-8"))
        self.assertEqual(data_mv.get("component_name"), "MemoVault")
        self.assertNotEqual(data_mv.get("component_name"), "RecallFlow")

    # --------------------------------------------------------------------------
    # Check 5: API Failure Isolation (Never Converted to Frontend HTML)
    # --------------------------------------------------------------------------
    def test_05_api_error_isolation(self):
        """API errors (404, 500, 422) return upstream JSON, never fallback HTML."""
        # 404 API error
        status, headers, body = self._get("/RecallFlow/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        err_data = json.loads(body.decode("utf-8"))
        self.assertEqual(err_data.get("error"), "Resource not found")
        self.assertNotIn("<html>", body.decode("utf-8").lower())

        # 500 API error
        status, headers, body = self._get("/RecallFlow/api/fault")
        self.assertEqual(status, 500)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        err_data = json.loads(body.decode("utf-8"))
        self.assertEqual(err_data.get("code"), 500)

        # 422 POST API error
        status, headers, body = self._post("/RecallFlow/api/fail", {"invalid": "payload"})
        self.assertEqual(status, 422)
        err_data = json.loads(body.decode("utf-8"))
        self.assertEqual(err_data.get("status"), 422)

    # --------------------------------------------------------------------------
    # Check 6: Query String & Header Rewriting
    # --------------------------------------------------------------------------
    def test_06_query_string_and_redirect_rewriting(self):
        """Query parameters are preserved and Location redirect headers are rewritten."""
        status, headers, body = self._get("/RecallFlow/api/search?q=agents&limit=10")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("received_query"), "q=agents&limit=10")
        self.assertEqual(data.get("forwarded_prefix"), "/RecallFlow")

        # Location redirect rewriting
        status_loc, headers_loc, _ = self._get("/RecallFlow/api/login-redirect")
        self.assertEqual(status_loc, 302)
        # Upstream returned Location: /api/auth/login -> Gateway rewrites to /RecallFlow/api/auth/login
        self.assertEqual(headers_loc.get("Location"), "/RecallFlow/api/auth/login")
        # Upstream returned Set-Cookie: Path=/ -> Gateway rewrites to Path=/RecallFlow
        cookie = headers_loc.get("Set-Cookie", "")
        self.assertIn("Path=/RecallFlow", cookie)

    # --------------------------------------------------------------------------
    # Check 7: Security & Traversal Rejection
    # --------------------------------------------------------------------------
    def test_07_traversal_and_malformed_rejection(self):
        """Rejects traversal attempts with 400 Bad Request."""
        status, headers, body = self._get("/RecallFlow/../../etc/passwd")
        self.assertEqual(status, 400)

        status2, headers2, _ = self._get("/RecallFlow/%2e%2e/%2e%2e/secret")
        self.assertEqual(status2, 400)

    # --------------------------------------------------------------------------
    # Check 8: Conflict Rejection and Isolation During Updates
    # --------------------------------------------------------------------------
    def test_08_conflict_rejection_and_isolation(self):
        """Registering a conflicting app path is rejected; updating one does not alter the other."""
        # Conflict
        duplicate_app = {
            "name": "RecallFlowClone",
            "public_path": "/recallflow",  # Conflict
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "http://127.0.0.1:9999",
                    "routing": {"path_prefix": "/"}
                }
            }
        }
        with self.assertRaises(RouteConflictError):
            self.registry.register(duplicate_app)

        # Update MemoVault
        self.registry.update("MemoVault", {"display_name": "MemoVault V2 Extended"})
        rf_entry = self.registry.get_by_name("RecallFlow")
        self.assertEqual(rf_entry.display_name, "RecallFlow AI")

    # --------------------------------------------------------------------------
    # Check 9: Multi-Segment Sub-Application Routing (/apps/ExamBuddy)
    # --------------------------------------------------------------------------
    def test_09_multi_segment_subapp_routing(self):
        """Multi-segment public paths like /apps/ExamBuddy match correctly with longest-prefix."""
        eb_srv, eb_port, _ = make_mock_server("frontend", "ExamBuddy")
        try:
            self.mock_apps_db["apps"]["app-exambuddy"] = {
                "name": "ExamBuddy",
                "public_path": "/apps/ExamBuddy",
                "status": "active",
                "components": {
                    "frontend": {
                        "type": "frontend",
                        "provider": "self-hosted",
                        "destination": f"http://127.0.0.1:{eb_port}",
                        "routing": {
                            "path_prefix": "/",
                            "strip_mode": "strip_app"
                        }
                    }
                }
            }
            # Test trailing slash normalization
            status, headers, _ = self._get("/apps/ExamBuddy")
            self.assertEqual(status, 308)
            self.assertEqual(headers.get("Location"), "/apps/ExamBuddy/")

            # Test route resolution
            status_eb, headers_eb, body_eb = self._get("/apps/ExamBuddy/")
            self.assertEqual(status_eb, 200)
            self.assertIn("ExamBuddy Frontend", body_eb.decode("utf-8"))

            # Test subpath
            status_sub, _, body_sub = self._get("/apps/ExamBuddy/exam")
            self.assertEqual(status_sub, 200)
            self.assertIn("Path: /exam", body_sub.decode("utf-8"))
        finally:
            self.mock_apps_db["apps"].pop("app-exambuddy", None)
            eb_srv.shutdown()


if __name__ == "__main__":
    unittest.main()

