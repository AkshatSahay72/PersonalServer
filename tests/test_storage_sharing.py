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
from http.server import HTTPServer, ThreadingHTTPServer

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

        self._orig_env_data_dir = os.environ.get("PERSONALSERVER_DATA_DIR")
        os.environ["PERSONALSERVER_DATA_DIR"] = str(self.data_dir)

        # Start NodeAPI test server on loopback with ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), NodeAPIHandler)
        self.server.daemon_threads = True
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

        if self._orig_env_data_dir is not None:
            os.environ["PERSONALSERVER_DATA_DIR"] = self._orig_env_data_dir
        else:
            os.environ.pop("PERSONALSERVER_DATA_DIR", None)

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
        # Canonical Path-Based Public Sharing URL
        self.assertIn("/share/document.txt", body.get("url"))
        self.assertIn("/share/document.txt", body.get("share_url"))
        self.assertIn(f"/share/{share_id}", body.get("token_url"))

        # Verify status query
        q_status, q_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=document.txt",
            method="GET",
            headers=self.auth_headers()
        )
        self.assertEqual(q_status, 200)
        self.assertTrue(q_body.get("shared"))
        self.assertEqual(q_body.get("share_id"), share_id)
        self.assertIn("/share/document.txt", q_body.get("url"))
        self.assertIn("/share/document.txt", q_body.get("share_url"))
        self.assertIn(f"/share/{share_id}", q_body.get("token_url"))

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

    def test_04b_public_file_share_under_share_id(self):
        """Verify Phase 2E: public file access works under /share/<share_id> route."""
        _, create_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        share_id = create_body["share_id"]

        # 1. Root /share/<share_id> serves file inline
        root_status, _, root_bytes, root_headers = http_request(
            f"{self.base_url}/share/{share_id}",
            method="GET"
        )
        self.assertEqual(root_status, 200)
        self.assertEqual(root_bytes.decode("utf-8"), "Hello PersonalServer Storage!")
        self.assertIn("inline", root_headers.get("Content-Disposition", ""))
        self.assertEqual(root_headers.get("X-Content-Type-Options"), "nosniff")

        # 2. /share/<share_id>/file serves file inline
        file_status, _, file_bytes, _ = http_request(
            f"{self.base_url}/share/{share_id}/file",
            method="GET"
        )
        self.assertEqual(file_status, 200)
        self.assertEqual(file_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

        # 3. /share/<share_id>/download serves as attachment
        dl_status, _, dl_bytes, dl_headers = http_request(
            f"{self.base_url}/share/{share_id}/download",
            method="GET"
        )
        self.assertEqual(dl_status, 200)
        self.assertEqual(dl_bytes.decode("utf-8"), "Hello PersonalServer Storage!")
        self.assertIn("attachment", dl_headers.get("Content-Disposition", ""))

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
    # 7. Secure Folder Sharing (Phase 2D)
    # --------------------------------------------------------------------------
    def test_07a_folder_sharing_enable_and_containment(self):
        """Verify folders can be shared, generating dedicated viewer and read-only endpoints."""
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["shared"])
        self.assertTrue(body["is_dir"])
        share_id = body["share_id"]
        self.assertTrue(share_id)

        # 1. Root /share/<share_id> serves dedicated HTML folder viewer
        v_status, _, v_bytes, v_headers = http_request(f"{self.base_url}/share/{share_id}")
        self.assertEqual(v_status, 200)
        self.assertIn("text/html", v_headers.get("Content-Type", ""))
        self.assertIn(b"Shared Folder", v_bytes)

        # 2. /share/<share_id>/list returns folder items
        l_status, l_body, _, _ = http_request(f"{self.base_url}/share/{share_id}/list")
        self.assertEqual(l_status, 200)
        self.assertEqual(l_body["share_id"], share_id)
        item_names = [it["name"] for it in l_body["items"]]
        self.assertIn("inner.txt", item_names)

        # 3. /share/<share_id>/file?subpath=inner.txt streams file inline
        f_status, _, f_bytes, f_headers = http_request(f"{self.base_url}/share/{share_id}/file?subpath=inner.txt")
        self.assertEqual(f_status, 200)
        self.assertEqual(f_bytes.decode("utf-8"), "Inner file content")
        self.assertIn("inline", f_headers.get("Content-Disposition", ""))

        # 4. /share/<share_id>/download?subpath=inner.txt downloads as attachment
        d_status, _, d_bytes, d_headers = http_request(f"{self.base_url}/share/{share_id}/download?subpath=inner.txt")
        self.assertEqual(d_status, 200)
        self.assertEqual(d_bytes.decode("utf-8"), "Inner file content")
        self.assertIn("attachment", d_headers.get("Content-Disposition", ""))

    def test_07b_folder_boundary_and_traversal_rejection(self):
        """Enforce strict folder containment: reject traversal and access to parent/sibling files."""
        _, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder"},
            headers=self.auth_headers()
        )
        share_id = body["share_id"]

        # Traversal in listing
        status_list_dot, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/list?subpath=../")
        self.assertIn(status_list_dot, (400, 403, 404))

        status_list_deep, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/list?subpath=../../etc")
        self.assertIn(status_list_deep, (400, 403, 404))

        # Traversal in file access trying to reach sibling file (document.txt)
        status_file_sibling, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/file?subpath=../document.txt")
        self.assertIn(status_file_sibling, (400, 403, 404))

        # Direct escape attempting to access secrets
        status_file_escape, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/file?subpath=../../config/secrets/auth.token")
        self.assertIn(status_file_escape, (400, 403, 404))

    def test_07c_folder_revocation_cascades_immediately(self):
        """Verify turning OFF folder share immediately revokes all descendant access."""
        _, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder"},
            headers=self.auth_headers()
        )
        share_id = body["share_id"]

        # Verify accessible before revocation
        s_before, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/file?subpath=inner.txt")
        self.assertEqual(s_before, 200)

        # Revoke folder share
        del_status, del_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=nested_folder",
            method="DELETE",
            headers=self.auth_headers()
        )
        self.assertEqual(del_status, 200)
        self.assertFalse(del_body["shared"])

        # Immediate revocation across all endpoints
        s_after_root, _, _, _ = http_request(f"{self.base_url}/share/{share_id}")
        self.assertEqual(s_after_root, 404)

        s_after_list, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/list")
        self.assertEqual(s_after_list, 404)

        s_after_file, _, _, _ = http_request(f"{self.base_url}/share/{share_id}/file?subpath=inner.txt")
        self.assertEqual(s_after_file, 404)

    def test_07d_nested_folder_navigation(self):
        """Verify navigating and accessing deeply nested folders within the shared boundary."""
        deep_dir = self.sub_folder / "sub1" / "sub2"
        deep_dir.mkdir(parents=True, exist_ok=True)
        deep_file = deep_dir / "deep_doc.txt"
        deep_file.write_text("Deeply nested text content", encoding="utf-8")

        _, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder"},
            headers=self.auth_headers()
        )
        share_id = body["share_id"]

        # List subfolder sub1/sub2
        l_status, l_body, _, _ = http_request(f"{self.base_url}/share/{share_id}/list?subpath=sub1/sub2")
        self.assertEqual(l_status, 200)
        item_names = [it["name"] for it in l_body["items"]]
        self.assertIn("deep_doc.txt", item_names)

        # Access deep file
        f_status, _, f_bytes, _ = http_request(f"{self.base_url}/share/{share_id}/file?subpath=sub1/sub2/deep_doc.txt")
        self.assertEqual(f_status, 200)
        self.assertEqual(f_bytes.decode("utf-8"), "Deeply nested text content")

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
        self.assertIn("/share/document.txt", body["url"])
        self.assertIn(f"/share/{body['share_id']}", body["token_url"])

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
        self.assertIn("share/CV/My%20Resume.pdf", create_body["url"])
        self.assertIn(f"/share/{create_body['share_id']}", create_body["token_url"])

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

    # --------------------------------------------------------------------------
    # 20. Multipart Upload & Network Auth Isolation
    # --------------------------------------------------------------------------
    def test_20_multipart_upload_and_tailscale_private_auth(self):
        """Verify multipart upload saves files, preserves filenames, and enforces network scope."""
        boundary = "----TestBoundary12345"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="My Uploaded Doc.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"Uploaded content with spaces in name\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        # 1. Upload via valid auth token
        req = urllib.request.Request(
            f"{self.base_url}/storage/upload?path=",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "Authorization": f"Bearer {self.auth_token}"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            res_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res_data.get("status"), "uploaded")
            self.assertIn("My Uploaded Doc.txt", res_data.get("files", []))

        uploaded_file = self.storage_dir / "My Uploaded Doc.txt"
        self.assertTrue(uploaded_file.exists())
        self.assertEqual(uploaded_file.read_text(encoding="utf-8"), "Uploaded content with spaces in name")

    # --------------------------------------------------------------------------
    # 21. Preview and Share UI Flow & Node Normalization (Phase 2C.4)
    # --------------------------------------------------------------------------
    def test_21_preview_and_share_ui_flow_and_node_normalization(self):
        """
        Validates Phase 2C.4 acceptance criteria:
        1. Preview works with node=auto and on unshared files via local loopback.
        2. Share query, enable, and revoke work with node=auto.
        3. Nested files with spaces and unicode are handled cleanly.
        4. Public proxied access without CF email/token is rejected.
        """
        # Create a nested test file with spaces
        nested_dir = self.storage_dir / "Nested Folder"
        nested_dir.mkdir(parents=True, exist_ok=True)
        test_file = nested_dir / "Preview Test File.txt"
        test_file.write_text("Detailed preview test content for Phase 2C.4", encoding="utf-8")

        encoded_path = urllib.parse.quote("Nested Folder/Preview Test File.txt")

        # 1. Preview of unshared file from loopback (no bearer token header) succeeds
        status, _, content, hdrs = http_request(
            f"{self.base_url}/storage/preview?path={encoded_path}&node=auto"
        )
        self.assertEqual(status, 200, "Loopback preview with node=auto must return 200")
        self.assertEqual(content.decode("utf-8"), "Detailed preview test content for Phase 2C.4")
        self.assertIn("inline", hdrs.get("Content-Disposition", ""))

        # 2. Query initial share status with node=auto -> shared: False
        status, res, _, _ = http_request(
            f"{self.base_url}/storage/share?path={encoded_path}&node=auto"
        )
        self.assertEqual(status, 200)
        self.assertFalse(res.get("shared"), "Initially unshared file must report shared=False")

        # 3. Enable sharing via POST /storage/share with node="auto"
        status, res, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={
                "path": "Nested Folder/Preview Test File.txt",
                "node": "auto"
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("shared"))
        share_url = res.get("url")
        self.assertTrue(share_url and "/share/" in share_url)

        # 4. Query share status again with node=auto -> shared: True
        status, res, _, _ = http_request(
            f"{self.base_url}/storage/share?path={encoded_path}&node=auto"
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("shared"))
        self.assertEqual(res.get("url"), share_url)

        # 5. Public access to shared file succeeds without authentication
        pub_status, _, pub_content, _ = http_request(
            f"{self.base_url}/storage/Nested%20Folder/Preview%20Test%20File.txt"
        )
        self.assertEqual(pub_status, 200)
        self.assertEqual(pub_content.decode("utf-8"), "Detailed preview test content for Phase 2C.4")

        # 6. Revoke share via DELETE /storage/share with node="auto"
        status, res, _, _ = http_request(
            f"{self.base_url}/storage/share?path={encoded_path}&node=auto",
            method="DELETE"
        )
        self.assertEqual(status, 200)
        self.assertFalse(res.get("shared"))
        self.assertEqual(res.get("status"), "revoked")

        # 7. Share query reflects revoked state -> shared: False
        status, res, _, _ = http_request(
            f"{self.base_url}/storage/share?path={encoded_path}&node=auto"
        )
        self.assertEqual(status, 200)
        self.assertFalse(res.get("shared"))

        # 8. Unauthenticated public access to now-revoked file returns 404
        pub_status, _, _, _ = http_request(
            f"{self.base_url}/storage/Nested%20Folder/Preview%20Test%20File.txt",
            headers={"Cf-Ray": "simulated-public-cf-tunnel"}
        )
        self.assertEqual(pub_status, 404, "Public access to revoked file must return 404")

        # 9. Public proxied preview of unshared file without auth returns 401
        proxy_preview_status, _, _, _ = http_request(
            f"{self.base_url}/storage/preview?path={encoded_path}",
            headers={"Cf-Ray": "simulated-public-cf-tunnel"}
        )
        self.assertEqual(proxy_preview_status, 401, "Public preview without auth must return 401")

    def test_22_concurrent_requests_slow_client_non_blocking(self):
        """
        Phase 2C.7 Regression: Verify holding one HTTP connection open with an incomplete request
        does not block subsequent concurrent requests (e.g. /health) from completing promptly.
        """
        import socket
        # 1. Connect first client socket and send incomplete headers (holding connection open)
        s1 = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s1.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: true\r\n")
            # Do NOT send the terminating \r\n\r\n yet; socket remains open and waiting in receive

            # 2. While s1 is holding connection open, send a prompt request to /health from second client
            t0 = time.time()
            status, res, _, _ = http_request(f"{self.base_url}/health")
            elapsed = time.time() - t0

            # 3. Assert second request returned 200 and completed promptly (< 1.5 seconds)
            self.assertEqual(status, 200, "Concurrent /health request must return 200")
            self.assertEqual(res.get("status"), "online")
            self.assertLess(elapsed, 3.0, f"/health took {elapsed:.3f}s; must not be starved by slow connection")
        finally:
            try:
                s1.close()
            except Exception:
                pass

    def test_23_client_disconnect_during_file_response(self):
        """
        Phase 2C.7 Regression: Verify client abrupt disconnection during streaming of a larger file
        does not crash the server or impair subsequent request handling.
        """
        import socket
        # 1. Create a larger test file (512 KB)
        large_file = self.storage_dir / "large_stream_test.bin"
        chunk_pattern = b"A" * 65536
        with open(large_file, "wb") as f:
            for _ in range(8):  # 512 KB
                f.write(chunk_pattern)

        # 2. Connect raw socket and request the file
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            req = f"GET /storage/preview?path=large_stream_test.bin HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            s.sendall(req.encode("utf-8"))

            # Read only response headers and partial data (e.g. first 1024 bytes)
            partial = s.recv(1024)
            self.assertIn(b"200 OK", partial)

            # 3. Abruptly close the socket mid-stream (simulates browser tab close or network loss)
            s.close()
        except Exception:
            try:
                s.close()
            except Exception:
                pass

        # 4. Give server a moment to observe disconnect and ensure it continues serving requests
        time.sleep(0.1)
        status, res, _, _ = http_request(f"{self.base_url}/health")
        self.assertEqual(status, 200, "Server must remain healthy and responsive after client disconnect")
        self.assertEqual(res.get("status"), "online")

        # 5. Clean up
        try:
            large_file.unlink()
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Phase 4 Tests: Path-Based Public Sharing
    # --------------------------------------------------------------------------
    def test_24_path_based_file_share_inline_and_download(self):
        """Phase 4: Active public file share returns content inline and supports ?download=1."""
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("shared"))
        canonical_url = body.get("url")
        self.assertIn("/share/document.txt", canonical_url)

        # 1. Unauthenticated request to canonical path URL serves inline content
        pub_status, _, pub_bytes, pub_headers = http_request(f"{self.base_url}/share/document.txt")
        self.assertEqual(pub_status, 200)
        self.assertEqual(pub_bytes.decode("utf-8"), "Hello PersonalServer Storage!")
        self.assertIn("inline", pub_headers.get("Content-Disposition", ""))
        self.assertEqual(pub_headers.get("X-Content-Type-Options"), "nosniff")

        # 2. Unauthenticated request with ?download=1 serves as download attachment
        dl_status, _, dl_bytes, dl_headers = http_request(f"{self.base_url}/share/document.txt?download=1")
        self.assertEqual(dl_status, 200)
        self.assertEqual(dl_bytes.decode("utf-8"), "Hello PersonalServer Storage!")
        self.assertIn("attachment", dl_headers.get("Content-Disposition", ""))

    def test_25_path_based_folder_share_renders_folder_page(self):
        """Phase 4: Active public folder share renders folder page with injected __SHARE_RECORD__."""
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder", "is_dir": True},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("shared"))
        self.assertTrue(body.get("is_dir"))
        canonical_url = body.get("url")
        self.assertIn("/share/nested_folder", canonical_url)

        # 1. Unauthenticated request to folder URL serves shared_folder.html with injected record
        pub_status, _, pub_bytes, pub_headers = http_request(f"{self.base_url}/share/nested_folder")
        self.assertEqual(pub_status, 200)
        html_text = pub_bytes.decode("utf-8")
        self.assertIn("text/html", pub_headers.get("Content-Type", ""))
        self.assertIn("window.__SHARE_RECORD__", html_text)
        self.assertIn(body["share_id"], html_text)
        self.assertIn("nested_folder", html_text)

        # 2. Unauthenticated request to contained file within shared folder succeeds
        inner_status, _, inner_bytes, _ = http_request(f"{self.base_url}/share/nested_folder/inner.txt")
        self.assertEqual(inner_status, 200)
        self.assertEqual(inner_bytes.decode("utf-8"), "Inner file content")

    def test_26_path_based_share_revocation_immediate_denial(self):
        """Phase 4: Turning share OFF immediately revokes access (404); re-enabling restores access."""
        # Enable share
        http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        status, _, bytes_val, _ = http_request(f"{self.base_url}/share/document.txt")
        self.assertEqual(status, 200)

        # Turn share OFF (DELETE)
        del_status, del_body, _, _ = http_request(
            f"{self.base_url}/storage/share?path=document.txt",
            method="DELETE",
            headers=self.auth_headers()
        )
        self.assertEqual(del_status, 200)
        self.assertEqual(del_body.get("status"), "revoked")

        # Immediately denied with 404
        revoked_status, revoked_body, revoked_bytes, _ = http_request(f"{self.base_url}/share/document.txt")
        self.assertEqual(revoked_status, 404)
        self.assertIn("revoked", revoked_body.get("error", "").lower())

        # HTML accept receives styled error card
        html_status, _, html_bytes, html_hdrs = http_request(
            f"{self.base_url}/share/document.txt",
            headers={"Accept": "text/html,application/xhtml+xml"}
        )
        self.assertEqual(html_status, 404)
        self.assertIn("text/html", html_hdrs.get("Content-Type", ""))
        self.assertIn("Share Link Unavailable", html_bytes.decode("utf-8"))

        # Re-enable sharing restores access
        http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        restore_status, _, restore_bytes, _ = http_request(f"{self.base_url}/share/document.txt")
        self.assertEqual(restore_status, 200)
        self.assertEqual(restore_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

    def test_27_path_based_share_spaces_nested_and_unicode(self):
        """Phase 4: Proper URL encoding/decoding for spaces, nested paths, and Unicode filenames."""
        # Create nested file with spaces
        spaced_file = self.sub_folder / "My Resume.pdf"
        spaced_file.write_text("%PDF-1.5 Resume with spaces", encoding="utf-8")

        # Create nested file with Unicode
        unicode_file = self.sub_folder / "résumé.pdf"
        unicode_file.write_text("%PDF-1.5 Unicode résumé content", encoding="utf-8")

        # Share spaced file
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder/My Resume.pdf"},
            headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("nested_folder/My%20Resume.pdf", body.get("url"))

        # Access with encoded space
        sp_status, _, sp_bytes, _ = http_request(f"{self.base_url}/share/nested_folder/My%20Resume.pdf")
        self.assertEqual(sp_status, 200)
        self.assertIn(b"Resume with spaces", sp_bytes)

        # Share Unicode file
        u_status, u_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder/résumé.pdf"},
            headers=self.auth_headers()
        )
        self.assertEqual(u_status, 200)

        # Access with percent-encoded Unicode path
        encoded_unicode = urllib.parse.quote("résumé.pdf")
        u_get_status, _, u_get_bytes, _ = http_request(f"{self.base_url}/share/nested_folder/{encoded_unicode}")
        self.assertEqual(u_get_status, 200)
        self.assertIn("résumé".encode("utf-8"), u_get_bytes)

    def test_28_path_based_share_security_traversal_and_unshared_denied(self):
        """Phase 4: Deny unshared files, directory traversal, null bytes, and path escapes."""
        # Unshared file in storage must return 404 (never exposed just because path is known)
        secret_file = self.storage_dir / "confidential_secrets.txt"
        secret_file.write_text("CLASSIFIED", encoding="utf-8")
        unshared_status, _, _, _ = http_request(f"{self.base_url}/share/confidential_secrets.txt")
        self.assertEqual(unshared_status, 404, "Unshared file must never be accessible via /share/")

        # Traversal attempts
        traversal_attempts = [
            f"{self.base_url}/share/../config/secrets/auth.token",
            f"{self.base_url}/share/..%2fconfig%2fsecrets%2fauth.token",
            f"{self.base_url}/share/nested_folder/../../config/secrets/auth.token",
            f"{self.base_url}/share/document.txt%00.png",
            f"{self.base_url}/share//etc/passwd",
            f"{self.base_url}/share/",
        ]
        for url in traversal_attempts:
            t_status, _, _, _ = http_request(url)
            self.assertEqual(t_status, 404, f"Traversal attempt '{url}' must be denied with 404")

    def test_29_backward_compatibility_token_based_shares(self):
        """Phase 4: Existing token-based share URLs (/share/<token>) remain operational."""
        # 1. File share token
        status, body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "document.txt"},
            headers=self.auth_headers()
        )
        share_id = body["share_id"]

        token_status, _, token_bytes, _ = http_request(f"{self.base_url}/share/{share_id}")
        self.assertEqual(token_status, 200)
        self.assertEqual(token_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

        token_file_status, _, token_file_bytes, _ = http_request(f"{self.base_url}/share/{share_id}/file")
        self.assertEqual(token_file_status, 200)
        self.assertEqual(token_file_bytes.decode("utf-8"), "Hello PersonalServer Storage!")

        # 2. Folder share token
        f_status, f_body, _, _ = http_request(
            f"{self.base_url}/storage/share",
            method="POST",
            data={"path": "nested_folder", "is_dir": True},
            headers=self.auth_headers()
        )
        f_share_id = f_body["share_id"]

        list_status, list_body, _, _ = http_request(f"{self.base_url}/share/{f_share_id}/list")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_body["folder_name"], "nested_folder")
        self.assertTrue(any(item["name"] == "inner.txt" for item in list_body.get("items", [])))

    def test_30_private_storage_apis_remain_protected(self):
        """Phase 4: Verify /storage/list, /storage/download, /storage/upload, /storage/mkdir require auth."""
        private_endpoints = [
            (f"{self.base_url}/storage/list", "GET", None),
            (f"{self.base_url}/storage/download?path=document.txt", "GET", None),
            (f"{self.base_url}/storage/upload?path=", "POST", {}),
            (f"{self.base_url}/storage/mkdir", "POST", {"name": "TestDir"}),
        ]
        unauth_header = {"Authorization": "Bearer invalid-unauthorized-token"}
        for url, method, data in private_endpoints:
            status, _, _, _ = http_request(url, method=method, data=data, headers=unauth_header)
            self.assertEqual(status, 401, f"Private endpoint {method} {url} must return 401")


if __name__ == "__main__":
    unittest.main()


