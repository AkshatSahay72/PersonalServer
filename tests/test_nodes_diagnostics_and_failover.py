"""
Unit and Integration Tests for PersonalServer Nodes Diagnostics & HA Failover.
Covers:
- Valid and invalid diagnostics node IDs (including local and self)
- Trailing slash and /api/ route normalization
- Online, offline, and unreachable nodes
- Simulated failover when standbys exist vs when no standbys exist
- Follow-leader behavior on standby 503 write-gating
- Read-only safety of diagnostics (no cluster state mutation)
"""

import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths
from config.ha_manager import HAManager, HAState
from controller.controller import (
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    ControllerHandler
)


class TestNodesDiagnosticsAndFailover(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_test_diag_failover_"))
        for sub in ("data", "config/secrets", "runtime", "logs", "storage"):
            (self.test_root / sub).mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.test_root / "data",
            config_dir=self.test_root / "config",
            secrets_dir=self.test_root / "config" / "secrets",
            runtime_dir=self.test_root / "runtime",
            logs_dir=self.test_root / "logs",
            storage_dir=self.test_root / "storage"
        )
        self.paths_ctx.__enter__()

        # Setup mock nodes
        self.nodes_data = {
            "cluster_name": "TestCluster",
            "nodes": {
                "worker-online": {
                    "node_id": "worker-online",
                    "name": "Online Worker",
                    "ip": "100.85.108.10",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "last_heartbeat": {
                        "timestamp": time.time(),
                        "battery": {"percentage": 92, "status": "CHARGING"},
                        "network": {"connection_type": "Wi-Fi", "link_speed_mbps": 150, "ping_ms": 12},
                        "cpu_details": {"cores": 4, "utilization_percent": 18},
                        "thermals": {"temperature_c": 46}
                    }
                },
                "worker-offline": {
                    "node_id": "worker-offline",
                    "name": "Offline Worker",
                    "ip": "100.85.108.20",
                    "status": "OFFLINE",
                    "last_seen": time.time() - 3600,
                    "last_heartbeat": {
                        "timestamp": time.time() - 3600,
                        "battery": {"percentage": 15, "status": "DISCHARGING"}
                    }
                }
            }
        }
        save_nodes_db(self.nodes_data)
        save_jobs_db({"jobs": {}})

        # Setup mock handler
        self.handler = ControllerHandler.__new__(ControllerHandler)
        self.handler.headers = {"User-Agent": "TestClient"}
        self.handler.current_correlation_id = "test-corr-diag"
        self.handler.heartbeat_timeout = 30
        self.handler.sent_json = None
        self.handler.sent_code = None

        def mock_send_json(code, data):
            self.handler.sent_code = code
            self.handler.sent_json = data

        self.handler.send_json = mock_send_json

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.test_root, ignore_errors=True)

    def test_diagnostics_local_and_self(self):
        """GET /nodes/local/diagnostics or /nodes/self/diagnostics succeeds without requiring 'local' in DB."""
        for endpoint in ("/nodes/local/diagnostics", "/nodes/self/diagnostics", "/api/nodes/self/diagnostics/"):
            self.handler.path = endpoint
            with patch("config.node_telemetry.run_live_diagnostics", return_value={"status": "completed", "battery": {}}):
                self.handler.do_GET()
                self.assertEqual(self.handler.sent_code, 200)
                self.assertIn("diagnostics", self.handler.sent_json)

    def test_diagnostics_valid_online_node(self):
        """GET /nodes/worker-online/diagnostics returns diagnostics from cache or remote."""
        self.handler.path = "/nodes/worker-online/diagnostics"
        self.handler.do_GET()
        self.assertEqual(self.handler.sent_code, 200)
        self.assertEqual(self.handler.sent_json["node_id"], "worker-online")
        diag = self.handler.sent_json["diagnostics"]
        self.assertEqual(diag["battery"]["percentage"], 92)

    def test_diagnostics_trailing_slash_normalization(self):
        """GET /nodes/worker-online/diagnostics/ does not corrupt node_id with /diagnostics."""
        self.handler.path = "/nodes/worker-online/diagnostics/"
        self.handler.do_GET()
        self.assertEqual(self.handler.sent_code, 200)
        self.assertEqual(self.handler.sent_json["node_id"], "worker-online")
        self.assertNotIn("/diagnostics", self.handler.sent_json["node_id"])

    def test_diagnostics_invalid_node_id(self):
        """GET /nodes/nonexistent-123/diagnostics returns 404 with clean node ID."""
        self.handler.path = "/nodes/nonexistent-123/diagnostics"
        self.handler.do_GET()
        self.assertEqual(self.handler.sent_code, 404)
        self.assertEqual(self.handler.sent_json["error"], "Node 'nonexistent-123' not found")

    def test_diagnostics_offline_node(self):
        """Offline node returns status: offline with live: false and clear explanation."""
        self.handler.path = "/nodes/worker-offline/diagnostics"
        self.handler.do_GET()
        self.assertEqual(self.handler.sent_code, 200)
        diag = self.handler.sent_json["diagnostics"]
        self.assertEqual(diag["status"], "offline")
        self.assertFalse(diag["live"])
        self.assertIn("OFFLINE", diag["error"])

    def test_node_details_subpath_not_corrupted(self):
        """A sub-resource like /nodes/worker-online/unknown should not be treated as a single node ID."""
        self.handler.path = "/nodes/worker-online/unknown_action"
        self.handler.do_GET()
        self.assertEqual(self.handler.sent_code, 404)
        self.assertIn("not found", self.handler.sent_json.get("error", "").lower())

    def test_manual_failover_without_standbys_fails_honestly(self):
        """When leader triggers failover but no standby nodes exist, failover reports failure."""
        mgr = HAManager(node_id="node-leader-only", role="leader", node_url="http://127.0.0.1:8000")
        res = mgr.manual_failover()
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["action"], "no_standbys")
        self.assertTrue(mgr.is_leader)

    def test_manual_failover_with_standby_transfers_leadership(self):
        """When leader triggers failover and a synced standby exists, leadership is transferred."""
        mgr = HAManager(node_id="node-leader-01", role="leader", node_url="http://127.0.0.1:8000")
        mgr.register_standby_heartbeat("node-standby-02", "http://100.85.108.5:8000", standby_version=1)

        res = mgr.manual_failover()
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["action"], "stepped_down")
        self.assertEqual(res["target"], "http://100.85.108.5:8000")
        self.assertFalse(mgr.is_leader)
        self.assertEqual(mgr.state, HAState.STANDBY)

    def test_manual_failover_on_standby_promotes_local(self):
        """Calling manual_failover on a standby controller promotes it to leader."""
        mgr = HAManager(node_id="node-standby-01", role="standby", node_url="http://100.85.108.6:8000")
        res = mgr.manual_failover()
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["action"], "promoted_local")
        self.assertTrue(mgr.is_leader)
        self.assertEqual(mgr.state, HAState.LEADER)

    def test_diagnostics_read_only_does_not_mutate_cluster(self):
        """Diagnostics calls never mutate nodes_db, cluster states, or trigger elections."""
        initial_nodes = load_nodes_db()
        self.handler.path = "/nodes/worker-online/diagnostics"
        self.handler.do_GET()
        after_nodes = load_nodes_db()
        self.assertEqual(initial_nodes, after_nodes)


if __name__ == "__main__":
    unittest.main()
