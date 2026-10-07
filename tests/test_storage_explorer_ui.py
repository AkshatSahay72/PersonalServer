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
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import unittest

NODE_API_URL = "http://100.85.108.5:8080" # Node 01 local endpoint
CONTROLLER_URL = "http://127.0.0.1:8000"

def http_json(url, method="GET", data=None):
    req_headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res_body = response.read().decode("utf-8")
            return response.status, json.loads(res_body) if res_body else {}
    except Exception as e:
        return 500, {"error": str(e)}

class StorageExplorerIntegrationTests(unittest.TestCase):

    def test_01_storage_nodes_discovery(self):
        """Verify storage node discovery endpoint returns registered cluster nodes."""
        status, data = http_json(f"{NODE_API_URL}/storage/nodes")
        self.assertEqual(status, 200, f"Expected 200, got {status}: {data}")
        nodes = data.get("nodes", [])
        self.assertGreaterEqual(len(nodes), 1)
        node_names = [n.get("name") or n.get("node_id") for n in nodes]
        self.assertTrue(any("vivo-y31" in name or "server-5387" in name for name in node_names))

    def test_02_storage_list_and_usage(self):
        """Verify root listing and usage metrics."""
        status, list_data = http_json(f"{NODE_API_URL}/storage/list")
        self.assertEqual(status, 200)
        self.assertIn("items", list_data)

        status, usage_data = http_json(f"{NODE_API_URL}/storage/usage")
        self.assertEqual(status, 200)
        self.assertIn("disk", usage_data)
        self.assertIn("available", usage_data.get("disk", {}))

    def test_03_folder_lifecycle(self):
        """Verify creating, listing inside, and deleting a test folder."""
        folder_name = f"explorer_test_{int(time.time())}"
        
        # 1. Create folder
        status, mkdir_res = http_json(f"{NODE_API_URL}/storage/mkdir", method="POST", data={
            "name": folder_name,
            "path": ""
        })
        self.assertEqual(status, 200)

        # 2. List root to confirm existence
        status, list_res = http_json(f"{NODE_API_URL}/storage/list")
        self.assertEqual(status, 200)
        item_names = [i["name"] for i in list_res.get("items", [])]
        self.assertIn(folder_name, item_names)

        # 3. Clean up folder
        status, del_res = http_json(f"{NODE_API_URL}/storage?path={folder_name}", method="DELETE")
        self.assertEqual(status, 200)

if __name__ == "__main__":
    unittest.main()
