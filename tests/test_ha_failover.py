#!/usr/bin/env python3
"""
PersonalServer High Availability (HA) & Master Failover Integration Test Suite
=============================================================================
Exercises automated master failure detection, deterministic leader election,
cluster state replication, split-brain immunity, and transparent worker redirection.
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

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths, ControllerPaths
from config.ha_manager import (
    HAManager,
    HAState,
    LeaderLease,
    find_active_leader
)
from controller.controller import (
    ControllerHandler,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    load_apps_db,
    save_apps_db,
    get_ha_manager,
    JOB_STATE_QUEUED,
    JOB_STATE_SUCCEEDED
)
from agent.simulated_worker import SimulatedWorker, WorkerBehavior


class TestHAFailover(unittest.TestCase):

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_ha_test_"))
        self.master_dir = self.test_root / "master"
        self.standby_dir = self.test_root / "standby"

        for d in (self.master_dir, self.standby_dir):
            for sub in ("data", "config/secrets", "runtime", "logs", "storage"):
                (d / sub).mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "test-ha-cluster-enrollment-secret"
        for d in (self.master_dir, self.standby_dir):
            (d / "config" / "secrets" / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        # Activate paths override for the test lifecycle
        self.paths_ctx = override_controller_paths(
            data_dir=self.master_dir / "data",
            config_dir=self.master_dir / "config",
            secrets_dir=self.master_dir / "config" / "secrets",
            runtime_dir=self.master_dir / "runtime",
            logs_dir=self.master_dir / "logs",
            storage_dir=self.master_dir / "storage"
        )
        self.master_paths = self.paths_ctx.__enter__()

        self.standby_paths = ControllerPaths(
            data_dir=self.standby_dir / "data",
            config_dir=self.standby_dir / "config",
            secrets_dir=self.standby_dir / "config" / "secrets",
            runtime_dir=self.standby_dir / "runtime",
            logs_dir=self.standby_dir / "logs",
            storage_dir=self.standby_dir / "storage"
        )

        get_ha_manager(paths=self.master_paths, force_reload=True)

    def tearDown(self):
        if hasattr(self, "paths_ctx"):
            self.paths_ctx.__exit__(None, None, None)
        try:
            shutil.rmtree(self.test_root)
        except Exception:
            pass

    def test_leader_lease_lifecycle_and_persistence(self):
        """Verifies LeaderLease calculation, validity, serialization, and disk persistence."""
        lease_file = self.master_dir / "data" / "leader_lease.json"
        now = time.time()

        lease = LeaderLease(
            term=1,
            leader_id="node-alpha",
            leader_url="http://127.0.0.1:8000",
            lease_expires_at=now + 10.0,
            state_version=5
        )

        self.assertTrue(lease.is_valid())
        self.assertGreater(lease.time_remaining_sec(), 5.0)

        lease.save_to_file(lease_file)
        self.assertTrue(lease_file.exists())

        loaded = LeaderLease.load_from_file(lease_file)
        self.assertEqual(loaded.term, 1)
        self.assertEqual(loaded.leader_id, "node-alpha")
        self.assertEqual(loaded.leader_url, "http://127.0.0.1:8000")
        self.assertEqual(loaded.state_version, 5)
        self.assertTrue(loaded.is_valid())

        # Test expired lease
        expired_lease = LeaderLease(
            term=1,
            leader_id="node-alpha",
            leader_url="http://127.0.0.1:8000",
            lease_expires_at=now - 5.0
        )
        self.assertFalse(expired_lease.is_valid())
        self.assertEqual(expired_lease.time_remaining_sec(), 0.0)

    def test_ha_manager_initialization_and_lease_renewal(self):
        """Tests that HAManager claims leadership when no remote leader exists and renews lease."""
        mgr = HAManager(
            node_id="master-pc",
            node_url="http://127.0.0.1:8000",
            candidate_urls=["http://127.0.0.1:8000"],
            role="leader",
            lease_duration_sec=5.0,
            paths=self.master_paths
        )
        mgr.initialize()

        self.assertTrue(mgr.is_leader)
        self.assertEqual(mgr.current_term, 1)
        self.assertEqual(mgr.current_leader_id, "master-pc")
        self.assertTrue(mgr.current_lease.is_valid())

        # Bump state version
        v1 = mgr.bump_state_version()
        self.assertEqual(v1, 1)
        v2 = mgr.bump_state_version()
        self.assertEqual(v2, 2)
        self.assertEqual(mgr.state_version, 2)

        # Renew lease
        initial_expiry = mgr.current_lease.lease_expires_at
        time.sleep(0.1)
        renewed = mgr.renew_lease()
        self.assertTrue(renewed)
        self.assertGreaterEqual(mgr.current_lease.lease_expires_at, initial_expiry)

        status = mgr.get_status_dict()
        self.assertEqual(status["role"], "leader")
        self.assertEqual(status["term"], 1)
        self.assertEqual(status["state_version"], 2)

    def test_standby_state_synchronization(self):
        """Tests that Standby node fetches state snapshot (/ha/sync) and replicates databases."""
        # Seed cluster data on Master
        save_nodes_db({
            "cluster_name": "PersonalServer-HA",
            "nodes": {
                "node-worker-1": {
                    "node_id": "node-worker-1",
                    "name": "Termux-Phone",
                    "status": "ONLINE",
                    "role": "compute"
                }
            }
        })
        save_jobs_db({
            "jobs": {
                "job-task-001": {
                    "job_id": "job-task-001",
                    "type": "health-check",
                    "status": JOB_STATE_QUEUED
                }
            }
        })
        save_apps_db({
            "apps": {
                "demo-app": {
                    "id": "demo-app",
                    "name": "Demo Web Service",
                    "port": 8088
                }
            }
        })

        master_server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        master_port = master_server.server_address[1]
        master_url = f"http://127.0.0.1:{master_port}"

        master_thread = threading.Thread(target=master_server.serve_forever, daemon=True)
        master_thread.start()

        try:
            standby_mgr = HAManager(
                node_id="standby-pi",
                node_url="http://127.0.0.1:8001",
                candidate_urls=[master_url, "http://127.0.0.1:8001"],
                role="standby",
                auth_token=self.enrollment_token,
                paths=self.standby_paths
            )

            # Standby syncs state from active master
            sync_success = standby_mgr.sync_state_from_leader(master_url, self.enrollment_token)
            self.assertTrue(sync_success)

            # Verify standby's local databases match master's databases exactly!
            with open(self.standby_paths.nodes_file, "r", encoding="utf-8") as f:
                replicated_nodes = json.load(f)
            with open(self.standby_paths.jobs_file, "r", encoding="utf-8") as f:
                replicated_jobs = json.load(f)
            with open(self.standby_paths.apps_file, "r", encoding="utf-8") as f:
                replicated_apps = json.load(f)

            self.assertIn("node-worker-1", replicated_nodes.get("nodes", {}))
            self.assertEqual(replicated_nodes["nodes"]["node-worker-1"]["name"], "Termux-Phone")

            self.assertIn("job-task-001", replicated_jobs.get("jobs", {}))
            self.assertEqual(replicated_jobs["jobs"]["job-task-001"]["status"], JOB_STATE_QUEUED)

            self.assertIn("demo-app", replicated_apps.get("apps", {}))
            self.assertEqual(replicated_apps["apps"]["demo-app"]["name"], "Demo Web Service")

        finally:
            master_server.shutdown()
            master_server.server_close()

    def test_automatic_master_failover_when_master_goes_down(self):
        """
        Simulates Master death:
        1. Master is running and active.
        2. Standby syncs and monitors master.
        3. Master server terminates abruptly (simulating device power outage).
        4. Standby detects failure, triggers election, and promotes to LEADER!
        5. Cluster state remains fully intact on new leader.
        """
        save_nodes_db({
            "cluster_name": "PersonalServer-FailoverTest",
            "nodes": {
                "sim-worker-alpha": {
                    "node_id": "sim-worker-alpha",
                    "status": "ONLINE",
                    "role": "compute"
                }
            }
        })
        save_jobs_db({
            "jobs": {
                "critical-job-42": {
                    "job_id": "critical-job-42",
                    "type": "health-check",
                    "status": JOB_STATE_QUEUED
                }
            }
        })

        master_server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        master_port = master_server.server_address[1]
        master_url = f"http://127.0.0.1:{master_port}"
        master_thread = threading.Thread(target=master_server.serve_forever, daemon=True)
        master_thread.start()

        promoted_event = threading.Event()

        def on_promoted():
            promoted_event.set()

        standby_mgr = HAManager(
            node_id="node-standby-02",
            node_url="http://127.0.0.1:8002",
            candidate_urls=[master_url, "http://127.0.0.1:8002"],
            role="standby",
            lease_duration_sec=0.8,
            check_interval_sec=0.1,
            auth_token=self.enrollment_token,
            paths=self.standby_paths,
            on_promotion_callback=on_promoted
        )

        # Sync initial state from live master
        sync_ok = standby_mgr.sync_state_from_leader(master_url, self.enrollment_token)
        self.assertTrue(sync_ok)

        # Start monitoring
        standby_mgr.start_monitor()

        # Verify standby sees healthy master
        self.assertFalse(standby_mgr.is_leader)
        self.assertTrue(standby_mgr.is_standby)

        # 3. CRASH THE MASTER (device power outage / network disconnection)
        master_server.shutdown()
        master_server.server_close()

        # 4. Wait for Standby to detect failure and promote to LEADER
        promoted = promoted_event.wait(timeout=3.0)
        self.assertTrue(promoted, "Standby did not promote to Leader within timeout after Master crash")
        self.assertTrue(standby_mgr.is_leader)
        self.assertEqual(standby_mgr.current_leader_id, "node-standby-02")
        self.assertGreaterEqual(standby_mgr.current_term, 1)

        # 5. Verify that critical cluster job and worker node survived failover intact
        with open(self.standby_paths.jobs_file, "r", encoding="utf-8") as f:
            replicated_jobs = json.load(f)
        with open(self.standby_paths.nodes_file, "r", encoding="utf-8") as f:
            replicated_nodes = json.load(f)
        self.assertIn("critical-job-42", replicated_jobs.get("jobs", {}))
        self.assertIn("sim-worker-alpha", replicated_nodes.get("nodes", {}))

        standby_mgr.stop_monitor()

    def test_worker_transparent_failover_redirection(self):
        """
        Tests that a worker connected to Master switches to the newly promoted Master
        and successfully sends heartbeats without dropping offline.
        """
        # 1. Setup Master controller
        master_server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        m_port = master_server.server_address[1]
        master_url = f"http://127.0.0.1:{m_port}"
        threading.Thread(target=master_server.serve_forever, daemon=True).start()

        # 2. Setup Standby controller (sharing enrollment token)
        standby_server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        s_port = standby_server.server_address[1]
        standby_url = f"http://127.0.0.1:{s_port}"
        threading.Thread(target=standby_server.serve_forever, daemon=True).start()

        try:
            # 3. Simulated worker registers with master_url
            worker = SimulatedWorker(
                node_id="sim-node-worker-ha",
                controller_url=master_url,
                enrollment_token=self.enrollment_token
            )
            worker.candidate_controller_urls = [master_url, standby_url]
            reg_resp = worker.register()
            self.assertIn("auth_token", reg_resp)

            # Send heartbeat to initial master
            hb_resp = worker.send_heartbeat()
            self.assertIn(hb_resp["status"], ("ok", "online"))

            # 4. Master goes DOWN (crash)
            master_server.shutdown()
            master_server.server_close()

            # Standby promotes self to Master
            req_promote = urllib.request.Request(
                f"{standby_url}/ha/promote",
                data=json.dumps({"reason": "Test promotion"}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.enrollment_token}", "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req_promote, timeout=3) as resp:
                promote_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(promote_data["status"], "promoted")

            # 5. Worker detects master failure and auto-failovers to standby_url
            failover_detected = worker.auto_failover([master_url, standby_url])
            self.assertTrue(failover_detected)
            self.assertEqual(worker.controller_url, standby_url)

            # Worker re-registers and sends heartbeat to the new master
            re_reg = worker.register()
            self.assertIn("auth_token", re_reg)
            hb_new = worker.send_heartbeat()
            self.assertIn(hb_new["status"], ("ok", "online"))

        finally:
            standby_server.shutdown()
            standby_server.server_close()

    def test_split_brain_prevention_old_master_steps_down(self):
        """
        Tests that an old partitioned master that recovers sees a higher term
        from the new leader, demotes itself to STANDBY, and yields authority.
        """
        mgr = HAManager(
            node_id="old-master",
            node_url="http://127.0.0.1:8000",
            role="leader",
            paths=self.master_paths
        )
        mgr.initialize()
        self.assertTrue(mgr.is_leader)
        self.assertEqual(mgr.current_term, 1)

        # New master was elected with Term 2
        mgr.step_down(new_term=2, new_leader_id="new-master", new_leader_url="http://127.0.0.1:8005")

        # Verify old master is now safely in STANDBY state
        self.assertFalse(mgr.is_leader)
        self.assertTrue(mgr.is_standby)
        self.assertEqual(mgr.current_term, 2)
        self.assertEqual(mgr.current_leader_id, "new-master")
        self.assertEqual(mgr.current_leader_url, "http://127.0.0.1:8005")

    def test_find_active_leader_discovery(self):
        """Tests that find_active_leader accurately identifies the alive LEADER node."""
        server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        port = server.server_address[1]
        live_url = f"http://127.0.0.1:{port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            candidates = ["http://127.0.0.1:19999", live_url, "http://127.0.0.1:19998"]
            leader_url, leader_data = find_active_leader(candidates, timeout=1.0)

            self.assertEqual(leader_url, live_url)
            self.assertIsNotNone(leader_data)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
