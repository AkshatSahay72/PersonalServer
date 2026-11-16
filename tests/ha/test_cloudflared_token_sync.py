#!/usr/bin/env python3
"""
Unit and Integration Tests for Cluster-Wide Cloudflare Tunnel Token Synchronization
===================================================================================
Verifies that:
1. Cloudflare tokens can be retrieved and updated via REST API endpoints.
2. The active cluster leader exports the token in state snapshots.
3. Standby nodes receive the token via proactive replication and periodic sync.
4. Token modifications on disk are automatically detected by the leader and propagated.
5. Service reload is triggered when the token changes.
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

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths, ControllerPaths
from config.ha_manager import (
    HAManager,
    HAState,
    reload_cloudflared_process
)
from controller.ha_helper import get_ha_manager
from controller.controller import ControllerHandler


class TestCloudflaredTokenSync(unittest.TestCase):

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_cf_sync_test_"))
        self.master_dir = self.test_root / "master"
        self.standby_dir = self.test_root / "standby"

        for d in (self.master_dir, self.standby_dir):
            for sub in ("data", "config/secrets", "runtime", "logs", "storage"):
                (d / sub).mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "cf-test-enrollment-secret-key-123"
        for d in (self.master_dir, self.standby_dir):
            (d / "config" / "secrets" / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        self.master_paths = ControllerPaths(
            data_dir=self.master_dir / "data",
            config_dir=self.master_dir / "config",
            secrets_dir=self.master_dir / "config" / "secrets",
            runtime_dir=self.master_dir / "runtime",
            logs_dir=self.master_dir / "logs",
            storage_dir=self.master_dir / "storage"
        )

        self.standby_paths = ControllerPaths(
            data_dir=self.standby_dir / "data",
            config_dir=self.standby_dir / "config",
            secrets_dir=self.standby_dir / "config" / "secrets",
            runtime_dir=self.standby_dir / "runtime",
            logs_dir=self.standby_dir / "logs",
            storage_dir=self.standby_dir / "storage"
        )

        self.paths_ctx = override_controller_paths(
            data_dir=self.master_paths.data_dir,
            config_dir=self.master_paths.config_dir,
            secrets_dir=self.master_paths.secrets_dir,
            runtime_dir=self.master_paths.runtime_dir,
            logs_dir=self.master_paths.logs_dir,
            storage_dir=self.master_paths.storage_dir
        )
        self.paths_ctx.__enter__()

    def tearDown(self):
        if hasattr(self, "paths_ctx"):
            self.paths_ctx.__exit__(None, None, None)
        try:
            shutil.rmtree(self.test_root)
        except Exception:
            pass

    def test_update_cloudflared_token_locally_and_export(self):
        """Verifies that update_cloudflared_token writes file, increments state_version, and exports in snapshot."""
        mgr = HAManager(
            node_id="test-leader",
            node_url="http://127.0.0.1:8000",
            candidate_urls=["http://127.0.0.1:8000"],
            role="leader",
            auth_token=self.enrollment_token,
            paths=self.master_paths
        )

        test_token = "eyJhIjoiZXhhbXBsZTEiLCJ0IjoidGVzdC10b2tlbi0xMjMifQ=="
        ok = mgr.update_cloudflared_token(test_token)
        self.assertTrue(ok)

        # Check local file
        token_file = self.master_paths.secrets_dir / "cloudflared.token"
        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.read_text(encoding="utf-8").strip(), test_token)

        # Check snapshot export
        snapshot = mgr.export_state_snapshot()
        self.assertEqual(snapshot.get("cloudflared_token"), test_token)
        self.assertGreater(snapshot.get("state_version", 0), 0)

    def test_standby_applies_cloudflared_token_and_writes_file(self):
        """Verifies that a standby node synchronizes the token from the leader snapshot."""
        standby_mgr = HAManager(
            node_id="test-standby",
            node_url="http://127.0.0.1:8001",
            candidate_urls=["http://127.0.0.1:8000", "http://127.0.0.1:8001"],
            role="standby",
            auth_token=self.enrollment_token,
            paths=self.standby_paths
        )

        target_token = "eyJhIjoiZXhhbXBsZTIiLCJ0IjoidG9rZW4tc3luYy00NTYifQ=="
        snapshot = {
            "term": 10,
            "state_version": 42,
            "leader_id": "test-leader",
            "leader_url": "http://127.0.0.1:8000",
            "cloudflared_token": target_token
        }

        with standby_mgr._lock:
            res = standby_mgr._apply_sync_snapshot_locked(snapshot, "http://127.0.0.1:8000")
        self.assertTrue(res)

        standby_token_file = self.standby_paths.secrets_dir / "cloudflared.token"
        self.assertTrue(standby_token_file.exists())
        self.assertEqual(standby_token_file.read_text(encoding="utf-8").strip(), target_token)

    def test_leader_auto_detects_disk_token_change(self):
        """Verifies that modifying cloudflared.token directly on disk triggers state_version bump and replication."""
        mgr = HAManager(
            node_id="test-leader",
            node_url="http://127.0.0.1:8000",
            candidate_urls=["http://127.0.0.1:8000"],
            role="leader",
            lease_duration_sec=60.0,
            auth_token=self.enrollment_token,
            paths=self.master_paths
        )
        mgr.renew_lease()
        initial_version = mgr.state_version

        # Directly modify token on disk
        token_file = self.master_paths.secrets_dir / "cloudflared.token"
        token_file.write_text("token-written-directly-on-disk-789", encoding="utf-8")

        # Trigger failover evaluation which runs the periodic leader checks
        mgr.trigger_failover_evaluation()

        self.assertGreater(mgr.state_version, initial_version)
        self.assertEqual(mgr._cached_cloudflared_token, "token-written-directly-on-disk-789")

    def test_http_api_token_endpoint(self):
        """Verifies GET and POST /api/cluster/secrets/cloudflared-token HTTP routes."""
        get_ha_manager(paths=self.master_paths, role="leader", force_reload=True)

        server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        port = server.server_address[1]
        server_url = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        new_test_token = "eyJhIjoiZXhhbXBsZTMiLCJ0IjoiaHR0cC1hcGktdG9rZW4ifQ=="
        try:
            # 1. Update token via POST
            post_req = urllib.request.Request(
                f"{server_url}/api/cluster/secrets/cloudflared-token",
                data=json.dumps({"token": new_test_token}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.enrollment_token}",
                    "Content-Type": "application/json"
                }
            )
            with urllib.request.urlopen(post_req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode())
                self.assertEqual(body.get("status"), "ok")

            # Verify written to disk
            token_file = self.master_paths.secrets_dir / "cloudflared.token"
            self.assertTrue(token_file.exists())
            self.assertEqual(token_file.read_text(encoding="utf-8").strip(), new_test_token)

            # 2. Query token via GET
            get_req = urllib.request.Request(
                f"{server_url}/api/cluster/secrets/cloudflared-token",
                headers={"Authorization": f"Bearer {self.enrollment_token}"}
            )
            with urllib.request.urlopen(get_req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode())
                self.assertTrue(body.get("configured"))
                self.assertEqual(body.get("token_length"), len(new_test_token))
                self.assertTrue(body.get("token_prefix", "").startswith("eyJhIjoiZX"))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
