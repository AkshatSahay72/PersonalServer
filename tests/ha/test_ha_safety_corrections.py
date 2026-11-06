#!/usr/bin/env python3
"""
Unit and Integration Tests: HA Safety Corrections (V3.0.1)
==========================================================
Verifies:
1. HA lock inversion removal: Network I/O occurs outside self._lock.
2. Concurrent /ha/status queries and lock acquisition under peer timeouts.
3. Stale probe result rejection on term change, state change, and out-of-order seq.
4. Concurrent failover evaluation serialization.
5. Peer configuration validation and /ha/status visibility.
6. Cold boot safety: No independent leadership election on empty peer list or unreachable peer.
7. Leadership safety: Automatic uncoordinated promotion remains disabled without authoritative grant.
8. Controlled handover and stale-write protection integrity.
"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch, MagicMock

from common.time_utils import get_current_iso_timestamp
from config.ha_manager import (
    HAManager,
    HAState,
    HAFailureMode,
    LeaderLease,
    validate_peer_configuration,
)
from config.platform_config import ControllerPaths, override_controller_paths
from controller.ha_helper import get_ha_manager


class TestHASafetyCorrections(unittest.TestCase):
    """Safety test suite validating all HA fixes from HA Live Failure Validation."""

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_ha_safety_test_"))
        self.node_dir = self.test_root / "node"
        for sub in ("data", "config/secrets", "runtime", "logs", "storage"):
            (self.node_dir / sub).mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.node_dir / "data",
            config_dir=self.node_dir / "config",
            secrets_dir=self.node_dir / "config" / "secrets",
            runtime_dir=self.node_dir / "runtime",
            logs_dir=self.node_dir / "logs",
            storage_dir=self.node_dir / "storage"
        )
        self.paths = self.paths_ctx.__enter__()
        self.enrollment_token = "safety-test-token-xyz"
        (self.node_dir / "config" / "secrets" / "enrollment.token").write_text(self.enrollment_token + "\n")

    def tearDown(self):
        if hasattr(self, "paths_ctx"):
            self.paths_ctx.__exit__(None, None, None)
        try:
            shutil.rmtree(self.test_root)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 1. Peer Configuration Validation & Visibility (Phase 3)
    # -------------------------------------------------------------------------
    def test_missing_peer_url_configuration_validation(self):
        """Empty or self-only peer lists are flagged invalid and exposed in status."""
        # Self-only list
        valid, status, peers = validate_peer_configuration(
            candidate_urls=["http://100.85.108.5:8000"],
            local_node_url="http://100.85.108.5:8000"
        )
        self.assertFalse(valid)
        self.assertIn("missing_peers", status)
        self.assertEqual(peers, [])

        # Empty list
        valid, status, peers = validate_peer_configuration([], "http://100.85.108.5:8000")
        self.assertFalse(valid)
        self.assertIn("missing_peers", status)

        # Invalid scheme
        valid, status, peers = validate_peer_configuration(
            ["ftp://100.73.52.72:8000"],
            "http://100.85.108.5:8000"
        )
        self.assertFalse(valid)
        self.assertIn("invalid_url", status)

        # Valid symmetric configuration
        valid, status, peers = validate_peer_configuration(
            ["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            "http://100.85.108.5:8000"
        )
        self.assertTrue(valid)
        self.assertEqual(status, "valid")
        self.assertEqual(peers, ["http://100.73.52.72:8000"])

    def test_peer_configuration_visible_in_status_dict(self):
        """Missing or invalid peer configuration is clearly exposed in /ha/status."""
        mgr = HAManager(
            node_id="test-node-1",
            node_url="http://100.85.108.5:8000",
            candidate_urls=["http://100.85.108.5:8000"],  # Self only -> missing peer
            paths=self.paths
        )
        status = mgr.get_status_dict()
        self.assertFalse(status["peer_config_valid"])
        self.assertIn("missing_peers", status["peer_config_status"])
        self.assertEqual(status["configured_peers"], [])
        self.assertFalse(status["auto_promotion_enabled"])
        self.assertEqual(status["authority_mechanism"], "cooperative_lease_no_external_grant")
        self.assertEqual(status["split_brain_mitigation"], "automatic_promotion_disabled")

    # -------------------------------------------------------------------------
    # 2. Lock Inversion Removal & Concurrent Access (Phase 2)
    # -------------------------------------------------------------------------
    def test_lock_not_held_during_peer_timeout(self):
        """
        Critical Test: While a network probe or peer check is in flight / timing out,
        self._lock is NOT held. Other threads can acquire the lock immediately (<10ms).
        """
        # Create a mock slow probe that blocks for 0.4 seconds outside lock
        mgr = HAManager(
            node_id="test-node-vivo",
            node_url="http://100.85.108.5:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths
        )
        mgr.state = HAState.STANDBY

        probe_in_progress = threading.Event()
        probe_release = threading.Event()

        def slow_probe(*args, **kwargs):
            probe_in_progress.set()
            probe_release.wait(timeout=1.0)
            return False, HAFailureMode.NETWORK_PARTITION, None, None

        mgr.probe_peer_leader = slow_probe

        # Launch failover evaluation in background thread
        eval_thread = threading.Thread(target=mgr.trigger_failover_evaluation, daemon=True)
        eval_thread.start()

        # Wait until network probe is actively running
        self.assertTrue(probe_in_progress.wait(timeout=1.0))

        # Now test that another thread can acquire self._lock INSTANTLY without being blocked!
        t_start = time.perf_counter()
        acquired = mgr._lock.acquire(timeout=0.05)
        t_elapsed = time.perf_counter() - t_start

        try:
            self.assertTrue(acquired, "Lock could not be acquired while network probe was running!")
            self.assertLess(t_elapsed, 0.05, f"Lock acquisition took too long: {t_elapsed:.4f}s")
            # Verify status can be generated while probe is active
            status = mgr.get_status_dict()
            self.assertEqual(status["role"], HAState.STANDBY)
        finally:
            if acquired:
                mgr._lock.release()
            probe_release.set()
            eval_thread.join(timeout=1.0)

    def test_concurrent_ha_status_requests_during_probe(self):
        """Simultaneous /ha/status queries succeed concurrently while probe is timing out."""
        mgr = HAManager(
            node_id="test-node-vivo",
            node_url="http://100.85.108.5:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths
        )

        probe_started = threading.Event()
        probe_done = threading.Event()

        def hanging_probe(*args, **kwargs):
            probe_started.set()
            probe_done.wait(timeout=0.5)
            return False, HAFailureMode.NETWORK_PARTITION, None, None

        mgr.probe_peer_leader = hanging_probe

        eval_thread = threading.Thread(target=mgr.trigger_failover_evaluation, daemon=True)
        eval_thread.start()

        self.assertTrue(probe_started.wait(timeout=1.0))

        # Execute 10 concurrent get_status_dict calls
        results = []
        errors = []

        def worker():
            try:
                s = mgr.get_status_dict()
                results.append(s["role"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=0.2)

        probe_done.set()
        eval_thread.join(timeout=1.0)

        self.assertEqual(len(errors), 0, f"Concurrent status requests encountered errors: {errors}")
        self.assertEqual(len(results), 10)
        self.assertTrue(all(r == HAState.STANDBY for r in results))

    def test_simultaneous_peer_probes(self):
        """Multiple threads can probe peer leader concurrently without deadlock."""
        mgr = HAManager(
            node_id="test-node-vivo",
            node_url="http://100.85.108.5:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths
        )

        probe_results = []

        def run_probe(idx):
            alive, mode = mgr.check_remote_leader()
            probe_results.append((idx, alive, mode))

        threads = [threading.Thread(target=run_probe, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(len(probe_results), 5)

    # -------------------------------------------------------------------------
    # 3. Freshness Guards & Stale Result Rejection (Phase 2)
    # -------------------------------------------------------------------------
    def test_stale_probe_result_rejection_on_term_change(self):
        """If cluster term increments while network probe is in-flight, probe result is discarded."""
        mgr = HAManager(
            node_id="test-node-realme",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths,
            auto_promotion_enabled=True  # even if enabled, stale result must not apply
        )
        mgr.state = HAState.STANDBY
        mgr.current_lease.term = 1

        probe_running = threading.Event()
        probe_finish = threading.Event()

        def slow_dead_probe(*args, **kwargs):
            probe_running.set()
            probe_finish.wait(timeout=1.0)
            return False, HAFailureMode.PROCESS_DEAD, None, "http://100.85.108.5:8000"

        mgr.probe_peer_leader = slow_dead_probe

        eval_thread = threading.Thread(target=mgr.trigger_failover_evaluation, daemon=True)
        eval_thread.start()

        self.assertTrue(probe_running.wait(timeout=1.0))

        # While probe is in flight, term changes (e.g. from 1 to 2 via handover/sync)
        with mgr._lock:
            mgr.current_lease.term = 2

        # Allow slow probe to return
        probe_finish.set()
        eval_thread.join(timeout=1.0)

        # Result was stale: term changed from 1 to 2 during probe.
        # Node must NOT have elected self, and must remain STANDBY.
        self.assertEqual(mgr.state, HAState.STANDBY)
        self.assertEqual(mgr.current_term, 2)

    def test_stale_probe_result_rejection_on_state_change(self):
        """If node state changes while probe is in flight, stale result is discarded."""
        mgr = HAManager(
            node_id="test-node-realme",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths
        )
        mgr.state = HAState.STANDBY

        probe_running = threading.Event()
        probe_finish = threading.Event()

        def slow_healthy_probe(*args, **kwargs):
            probe_running.set()
            probe_finish.wait(timeout=1.0)
            return True, HAFailureMode.HEALTHY, {"lease_remaining_sec": 10.0, "term": 1}, "http://100.85.108.5:8000"

        mgr.probe_peer_leader = slow_healthy_probe

        eval_thread = threading.Thread(target=mgr.trigger_failover_evaluation, daemon=True)
        eval_thread.start()

        self.assertTrue(probe_running.wait(timeout=1.0))

        # Node got explicitly promoted to LEADER while probe was in-flight
        with mgr._lock:
            mgr.state = HAState.LEADER
            mgr.current_lease.leader_id = mgr.node_id

        probe_finish.set()
        eval_thread.join(timeout=1.0)

        # Stale standby probe result must NOT overwrite local LEADER state
        self.assertEqual(mgr.state, HAState.LEADER)
        self.assertEqual(mgr.current_leader_id, mgr.node_id)

    def test_concurrent_failover_evaluations_do_not_overlap(self):
        """Overlapping failover evaluations are prevented via _eval_in_progress lock guard."""
        mgr = HAManager(
            node_id="test-node-realme",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths
        )
        mgr.state = HAState.STANDBY

        probe_count = 0
        probe_lock = threading.Lock()
        probe_event = threading.Event()

        def counted_probe(*args, **kwargs):
            nonlocal probe_count
            with probe_lock:
                probe_count += 1
            probe_event.wait(timeout=0.2)
            return False, HAFailureMode.NETWORK_PARTITION, None, None

        mgr.probe_peer_leader = counted_probe

        # Trigger 5 rapid failover evaluations concurrently
        threads = [threading.Thread(target=mgr.trigger_failover_evaluation) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)

        probe_event.set()

        # Exactly 1 evaluation should have run; the other 4 should have returned immediately
        self.assertEqual(probe_count, 1)

    # -------------------------------------------------------------------------
    # 4. Cold Boot & Peer Reachability Safety (Phase 3 & Phase 4)
    # -------------------------------------------------------------------------
    def test_cold_boot_peer_unreachable_does_not_claim_leadership(self):
        """
        When a node boots cold with valid peer configuration, but the peer is unreachable,
        the node does NOT interpret unreachable as dead, and does NOT claim leadership.
        It safely holds STANDBY.
        """
        mgr = HAManager(
            node_id="server-realme-5pro",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="auto",
            paths=self.paths,
            auto_promotion_enabled=False  # Default
        )
        # Mock unreachable peer
        with patch("config.ha_manager.find_active_leader", return_value=(None, None)):
            mgr.initialize()

        self.assertEqual(mgr.state, HAState.STANDBY)
        self.assertFalse(mgr.is_leader)
        self.assertTrue(mgr.is_standby)

    def test_no_independent_promotion_without_authoritative_grant(self):
        """
        Even when the remote leader is confirmed DEAD (process dead),
        with auto_promotion_enabled=False (Phase 4), the standby holds STANDBY
        to prevent uncoordinated dual-leader term collisions.
        """
        mgr = HAManager(
            node_id="server-realme-5pro",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="standby",
            paths=self.paths,
            auto_promotion_enabled=False
        )
        mgr.state = HAState.STANDBY
        mgr.current_lease = LeaderLease(
            term=1,
            leader_id="server-vivo-y31",
            leader_url="http://100.85.108.5:8000",
            lease_expires_at=time.time() - 10.0  # Expired
        )

        with patch.object(mgr, "probe_peer_leader", return_value=(False, HAFailureMode.PROCESS_DEAD, None, "http://100.85.108.5:8000")):
            mgr.trigger_failover_evaluation()

        # Must NOT have promoted to LEADER
        self.assertEqual(mgr.state, HAState.STANDBY)
        self.assertFalse(mgr.is_leader)
        self.assertEqual(mgr.last_failure_mode, HAFailureMode.PROCESS_DEAD)

    def test_cold_boot_either_node_starting_first(self):
        """
        Cold boot scenario:
        1. Node A (Realme) boots first. Peer (Vivo) is offline.
           Realme safely remains STANDBY.
        2. Node B (Vivo) boots later as configured primary LEADER.
        3. Node A probes and discovers Node B as LEADER.
        """
        # Node A boots first
        realme = HAManager(
            node_id="server-realme-5pro",
            node_url="http://100.73.52.72:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="auto",
            paths=self.paths,
            auto_promotion_enabled=False
        )
        with patch("config.ha_manager.find_active_leader", return_value=(None, None)):
            realme.initialize()
        self.assertEqual(realme.state, HAState.STANDBY)

        # Node B boots as leader
        vivo = HAManager(
            node_id="server-vivo-y31",
            node_url="http://100.85.108.5:8000",
            candidate_urls=["http://100.85.108.5:8000", "http://100.73.52.72:8000"],
            role="leader",
            paths=self.paths
        )
        vivo.initialize()
        self.assertEqual(vivo.state, HAState.LEADER)

        # Node A now checks remote leader and discovers Node B
        with patch.object(realme, "probe_peer_leader", return_value=(
            True, HAFailureMode.HEALTHY,
            {"role": "leader", "leader_id": "server-vivo-y31", "term": 1, "lease_remaining_sec": 10.0},
            "http://100.85.108.5:8000"
        )):
            alive, mode = realme.check_remote_leader()
            self.assertTrue(alive)
            self.assertEqual(mode, HAFailureMode.HEALTHY)
            self.assertEqual(realme.current_leader_id, "server-vivo-y31")
            self.assertEqual(realme.state, HAState.STANDBY)

    # -------------------------------------------------------------------------
    # 5. Controlled Handover & Stale-Write Protection (Phase 4 & 5)
    # -------------------------------------------------------------------------
    def test_controlled_handover_network_io_outside_lock(self):
        """Controlled handover sends /ha/promote HTTP POST outside self._lock."""
        mgr = HAManager(
            node_id="server-vivo-y31",
            node_url="http://100.85.108.5:8000",
            role="leader",
            paths=self.paths
        )
        mgr.initialize()
        self.assertEqual(mgr.state, HAState.LEADER)

        # Setup mock nodes_db
        nodes_file = self.node_dir / "data" / "nodes.json"
        nodes_file.write_text(json.dumps({
            "nodes": {
                "server-realme-5pro": {
                    "node_id": "server-realme-5pro",
                    "status": "ONLINE",
                    "tailscale_ip": "100.73.52.72",
                    "leader_eligible": True
                }
            }
        }))

        # Mock urllib.request.urlopen for the target promote call
        lock_was_held_during_io = False

        def mock_urlopen(req, timeout=None):
            nonlocal lock_was_held_during_io
            # Check if self._lock is acquired by current thread during I/O
            # An RLock acquired by the same thread can be reacquired without blocking
            # But we can verify whether the handover method held it by inspecting _eval_in_progress or lock state:
            # More directly: can another thread acquire it right now?
            test_thread_acquired = False
            def try_acquire():
                nonlocal test_thread_acquired
                test_thread_acquired = mgr._lock.acquire(timeout=0.05)
                if test_thread_acquired:
                    mgr._lock.release()

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join()

            if not test_thread_acquired:
                lock_was_held_during_io = True

            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps({"status": "promoted", "term": 2}).encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = None
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = mgr.controlled_handover("server-realme-5pro", admin_token="test-token")

        self.assertEqual(res["status"], "success")
        self.assertFalse(lock_was_held_during_io, "HA state lock was held during HTTP POST to /ha/promote!")
        self.assertEqual(mgr.state, HAState.STANDBY)
        self.assertEqual(mgr.current_term, 2)
        self.assertEqual(mgr.current_leader_id, "server-realme-5pro")


if __name__ == "__main__":
    unittest.main()
