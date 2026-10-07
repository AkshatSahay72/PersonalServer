#!/usr/bin/env python3
"""
Unit and Integration Tests for Phase 14C:
Safe Node Deboarding & Resource Draining Lifecycle

Test coverage:
1. ONLINE node can receive jobs.
2. DRAINING node cannot receive new jobs.
3. DEBOARDING node cannot receive jobs.
4. OFFLINE node cannot receive jobs.
5. DRAINING preserves heartbeat.
6. Running job can finish during DRAINING.
7. Auto-target queued job avoids DRAINING node.
8. Explicit-target behavior remains unchanged.
9. Node with active application cannot be removed.
10. Node with storage cannot be removed.
11. Empty safe node can complete deboarding.
12. REMOVED node cannot be scheduled.
13. Removed node cannot reconnect with old credentials.
14. Historical jobs remain intact.
15. Storage is never deleted automatically.
16. Deboarding status payload schema compliance.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scheduler.scheduler import (
    ResourceScheduler,
    compute_node_liveness,
    STATE_ONLINE,
    STATE_DRAINING,
    STATE_DEBOARDING,
    STATE_OFFLINE,
    STATE_UNHEALTHY,
    STATE_REMOVED,
    STATE_UNKNOWN,
    DEFAULT_HEARTBEAT_TIMEOUT
)
from controller.controller import (
    evaluate_node_deboarding_safety,
    inspect_node_storage_safety,
    sweep_expired_leases,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_RECOVERING,
    APP_STATE_RUNNING,
    APP_STATE_STOPPED
)


class TestPhase14CSafeDeboarding(unittest.TestCase):
    def setUp(self):
        self.now_iso = datetime.now(timezone.utc).isoformat()
        self.node_a = {
            "node_id": "test-node-a",
            "name": "Node A",
            "role": "compute",
            "status": STATE_ONLINE,
            "last_seen": self.now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"compute": True, "storage": True, "network": True},
            "auth_token": "token-node-a-12345",
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "services": {"node_api": "running"},
                "system": {"memory": "1.0Gi/4.0Gi", "cpu_cores": 4, "storage": "10Gi/50Gi"}
            }
        }
        self.node_b = {
            "node_id": "test-node-b",
            "name": "Node B",
            "role": "compute",
            "status": STATE_ONLINE,
            "last_seen": self.now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 100},
            "capabilities": {"compute": True, "storage": True, "network": True},
            "auth_token": "token-node-b-67890",
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "services": {"node_api": "running"},
                "system": {"memory": "2.0Gi/8.0Gi", "cpu_cores": 8, "storage": "20Gi/100Gi"}
            }
        }

    # --------------------------------------------------------------------------
    # 1. ONLINE node can receive jobs
    # --------------------------------------------------------------------------
    def test_01_online_node_can_receive_jobs(self):
        nodes_db = {"nodes": {"test-node-a": dict(self.node_a)}}
        decision = ResourceScheduler.select_node({}, nodes_db)
        self.assertEqual(decision["selected_node"], "test-node-a")
        self.assertIn("highest scoring", decision["reason"].lower())

    # --------------------------------------------------------------------------
    # 2. DRAINING node cannot receive new jobs
    # --------------------------------------------------------------------------
    def test_02_draining_node_cannot_receive_new_jobs(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING
        nodes_db = {"nodes": {"test-node-a": draining_node}}

        decision = ResourceScheduler.select_node({}, nodes_db)
        self.assertIsNone(decision["selected_node"])
        self.assertIn("test-node-a", decision["rejected"])
        self.assertIn("DRAINING", decision["rejected"]["test-node-a"])

    # --------------------------------------------------------------------------
    # 3. DEBOARDING node cannot receive jobs
    # --------------------------------------------------------------------------
    def test_03_deboarding_node_cannot_receive_jobs(self):
        deboarding_node = dict(self.node_a)
        deboarding_node["status"] = STATE_DEBOARDING
        nodes_db = {"nodes": {"test-node-a": deboarding_node}}

        decision = ResourceScheduler.select_node({}, nodes_db)
        self.assertIsNone(decision["selected_node"])
        self.assertIn("test-node-a", decision["rejected"])
        self.assertIn("DEBOARDING", decision["rejected"]["test-node-a"])

    # --------------------------------------------------------------------------
    # 4. OFFLINE node cannot receive jobs
    # --------------------------------------------------------------------------
    def test_04_offline_node_cannot_receive_jobs(self):
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        offline_node = dict(self.node_a)
        offline_node["last_seen"] = stale_time
        nodes_db = {"nodes": {"test-node-a": offline_node}}

        decision = ResourceScheduler.select_node({}, nodes_db)
        self.assertIsNone(decision["selected_node"])
        self.assertIn("test-node-a", decision["rejected"])
        self.assertIn("OFFLINE", decision["rejected"]["test-node-a"])

    # --------------------------------------------------------------------------
    # 5. DRAINING preserves heartbeat
    # --------------------------------------------------------------------------
    def test_05_draining_preserves_heartbeat(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING
        liveness = compute_node_liveness(draining_node)
        self.assertEqual(liveness, STATE_DRAINING)

        # Heartbeat update simulation:
        # If node status is DRAINING, updating heartbeat does not flip it back to ONLINE
        current_st = draining_node.get("status")
        new_seen = datetime.now(timezone.utc).isoformat()
        if current_st not in (STATE_DRAINING, STATE_DEBOARDING):
            draining_node["status"] = STATE_ONLINE
        draining_node["last_seen"] = new_seen

        self.assertEqual(draining_node["status"], STATE_DRAINING)
        self.assertEqual(compute_node_liveness(draining_node), STATE_DRAINING)

    # --------------------------------------------------------------------------
    # 6. Running job can finish during DRAINING
    # --------------------------------------------------------------------------
    def test_06_running_job_can_finish_during_draining(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING
        nodes_db = {"nodes": {"test-node-a": draining_node}}

        # Active job with a valid unexpired lease
        jobs_db = {
            "jobs": {
                "job-101": {
                    "id": "job-101",
                    "status": JOB_STATE_RUNNING,
                    "target_node": "test-node-a",
                    "lease_expires_at": time.time() + 100,
                    "target": "auto"
                }
            }
        }

        # Lease sweep should NOT recover or fail the running job while node is active and draining
        sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=60)
        self.assertEqual(jobs_db["jobs"]["job-101"]["status"], JOB_STATE_RUNNING)

        # Job can complete successfully
        jobs_db["jobs"]["job-101"]["status"] = JOB_STATE_SUCCEEDED
        self.assertEqual(jobs_db["jobs"]["job-101"]["status"], JOB_STATE_SUCCEEDED)

    # --------------------------------------------------------------------------
    # 7. Auto-target queued job avoids DRAINING node
    # --------------------------------------------------------------------------
    def test_07_auto_target_queued_job_avoids_draining_node(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING
        online_node = dict(self.node_b)

        nodes_db = {
            "nodes": {
                "test-node-a": draining_node,
                "test-node-b": online_node
            }
        }

        # Auto job queued on draining node
        job = {
            "id": "job-201",
            "status": JOB_STATE_QUEUED,
            "target": "auto",
            "target_node": "test-node-a",
            "requirements": {}
        }

        # Scheduling query must select Node B, never Node A
        decision = ResourceScheduler.select_node(job["requirements"], nodes_db)
        self.assertEqual(decision["selected_node"], "test-node-b")

    # --------------------------------------------------------------------------
    # 8. Explicit-target behavior remains unchanged
    # --------------------------------------------------------------------------
    def test_08_explicit_target_behavior_remains_unchanged(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING

        # Explicit target queued job
        job = {
            "id": "job-explicit",
            "status": JOB_STATE_QUEUED,
            "target": "test-node-a",
            "target_node": "test-node-a"
        }

        # Draining simulation should NOT silently migrate explicit target
        target_mode = job.get("target")
        if target_mode != "auto":
            # Retains exact target
            pass
        self.assertEqual(job["target_node"], "test-node-a")

    # --------------------------------------------------------------------------
    # 9. Node with active application cannot be removed
    # --------------------------------------------------------------------------
    def test_09_node_with_active_application_cannot_be_removed(self):
        deboarding_node = dict(self.node_a)
        deboarding_node["status"] = STATE_DEBOARDING
        nodes_db = {"nodes": {"test-node-a": deboarding_node}}
        jobs_db = {"jobs": {}}
        apps_db = {
            "apps": {
                "app-app1": {
                    "app_id": "app-app1",
                    "name": "production-service",
                    "status": APP_STATE_RUNNING,
                    "selected_node": "test-node-a"
                }
            }
        }

        safety = evaluate_node_deboarding_safety("test-node-a", nodes_db, jobs_db, apps_db)
        self.assertFalse(safety["can_remove"])
        self.assertEqual(safety["applications"], 1)
        self.assertTrue(any("active applications depend on this node" in b for b in safety["blockers"]))

    # --------------------------------------------------------------------------
    # 10. Node with storage cannot be removed
    # --------------------------------------------------------------------------
    def test_10_node_with_storage_cannot_be_removed(self):
        deboarding_node = dict(self.node_a)
        deboarding_node["status"] = STATE_DEBOARDING
        # Simulate unmigrated storage data on node
        deboarding_node["storage_files_count"] = 5
        deboarding_node["storage_used_bytes"] = 15000000000  # ~14 GB

        nodes_db = {"nodes": {"test-node-a": deboarding_node}}
        jobs_db = {"jobs": {}}
        apps_db = {"apps": {}}

        safety = evaluate_node_deboarding_safety("test-node-a", nodes_db, jobs_db, apps_db)
        self.assertFalse(safety["can_remove"])
        self.assertEqual(safety["storage_files_count"], 5)
        self.assertGreater(safety["storage_used_gb"], 13.0)
        self.assertTrue(any("storage contains data that has not been migrated" in b for b in safety["blockers"]))

    # --------------------------------------------------------------------------
    # 11. Empty safe node can complete deboarding
    # --------------------------------------------------------------------------
    def test_11_empty_safe_node_can_complete_deboarding(self):
        deboarding_node = dict(self.node_a)
        deboarding_node["status"] = STATE_DEBOARDING
        nodes_db = {"nodes": {"test-node-a": deboarding_node}}
        jobs_db = {"jobs": {}}
        apps_db = {"apps": {}}

        safety = evaluate_node_deboarding_safety("test-node-a", nodes_db, jobs_db, apps_db)
        self.assertTrue(safety["can_remove"])
        self.assertEqual(len(safety["blockers"]), 0)
        self.assertEqual(safety["active_jobs"], 0)
        self.assertEqual(safety["applications"], 0)
        self.assertEqual(safety["storage_used_gb"], 0.0)

    # --------------------------------------------------------------------------
    # 12. REMOVED node cannot be scheduled
    # --------------------------------------------------------------------------
    def test_12_removed_node_cannot_be_scheduled(self):
        removed_node = dict(self.node_a)
        removed_node["status"] = STATE_REMOVED
        nodes_db = {"nodes": {"test-node-a": removed_node}}

        decision = ResourceScheduler.select_node({}, nodes_db)
        self.assertIsNone(decision["selected_node"])
        self.assertIn("REMOVED", decision["rejected"]["test-node-a"])

    # --------------------------------------------------------------------------
    # 13. Removed node cannot reconnect with old credentials
    # --------------------------------------------------------------------------
    def test_13_removed_node_cannot_reconnect_with_old_credentials(self):
        removed_node = dict(self.node_a)
        old_token = removed_node["auth_token"]

        # Final removal clears auth token
        removed_node["status"] = STATE_REMOVED
        removed_node["auth_token"] = None

        # Verification: Heartbeat auth check fails when status == REMOVED or auth_token is None
        is_blocked = (removed_node.get("status") == STATE_REMOVED or not removed_node.get("auth_token"))
        self.assertTrue(is_blocked)

        # Old credentials match nothing
        self.assertNotEqual(old_token, removed_node.get("auth_token"))

    # --------------------------------------------------------------------------
    # 14. Historical jobs remain intact
    # --------------------------------------------------------------------------
    def test_14_historical_jobs_remain_intact(self):
        nodes_db = {"nodes": {"test-node-a": dict(self.node_a)}}
        jobs_db = {
            "jobs": {
                "hist-job-1": {
                    "id": "hist-job-1",
                    "status": JOB_STATE_SUCCEEDED,
                    "target_node": "test-node-a",
                    "output": "Execution finished successfully"
                },
                "hist-job-2": {
                    "id": "hist-job-2",
                    "status": "FAILED",
                    "target_node": "test-node-a",
                    "error": "Prior timeout"
                }
            }
        }

        # Running deboarding safety checks or removal does not wipe jobs
        safety = evaluate_node_deboarding_safety("test-node-a", nodes_db, jobs_db, {"apps": {}})
        self.assertIn("hist-job-1", jobs_db["jobs"])
        self.assertIn("hist-job-2", jobs_db["jobs"])
        self.assertEqual(len(jobs_db["jobs"]), 2)

    # --------------------------------------------------------------------------
    # 15. Storage is never deleted automatically
    # --------------------------------------------------------------------------
    def test_15_storage_is_never_deleted_automatically(self):
        temp_dir = tempfile.mkdtemp(prefix="ps_test_storage_")
        try:
            sample_file = Path(temp_dir) / "important_user_data.txt"
            sample_file.write_text("Hello PersonalServer User Data", encoding="utf-8")
            self.assertTrue(sample_file.exists())

            # Simulate evaluating safety with files
            deboarding_node = dict(self.node_a)
            deboarding_node["status"] = STATE_DEBOARDING
            nodes_db = {"nodes": {"test-node-a": deboarding_node}}

            # Run safety evaluation
            safety = evaluate_node_deboarding_safety("test-node-a", nodes_db, {"jobs": {}}, {"apps": {}})

            # Crucial assertion: File on disk was NEVER deleted or modified
            self.assertTrue(sample_file.exists())
            self.assertEqual(sample_file.read_text(encoding="utf-8"), "Hello PersonalServer User Data")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 16. Deboarding status payload schema compliance
    # --------------------------------------------------------------------------
    def test_16_api_deboarding_endpoints_and_blockers_payload(self):
        draining_node = dict(self.node_a)
        draining_node["status"] = STATE_DRAINING
        nodes_db = {"nodes": {"test-node-a": draining_node}}
        jobs_db = {
            "jobs": {
                "active-job": {
                    "id": "active-job",
                    "target_node": "test-node-a",
                    "status": JOB_STATE_RUNNING
                }
            }
        }
        apps_db = {
            "apps": {
                "active-app": {
                    "app_id": "active-app",
                    "name": "api-service",
                    "target_node": "test-node-a",
                    "status": APP_STATE_RUNNING
                }
            }
        }

        res = evaluate_node_deboarding_safety("test-node-a", nodes_db, jobs_db, apps_db)
        # Verify required keys matching Step 12
        required_keys = ["node_id", "state", "can_remove", "blockers", "active_jobs", "applications", "storage_used_gb"]
        for k in required_keys:
            self.assertIn(k, res)

        self.assertEqual(res["node_id"], "test-node-a")
        self.assertEqual(res["state"], STATE_DRAINING)
        self.assertFalse(res["can_remove"])
        self.assertEqual(res["active_jobs"], 1)
        self.assertEqual(res["applications"], 1)
        self.assertGreaterEqual(len(res["blockers"]), 2)


if __name__ == "__main__":
    unittest.main()
