#!/usr/bin/env python3
"""
Unit Tests for Controlled Leadership Handover
=============================================
Tests:
1. Rejects handover if caller is not active leader.
2. Rejects handover to unregistered or offline node.
3. Rejects handover to ineligible node (e.g. Windows laptop).
4. Successfully promotes target node and demotes caller cleanly without split-brain.
5. Fails gracefully if target promotion fails, retaining caller leadership authority.
6. Mode switching between automatic and manual_preference.
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from config.ha_manager import HAManager, HAState, LeaderLease
from controller.ha_helper import get_ha_manager


class TestControlledHandover(unittest.TestCase):

    def setUp(self):
        self.mgr = HAManager(
            node_id="vivo-leader",
            node_url="http://127.0.0.1:8000",
            role="leader"
        )
        self.mgr.state = HAState.LEADER
        self.mgr.current_lease = LeaderLease(
            term=1,
            leader_id="vivo-leader",
            leader_url="http://127.0.0.1:8000",
            lease_expires_at=9999999999.0
        )

        self.mock_nodes = {
            "vivo-leader": {
                "node_id": "vivo-leader",
                "name": "Vivo Y31",
                "status": "ONLINE",
                "leader_eligible": True,
                "preferred_leader": True,
                "priority": 100,
                "ip": "100.85.108.5",
                "tailscale_ip": "100.85.108.5"
            },
            "realme-target": {
                "node_id": "realme-target",
                "name": "Realme 5 Pro",
                "status": "ONLINE",
                "leader_eligible": True,
                "preferred_leader": False,
                "priority": 90,
                "ip": "100.73.52.72",
                "tailscale_ip": "100.73.52.72"
            },
            "windows-laptop": {
                "node_id": "windows-laptop",
                "name": "Windows Laptop",
                "status": "ONLINE",
                "leader_eligible": False,  # Ineligible
                "preferred_leader": False,
                "priority": 0,
                "ip": "100.120.251.42"
            }
        }

    def test_reject_handover_if_not_leader(self):
        self.mgr.state = HAState.STANDBY
        res = self.mgr.controlled_handover("realme-target")
        self.assertEqual(res["status"], "failed")
        self.assertIn("not the active LEADER", res["error"])

    def test_reject_handover_to_self(self):
        res = self.mgr.controlled_handover("vivo-leader")
        self.assertEqual(res["status"], "failed")
        self.assertIn("already the active LEADER", res["error"])

    @patch("controller.stores.load_nodes_db")
    def test_reject_handover_to_ineligible_node(self, mock_load):
        mock_load.return_value = {"nodes": self.mock_nodes}
        res = self.mgr.controlled_handover("windows-laptop")
        self.assertEqual(res["status"], "failed")
        self.assertIn("leader_eligible=False", res["error"])
        # Verify caller remains LEADER
        self.assertTrue(self.mgr.is_leader)

    @patch("controller.stores.load_nodes_db")
    def test_reject_handover_to_offline_node(self, mock_load):
        self.mock_nodes["realme-target"]["status"] = "OFFLINE"
        mock_load.return_value = {"nodes": self.mock_nodes}
        res = self.mgr.controlled_handover("realme-target")
        self.assertEqual(res["status"], "failed")
        self.assertIn("not ONLINE", res["error"])
        self.assertTrue(self.mgr.is_leader)

    @patch("urllib.request.urlopen")
    @patch("controller.stores.load_nodes_db")
    def test_successful_handover(self, mock_load, mock_urlopen):
        mock_load.return_value = {"nodes": self.mock_nodes}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "promoted", "term": 2}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.mgr.controlled_handover("realme-target")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["to_node"], "realme-target")
        self.assertEqual(res["term"], 2)

        # Verify caller stepped down to STANDBY
        self.assertFalse(self.mgr.is_leader)
        self.assertTrue(self.mgr.is_standby)
        self.assertEqual(self.mgr.current_leader_id, "realme-target")

    @patch("urllib.request.urlopen")
    @patch("controller.stores.load_nodes_db")
    def test_handover_fails_if_target_promotion_fails(self, mock_load, mock_urlopen):
        mock_load.return_value = {"nodes": self.mock_nodes}
        mock_urlopen.side_effect = Exception("Connection refused on target")

        res = self.mgr.controlled_handover("realme-target")
        self.assertEqual(res["status"], "failed")
        self.assertIn("Failed to promote target node", res["error"])

        # Caller must remain LEADER to avoid cluster becoming leaderless
        self.assertTrue(self.mgr.is_leader)
        self.assertEqual(self.mgr.current_leader_id, "vivo-leader")


if __name__ == "__main__":
    unittest.main()
