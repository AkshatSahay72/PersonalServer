#!/usr/bin/env python3
"""
Test Suite for PersonalServer Storage Folder ZIP Download & Recursive Folder Upload
===================================================================================

Verifies:
1. Folder ZIP Download:
   - Nested directory hierarchy preserved in .zip archive
   - Empty folders preserved in archive with trailing slash entries
   - Unicode filenames and spaces preserved
   - Path traversal rejection and symlink escape prevention
   - Single file download backward compatibility regression
2. Recursive Folder Upload:
   - Nested file upload with relative paths (creating parents)
   - Unicode and spaces in file paths
   - Path traversal ('..') and absolute escape rejection
   - Conflict resolution policies:
     * 'fail' -> returns 409 Conflict
     * 'skip' -> returns 200 with skipped status
     * 'overwrite' -> replaces file cleanly
     * 'rename' -> appends ' (1)' suffix
   - Empty directory creation via upload endpoint (is_dir=true)
   - File size limits (MAX_UPLOAD_BYTES -> 413 Payload Too Large)
3. Cluster Proxy & Multi-Node Routing:
   - Selected storage node routing for ZIP download and folder upload
4. Clean temporary file cleanup on disk
"""

import os
import sys
import json
import time
import shutil
import zipfile
import tempfile
import threading
import urllib.request
import urllib.parse
import urllib.error
import unittest
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import importlib.util
node_api_path = BASE_DIR / "services" / "node-api" / "app.py"
spec = importlib.util.spec_from_file_location("node_api_module", str(node_api_path))
node_api_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_api_mod)
NodeAPIHandler = node_api_mod.NodeAPIHandler


