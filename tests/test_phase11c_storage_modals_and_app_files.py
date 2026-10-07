#!/usr/bin/env python3
"""
Test Suite for Phase 11C:
- Reusable Native Dark Modal System for Storage Explorer
- Sandboxed Application File Management Foundation (GET/POST/DELETE /apps/<app_id>/files)
- Security boundary testing: rejection of escaping paths, host filesystem protection
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import unittest
from pathlib import Path

CONTROLLER_URL = "http://127.0.0.1:8000"
NODE_API_URL = "http://100.85.108.5:8080"
ENROLLMENT_TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "secrets", "enrollment.token")

def get_token():
    if os.path.exists(ENROLLMENT_TOKEN_PATH):
        with open(ENROLLMENT_TOKEN_PATH, "r") as f:
            return f.read().strip()
    return "dev-enrollment-token-insecure"

AUTH_HEADER = {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

def http_json(url, method="GET", data=None, headers=None):
    req_headers = {"Content-Type": "application/json"}
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

class StorageModalsAndAppFilesTests(unittest.TestCase):

    def test_01_static_bundle_versioning_and_modal_dom(self):
        """Verify index.html contains modal system and cache-busting v1.4.0 assets."""
        with urllib.request.urlopen(f"{NODE_API_URL}/") as r:
            self.assertEqual(r.status, 200)
            html = r.read().decode("utf-8")
            self.assertIn("ps-modal-backdrop", html)
            self.assertIn("ps-modal-frame", html)
            self.assertIn("app-files-panel", html)
            self.assertIn("style.css?v=1.4.0", html)
            self.assertIn("app.js?v=1.4.0", html)

    def test_02_app_files_lifecycle_and_sandboxing(self):
        """Verify sandboxed application files creation, listing, subfolder, and delete."""
        app_name = f"files_app_{int(time.time())}"
        status, create_res = http_json(f"{CONTROLLER_URL}/apps", method="POST", data={
            "name": app_name,
            "image": "python:3.11-slim",
            "port": 8000
        }, headers=AUTH_HEADER)
        self.assertIn(status, [200, 201])
        app_id = create_res.get("app", {}).get("app_id")
        self.assertTrue(app_id)

        try:
            # 1. Initial file listing (should have auto-initialized README.md and Dockerfile)
            status, list_res = http_json(f"{CONTROLLER_URL}/apps/{app_id}/files", headers=AUTH_HEADER)
            self.assertEqual(status, 200)
            item_names = [i["name"] for i in list_res.get("items", [])]
            self.assertIn("README.md", item_names)
            self.assertIn("Dockerfile", item_names)

            # 2. Subfolder creation inside app storage
            status, mkdir_res = http_json(f"{CONTROLLER_URL}/apps/{app_id}/files/mkdir", method="POST", data={
                "name": "src",
                "path": ""
            }, headers=AUTH_HEADER)
            self.assertEqual(status, 200)

            # 3. List inside subfolder (empty)
            status, sublist_res = http_json(f"{CONTROLLER_URL}/apps/{app_id}/files?path=src", headers=AUTH_HEADER)
            self.assertEqual(status, 200)
            self.assertEqual(sublist_res.get("count"), 0)

            # 4. Sandboxing security: attempt path traversal outside app directory
            status, escape_res = http_json(f"{CONTROLLER_URL}/apps/{app_id}/files?path=../../etc", headers=AUTH_HEADER)
            self.assertIn(status, [400, 403, 404])

            # 5. Delete file inside app
            status, del_res = http_json(f"{CONTROLLER_URL}/apps/{app_id}/files?path=src", method="DELETE", headers=AUTH_HEADER)
            self.assertEqual(status, 200)

        finally:
            # Clean up application
            http_json(f"{CONTROLLER_URL}/apps/{app_id}", method="DELETE", headers=AUTH_HEADER)

if __name__ == "__main__":
    unittest.main()
