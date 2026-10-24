#!/usr/bin/env python3
"""
PersonalServer V1.6 Durable Controller Restart Recovery Test Suite
==================================================================
Validates end-to-end recovery of unfinished jobs, active leases, fencing tokens,
and multi-step DAG workflows across controller restarts.
"""

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from common.time_utils import get_current_iso_timestamp
from config.platform_config import override_controller_paths
from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    STATE_ONLINE,
)
from controller.http.job_routes import handle_job_get, handle_job_post
from controller.lease_sweeper import (
    reconcile_startup_recovery,
    sweep_expired_leases,
)
from controller.stores import (
    load_artifacts_db,
    load_jobs_db,
    load_nodes_db,
    load_workflows_db,
    save_artifacts_db,
    save_jobs_db,
    save_nodes_db,
    save_workflows_db,
)
from controller.workflow_engine import (
    evaluate_workflow_progress,
    instantiate_workflow,
    progress_workflows,
)


class TestControllerRestartRecovery(unittest.TestCase):
    """Integration test suite for durable controller restart recovery."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_ctrl_recovery_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "enrollment-token-recovery-test"
        (self.secrets_dir / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir
        )
        self.paths = self.paths_ctx.__enter__()

        # Register nodes
        self.node_token_1 = "tok-node-1"
        self.node_token_2 = "tok-node-2"
        now_iso = get_current_iso_timestamp()
        nodes_db = {
            "nodes": {
                "node-1": {
                    "node_id": "node-1",
                    "name": "worker-alpha",
                    "auth_token": self.node_token_1,
                    "status": STATE_ONLINE,
                    "last_seen": now_iso,
                    "capabilities": ["python3", "storage"],
                    "verified_capabilities": ["python3", "storage"],
                    "advertised_capabilities": ["python3", "storage"],
                    "metrics": {"cpu_percent": 10.0, "memory": "1.0Gi/4.0Gi", "storage": "10G/50G"}
                },
                "node-2": {
                    "node_id": "node-2",
                    "name": "worker-beta",
                    "auth_token": self.node_token_2,
                    "status": STATE_ONLINE,
                    "last_seen": now_iso,
                    "capabilities": ["python3", "udocker"],
                    "verified_capabilities": ["python3", "udocker"],
                    "advertised_capabilities": ["python3", "udocker"],
                    "metrics": {"cpu_percent": 15.0, "memory": "1.2Gi/4.0Gi", "storage": "20G/60G"}
                }
            }
        }
        save_nodes_db(nodes_db, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)
        save_workflows_db({"workflows": {}}, paths=self.paths)
        save_artifacts_db({"artifacts": {}}, paths=self.paths)

        self.handler = self._create_mock_handler()

    def tearDown(self):
        self.paths_ctx.__exit__(None, None, None)

    def _create_mock_handler(self):
        handler = MagicMock()
        handler.current_correlation_id = "corr-recovery-test"
        handler.send_json = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()
        handler.headers = {}
        handler.heartbeat_timeout = 60
        handler.default_backoff_sec = 0.0

        def _parse_auth_token():
            auth = handler.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            return self.enrollment_token
        handler.parse_auth_token.side_effect = _parse_auth_token

        def _is_auth_admin_or_node():
            tok = handler.parse_auth_token()
            return tok in (self.enrollment_token, self.node_token_1, self.node_token_2)
        handler.is_authenticated_admin_or_node.side_effect = _is_auth_admin_or_node

        def _is_auth_node(node_id):
            tok = handler.parse_auth_token()
            if tok == self.enrollment_token:
                return True
            nodes_db = load_nodes_db(paths=self.paths)
            node = nodes_db.get("nodes", {}).get(node_id)
            return node and tok == node.get("auth_token")
        handler.is_authenticated_node.side_effect = _is_auth_node

        return handler

    def test_controller_restart_preserves_active_in_flight_lease(self):
        """
        Scenario 1: Controller restarts while Step 1 is in-flight with a valid active lease.
        The restart reconciliation must PRESERVE the lease, attempt number, and lease generation.
        When Node 1 finishes Step 1 after restart, controller accepts result, unblocks Step 2,
        and Step 2 completes without Step 1 being re-executed.
        """
        # 1. Instantiate 2-step DAG
        jobs_db = load_jobs_db(paths=self.paths)
        wf_spec = {
            "name": "data-pipeline",
            "steps": {
                "step1": {
                    "type": "echo",
                    "target": "node-1",
                    "timeout": 120,
                    "parameters": {"message": "producing data", "outputs": [{"name": "dataset.json", "filename": "dataset.json"}]}
                },
                "step2": {
                    "type": "echo",
                    "target": "node-2",
                    "timeout": 120,
                    "depends_on": ["step1"],
                    "inputs": [{"step": "step1", "artifact": "dataset.json"}],
                    "parameters": {"message": "consuming data"}
                }
            }
        }
        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)
        save_jobs_db(jobs_db, paths=self.paths)
        workflows_db = load_workflows_db(paths=self.paths)
        workflows_db["workflows"][wf_rec["workflow_id"]] = wf_rec
        save_workflows_db(workflows_db, paths=self.paths)

        step1_job_id = wf_rec["step_to_job_id"]["step1"]
        step2_job_id = wf_rec["step_to_job_id"]["step2"]

        # 2. Worker node-1 claims Step 1
        self.handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        handled = handle_job_get(self.handler, f"/nodes/node-1/jobs/next", {})
        self.assertTrue(handled)
        claim_call = self.handler.send_json.call_args[0]
        self.assertEqual(claim_call[0], 200)
        claimed_job = claim_call[1]["job"]
        self.assertEqual(claimed_job["job_id"], step1_job_id)
        self.assertEqual(claimed_job["attempt"], 1)
        self.assertEqual(claimed_job["lease_generation"], 1)
        self.assertIsNotNone(claimed_job["lease_expires_at"])
        initial_lease_exp = claimed_job["lease_expires_at"]

        # Step 1 marked RUNNING
        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"][step1_job_id]["status"] = JOB_STATE_RUNNING
        jobs_db["jobs"][step1_job_id]["state"] = JOB_STATE_RUNNING
        save_jobs_db(jobs_db, paths=self.paths)

        # Verify Step 2 is WAITING_FOR_DEPENDENCY
        self.assertEqual(jobs_db["jobs"][step2_job_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # 3. SIMULATE CONTROLLER RESTART
        # New controller instance starts up, loads persisted stores, runs reconcile_startup_recovery
        new_handler = self._create_mock_handler()
        summary = reconcile_startup_recovery(timeout_seconds=60, paths=self.paths)

        self.assertEqual(summary["active_leases_preserved"], 1)
        self.assertEqual(summary["expired_leases_swept"], 0)
        self.assertEqual(summary["steps_unblocked"], 0)

        # Verify on-disk job state after restart: Step 1 active lease is INTACT
        jobs_after_restart = load_jobs_db(paths=self.paths)
        job1_post = jobs_after_restart["jobs"][step1_job_id]
        self.assertEqual(job1_post["status"], JOB_STATE_RUNNING)
        self.assertEqual(job1_post["attempt"], 1)
        self.assertEqual(job1_post["lease_generation"], 1)
        self.assertEqual(job1_post["lease_expires_at"], initial_lease_exp)
        self.assertEqual(job1_post["assigned_node"], "node-1")

        # Verify Step 2 remains in WAITING_FOR_DEPENDENCY
        job2_post = jobs_after_restart["jobs"][step2_job_id]
        self.assertEqual(job2_post["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # 4. Node 1 finishes Step 1 and posts result to restarted controller
        result_payload = {
            "job_id": step1_job_id,
            "attempt": 1,
            "lease_generation": 1,
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0,
            "stdout": "raw data generated",
            "duration_ms": 150,
            "artifacts": [
                {
                    "artifact_id": "art-dataset-001",
                    "name": "dataset.json",
                    "filename": "dataset.json",
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "size_bytes": 1024
                }
            ]
        }
        new_handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        post_handled = handle_job_post(new_handler, f"/jobs/{step1_job_id}/result", result_payload, self.node_token_1, {})
        self.assertTrue(post_handled)
        post_call = new_handler.send_json.call_args[0]
        self.assertEqual(post_call[0], 200)
        self.assertEqual(post_call[1]["job_status"], JOB_STATE_SUCCEEDED)

        # 5. Verify Step 2 was automatically unblocked with artifact references
        jobs_db = load_jobs_db(paths=self.paths)
        job2_unblocked = jobs_db["jobs"][step2_job_id]
        self.assertEqual(job2_unblocked["status"], JOB_STATE_QUEUED)
        self.assertEqual(len(job2_unblocked["input_artifacts"]), 1)
        self.assertEqual(job2_unblocked["input_artifacts"][0]["artifact_id"], "art-dataset-001")
        self.assertEqual(job2_unblocked["input_artifacts"][0]["sha256"], "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

        # 6. Worker node-2 claims Step 2, executes it, and submits result
        new_handler.headers = {"Authorization": f"Bearer {self.node_token_2}"}
        claim2_handled = handle_job_get(new_handler, f"/nodes/node-2/jobs/next", {})
        self.assertTrue(claim2_handled)
        claimed_job2 = new_handler.send_json.call_args[0][1]["job"]
        self.assertEqual(claimed_job2["job_id"], step2_job_id)

        res2_payload = {
            "job_id": step2_job_id,
            "attempt": 1,
            "lease_generation": claimed_job2["lease_generation"],
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0,
            "stdout": "consumed dataset successfully",
            "duration_ms": 120
        }
        handle_job_post(new_handler, f"/jobs/{step2_job_id}/result", res2_payload, self.node_token_2, {})

        # 7. Verify overall workflow reached SUCCEEDED
        wf_final = load_workflows_db(paths=self.paths)["workflows"][wf_rec["workflow_id"]]
        self.assertEqual(wf_final["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_final["completed_steps"], 2)
        self.assertEqual(wf_final["progress_percent"], 100.0)

        # Verify Step 1 attempt count was NEVER duplicated
        final_job1 = load_jobs_db(paths=self.paths)["jobs"][step1_job_id]
        self.assertEqual(final_job1["attempt"], 1)
        self.assertEqual(len(final_job1["attempts_history"]), 1)

    def test_controller_restart_sweeps_expired_lease_and_fences_stale_worker(self):
        """
        Scenario 2: Controller restarts and discovers an in-flight job whose lease expired during outage.
        Restart recovery must increment attempt count, advance lease_generation fencing token,
        and transition the job to RETRY_WAIT / RECOVERING.
        If the zombie worker reports results from attempt 1, it must be REJECTED with 409 Conflict.
        The new attempt can then be safely claimed, executed, and allow the DAG to succeed.
        """
        # 1. Setup workflow
        jobs_db = load_jobs_db(paths=self.paths)
        wf_spec = {
            "name": "resilient-dag",
            "steps": {
                "step1": {
                    "type": "echo",
                    "target": "auto",
                    "timeout": 30,
                    "max_attempts": 3,
                    "parameters": {"message": "stage 1", "outputs": [{"name": "s1.out", "filename": "s1.out"}]}
                },
                "step2": {
                    "type": "echo",
                    "target": "capability:python3,udocker",
                    "depends_on": ["step1"],
                    "inputs": [{"step": "step1", "artifact": "s1.out"}],
                    "parameters": {"message": "stage 2"}
                }
            }
        }
        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)
        save_jobs_db(jobs_db, paths=self.paths)
        workflows_db = load_workflows_db(paths=self.paths)
        workflows_db["workflows"][wf_rec["workflow_id"]] = wf_rec
        save_workflows_db(workflows_db, paths=self.paths)

        step1_job_id = wf_rec["step_to_job_id"]["step1"]
        step2_job_id = wf_rec["step_to_job_id"]["step2"]

        # Node 1 claims Step 1
        self.handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        handle_job_get(self.handler, "/nodes/node-1/jobs/next", {})
        claimed_job1 = self.handler.send_json.call_args[0][1]["job"]
        self.assertEqual(claimed_job1["job_id"], step1_job_id)
        self.assertEqual(claimed_job1["attempt"], 1)
        self.assertEqual(claimed_job1["lease_generation"], 1)

        # Simulate controller outage during which the lease expires
        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"][step1_job_id]["status"] = JOB_STATE_RUNNING
        jobs_db["jobs"][step1_job_id]["lease_expires_at"] = time.time() - 30  # Expired in past
        save_jobs_db(jobs_db, paths=self.paths)

        # 2. SIMULATE CONTROLLER RESTART
        new_handler = self._create_mock_handler()
        summary = reconcile_startup_recovery(timeout_seconds=60, paths=self.paths)

        self.assertEqual(summary["active_leases_preserved"], 0)
        self.assertEqual(summary["expired_leases_swept"], 1)

        # Check Step 1 state after restart recovery
        jobs_db = load_jobs_db(paths=self.paths)
        recovered_job = jobs_db["jobs"][step1_job_id]
        self.assertEqual(recovered_job["status"], JOB_STATE_RECOVERING)
        self.assertEqual(recovered_job["state"], JOB_STATE_RETRY_WAIT)
        self.assertEqual(recovered_job["attempt"], 2)
        # Fencing token advanced!
        self.assertGreaterEqual(recovered_job["lease_generation"], 2)
        self.assertIsNone(recovered_job["lease_expires_at"])

        # 3. Zombie Node 1 attempts to submit result for attempt 1 (generation 1)
        zombie_payload = {
            "job_id": step1_job_id,
            "attempt": 1,
            "lease_generation": 1,
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0,
            "stdout": "zombie result",
            "duration_ms": 999
        }
        new_handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        handle_job_post(new_handler, f"/jobs/{step1_job_id}/result", zombie_payload, self.node_token_1, {})
        zombie_call = new_handler.send_json.call_args[0]
        # Must be rejected with 409 Conflict
        self.assertEqual(zombie_call[0], 409)
        self.assertTrue(
            zombie_call[1].get("stale_attempt")
            or zombie_call[1].get("superseded")
            or zombie_call[1].get("lease_expired")
        )

        # Verify Step 1 is NOT corrupted by zombie result
        jobs_db = load_jobs_db(paths=self.paths)
        self.assertEqual(jobs_db["jobs"][step1_job_id]["status"], JOB_STATE_RECOVERING)

        # 4. Clear backoff so attempt 2 can be claimed immediately
        jobs_db["jobs"][step1_job_id]["retry_after"] = 0
        save_jobs_db(jobs_db, paths=self.paths)

        # Node 1 claims retry attempt 2
        new_handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        handle_job_get(new_handler, "/nodes/node-1/jobs/next", {})
        claimed_attempt2 = new_handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(claimed_attempt2)
        self.assertEqual(claimed_attempt2["job_id"], step1_job_id)
        self.assertEqual(claimed_attempt2["attempt"], 2)
        gen2 = claimed_attempt2["lease_generation"]

        # Node 1 reports successful result for attempt 2 with artifact
        res2_payload = {
            "job_id": step1_job_id,
            "attempt": 2,
            "lease_generation": gen2,
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0,
            "stdout": "legitimate retry output",
            "duration_ms": 200,
            "artifacts": [
                {
                    "artifact_id": "art-s1-retry",
                    "name": "s1.out",
                    "filename": "s1.out",
                    "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                    "size_bytes": 512
                }
            ]
        }
        handle_job_post(new_handler, f"/jobs/{step1_job_id}/result", res2_payload, self.node_token_1, {})
        self.assertEqual(new_handler.send_json.call_args[0][0], 200)

        # Step 2 unblocks with retry artifact
        jobs_db = load_jobs_db(paths=self.paths)
        self.assertEqual(jobs_db["jobs"][step2_job_id]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][step2_job_id]["input_artifacts"][0]["artifact_id"], "art-s1-retry")

        # Node 2 executes Step 2
        new_handler.headers = {"Authorization": f"Bearer {self.node_token_2}"}
        handle_job_get(new_handler, "/nodes/node-2/jobs/next", {})
        claimed_s2 = new_handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(claimed_s2)
        self.assertEqual(claimed_s2["job_id"], step2_job_id)
        s2_payload = {
            "job_id": step2_job_id,
            "attempt": 1,
            "lease_generation": claimed_s2["lease_generation"],
            "status": JOB_STATE_SUCCEEDED,
            "exit_code": 0
        }
        handle_job_post(new_handler, f"/jobs/{step2_job_id}/result", s2_payload, self.node_token_2, {})

        # Final workflow verification
        wf_final = load_workflows_db(paths=self.paths)["workflows"][wf_rec["workflow_id"]]
        self.assertEqual(wf_final["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_final["completed_steps"], 2)

    def test_controller_restart_unblocks_waiting_dependent_step_with_artifacts(self):
        """
        Scenario 3: Step 1 finished and succeeded before controller restart,
        but Step 2 was waiting and controller process restart interrupted progression.
        On restart, reconcile_startup_recovery must cascade DAG dependencies, unblock Step 2
        to QUEUED with resolved artifact metadata, and immediately schedule it.
        """
        jobs_db = load_jobs_db(paths=self.paths)
        wf_spec = {
            "name": "artifact-handoff-dag",
            "steps": {
                "step1": {
                    "type": "echo",
                    "target": "node-1",
                    "parameters": {"message": "stage 1", "outputs": [{"name": "model.bin", "filename": "model.bin"}]}
                },
                "step2": {
                    "type": "echo",
                    "target": "capability:python3,udocker",
                    "depends_on": ["step1"],
                    "inputs": [{"step": "step1", "artifact": "model.bin"}],
                    "parameters": {"message": "evaluate model"}
                }
            }
        }
        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)
        step1_job_id = wf_rec["step_to_job_id"]["step1"]
        step2_job_id = wf_rec["step_to_job_id"]["step2"]

        # Step 1 was completed and produced artifact before restart
        now_iso = get_current_iso_timestamp()
        jobs_db["jobs"][step1_job_id]["status"] = JOB_STATE_SUCCEEDED
        jobs_db["jobs"][step1_job_id]["state"] = JOB_STATE_SUCCEEDED
        jobs_db["jobs"][step1_job_id]["finished_at"] = now_iso
        jobs_db["jobs"][step1_job_id]["artifacts"] = [
            {
                "artifact_id": "art-model-42",
                "name": "model.bin",
                "filename": "model.bin",
                "sha256": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
                "size_bytes": 2048,
                "producing_node": "node-1"
            }
        ]
        # Also persisted in artifacts.json
        artifacts_db = load_artifacts_db(paths=self.paths)
        artifacts_db["artifacts"]["art-model-42"] = jobs_db["jobs"][step1_job_id]["artifacts"][0]
        save_artifacts_db(artifacts_db, paths=self.paths)

        # Step 2 is in WAITING_FOR_DEPENDENCY
        jobs_db["jobs"][step2_job_id]["status"] = JOB_STATE_WAITING_FOR_DEPENDENCY
        save_jobs_db(jobs_db, paths=self.paths)

        wf_rec["status"] = "RUNNING"
        wf_rec["steps"]["step1"]["status"] = JOB_STATE_SUCCEEDED
        wf_rec["steps"]["step2"]["status"] = JOB_STATE_WAITING_FOR_DEPENDENCY
        workflows_db = load_workflows_db(paths=self.paths)
        workflows_db["workflows"][wf_rec["workflow_id"]] = wf_rec
        save_workflows_db(workflows_db, paths=self.paths)

        # SIMULATE CONTROLLER RESTART
        summary = reconcile_startup_recovery(paths=self.paths)
        self.assertEqual(summary["steps_unblocked"], 1)

        # Verify Step 2 unblocked and scheduled on eligible node (node-2 has python3,udocker)
        jobs_after = load_jobs_db(paths=self.paths)
        job2 = jobs_after["jobs"][step2_job_id]
        self.assertEqual(job2["status"], JOB_STATE_QUEUED)
        self.assertEqual(job2["target_node"], "node-2")
        self.assertEqual(len(job2["input_artifacts"]), 1)
        self.assertEqual(job2["input_artifacts"][0]["artifact_id"], "art-model-42")
        self.assertEqual(job2["input_artifacts"][0]["sha256"], "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a")

        # Step 1 was NEVER re-run
        self.assertEqual(jobs_after["jobs"][step1_job_id]["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(jobs_after["jobs"][step1_job_id]["attempt"], 1)

    def test_controller_restart_terminal_state_fencing(self):
        """
        Verify that a job already in terminal SUCCEEDED state before restart
        strictly ignores subsequent duplicate results from workers.
        """
        jobs_db = load_jobs_db(paths=self.paths)
        now_iso = get_current_iso_timestamp()
        jobs_db["jobs"]["term-job-1"] = {
            "id": "term-job-1",
            "job_id": "term-job-1",
            "target": "node-1",
            "target_node": "node-1",
            "assigned_node": "node-1",
            "status": JOB_STATE_SUCCEEDED,
            "state": JOB_STATE_SUCCEEDED,
            "attempt": 1,
            "lease_generation": 1,
            "finished_at": now_iso,
            "result": {"status": JOB_STATE_SUCCEEDED, "exit_code": 0, "stdout": "original"}
        }
        save_jobs_db(jobs_db, paths=self.paths)

        # Restart
        reconcile_startup_recovery(paths=self.paths)

        # Worker posts duplicate result
        handler = self._create_mock_handler()
        handler.headers = {"Authorization": f"Bearer {self.node_token_1}"}
        handle_job_post(handler, "/jobs/term-job-1/result", {"job_id": "term-job-1", "status": JOB_STATE_SUCCEEDED, "exit_code": 0, "stdout": "duplicate"}, self.node_token_1, {})
        call_res = handler.send_json.call_args[0]
        self.assertEqual(call_res[0], 200)
        self.assertTrue(call_res[1].get("duplicate"))

        # Verify original result untouched
        jobs_after = load_jobs_db(paths=self.paths)
        self.assertEqual(jobs_after["jobs"]["term-job-1"]["result"]["stdout"], "original")

    def test_controller_restart_finalizes_completed_workflow(self):
        """
        Verify that when all steps of a workflow reached terminal success,
        restart reconciliation transitions the workflow to SUCCEEDED and sets finished_at.
        """
        now_iso = get_current_iso_timestamp()
        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"]["j1"] = {"job_id": "j1", "status": JOB_STATE_SUCCEEDED, "finished_at": now_iso}
        jobs_db["jobs"]["j2"] = {"job_id": "j2", "status": JOB_STATE_SUCCEEDED, "finished_at": now_iso}
        save_jobs_db(jobs_db, paths=self.paths)

        wf = {
            "workflow_id": "wf-completed",
            "status": "RUNNING",
            "total_steps": 2,
            "completed_steps": 1,
            "progress_percent": 50.0,
            "steps": {
                "s1": {"job_id": "j1", "status": JOB_STATE_SUCCEEDED},
                "s2": {"job_id": "j2", "status": JOB_STATE_SUCCEEDED}
            }
        }
        workflows_db = load_workflows_db(paths=self.paths)
        workflows_db["workflows"]["wf-completed"] = wf
        save_workflows_db(workflows_db, paths=self.paths)

        # Restart
        summary = reconcile_startup_recovery(paths=self.paths)
        self.assertEqual(summary["workflows_succeeded"], 1)

        wf_after = load_workflows_db(paths=self.paths)["workflows"]["wf-completed"]
        self.assertEqual(wf_after["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_after["completed_steps"], 2)
        self.assertEqual(wf_after["progress_percent"], 100.0)
        self.assertIsNotNone(wf_after["finished_at"])


if __name__ == "__main__":
    unittest.main()