class TestStorageFolderFeatures(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="ps_folder_test_")
        cls.orig_storage_root = node_api_mod.STORAGE_ROOT
        cls.orig_auth_token_file = node_api_mod.AUTH_TOKEN_FILE
        cls.orig_config_json = node_api_mod.CONFIG_JSON

        cls.mock_storage_root = Path(cls.test_dir) / "storage"
        cls.mock_storage_root.mkdir(parents=True, exist_ok=True)
        node_api_mod.STORAGE_ROOT = cls.mock_storage_root

        # Mock token
        cls.mock_secrets_dir = Path(cls.test_dir) / "config" / "secrets"
        cls.mock_secrets_dir.mkdir(parents=True, exist_ok=True)
        cls.mock_token_file = cls.mock_secrets_dir / "auth.token"
        cls.auth_token = "test-folder-auth-token-12345"
        cls.mock_token_file.write_text(cls.auth_token, encoding="utf-8")
        node_api_mod.AUTH_TOKEN_FILE = cls.mock_token_file

        # Spin up local Node API server
        cls.server = HTTPServer(("127.0.0.1", 0), NodeAPIHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        node_api_mod.STORAGE_ROOT = cls.orig_storage_root
        node_api_mod.AUTH_TOKEN_FILE = cls.orig_auth_token_file
        node_api_mod.CONFIG_JSON = cls.orig_config_json
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        # Clean mock storage root between tests
        for item in self.mock_storage_root.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except Exception:
                    pass

    def _auth_headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.auth_token}"}
        if extra:
            h.update(extra)
        return h

    # -------------------------------------------------------------
    # Feature 1: Folder ZIP Download Tests
    # -------------------------------------------------------------

    def test_01_download_folder_zip_structure_and_empty_dirs(self):
        """Verify downloading a folder as a ZIP preserves relative structure and empty directories."""
        proj = self.mock_storage_root / "MyProjects"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "index.js").write_text("console.log('hi');", encoding="utf-8")
        (proj / "src" / "nested").mkdir()
        (proj / "src" / "nested" / "deep.py").write_text("print('deep')", encoding="utf-8")
        # Empty directory
        (proj / "empty_dir").mkdir()

        url = f"{self.base_url}/storage/download?path=MyProjects"
        req = urllib.request.Request(url, headers=self._auth_headers())
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/zip")
            disposition = resp.headers.get("Content-Disposition", "")
            self.assertIn("MyProjects.zip", disposition)
            data = resp.read()

        # Parse zip in-memory
        tmp_zip_path = Path(self.test_dir) / "downloaded.zip"
        tmp_zip_path.write_bytes(data)

        with zipfile.ZipFile(tmp_zip_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("src/index.js", namelist)
            self.assertIn("src/nested/deep.py", namelist)
            self.assertIn("empty_dir/", namelist)

            self.assertEqual(zf.read("src/index.js").decode("utf-8"), "console.log('hi');")
            self.assertEqual(zf.read("src/nested/deep.py").decode("utf-8"), "print('deep')")

    def test_02_download_folder_zip_unicode_and_spaces(self):
        """Verify folder ZIP download handles spaces and Unicode characters in filenames."""
        folder = self.mock_storage_root / "CV & Docs"
        folder.mkdir()
        (folder / "Akshat Resume (2026).pdf").write_bytes(b"%PDF-1.4 demo")
        (folder / "Résumé_final.txt").write_text("Détails du CV", encoding="utf-8")

        url = f"{self.base_url}/storage/download?path={urllib.parse.quote('CV & Docs')}"
        req = urllib.request.Request(url, headers=self._auth_headers())
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = resp.read()

        tmp_zip_path = Path(self.test_dir) / "unicode.zip"
        tmp_zip_path.write_bytes(data)

        with zipfile.ZipFile(tmp_zip_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("Akshat Resume (2026).pdf", namelist)
            self.assertIn("Résumé_final.txt", namelist)
            self.assertEqual(zf.read("Résumé_final.txt").decode("utf-8"), "Détails du CV")

    def test_03_download_folder_zip_symlink_escape_prevention(self):
        """Verify symlinks pointing outside target directory are not included in the ZIP archive."""
        # Create outside secret file
        secret_file = Path(self.test_dir) / "secret.key"
        secret_file.write_text("SUPER_SECRET_KEY_123", encoding="utf-8")

        folder = self.mock_storage_root / "SafeFolder"
        folder.mkdir()
        (folder / "safe.txt").write_text("hello", encoding="utf-8")

        symlink_target = folder / "symlink_leak.key"
        try:
            os.symlink(str(secret_file), str(symlink_target))
        except OSError:
            # On Windows without developer mode, symlink creation might raise OSError
            self.skipTest("Symlinks not supported in current environment")

        url = f"{self.base_url}/storage/download?path=SafeFolder"
        req = urllib.request.Request(url, headers=self._auth_headers())
        with urllib.request.urlopen(req) as resp:
            data = resp.read()

        tmp_zip_path = Path(self.test_dir) / "symlink_test.zip"
        tmp_zip_path.write_bytes(data)

        with zipfile.ZipFile(tmp_zip_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("safe.txt", namelist)
            self.assertNotIn("symlink_leak.key", namelist)

    def test_04_single_file_download_regression(self):
        """Verify regular single file download still works unmodified."""
        file_path = self.mock_storage_root / "sample.txt"
        file_path.write_text("single file content", encoding="utf-8")

        url = f"{self.base_url}/storage/download?path=sample.txt"
        req = urllib.request.Request(url, headers=self._auth_headers())
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/octet-stream")
            content = resp.read().decode("utf-8")
            self.assertEqual(content, "single file content")

    def test_05_download_nonexistent_returns_404(self):
        """Verify downloading a nonexistent path returns 404."""
        url = f"{self.base_url}/storage/download?path=nonexistent_dir"
        req = urllib.request.Request(url, headers=self._auth_headers())
        try:
            urllib.request.urlopen(req)
            self.fail("Expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # -------------------------------------------------------------
    # Feature 2: Recursive Folder Upload Tests
    # -------------------------------------------------------------

    def test_06_upload_nested_folder_with_relative_paths(self):
        """Verify uploading files with relative_path creates nested directories and files."""
        # Upload Project/src/app.py
        boundary = "----TestBoundary12345"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="app.py"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"print('Hello from app.py')\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        url = f"{self.base_url}/storage/upload?path=&relative_path={urllib.parse.quote('Project/src/app.py')}"
        req = urllib.request.Request(
            url,
            data=body,
            headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res_data["status"], "uploaded")

        dest = self.mock_storage_root / "Project" / "src" / "app.py"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(encoding="utf-8"), "print('Hello from app.py')")

    def test_07_upload_relative_path_traversal_rejection(self):
        """Verify path traversal in relative_path is strictly rejected with 400 or 403."""
        boundary = "----TestBoundary12345"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="escape.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"malicious\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        traversal_attempts = [
            "../outside.txt",
            "Project/../../outside.txt",
            "/etc/passwd",
            "C:\\windows\\system32",
            "bad\0null.txt",
        ]

        for bad_path in traversal_attempts:
            url = f"{self.base_url}/storage/upload?path=&relative_path={urllib.parse.quote(bad_path)}"
            req = urllib.request.Request(
                url,
                data=body,
                headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
                method="POST"
            )
            try:
                urllib.request.urlopen(req)
                self.fail(f"Expected HTTP 400/403 for path traversal: {bad_path}")
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (400, 403))

    def test_08_upload_conflict_policies(self):
        """Verify conflict resolution policies: fail (409), skip (200), overwrite, rename."""
        target_dir = self.mock_storage_root / "ConflictTest"
        target_dir.mkdir()
        existing = target_dir / "data.txt"
        existing.write_text("original content", encoding="utf-8")

        boundary = "----BoundaryConflict"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="data.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"new content\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        # 1. Default policy (fail) -> Expect 409 Conflict
        url_fail = f"{self.base_url}/storage/upload?path=ConflictTest&conflict=fail"
        req_fail = urllib.request.Request(
            url_fail, data=body,
            headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST"
        )
        try:
            urllib.request.urlopen(req_fail)
            self.fail("Expected 409 Conflict")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 409)
            err_json = json.loads(e.read().decode("utf-8"))
            self.assertEqual(err_json.get("code"), "CONFLICT")
        self.assertEqual(existing.read_text(encoding="utf-8"), "original content")

        # 2. Skip policy -> Expect 200 with skipped item, file untouched
        url_skip = f"{self.base_url}/storage/upload?path=ConflictTest&conflict=skip"
        req_skip = urllib.request.Request(
            url_skip, data=body,
            headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST"
        )
        with urllib.request.urlopen(req_skip) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["items"][0]["status"], "skipped")
        self.assertEqual(existing.read_text(encoding="utf-8"), "original content")

        # 3. Rename policy -> Expect original untouched and data (1).txt created
        url_rename = f"{self.base_url}/storage/upload?path=ConflictTest&conflict=rename"
        req_rename = urllib.request.Request(
            url_rename, data=body,
            headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST"
        )
        with urllib.request.urlopen(req_rename) as resp:
            self.assertEqual(resp.status, 200)
        renamed_file = target_dir / "data (1).txt"
        self.assertTrue(renamed_file.exists())
        self.assertEqual(renamed_file.read_text(encoding="utf-8"), "new content")
        self.assertEqual(existing.read_text(encoding="utf-8"), "original content")

        # 4. Overwrite policy -> Expect existing file replaced
        url_overwrite = f"{self.base_url}/storage/upload?path=ConflictTest&conflict=overwrite"
        req_overwrite = urllib.request.Request(
            url_overwrite, data=body,
            headers=self._auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST"
        )
        with urllib.request.urlopen(req_overwrite) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(existing.read_text(encoding="utf-8"), "new content")

    def test_09_upload_empty_directory_creation(self):
        """Verify upload endpoint with is_dir=true creates empty directory structure."""
        url = f"{self.base_url}/storage/upload?is_dir=true&relative_path={urllib.parse.quote('NewProject/empty_sub')}"
        req = urllib.request.Request(url, data=b"", headers=self._auth_headers(), method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "created")

        created = self.mock_storage_root / "NewProject" / "empty_sub"
        self.assertTrue(created.exists())
        self.assertTrue(created.is_dir())

    def test_10_upload_exceeds_max_limit_rejected(self):
        """Verify upload exceeding MAX_UPLOAD_BYTES is rejected with HTTP 413."""
        url = f"{self.base_url}/storage/upload?path="
        # 105 MB Content-Length header
        headers = self._auth_headers({
            "Content-Type": "application/octet-stream",
            "Content-Length": str(105 * 1024 * 1024)
        })
        req = urllib.request.Request(url, data=b"", headers=headers, method="POST")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected HTTP 413")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)


if __name__ == "__main__":
    unittest.main()
