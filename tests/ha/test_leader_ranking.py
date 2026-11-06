#!/usr/bin/env python3
"""
Unit Tests for Cluster Leader Ranking & Eligibility Engine
==========================================================
Verifies:
1. Eligibility filtering: Windows laptop is strictly excluded from leadership despite higher specs.
2. Priority weighting: Priority dominates score so administrative preference is respected.
3. Non-preemption rule: Healthy current leader retains leadership without churn.
4. Manual preference mode: Preferred node is hoisted if eligible and healthy.
5. Ineligible / offline node exclusion: Setting leader_eligible=False or status=OFFLINE removes node.
6. Safe empty return when no candidates are eligible or online.
"""

import unittest
from controller.leader_ranking import (
    apply_default_leadership_metadata,
    calculate_node_performance_score,
    rank_leader_candidates,
    select_best_leader,
)


class TestLeaderRanking(unittest.TestCase):

    def setUp(self):
        self.nodes = {
            "vivo-node": {
                "node_id": "vivo-node",
                "name": "Vivo Y31",
                "status": "ONLINE",
                "platform": "linux",
                "resources": {"cpu_cores": 8, "ram_mb": 4096},
                "last_heartbeat": {"system": {"cpu_percent": 15.0, "uptime": "86400"}},
                "leader_eligible": True,
                "preferred_leader": True,
                "priority": 100
            },
            "realme-node": {
                "node_id": "realme-node",
                "name": "Realme 5 Pro",
                "status": "ONLINE",
                "platform": "linux",
                "resources": {"cpu_cores": 8, "ram_mb": 4096},
                "last_heartbeat": {"system": {"cpu_percent": 10.0, "uptime": "43200"}},
                "leader_eligible": True,
                "preferred_leader": False,
                "priority": 90
            },
            "windows-laptop": {
                "node_id": "windows-laptop",
                "name": "Aki Windows Laptop",
                "status": "ONLINE",
                "platform": "win32",
                "resources": {"cpu_cores": 12, "ram_mb": 24576},
                "last_heartbeat": {"system": {"cpu_percent": 5.0, "uptime": "100000"}},
                "leader_eligible": False,  # EXCLUDED
                "preferred_leader": False,
                "priority": 0
            }
        }

    def test_laptop_is_strictly_excluded_from_leadership(self):
        ranked = rank_leader_candidates(self.nodes)
        candidate_ids = [c[0] for c in ranked]
        self.assertNotIn("windows-laptop", candidate_ids, "Windows laptop must be excluded from leader candidates")
        self.assertIn("vivo-node", candidate_ids)
        self.assertIn("realme-node", candidate_ids)

    def test_default_metadata_application(self):
        raw_win = {"node_id": "test-win", "platform": "win32", "status": "ONLINE"}
        apply_default_leadership_metadata("test-win", raw_win)
        self.assertFalse(raw_win["leader_eligible"], "Windows platform should default to leader_eligible=False")
        self.assertTrue(raw_win["build_worker_enabled"])

        raw_phone = {"node_id": "test-phone", "platform": "linux", "status": "ONLINE"}
        apply_default_leadership_metadata("test-phone", raw_phone)
        self.assertTrue(raw_phone["leader_eligible"])
        self.assertTrue(raw_phone["worker_enabled"])

    def test_vivo_ranks_above_realme_by_priority(self):
        ranked = rank_leader_candidates(self.nodes)
        self.assertEqual(ranked[0][0], "vivo-node")
        self.assertEqual(ranked[1][0], "realme-node")
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_disabling_leader_eligibility_removes_node(self):
        self.nodes["vivo-node"]["leader_eligible"] = False
        ranked = rank_leader_candidates(self.nodes)
        candidate_ids = [c[0] for c in ranked]
        self.assertNotIn("vivo-node", candidate_ids)
        self.assertEqual(candidate_ids, ["realme-node"])

    def test_offline_node_is_excluded(self):
        self.nodes["vivo-node"]["status"] = "OFFLINE"
        ranked = rank_leader_candidates(self.nodes)
        candidate_ids = [c[0] for c in ranked]
        self.assertNotIn("vivo-node", candidate_ids)
        self.assertEqual(candidate_ids, ["realme-node"])

    def test_non_preemption_retains_healthy_current_leader(self):
        # Realme is currently leader
        # Vivo has higher priority and higher score
        selected, reason = select_best_leader(
            self.nodes,
            current_leader_id="realme-node",
            mode="automatic"
        )
        self.assertEqual(selected, "realme-node", "Healthy current leader must retain leadership (non-preemption)")
        self.assertIn("non-preemption", reason)

    def test_manual_preference_mode_prioritizes_preferred_node(self):
        # Even if Realme is current leader, manual preference mode requests vivo-node
        selected, reason = select_best_leader(
            self.nodes,
            current_leader_id="realme-node",
            preferred_leader_id="vivo-node",
            mode="manual_preference"
        )
        self.assertEqual(selected, "vivo-node")
        self.assertIn("Manual preference", reason)

    def test_all_ineligible_returns_none(self):
        for n in self.nodes.values():
            n["leader_eligible"] = False
        selected = select_best_leader(self.nodes)
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
