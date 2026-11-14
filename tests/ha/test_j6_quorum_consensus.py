#!/usr/bin/env python3
"""
PersonalServer J6 Quorum Consensus & Distributed Failover Tests
==============================================================
Validates Raft-style RequestVote election, strict majority quorum:
Q = floor(N/2) + 1, persistent election state, single-vote constraint,
state completeness validation, and replicated multi-store synchronization.
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
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import ControllerPaths
from config.ha_manager import HAManager, HAState, LeaderLease


class MockVoterHandler(BaseHTTPRequestHandler):
    """Mock controller endpoint for testing RequestVote protocol."""
    vote_granted = True
    remote_term = 1
    remote_version = 1

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            req_data = json.loads(body)
        except Exception:
            req_data = {}

        if self.path == "/ha/vote":
            resp = {
                "term": max(self.remote_term, req_data.get("term", 1)),
                "vote_granted": self.vote_granted,
                "reason": "Mock voter decision"
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif self.path == "/ha/replicate":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "synchronized"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


class TestJ6QuorumConsensus(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ps_j6_test_")
        self.data_dir = Path(self.temp_dir) / "controller" / "data"
        self.secrets_dir = Path(self.temp_dir) / "config" / "secrets"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)

        self.paths = ControllerPaths(
            base_dir=Path(self.temp_dir),
            data_dir=self.data_dir,
            secrets_dir=self.secrets_dir
        )
        self.paths.ensure_directories()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_quorum_calculation_and_voter_management(self):
        """Verifies quorum calculation: Q = floor(N/2) + 1."""
        mgr = HAManager(node_id="node-test-1", node_url="http://127.0.0.1:8001", paths=self.paths)
        
        # 1 voter (self) -> Q = 1
        mgr.save_voting_members({
            "node-test-1": {"node_id": "node-test-1", "url": "http://127.0.0.1:8001"}
        })
        self.assertEqual(mgr.quorum_size, 1)

        # 2 voters -> Q = floor(2/2) + 1 = 2
        mgr.add_voting_member("node-test-2", "http://127.0.0.1:8002", "Node 2")
        self.assertEqual(len(mgr.voting_members), 2)
        self.assertEqual(mgr.quorum_size, 2)

        # 3 voters -> Q = floor(3/2) + 1 = 2
        mgr.add_voting_member("node-test-3", "http://127.0.0.1:8003", "Node 3")
        self.assertEqual(len(mgr.voting_members), 3)
        self.assertEqual(mgr.quorum_size, 2)

        # 4 voters -> Q = floor(4/2) + 1 = 3
        mgr.add_voting_member("node-test-4", "http://127.0.0.1:8004", "Node 4")
        self.assertEqual(len(mgr.voting_members), 4)
        self.assertEqual(mgr.quorum_size, 3)

        # 5 voters -> Q = floor(5/2) + 1 = 3
        mgr.add_voting_member("node-test-5", "http://127.0.0.1:8005", "Node 5")
        self.assertEqual(len(mgr.voting_members), 5)
        self.assertEqual(mgr.quorum_size, 3)

        # Remove voter
        mgr.remove_voting_member("node-test-5")
        self.assertEqual(len(mgr.voting_members), 4)
        self.assertEqual(mgr.quorum_size, 3)

    def test_request_vote_protocol_rules(self):
        """
        Validates RequestVote consensus constraints:
        1. Monotonic term check (reject stale term).
        2. Single vote per term per voter.
        3. State freshness check (reject stale candidate log).
        """
        mgr = HAManager(node_id="voter-node", node_url="http://127.0.0.1:8000", paths=self.paths, role="standby")
        mgr.current_lease.term = 5
        mgr.state_version = 100

        # Register candidate in voters
        mgr.add_voting_member("voter-node", "http://127.0.0.1:8000")
        mgr.add_voting_member("candidate-A", "http://127.0.0.1:8001")
        mgr.add_voting_member("candidate-B", "http://127.0.0.1:8002")

        # 1. Candidate term is older -> Reject
        res = mgr.handle_vote_request({
            "term": 4,
            "candidate_id": "candidate-A",
            "candidate_url": "http://127.0.0.1:8001",
            "last_state_version": 100
        })
        self.assertFalse(res["vote_granted"])

        # 2. Candidate state version is behind local version -> Reject
        res = mgr.handle_vote_request({
            "term": 6,
            "candidate_id": "candidate-A",
            "candidate_url": "http://127.0.0.1:8001",
            "last_state_version": 90  # local is 100
        })
        self.assertFalse(res["vote_granted"])

        # 3. Valid candidate with higher term and fresh state -> Grant vote!
        res = mgr.handle_vote_request({
            "term": 6,
            "candidate_id": "candidate-A",
            "candidate_url": "http://127.0.0.1:8001",
            "last_state_version": 105
        })
        self.assertTrue(res["vote_granted"])
        self.assertEqual(mgr.voted_for, "candidate-A")
        self.assertEqual(mgr.current_term, 6)

        # 4. Another candidate requests vote in same term -> Reject (single-vote constraint)
        res = mgr.handle_vote_request({
            "term": 6,
            "candidate_id": "candidate-B",
            "candidate_url": "http://127.0.0.1:8002",
            "last_state_version": 110
        })
        self.assertFalse(res["vote_granted"])
        self.assertIn("Already voted", res["reason"])

        # 5. Same candidate requests vote again in same term (retry) -> Grant
        res = mgr.handle_vote_request({
            "term": 6,
            "candidate_id": "candidate-A",
            "candidate_url": "http://127.0.0.1:8001",
            "last_state_version": 105
        })
        self.assertTrue(res["vote_granted"])

    def test_campaign_achieves_quorum_and_promotes(self):
        """
        Tests 3-node cluster quorum election:
        - Node 1 campaigns for leadership.
        - Node 2 (mock voter) grants vote.
        - Node 3 is offline.
        - 2 votes out of 3 >= Quorum (2). Node 1 successfully promotes to LEADER!
        """
        # Spin up mock voter for Node 2
        mock_server = HTTPServer(("127.0.0.1", 0), MockVoterHandler)
        mock_port = mock_server.server_address[1]
        mock_server_thread = threading.Thread(target=mock_server.serve_forever, daemon=True)
        mock_server_thread.start()

        try:
            MockVoterHandler.vote_granted = True
            MockVoterHandler.remote_term = 1

            mgr = HAManager(
                node_id="node-candidate",
                node_url="http://127.0.0.1:8001",
                paths=self.paths,
                role="standby"
            )

            # Setup 3 voters: Candidate, Mock voter (online), and Dead node (offline)
            mgr.save_voting_members({
                "node-candidate": {"node_id": "node-candidate", "url": "http://127.0.0.1:8001"},
                "node-voter-2": {"node_id": "node-voter-2", "url": f"http://127.0.0.1:{mock_port}"},
                "node-voter-3": {"node_id": "node-voter-3", "url": "http://127.0.0.1:59999"}  # dead port
            })
            self.assertEqual(mgr.quorum_size, 2)

            # Execute campaign
            promoted = mgr.campaign_for_leadership(reason="Test quorum election")
            self.assertTrue(promoted)
            self.assertTrue(mgr.is_leader)
            self.assertEqual(mgr.state, HAState.LEADER)
            self.assertEqual(mgr.current_leader_id, "node-candidate")
            self.assertTrue(mgr.current_lease.is_valid())
        finally:
            mock_server.shutdown()
            mock_server.server_close()

    def test_campaign_fails_when_quorum_not_reached(self):
        """
        Tests partition safety:
        - 3 voters configured (Quorum = 2).
        - Both remote voters are offline.
        - Candidate only receives 1 vote (self).
        - Quorum NOT reached: Node remains in STANDBY, zero split-brain mutations.
        """
        mgr = HAManager(
            node_id="isolated-node",
            node_url="http://127.0.0.1:8001",
            paths=self.paths,
            role="standby"
        )
        mgr.save_voting_members({
            "isolated-node": {"node_id": "isolated-node", "url": "http://127.0.0.1:8001"},
            "peer-1": {"node_id": "peer-1", "url": "http://127.0.0.1:59998"},
            "peer-2": {"node_id": "peer-2", "url": "http://127.0.0.1:59999"}
        })
        self.assertEqual(mgr.quorum_size, 2)

        promoted = mgr.campaign_for_leadership(reason="Partition test")
        self.assertFalse(promoted)
        self.assertFalse(mgr.is_leader)
        self.assertEqual(mgr.state, HAState.STANDBY)

    def test_complete_state_replication_scope(self):
        """
        Verifies that state snapshot and synchronization replicate:
        nodes, jobs, apps, workflows, shares, artifacts, onboarding_codes, and voters.
        """
        mgr = HAManager(node_id="master-sync", node_url="http://127.0.0.1:8001", paths=self.paths, role="leader")
        mgr.current_lease.term = 3
        mgr.state_version = 42

        # Write test data across stores
        from controller.stores.base import atomic_save_json
        atomic_save_json(self.paths.workflows_file, self.paths.backup_workflows_file, {"workflows": {"wf-1": {"name": "test"}}})
        atomic_save_json(self.paths.shares_file, self.paths.backup_shares_file, {"shares": {"s-1": {"path": "/tmp"}}})
        atomic_save_json(self.paths.artifacts_file, self.paths.backup_artifacts_file, {"artifacts": {"a-1": {"hash": "abc"}}})

        snapshot = mgr.export_state_snapshot()
        self.assertEqual(snapshot["term"], 3)
        self.assertEqual(snapshot["state_version"], 42)
        self.assertIn("nodes", snapshot)
        self.assertIn("jobs", snapshot)
        self.assertIn("apps", snapshot)
        self.assertIn("workflows", snapshot)
        self.assertIn("shares", snapshot)
        self.assertIn("artifacts", snapshot)
        self.assertIn("onboarding_codes", snapshot)
        self.assertIn("voters", snapshot)

        # Apply snapshot to a standby
        standby_dir = tempfile.mkdtemp(prefix="ps_standby_")
        try:
            standby_paths = ControllerPaths(
                base_dir=Path(standby_dir),
                data_dir=Path(standby_dir) / "data",
                secrets_dir=Path(standby_dir) / "secrets"
            )
            standby_paths.ensure_directories()
            standby_mgr = HAManager(node_id="standby-node", node_url="http://127.0.0.1:8002", paths=standby_paths, role="standby")

            with standby_mgr._lock:
                ok = standby_mgr._apply_sync_snapshot_locked(snapshot, "http://127.0.0.1:8001")
            self.assertTrue(ok)
            self.assertEqual(standby_mgr.current_term, 3)
            self.assertEqual(standby_mgr.state_version, 42)

            # Verify files were persisted to standby disk
            self.assertTrue(standby_paths.workflows_file.exists())
            self.assertTrue(standby_paths.shares_file.exists())
            self.assertTrue(standby_paths.artifacts_file.exists())
        finally:
            shutil.rmtree(standby_dir, ignore_errors=True)

    def test_authoritative_cluster_state_contract(self):
        """Verifies GET /api/cluster/state unified contract and single-leader guarantee."""
        from controller.http.node_routes import build_cluster_state
        from controller.stores.base import atomic_save_json

        # Setup mock nodes
        nodes_data = {
            "cluster_name": "PersonalServer-TestCluster",
            "nodes": {
                "node-leader": {
                    "node_id": "node-leader",
                    "name": "Leader Node",
                    "status": "ONLINE",
                    "role": "compute",
                    "leader_eligible": True,
                    "priority": 50,
                    "ip": "100.64.0.1",
                    "resources": {"ram_mb": 4096, "cpu_cores": 4},
                    "last_heartbeat": {"system": {"cpu_percent": 10.0}}
                },
                "node-standby": {
                    "node_id": "node-standby",
                    "name": "Standby Node",
                    "status": "ONLINE",
                    "role": "compute",
                    "leader_eligible": True,
                    "priority": 50,
                    "ip": "100.64.0.2",
                    "resources": {"ram_mb": 4096, "cpu_cores": 4},
                    "last_heartbeat": {"system": {"cpu_percent": 15.0}}
                }
            }
        }
        atomic_save_json(self.paths.nodes_file, self.paths.backup_nodes_file, nodes_data)

        # Setup leader lease on disk
        lease = LeaderLease(
            term=5,
            leader_id="node-leader",
            leader_url="http://100.64.0.1:8000",
            lease_expires_at=time.time() + 60.0,
            state_version=10
        )
        lease.save_to_file(self.paths.data_dir / "ha_lease.json")

        mgr = HAManager(node_id="test-node-local", node_url="http://127.0.0.1:8000", paths=self.paths, role="standby")
        mgr.current_lease = lease

        class MockHandler:
            heartbeat_timeout = 30
            ha_manager = mgr

        from config.platform_config import override_controller_paths
        with override_controller_paths(data_dir=self.paths.data_dir, secrets_dir=self.paths.secrets_dir):
            state = build_cluster_state(MockHandler())

        self.assertEqual(state["cluster_id"], "PersonalServer-TestCluster")
        self.assertEqual(state["leader"]["node_id"], "node-leader")
        self.assertEqual(state["leader"]["term"], 5)
        self.assertTrue(state["leader"]["lease_valid"])

        # Check nodes array
        leader_nodes = [n for n in state["nodes"] if n["is_leader"]]
        self.assertEqual(len(leader_nodes), 1, "Exactly ONE node must be stamped as leader")
        self.assertEqual(leader_nodes[0]["node_id"], "node-leader")

        standby_nodes = [n for n in state["nodes"] if not n["is_leader"]]
        self.assertEqual(len(standby_nodes), 1)
        self.assertFalse(standby_nodes[0]["is_leader"])
        self.assertGreater(standby_nodes[0]["merit_score"], 0.0)

    def test_merit_ranking_and_deterministic_tie_breaking(self):
        """Verifies merit ranking ordering (score DESC, priority DESC, node_id ASC)."""
        from controller.leader_ranking import rank_leader_candidates

        test_nodes = {
            "node-c": {
                "node_id": "node-c",
                "status": "ONLINE",
                "priority": 50,
                "leader_eligible": True,
                "resources": {"ram_mb": 4096},
                "last_heartbeat": {"system": {"cpu_percent": 20.0}}
            },
            "node-a": {
                "node_id": "node-a",
                "status": "ONLINE",
                "priority": 50,
                "leader_eligible": True,
                "resources": {"ram_mb": 4096},
                "last_heartbeat": {"system": {"cpu_percent": 20.0}}
            },
            "node-b": {
                "node_id": "node-b",
                "status": "ONLINE",
                "priority": 50,
                "leader_eligible": True,
                "resources": {"ram_mb": 4096},
                "last_heartbeat": {"system": {"cpu_percent": 20.0}}
            }
        }
        ranked = rank_leader_candidates(test_nodes)
        # All have identical specs and priority -> tie broken by node_id ASC: node-a, node-b, node-c
        ordered_ids = [r[0] for r in ranked]
        self.assertEqual(ordered_ids, ["node-a", "node-b", "node-c"])


if __name__ == "__main__":
    unittest.main()
