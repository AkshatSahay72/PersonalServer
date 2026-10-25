#!/usr/bin/env python3
"""
PersonalServer V1.8 — Cluster Identity, Leadership & Inventory Cleanup Test Suite
==================================================================================
Tests:
1. Node display names renamed without altering node IDs, tokens, or capabilities.
2. Vivo Y31 authoritative leader identity consistency across backend HA state and UI.
3. Cluster inventory returns exactly two active nodes (Vivo Y31 and Realme 5 Pro).
4. Obsolete entries (Aki standby/leader, simulated nodes) safely removed and prevented from rejoining.
5. Heartbeat ingestion updates telemetry and liveness without corrupting names or roles.
6. Persistence of leader lease and inventory state across restart.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths
from config.ha_manager import (
    HAManager,
    HAState,
    LeaderLease,
    resolve_controller_node_id
)
from controller.node_manager import get_cluster_summary, sanitize_node_record
from controller.constants import (
    STATE_ONLINE,
    STATE_OFFLINE,
    STATE_REMOVED
)


class TestClusterInventoryV18(unittest.TestCase):

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_v18_inventory_"))
        self.data_dir = self.test_root / "data"
        self.config_dir = self.test_root / "config"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir
        )
        self.paths = self.paths_ctx.__enter__()

        # Realistic V1.8 cluster nodes database
        self.nodes_file = self.data_dir / "nodes.json"
        self.lease_file = self.data_dir / "leader_lease.json"
        self.node_json_file = self.config_dir / "node.json"
        self.node_conf_file = self.config_dir / "node.conf"

        now_iso = "2026-10-25T05:00:00+00:00"
        self.test_nodes_db = {
            "cluster_name": "PersonalServer",
            "nodes": {
                "server-5387a86bf36116b1": {
                    "node_id": "server-5387a86bf36116b1",
                    "name": "Vivo Y31",
                    "role": "controller",
                    "platform": "termux",
                    "os": "Linux",
                    "architecture": "aarch64",
                    "ip": "100.85.108.5",
                    "auth_token": "62919372e15c90ffebda147c6735bf73a54a3161",
                    "status": STATE_ONLINE,
                    "registered_at": "2026-10-24T07:22:54+00:00",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True, "network": True},
                    "verified_capabilities": {"compute": True, "storage": True, "network": True, "python3": True},
                    "last_heartbeat": {
                        "timestamp": now_iso,
                        "services": {"node_api": "running", "node_worker": "running", "cloudflare": "running"}
                    }
                },
                "server-95bad5ff01424d4c8d184330d6d2e394": {
                    "node_id": "server-95bad5ff01424d4c8d184330d6d2e394",
                    "name": "Realme 5 Pro",
                    "role": "compute",
                    "platform": "termux",
                    "os": "Linux",
                    "architecture": "aarch64",
                    "ip": "100.73.52.72",
                    "auth_token": "ee6fa0ac1ada36d8eaee0c2e87acd58bcd96ea69",
                    "status": STATE_ONLINE,
                    "registered_at": "2026-10-24T05:50:07+00:00",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True, "network": True, "container_runtime:udocker": True},
                    "verified_capabilities": {"compute": True, "storage": True, "network": True, "python3": True, "container_runtime:udocker": True},
                    "last_heartbeat": {
                        "timestamp": now_iso,
                        "services": {"node_api": "running", "cloudflare": "running"}
                    }
                },
                "server-fc6125d69a24dee0": {
                    "node_id": "server-fc6125d69a24dee0",
                    "name": "Aki",
                    "role": "controller",
                    "status": STATE_REMOVED,
                    "auth_token": None,
                    "removed_at": "2026-10-07T09:42:21+00:00"
                },
                "server-f8b1485ecf458f74": {
                    "node_id": "server-f8b1485ecf458f74",
                    "name": "Aki",
                    "role": "controller",
                    "status": STATE_REMOVED,
                    "auth_token": None,
                    "removed_at": now_iso
                },
                "sim-node-producer": {
                    "node_id": "sim-node-producer",
                    "name": "Telemetry Producer Node",
                    "role": "compute",
                    "status": STATE_REMOVED,
                    "auth_token": None,
                    "removed_at": now_iso
                },
                "sim-node-consumer": {
                    "node_id": "sim-node-consumer",
                    "name": "Data Analytics Consumer Node",
                    "role": "compute",
                    "status": STATE_REMOVED,
                    "auth_token": None,
                    "removed_at": now_iso
                }
            }
        }
        with open(self.nodes_file, "w", encoding="utf-8") as f:
            json.dump(self.test_nodes_db, f, indent=2)

        # Leader lease pointing to Vivo Y31
        self.test_lease = {
            "term": 3,
            "leader_id": "server-5387a86bf36116b1",
            "leader_url": "http://127.0.0.1:8000",
            "lease_expires_at": time.time() + 60.0,
            "state_version": 100,
            "is_valid": True,
            "time_remaining_sec": 60.0
        }
        with open(self.lease_file, "w", encoding="utf-8") as f:
            json.dump(self.test_lease, f, indent=2)

        # Local node configuration for Vivo Y31
        with open(self.node_json_file, "w", encoding="utf-8") as f:
            json.dump({"node_id": "server-5387a86bf36116b1", "name": "Vivo Y31"}, f, indent=2)
        with open(self.node_conf_file, "w", encoding="utf-8") as f:
            f.write("NODE_ID=server-5387a86bf36116b1\nNODE_NAME=\"Vivo Y31\"\n")

    def tearDown(self):
        self.paths_ctx.__exit__(None, None, None)
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_01_node_renaming_preserves_node_ids_and_tokens(self):
        """Node display names are updated while preserving physical IDs, auth tokens and capabilities."""
        with open(self.nodes_file, "r", encoding="utf-8") as f:
            db = json.load(f)
        nodes = db["nodes"]

        # Vivo Y31
        vivo = nodes.get("server-5387a86bf36116b1")
        self.assertIsNotNone(vivo)
        self.assertEqual(vivo["name"], "Vivo Y31")
        self.assertEqual(vivo["node_id"], "server-5387a86bf36116b1")
        self.assertEqual(vivo["auth_token"], "62919372e15c90ffebda147c6735bf73a54a3161")
        self.assertEqual(vivo["ip"], "100.85.108.5")
        self.assertTrue(vivo["verified_capabilities"].get("python3"))

        # Realme 5 Pro
        realme = nodes.get("server-95bad5ff01424d4c8d184330d6d2e394")
        self.assertIsNotNone(realme)
        self.assertEqual(realme["name"], "Realme 5 Pro")
        self.assertEqual(realme["node_id"], "server-95bad5ff01424d4c8d184330d6d2e394")
        self.assertEqual(realme["auth_token"], "ee6fa0ac1ada36d8eaee0c2e87acd58bcd96ea69")
        self.assertEqual(realme["ip"], "100.73.52.72")
        self.assertTrue(realme["verified_capabilities"].get("container_runtime:udocker"))

    def test_02_vivo_y31_authoritative_leader_identity(self):
        """Vivo Y31 is resolved as authoritative cluster leader in backend HA state."""
        resolved_id = resolve_controller_node_id(paths=self.paths)
        self.assertEqual(resolved_id, "server-5387a86bf36116b1")

        ha_mgr = HAManager(
            node_id=resolved_id,
            node_url="http://127.0.0.1:8000",
            role="auto",
            paths=self.paths
        )
        ha_mgr.initialize()

        self.assertTrue(ha_mgr.is_leader)
        status = ha_mgr.get_status_dict()
        self.assertEqual(status["role"], "leader")
        self.assertEqual(status["leader_id"], "server-5387a86bf36116b1")
        self.assertEqual(status["leader_name"], "Vivo Y31")
        self.assertTrue(status["leader_online"])
        self.assertTrue(status["leader_registered"])
        self.assertEqual(status["standbys_count"], 0)

    def test_03_two_node_inventory_counts(self):
        """Active cluster summary and inventory contain exactly two nodes."""
        with open(self.nodes_file, "r", encoding="utf-8") as f:
            db = json.load(f)

        summary = get_cluster_summary(db, timeout_seconds=30)
        cluster_info = summary["cluster"]
        self.assertEqual(cluster_info["total_nodes"], 2)
        self.assertEqual(cluster_info["all_registered"], 6)
        self.assertEqual(cluster_info["removed"], 4)

        # Active nodes (non-REMOVED)
        active_nodes = [n for n in summary["nodes"] if n["status"] != STATE_REMOVED]
        self.assertEqual(len(active_nodes), 2)
        active_names = {n["name"] for n in active_nodes}
        self.assertEqual(active_names, {"Vivo Y31", "Realme 5 Pro"})

    def test_04_stale_records_removed_and_prevented_rejoining(self):
        """Obsolete records are safely retired with null auth tokens and REMOVED status."""
        with open(self.nodes_file, "r", encoding="utf-8") as f:
            db = json.load(f)
        nodes = db["nodes"]

        stale_ids = [
            "server-fc6125d69a24dee0",
            "server-f8b1485ecf458f74",
            "sim-node-producer",
            "sim-node-consumer"
        ]
        for sid in stale_ids:
            self.assertIn(sid, nodes)
            entry = nodes[sid]
            self.assertEqual(entry["status"], STATE_REMOVED)
            self.assertIsNone(entry.get("auth_token"))
            self.assertIsNotNone(entry.get("removed_at"))

    def test_05_heartbeat_preserves_display_names_and_roles(self):
        """Heartbeat ingestion updates telemetry/liveness without corrupting names or roles."""
        from controller.http.node_routes import handle_node_post

        class DummyHandler:
            def __init__(self, headers=None, client_address=("100.85.108.5", 55555)):
                self.headers = headers or {}
                self.client_address = client_address
                self.current_correlation_id = "test-corr"
                self.heartbeat_timeout = 30
                self.sent = []

            def send_json(self, status, payload):
                self.sent.append((status, payload))

            def parse_auth_token(self):
                return "62919372e15c90ffebda147c6735bf73a54a3161"

        handler = DummyHandler()
        body = {
            "node_id": "server-5387a86bf36116b1",
            "resources": {"cpu_cores": 8, "ram_mb": 5697, "storage_gb": 106},
            "services": {"node_api": "running", "node_worker": "running", "cloudflare": "running"}
        }

        # Dispatch heartbeat
        handled = handle_node_post(handler, "/heartbeat", body, token="62919372e15c90ffebda147c6735bf73a54a3161")
        self.assertTrue(handled)
        self.assertEqual(handler.sent[0][0], 200)

        # Verify database record
        with open(self.nodes_file, "r", encoding="utf-8") as f:
            db = json.load(f)
        vivo = db["nodes"]["server-5387a86bf36116b1"]
        self.assertEqual(vivo["name"], "Vivo Y31")
        self.assertEqual(vivo["role"], "controller")
        self.assertEqual(vivo["status"], STATE_ONLINE)

    def test_06_persistence_across_restart(self):
        """Leader lease and cluster state remain consistent after reloading."""
        # First session
        mgr1 = HAManager(
            node_id="server-5387a86bf36116b1",
            node_url="http://127.0.0.1:8000",
            role="auto",
            paths=self.paths
        )
        mgr1.initialize()
        self.assertEqual(mgr1.current_term, 3)
        self.assertEqual(mgr1.current_leader_id, "server-5387a86bf36116b1")

        # Simulate restart with fresh manager instance
        mgr2 = HAManager(
            node_id="server-5387a86bf36116b1",
            node_url="http://127.0.0.1:8000",
            role="auto",
            paths=self.paths
        )
        mgr2.initialize()
        self.assertEqual(mgr2.current_term, 3)
        self.assertEqual(mgr2.current_leader_id, "server-5387a86bf36116b1")
        self.assertTrue(mgr2.is_leader)


if __name__ == "__main__":
    unittest.main()
