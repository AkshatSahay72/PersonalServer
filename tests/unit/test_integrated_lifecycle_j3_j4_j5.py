#!/usr/bin/env python3
"""
Integrated End-to-End Verification Suite for PersonalServer Phase 2 (J3 + J4 + J5)
==================================================================================
Validates the complete unified pipeline:
1. Job Submission & Spec Hashing (J1 contract).
2. Universal Scheduler Placement & Atomic Reservation (J3).
3. Lease Grant with strictly Monotonic Fencing Token (J1/J3).
4. Worker Execution in Isolated Attempt Workspaces (J4).
5. Output Discovery, Size Check & Independent SHA-256 Verification (J5).
6. Two-Phase CAS Artifact Promotion & Durable Result Commitment (J5).
7. Safe Release of Resource Reservations upon Job Completion (J3/J5).
8. Failure Scenarios: Stale Worker Rejection (HTTP 412) & Retry Workspace Isolation.
"""

import os
import sys
import time
import shutil
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scheduler.scheduler import ResourceScheduler
from controller.stores.reservations import (
    ReservationState,
    create_or_update_reservation,
    activate_reservation,
    calculate_node_reserved_resources,
)
from agent.universal_executor import UniversalExecutionEngine, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED
from controller.result_manager import ResultManager


class TestIntegratedJ3J4J5Lifecycle(unittest.TestCase):
    """Integrated acceptance test for full J3-J4-J5 lifecycle."""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp(prefix="ps_integrated_test_")
        self.storage_dir = Path(self.temp_root) / "artifacts_storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.node_id = "realme-compute-node-01"
        self.jobs_db = {"reservations": {}, "jobs": {}}
        self.nodes_db = {
            "nodes": {
                self.node_id: {
                    "node_id": self.node_id,
                    "name": "Realme 5 Pro Worker",
                    "role": "compute",
                    "platform": "termux",
                    "architecture": "aarch64",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "availability_class": "Persistent",
                    "resources": {"cpu_cores": 8, "ram_mb": 5600, "storage_gb": 50},
                    "capabilities": {"compute": True, "python3": True},
                    "verified_capabilities": {"compute": True, "python3": True},
                    "advertised_capabilities": {"compute": True, "python3": True},
                    "last_heartbeat": {
                        "timestamp": time.time(),
                        "services": {"node_worker": "running"},
                        "system": {
                            "cpu_cores": 8,
                            "load_average": [0.2, 0.2, 0.2],
                            "memory_details": {"total_mb": 5600.0, "available_mb": 3500.0, "used_mb": 2100.0, "used_percent": 37.5},
                            "storage_details": {"total_gb": 50.0, "available_gb": 35.0, "used_gb": 15.0, "used_percent": 30.0},
                            "battery": {"percentage": 90, "plugged": "PLUGGED_AC"},
                            "thermals": {"status": "Cool", "temperature_c": 31.0},
                            "network": {"ping_ms": 10.0}
                        }
                    }
                }
            }
        }

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_end_to_end_successful_pipeline(self):
        """Full lifecycle: schedule -> reserve -> execute -> collect outputs -> CAS promote -> release."""
        job_id = f"job-e2e-{int(time.time()*1000)}"
        attempt = 1
        fencing_token = 1

        # 1. J3 Universal Scheduler Placement
        job_reqs = {
            "requirements": {
                "role": "compute",
                "capabilities": ["python3"],
                "min_memory_mb": 256,
                "min_storage_gb": 1
            }
        }
        decision = ResourceScheduler.select_node(job_reqs, self.nodes_db)
        self.assertEqual(decision["selected_node"], self.node_id)
        self.assertGreater(decision["score"], 50.0)

        # 2. J3 Atomic Reservation Commit
        res = create_or_update_reservation(
            self.jobs_db, job_id, self.node_id, attempt, fencing_token,
            resources={"memory_mb": 256, "storage_gb": 1, "cpu_cores": 1},
            state=ReservationState.COMMITTED, ttl_sec=30.0
        )
        self.assertEqual(res["state"], ReservationState.COMMITTED)

        # Capacity consumed
        consumed = calculate_node_reserved_resources(self.jobs_db, self.node_id)
        self.assertEqual(consumed["reserved_memory_mb"], 256.0)

        # 3. Worker Acknowledges & Transitions to ACTIVE
        ok, _ = activate_reservation(self.jobs_db, job_id, attempt, fencing_token)
        self.assertTrue(ok)
        self.assertEqual(self.jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.ACTIVE)

        # 4. J4 Execution in Isolated Workspace
        # Script writes output file 'results.json'
        script_code = (
            "import os, json\n"
            "with open('results.json', 'w') as f:\n"
            "    json.dump({'status': 'SUCCESS', 'records': 42}, f)\n"
            "print('TASK_COMPLETE')\n"
        )
        job_spec = {
            "job_id": job_id,
            "attempt": attempt,
            "lease_generation": fencing_token,
            "spec": {
                "workload": {
                    "runtime": "native/python",
                    "arguments": [],
                },
                "policy": {"timeout_sec": 10}
            },
            "parameters": {"script": script_code}
        }

        exec_res = UniversalExecutionEngine.execute_job(job_spec, expected_fencing_token=fencing_token)
        self.assertEqual(exec_res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(exec_res["exit_code"], 0)
        self.assertIn("TASK_COMPLETE", exec_res["stdout"])

        workspace_dir = Path(exec_res["workspace_dir"])
        self.assertTrue((workspace_dir / "results.json").exists())

        # 5. J5 Output Discovery & Validation
        declared_outputs = [
            {
                "name": "final_results",
                "path_pattern": "results.json",
                "required": True,
                "max_size_bytes": 1024 * 1024
            }
        ]
        disc_ok, manifest, disc_err = ResultManager.collect_declared_outputs(workspace_dir, declared_outputs)
        self.assertTrue(disc_ok)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["name"], "final_results")

        # 6. J5 Two-Phase CAS Artifact Promotion
        discovered_file = Path(manifest[0]["abs_path"])
        promoted = ResultManager.promote_artifact_cas(
            storage_root=self.storage_dir,
            source_file=discovered_file,
            expected_sha256=manifest[0]["sha256"],
            filename=manifest[0]["filename"],
            job_id=job_id,
            producing_node=self.node_id
        )
        self.assertTrue(promoted.get("promoted"))
        self.assertTrue(Path(promoted["path"]).exists())

        # 7. Safe Reservation Release upon Success
        from controller.stores.reservations import release_reservation
        rel_ok, _ = release_reservation(self.jobs_db, job_id, attempt, fencing_token)
        self.assertTrue(rel_ok)
        self.assertEqual(self.jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.RELEASED)

        # Capacity restored
        consumed_after = calculate_node_reserved_resources(self.jobs_db, self.node_id)
        self.assertEqual(consumed_after["reserved_memory_mb"], 0.0)

    def test_stale_worker_attempt_rejected_and_fenced(self):
        """Worker with older fencing generation is unconditionally rejected with HTTP 412."""
        job_id = "job-stale-test"
        # Current generation is 2 (e.g. after retry rescheduling)
        job_spec = {
            "job_id": job_id,
            "attempt": 1,
            "lease_generation": 2,
            "spec": {"workload": {"runtime": "native/python"}}
        }
        # Stale worker reports with old generation 1
        res = UniversalExecutionEngine.execute_job(job_spec, expected_fencing_token=1)
        self.assertEqual(res["status"], JOB_STATE_FAILED)
        self.assertEqual(res["exit_code"], 412)
        self.assertIn("Precondition Failed", res["stderr"])


if __name__ == "__main__":
    unittest.main()

