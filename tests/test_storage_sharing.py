#!/usr/bin/env python3
"""
Test Suite for PersonalServer Storage Public Link Sharing (Phase 2A)
===================================================================

Verifies:
1. Shares store persistence (atomic saves, rollback on corrupt JSON).
2. Share creation requires admin authentication (401 without auth).
3. Querying and deleting shares requires admin authentication.
4. Enabling sharing generates a fresh, cryptographically unguessable token.
5. Public share endpoint (/storage/s/<token>) works without authentication.
6. Disabling sharing immediately revokes public access (returns 404).
7. Re-enabling sharing generates a fresh URL; revoked URL cannot be reused.
8. Directory sharing is strictly rejected (returns 400).
9. Untrusted HTML and SVG files are served safely (Content-Type: text/plain, CSP sandbox).
10. Path traversal and unauthorized file access attempts are blocked.
11. Existing private storage endpoints (/storage/list, /storage/download, etc.) remain strictly private.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import importlib.util
node_api_path = BASE_DIR / "services" / "node-api" / "app.py"
spec = importlib.util.spec_from_file_location("node_api_module", str(node_api_path))
node_api_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_api_mod)
NodeAPIHandler = node_api_mod.NodeAPIHandler

from config.platform_config import ControllerPaths
from controller.stores.shares_store import (
    load_shares_db,
    save_shares_db,
    get_share_by_id,
    find_active_share,
    create_share,
    revoke_share,
)


def http_request(url: str, method: str = "GET", data: dict = None, headers: dict = None):
    req_headers = headers.copy() if headers else {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_bytes = resp.read()
            resp_headers = dict(resp.headers)
            try:
                json_data = json.loads(resp_bytes.decode("utf-8"))
            except Exception:
                json_data = None
            return resp.status, json_data, resp_bytes, resp_headers
    except urllib.error.HTTPError as e:
        resp_bytes = e.read()
        resp_headers = dict(e.headers)
        try:
            json_data = json.loads(resp_bytes.decode("utf-8"))
        except Exception:
            json_data = None
        return e.code, json_data, resp_bytes, resp_headers


class TestStorageSharing(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_storage_share_test_"))
        self.data_dir = self.test_dir / "controller" / "data"
        self.secrets_dir = self.test_dir / "config" / "secrets"
        self.storage_dir = self.test_dir / "storage"
        self.runtime_dir = self.test_dir / "runtime"
        self.logs_dir = self.test_dir / "logs"

        self.paths = ControllerPaths(
            base_dir=self.test_dir,
            data_dir=self.data_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir
        )
        self.paths.ensure_directories()

        # Auth token setup
        self.auth_token = "secret-test-token-778899"
        self.paths.enrollment_token_file.write_text(self.auth_token + "\n", encoding="utf-8")
        (self.secrets_dir / "auth.token").write_text(self.auth_token + "\n", encoding="utf-8")

        # Create sample files
        self.sample_txt = self.storage_dir / "document.txt"
        self.sample_txt.write_text("Hello PersonalServer Storage!", encoding="utf-8")

        self.sample_html = self.storage_dir / "untrusted.html"
        self.sample_html.write_text("<script>alert('xss')</script><h1>Untrusted</h1>", encoding="utf-8")

        self.sample_svg = self.storage_dir / "vector.svg"
        self.sample_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")

        self.sub_folder = self.storage_dir / "nested_folder"
        self.sub_folder.mkdir()
        (self.sub_folder / "inner.txt").write_text("Inner file content", encoding="utf-8")

        # Patch node_api_mod globals
        self._orig_storage_root = node_api_mod.STORAGE_ROOT
        self._orig_auth_token_file = node_api_mod.AUTH_TOKEN_FILE
        self._orig_secrets_dir = node_api_mod.SECRETS_DIR

        node_api_mod.STORAGE_ROOT = self.storage_dir
        node_api_mod.AUTH_TOKEN_FILE = self.secrets_dir / "auth.token"
        node_api_mod.SECRETS_DIR = self.secrets_dir

        # Start NodeAPI test server on loopback with non-loopback simulation
        self.server = HTTPServer(("127.0.0.1", 0), NodeAPIHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

        node_api_mod.STORAGE_ROOT = self._orig_storage_root
        node_api_mod.AUTH_TOKEN_FILE = self._orig_auth_token_file
        node_api_mod.SECRETS_DIR = self._orig_secrets_dir

        shutil.rmtree(self.test_dir, ignore_errors=True)

    def auth_headers(self):
        return {"Authorization": f"Bearer {self.auth_token}"}

    # --------------------------------------------------------------------------
    # 1. Shares Store Persistence & Rollback
    # --------------------------------------------------------------------------
    def test_01_shares_store_persistence_and_rollback(self):
        """Verify shares_store atomic persistence and backup rollback on corruption."""
        record = create_share("node-01", "document.txt", "document.txt", paths=self.paths)
        self.assertTrue(record["share_id"])
        self.assertFalse(record["revoked"])

        # Second atomic save creates .bak backup file
        create_share("node-01", "document.txt", "document.txt", paths=self.paths)

        # Load back
        active = find_active_share("node-01", "document.txt", paths=self.paths)
        self.assertIsNotNone(active)

        # Corrupt primary shares.json and verify rollback
        shares_file = self.paths.data_dir / "shares.json"
        self.assertTrue(shares_file.exists())
        self.assertTrue((self.paths.data_dir / "shares.json.bak").exists())

        shares_file.write_text("{corrupt-json", encoding="utf-8")
        recovered_db = load_shares_db(paths=self.paths)
        self.assertIn("shares", recovered_db)
        self.assertIn(record["share_id"], recovered_db["shares"])

    # --------------------------------------------------------------------------
    # 2. Authentication Protection for Share Management
    # --------------------------------------------------------------------------
    def test_02_share_endpoints_enforce_authentication(self):
        """Verify POST, GET, and DELETE /storage/share reject unauthorized external requests."""
        # Note: 127.0.0.1 is loopback, but requests with invalid token or Cf-Access headers are tested
        fake_auth = {"Authorization": "Bearer completely-invalid-token-xyz"}

        status, body, _, _ = http_request(f"{self.base_url}/storage/share", method="POST", data={"path": "document.txt"}, headers=fake_auth)
        self.assertEqual(status, 401)

        status, body, _, _ = http_request(f"{self.base_url}/storage/share?path=document.txt", method="GET", headers=fake_auth)
        self.assertEqual(status, 401)

        status, body, _, _ = http_request(f"{self.base_url}/storage/share?path=document.txt", method="DELETE", headers=fake_auth)
        self.assertEqual(status, 401)

    # --------------------------------------------------------------------------
    # 3. Share Creation Generates Fresh Unguessable Identifier
    # --------------------------------------------------------------------------
    def test_03_create_share_generates_fresh_unguessable_id(self):
        """Verify enabling sharing generates an unguessable token and valid URL format."""
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("shared"))
        self.assertEqual(body.get("status"), "enabled")

        share_id = body.get("share_id")
        self.assertTrue(share_id)
        self.assertGreaterEqual(len(share_id), 20)
        # Phase 2B: Stable memorable path URL returned as primary, token_url preserved
        self.assertIn("/storage/document.txt", body.get("url"))
        self.assertIn(f"/storage/s/{share_id}", body.get("token_url"))

        # Verify status query
        q_status, q_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=document.txt",
            method="GET",
            headers=self.auth_headers()
        )
        self.assertEqual(q_status, 200)
        self.assertTrue(q_body.get("shared"))
        self.assertEqual(q_body.get("share_id"), share_id)
        self.assertIn("/storage/document.txt", q_body.get("url"))
        self.assertIn(f"/storage/s/{share_id}", q_body.get("token_url"))

    # --------------------------------------------------------------------------
    # 4. Public Share Link Serves Content Without Authentication
    # --------------------------------------------------------------------------
    def test_04_public_share_enabled_without_auth(self):
        """Verify anyone possessing the share link can open the file without signing in."""
        _, create_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        share_id = create_body["share_id"]

        # Public access without ANY auth header or cookies
        pub_status, _, pub_bytes, pub_headers = http_request(
            f"{self.base_url}/storage/s/{share_id}",
            method="GET"
        )
        self.assertEqual(pub_status, 200)
        self.assertEqual(pub_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

        # Verify security headers
        self.assertIn("inline", pub_headers.get("Content-Disposition", ""))
        self.assertIn("document.txt", pub_headers.get("Content-Disposition", ""))
        self.assertEqual(pub_headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(pub_headers.get("X-Frame-Options"), "SAMEORIGIN")

        # Verify internal paths are not exposed
        for hdr_val in pub_headers.values():
            self.assertNotIn(str(self.storage_dir), str(hdr_val))

    # --------------------------------------------------------------------------
    # 5. Revoking Share Immediately Disables Access
    # --------------------------------------------------------------------------
    def test_05_revoking_share_disables_access_immediately(self):
        """Verify turning sharing OFF revokes public access immediately (returns 404)."""
        _, create_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        share_id = create_body["share_id"]

        # Confirm working
        pub_status, _, _, _ = http_request(f"{self.base_url}/storage/s/{share_id}")
        self.assertEqual(pub_status, 200)

        # Revoke share
        del_status, del_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=document.txt",
            method="DELETE",
            headers=self.auth_headers()
        )
        self.assertEqual(del_status, 200)
        self.assertEqual(del_body.get("status"), "revoked")
        self.assertFalse(del_body.get("shared"))

        # Public request must now return 404
        rev_status, rev_body, _, _ = http_request(f"{self.base_url}/storage/s/{share_id}")
        self.assertEqual(rev_status, 404)
        self.assertIn("revoked", rev_body.get("error", "").lower())

    # --------------------------------------------------------------------------
    # 6. Re-enabling Generates Fresh URL; Revoked URL Remains Dead
    # --------------------------------------------------------------------------
    def test_06_re_enabling_generates_fresh_id_and_old_id_remains_dead(self):
        """Verify re-enabling generates a new unguessable ID and old revoked token cannot work."""
        # 1. First share
        _, body1, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        id1 = body1["share_id"]

        # 2. Revoke
        http_request(f"{self.base_url}/storage/share?path=document.txt", method="DELETE", headers=self.auth_headers())

        # 3. Second share (re-enable)
        _, body2, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        id2 = body2["share_id"]

        self.assertNotEqual(id1, id2, "Re-enabling sharing must produce a fresh, unguessable token")

        # Old link returns 404
        s1, _, _, _ = http_request(f"{self.base_url}/storage/s/{id1}")
        self.assertEqual(s1, 404)

        # New link returns 200
        s2, _, bytes2, _ = http_request(f"{self.base_url}/storage/s/{id2}")
        self.assertEqual(s2, 200)
        self.assertEqual(bytes2.decode("utf-8"), "Hello PersonalServer Storage!")

    # --------------------------------------------------------------------------
    # 7. Directory Sharing Is Strictly Rejected
    # --------------------------------------------------------------------------
    def test_07_directory_sharing_rejected(self):
        """Verify directory shares are rejected with 400 in Phase 2A."""
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 400)
        self.assertIn("directory", body.get("error", "").lower())

    # --------------------------------------------------------------------------
    # 8. Untrusted HTML and SVG Content Served Safely
    # --------------------------------------------------------------------------
    def test_08_untrusted_html_and_svg_served_safely(self):
        """Verify HTML/SVG cannot execute active same-origin scripts."""
        # HTML file
        _, b_html, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "untrusted.html"},
            headers=self.auth_headers()
        )
        s_html, _, _, h_html = http_request(f"{self.base_url}/storage/s/{b_html['share_id']}")
        self.assertEqual(s_html, 200)
        self.assertIn("text/plain", h_html.get("Content-Type", ""))
        self.assertIn("sandbox", h_html.get("Content-Security-Policy", ""))
        self.assertEqual(h_html.get("X-Content-Type-Options"), "nosniff")

        # SVG file
        _, b_svg, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "vector.svg"},
            headers=self.auth_headers()
        )
        s_svg, _, _, h_svg = http_request(f"{self.base_url}/storage/s/{b_svg['share_id']}")
        self.assertEqual(s_svg, 200)
        self.assertIn("text/plain", h_svg.get("Content-Type", ""))
        self.assertIn("sandbox", h_svg.get("Content-Security-Policy", ""))
        self.assertEqual(h_svg.get("X-Content-Type-Options"), "nosniff")

    # --------------------------------------------------------------------------
    # 9. Path Traversal and Rogue Share Tokens
    # --------------------------------------------------------------------------
    def test_09_path_traversal_and_malformed_tokens_blocked(self):
        """Verify traversal attempts, null bytes, and arbitrary IDs return 404."""
        for rogue in (
            "../etc/passwd",
            "..%2f..%2fconfig",
            "nonexistent_token_1234567890",
            "token%00null",
            "token/subpath"
        ):
            status, _, _, _ = http_request(f"{self.base_url}/storage/s/{rogue}")
            self.assertEqual(status, 404)

    # --------------------------------------------------------------------------
    # 10. Private Storage Endpoints Still Require Authentication
    # --------------------------------------------------------------------------
    def test_10_private_storage_endpoints_still_require_auth(self):
        """Verify normal storage operations (/storage/list, /download) still enforce auth."""
        fake_auth = {"Authorization": "Bearer bad-token"}

        for ep in (
            "/storage/list",
            "/storage/download?path=document.txt",
            "/storage/usage",
            "/storage/nodes"
        ):
            status, _, _, _ = http_request(f"{self.base_url}{ep}", headers=fake_auth)
            self.assertEqual(status, 401, f"Endpoint {ep} should reject unauthorized requests")

    # --------------------------------------------------------------------------
    # 11. Stable Path URLs: Public Access When Sharing is ON
    # --------------------------------------------------------------------------
    def test_11_stable_path_public_access_when_sharing_on(self):
        """Verify GET /storage/<path> allows unauthenticated access when sharing is ON."""
        # Enable sharing for document.txt
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("/storage/document.txt", body["url"])

        # Unauthenticated GET /storage/document.txt
        pub_status, _, pub_bytes, pub_headers = http_request(f"{self.base_url}/storage/document.txt")
        self.assertEqual(pub_status, 200)
        self.assertEqual(pub_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

        # Verify inline disposition and nosniff headers
        self.assertIn("inline", pub_headers.get("Content-Disposition", ""))
        self.assertIn("document.txt", pub_headers.get("Content-Disposition", ""))
        self.assertEqual(pub_headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(pub_headers.get("X-Frame-Options"), "SAMEORIGIN")

    # --------------------------------------------------------------------------
    # 12. Stable Path URLs: Blocked When Sharing is OFF (Anti-Enumeration 404)
    # --------------------------------------------------------------------------
    def test_12_stable_path_blocked_when_sharing_off(self):
        """Verify unauthenticated GET /storage/<path> returns 404 when sharing is OFF."""
        # Create unshared private file
        private_file = self.storage_dir / "private_plan.txt"
        private_file.write_text("Top secret roadmap", encoding="utf-8")

        # Unauthenticated request MUST return 404 (not 401/403) to prevent file enumeration
        status, body, _, _ = http_request(f"{self.base_url}/storage/private_plan.txt")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error"), "File not found")

    # --------------------------------------------------------------------------
    # 13. Stable Path URLs: Immediate Revocation
    # --------------------------------------------------------------------------
    def test_13_stable_path_revocation_immediate(self):
        """Verify turning sharing OFF immediately disables stable public path access."""
        # Enable sharing
        http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        self.assertEqual(http_request(f"{self.base_url}/storage/document.txt")[0], 200)

        # Disable sharing
        del_status, del_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=document.txt",
            method="DELETE",
            headers=self.auth_headers()
        )
        self.assertEqual(del_status, 200)
        self.assertFalse(del_body.get("shared"))

        # The very next unauthenticated request MUST return 404
        rev_status, rev_body, _, _ = http_request(f"{self.base_url}/storage/document.txt")
        self.assertEqual(rev_status, 404)

    # --------------------------------------------------------------------------
    # 14. Encoded Paths, Spaces, and Unicode Filenames
    # --------------------------------------------------------------------------
    def test_14_encoded_paths_and_unicode_filenames(self):
        """Verify URLs with spaces and Unicode filenames resolve correctly and safely."""
        # 1. File with spaces: CV/My Resume.pdf
        cv_dir = self.storage_dir / "CV"
        cv_dir.mkdir(exist_ok=True)
        resume_file = cv_dir / "My Resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 sample pdf content")

        create_status, create_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "CV/My Resume.pdf"},
            headers=self.auth_headers()
        )
        self.assertEqual(create_status, 200)
        self.assertIn("CV/My%20Resume.pdf", create_body["url"])

        # Public access via URL with %20
        res_status, _, res_bytes, res_hdrs = http_request(f"{self.base_url}/storage/CV/My%20Resume.pdf")
        self.assertEqual(res_status, 200)
        self.assertEqual(res_bytes, b"%PDF-1.4 sample pdf content")
        self.assertIn("application/pdf", res_hdrs.get("Content-Type", ""))

        # 2. Unicode filename: CV/résumé.pdf
        unicode_file = cv_dir / "résumé.pdf"
        unicode_file.write_bytes(b"%PDF-1.4 unicode pdf")

        http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "CV/résumé.pdf"},
            headers=self.auth_headers()
        )
        encoded_unicode = "CV/r%C3%A9sum%C3%A9.pdf"
        uni_status, _, uni_bytes, _ = http_request(f"{self.base_url}/storage/{encoded_unicode}")
        self.assertEqual(uni_status, 200)
        self.assertEqual(uni_bytes, b"%PDF-1.4 unicode pdf")

        # Revoke and verify alternate encodings cannot bypass
        http_request(
            f"{self.base_url}/storage/share?path=CV/My%20Resume.pdf",
            method="DELETE",
            headers=self.auth_headers()
        )
        # Bypasses like %4d (%4d = 'M') or ./ variants must also get 404
        self.assertEqual(http_request(f"{self.base_url}/storage/CV/My%20Resume.pdf")[0], 404)
        self.assertEqual(http_request(f"{self.base_url}/storage/CV/%4dy%20Resume.pdf")[0], 404)

    # --------------------------------------------------------------------------
    # 15. Directory Access Blocked via Public File Routes
    # --------------------------------------------------------------------------
    def test_15_directory_access_via_public_path_blocked(self):
        """Verify folders cannot be listed or exposed via public path routes."""
        # nested_folder exists
        status, _, _, _ = http_request(f"{self.base_url}/storage/nested_folder")
        self.assertEqual(status, 404)

        status_slash, _, _, _ = http_request(f"{self.base_url}/storage/nested_folder/")
        self.assertEqual(status_slash, 404)

    # --------------------------------------------------------------------------
    # 16. Private Access: Authenticated vs Unauthenticated
    # --------------------------------------------------------------------------
    def test_16_private_access_authenticated_vs_unauthenticated(self):
        """Verify unshared private files are accessible to authenticated user, but 404 to public."""
        secret_file = self.storage_dir / "confidential.txt"
        secret_file.write_text("Private internal notes", encoding="utf-8")

        # 1. Unauthenticated -> 404 Not Found
        pub_status, _, _, _ = http_request(f"{self.base_url}/storage/confidential.txt")
        self.assertEqual(pub_status, 404)

        # 2. Authenticated -> 200 OK
        auth_status, _, auth_bytes, _ = http_request(
            f"{self.base_url}/storage/confidential.txt",
            headers=self.auth_headers()
        )
        self.assertEqual(auth_status, 200)
        self.assertEqual(auth_bytes.decode("utf-8"), "Private internal notes")

    # --------------------------------------------------------------------------
    # 17. Preview Endpoint Verification
    # --------------------------------------------------------------------------
    def test_17_preview_endpoint_behavior(self):
        """Verify /storage/preview behavior for authenticated, shared, and untrusted files."""
        # 1. Authenticated preview of private file
        status, _, content, hdrs = http_request(
            f"{self.base_url}/storage/preview?path=document.txt",
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("inline", hdrs.get("Content-Disposition", ""))
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")

        # 2. Unauthenticated preview of unshared private file -> 401
        fake_auth = {"Authorization": "Bearer bad-token"}
        status, _, _, _ = http_request(
            f"{self.base_url}/storage/preview?path=document.txt",
            headers=fake_auth
        )
        self.assertEqual(status, 401)

        # 3. Share document.txt -> unauthenticated preview succeeds
        http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        status, _, content, _ = http_request(f"{self.base_url}/storage/preview?path=document.txt")
        self.assertEqual(status, 200)
        self.assertEqual(content.decode("utf-8"), "Hello PersonalServer Storage!")

        # 4. Untrusted HTML preview: Text/plain with CSP sandbox
        status, _, html_bytes, html_hdrs = http_request(
            f"{self.base_url}/storage/preview?path=untrusted.html",
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("text/plain", html_hdrs.get("Content-Type", ""))
        self.assertIn("sandbox", html_hdrs.get("Content-Security-Policy", ""))

        # 5. Untrusted SVG preview: Text/plain with CSP sandbox
        status, _, svg_bytes, svg_hdrs = http_request(
            f"{self.base_url}/storage/preview?path=vector.svg",
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("text/plain", svg_hdrs.get("Content-Type", ""))
        self.assertIn("sandbox", svg_hdrs.get("Content-Security-Policy", ""))

        # 6. Traversal attempts on /storage/preview -> 403 or 404
        status, _, _, _ = http_request(
            f"{self.base_url}/storage/preview?path=../etc/passwd",
            headers=self.auth_headers()
        )
        self.assertIn(status, (403, 404))

    # --------------------------------------------------------------------------
    # 18. Path Traversal on Stable Path Route Blocked
    # --------------------------------------------------------------------------
    def test_18_path_traversal_on_stable_path_route_blocked(self):
        """Verify traversal attempts on /storage/<path> return 404 without leaking files."""
        for traversal in (
            "/storage/../etc/passwd",
            "/storage/..%2f..%2fconfig/secrets/auth.token",
            "/storage/nested_folder/../../storage/document.txt",
            "/storage/document.txt%00.png"
        ):
            status, _, _, _ = http_request(f"{self.base_url}{traversal}")
            self.assertEqual(status, 404, f"Traversal '{traversal}' must return 404")

    # --------------------------------------------------------------------------
    # 19. Reserved API Endpoints Remain Authenticated
    # --------------------------------------------------------------------------
    def test_19_reserved_api_routes_not_bypassed_by_public_router(self):
        """Verify reserved control-plane routes (/storage/list, /nodes, etc.) are never treated as files."""
        fake_auth = {"Authorization": "Bearer invalid-token"}
        for ep in (
            "/storage/list",
            "/storage/nodes",
            "/storage/download?path=document.txt",
            "/storage/usage",
            "/storage/share?path=document.txt"
        ):
            status, _, _, _ = http_request(f"{self.base_url}{ep}", headers=fake_auth)
            self.assertEqual(status, 401, f"Route {ep} must enforce authentication")


if __name__ == "__main__":
    unittest.main()

