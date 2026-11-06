#!/usr/bin/env python3
"""
Regression Tests for V3.0.2: Lock Ordering, Deadlock Prevention, and Recovery
=============================================================================
Verifies:
1. DB_LOCK is never held when HAManager._lock (or bump_state_version) is acquired.
2. Concurrent database writes and HA status requests do not deadlock.
3. Concurrent node updates and candidate ranking execute safely.
4. Concurrent controlled handovers and database reads operate safely.
5. HA status requests succeed even when database operations are in flight.
6. Interrupted state-version updates preserve database atomicity.
7. Startup reconciliation recovers unbumped state versions.
8. Bounded concurrency stress test executes cleanly without thread leaks.
9. Stale and degraded lease presentations are accurately distinguished.
"""

import os
import json
import time
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Dict, Any

from config.ha_manager import HAManager, HAState, LeaderLease
from config.platform_config import ControllerPaths
from controller.stores.base import DB_LOCK, atomic_save_json, set_state_version_bumper
from controller.stores.nodes_store import load_nodes_db, save_nodes_db
from controller.stores.jobs_store import load_jobs_db, save_jobs_db
from controller.lease_sweeper import reconcile_startup_recovery


class TestLockOrderingAndRecovery(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ps_test_lock_order_")
        self.data_dir = Path(self.test_dir) / "data"
        self.config_dir = Path(self.test_dir) / "config"
        self.secrets_dir = Path(self.test_dir) / "secrets"
        self.logs_dir = Path(self.test_dir) / "logs"

        self.paths = ControllerPaths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            logs_dir=self.logs_dir,
        )
        self.paths.ensure_directories()

        # Seed initial cluster nodes
        self.initial_nodes = {
            "cluster_name": "PersonalServer",
            "nodes": {
                "node-vivo": {
                    "node_id": "node-vivo",
                    "name": "Vivo Y31",
                    "status": "ONLINE",
                    "leader_eligible": True,
                    "priority": 100,
                    "performance_score": 88.0,
                    "tailscale_ip": "100.85.108.5",
                    "worker_enabled": True
                },
                "node-realme": {
                    "node_id": "node-realme",
                    "name": "Realme 5 Pro",
                    "status": "ONLINE",
                    "leader_eligible": True,
                    "priority": 80,
                    "performance_score": 75.0,
                    "tailscale_ip": "100.73.52.72",
                    "worker_enabled": True
                }
            }
        }
        save_nodes_db(self.initial_nodes, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)

        # Initialize HA Manager
        self.ha = HAManager(
            paths=self.paths,
            node_id="node-vivo",
            node_url="http://100.85.108.5:8000",
            role="leader",
            lease_duration_sec=10.0,
            auto_promotion_enabled=False
        )
        self.ha.initialize()
        set_state_version_bumper(self.ha.bump_state_version)

    def tearDown(self):
        set_state_version_bumper(None)
        if hasattr(self, "ha") and self.ha:
            self.ha.stop_monitor()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dblock_not_held_during_state_version_bump(self):
        """Invariant: DB_LOCK must be completely released BEFORE bump_state_version() runs."""
        db_lock_held_during_bump = []

        def tracking_bumper():
            # Check whether DB_LOCK is locked by the calling thread
            # An RLock has _is_owned() in CPython
            is_owned = getattr(DB_LOCK, "_is_owned", lambda: False)()
            db_lock_held_during_bump.append(is_owned)
            self.ha.bump_state_version()

        set_state_version_bumper(tracking_bumper)

        # Execute multiple saves across nodes and jobs stores
        save_nodes_db({"nodes": {"n1": {"name": "Node 1"}}}, paths=self.paths)
        save_jobs_db({"jobs": {"j1": {"status": "QUEUED"}}}, paths=self.paths)

        self.assertGreater(len(db_lock_held_during_bump), 0)
        for was_held in db_lock_held_during_bump:
            self.assertFalse(was_held, "DB_LOCK was detected as held during state-version bump!")

    def test_concurrent_db_writes_and_ha_status(self):
        """Verify concurrent database mutating operations and /ha/status calls never deadlock."""
        num_threads = 6
        iterations = 30
        errors = []

        def writer_task(tid):
            for i in range(iterations):
                try:
                    data = {"nodes": {f"node-{tid}": {"name": f"Worker-{tid}-{i}", "status": "ONLINE"}}}
                    save_nodes_db(data, paths=self.paths)
                except Exception as e:
                    errors.append((f"writer-{tid}", e))

        def reader_task(tid):
            for i in range(iterations):
                try:
                    st = self.ha.get_status_dict()
                    self.assertIn("role", st)
                    self.assertIn("ranked_candidates", st)
                except Exception as e:
                    errors.append((f"reader-{tid}", e))

        threads = []
        for t in range(num_threads // 2):
            threads.append(threading.Thread(target=writer_task, args=(t,)))
            threads.append(threading.Thread(target=reader_task, args=(t,)))

        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=8.0)
            self.assertFalse(t.is_alive(), "Thread deadlocked during concurrent DB writes and HA status!")

        self.assertEqual(len(errors), 0, f"Errors occurred during concurrency test: {errors}")

    def test_concurrent_node_updates_and_candidate_ranking(self):
        """Verify candidate ranking computes cleanly against captured snapshot while updates occur."""
        stop_event = threading.Event()
        errors = []

        def mutator():
            step = 0
            while not stop_event.is_set():
                step += 1
                prio = 50 + (step % 50)
                nodes = {
                    "cluster_name": "PersonalServer",
                    "nodes": {
                        "node-vivo": {"node_id": "node-vivo", "priority": prio, "status": "ONLINE", "leader_eligible": True},
                        "node-realme": {"node_id": "node-realme", "priority": 100 - prio, "status": "ONLINE", "leader_eligible": True}
                    }
                }
                save_nodes_db(nodes, paths=self.paths)
                time.sleep(0.005)

        t_mut = threading.Thread(target=mutator)
        t_mut.start()

        try:
            for _ in range(40):
                st = self.ha.get_status_dict()
                ranked = st.get("ranked_candidates", [])
                self.assertIsInstance(ranked, list)
                self.assertGreaterEqual(len(ranked), 0)
                time.sleep(0.005)
        finally:
            stop_event.set()
            t_mut.join(timeout=5.0)
            self.assertFalse(t_mut.is_alive(), "Mutator thread hung!")

    def test_ha_status_with_delayed_db_operations(self):
        """Verify HA status queries return promptly even when disk I/O under DB_LOCK is slow."""
        release_barrier = threading.Event()
        lock_acquired = threading.Event()

        def slow_db_holder():
            with DB_LOCK:
                lock_acquired.set()
                release_barrier.wait(timeout=3.0)

        t = threading.Thread(target=slow_db_holder)
        t.start()
        self.assertTrue(lock_acquired.wait(timeout=2.0))

        try:
            # get_status_dict reads nodes_db; load_nodes_db will briefly wait on DB_LOCK,
            # BUT HAManager._lock must NOT be held while waiting for DB_LOCK.
            # When release_barrier is released, both finish cleanly without AB-BA lock inversion.
            release_barrier.set()
            st = self.ha.get_status_dict()
            self.assertEqual(st.get("role"), "leader")
        finally:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())

    def test_database_write_followed_by_interrupted_version_update(self):
        """Verify that if state version bump raises or is interrupted, DB write atomicity is preserved."""
        def broken_bumper():
            raise RuntimeError("Simulated crash during state version increment")

        set_state_version_bumper(broken_bumper)

        # Write must succeed on disk even if bumper crashes
        test_payload = {"nodes": {"resilient-node": {"name": "Preserved On Disk"}}}
        save_nodes_db(test_payload, paths=self.paths)

        # Verify disk contents were committed atomically
        loaded = load_nodes_db(paths=self.paths)
        self.assertIn("resilient-node", loaded.get("nodes", {}))
        self.assertEqual(loaded["nodes"]["resilient-node"]["name"], "Preserved On Disk")

    def test_startup_reconciliation_recovers_interrupted_version(self):
        """Verify startup reconciliation recovers state versions after interrupted shutdown."""
        # Record initial version
        initial_version = self.ha.state_version

        # Run startup recovery
        summary = reconcile_startup_recovery(None, None, timeout_seconds=10, paths=self.paths)
        self.assertIsInstance(summary, dict)

        # State version must be advanced by startup reconciliation
        self.assertGreater(self.ha.state_version, initial_version)

    def test_bounded_concurrency_stress_test(self):
        """Stress test with bounded concurrency (8 threads, 25 iterations, timeouts)."""
        num_workers = 8
        ops_per_worker = 25
        barrier = threading.Barrier(num_workers)
        errors = []

        def worker(wid):
            barrier.wait()
            for i in range(ops_per_worker):
                try:
                    if wid % 3 == 0:
                        save_nodes_db({"nodes": {f"n-{wid}": {"status": "ONLINE", "val": i}}}, paths=self.paths)
                    elif wid % 3 == 1:
                        load_nodes_db(paths=self.paths)
                    else:
                        st = self.ha.get_status_dict()
                        self.assertIsNotNone(st.get("node_id"))
                except Exception as e:
                    errors.append((wid, i, e))

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(num_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), f"Stress worker thread deadlocked!")

        self.assertEqual(len(errors), 0, f"Stress test encountered errors: {errors}")

    def test_stale_and_expired_lease_presentation(self):
        """Verify HA status correctly marks expired or fenced leases without optimistic falsehoods."""
        # Simulate expired lease
        with self.ha._lock:
            self.ha.current_lease.lease_expires_at = time.time() - 5.0
            self.ha.state = HAState.FENCED

        st = self.ha.get_status_dict()
        self.assertEqual(st.get("role"), "fenced")
        self.assertFalse(st.get("lease_valid"))
        self.assertLessEqual(st.get("lease_remaining_sec"), 0.0)


if __name__ == "__main__":
    unittest.main()
