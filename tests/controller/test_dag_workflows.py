#!/usr/bin/env python3
"""
PersonalServer V1.0 DAG Workflows Test Suite
============================================
Comprehensive test suite validating dependency-aware job scheduling,
DAG validation, cycle detection, artifact handoff, failure cascading,
and concurrent branch execution.
"""

import base64
import hashlib
import io
import json
import secrets
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from common.time_utils import get_current_iso_timestamp
from config.platform_config import override_controller_paths
from controller.constants import (
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
)
from controller.http.job_routes import (
    handle_job_get,
    handle_job_post,
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
    WorkflowValidationError,
    cancel_workflow,
    evaluate_workflow_progress,
    instantiate_workflow,
    progress_workflows,
    validate_workflow_dag,
)


class TestDagWorkflows(unittest.TestCase):
    """Integration and protocol-level tests for DAG job workflows."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_dag_wf_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "enrollment-token-dag-test"
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

        # Mock handler with realistic token authentication side effects
        self.handler = MagicMock()
        self.handler.current_correlation_id = "corr-dag-test"
        self.handler.send_json = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = io.BytesIO()
        self.handler.headers = {}
        self.handler.heartbeat_timeout = 60
        self.handler.parse_auth_token.return_value = self.enrollment_token

        def _is_auth_admin_or_node():
            tok = self.handler.parse_auth_token()
            if tok == self.enrollment_token:
                return True
            nodes_db = load_nodes_db(self.paths)
            for n in nodes_db.get("nodes", {}).values():
                if n.get("auth_token") and n.get("auth_token") == tok:
                    return True
            return False

        def _get_auth_node_id():
            tok = self.handler.parse_auth_token()
            nodes_db = load_nodes_db(self.paths)
            for nid, n in nodes_db.get("nodes", {}).items():
                if n.get("auth_token") and n.get("auth_token") == tok:
                    return nid
            return None

        self.handler.is_authenticated_admin_or_node.side_effect = _is_auth_admin_or_node
        self.handler.get_authenticated_node_id.side_effect = _get_auth_node_id

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _register_test_node(self, node_id: str, auth_token: str, cores: int = 4, ram_mb: int = 4096):
        """Helper to register an active online worker node."""
        nodes_db = load_nodes_db(self.paths)
        nodes_db.setdefault("nodes", {})[node_id] = {
            "node_id": node_id,
            "name": f"Node {node_id}",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": auth_token,
            "last_seen": get_current_iso_timestamp(),
            "resources": {"cpu_cores": cores, "ram_mb": ram_mb, "storage_gb": 50},
            "last_heartbeat": {
                "system": {
                    "cpu_cores": cores,
                    "memory": f"500M/{ram_mb}M",
                    "storage": "5G/50G"
                }
            }
        }
        save_nodes_db(nodes_db, self.paths)

    # -------------------------------------------------------------------------
    # Test A: A -> B: B cannot be claimed before A succeeds
    # -------------------------------------------------------------------------
    def test_a_dependency_blocking_cannot_claim_before_parent_success(self):
        """Job B depending on Job A starts in WAITING_FOR_DEPENDENCY and cannot be claimed until A succeeds."""
        self._register_test_node("worker-1", "token-w1")

        wf_spec = {
            "name": "Pipeline A-B",
            "steps": {
                "stepA": {"type": "echo", "target": "worker-1", "parameters": {"msg": "A"}},
                "stepB": {"type": "echo", "target": "worker-1", "depends_on": ["stepA"], "parameters": {"msg": "B"}}
            }
        }

        # Submit workflow via POST /workflows
        handled = handle_job_post(self.handler, "/workflows", wf_spec, token=self.enrollment_token, query={})
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 201)
        wf_rec = call_body["workflow"]
        wf_id = wf_rec["workflow_id"]
        job_a_id = wf_rec["steps"]["stepA"]["job_id"]
        job_b_id = wf_rec["steps"]["stepB"]["job_id"]

        jobs_db = load_jobs_db(self.paths)
        self.assertEqual(jobs_db["jobs"][job_a_id]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][job_b_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)
        self.assertIsNotNone(jobs_db["jobs"][job_b_id]["waiting_reason"])

        # Worker-1 polls for next job: MUST receive Job A, NOT Job B
        self.handler.parse_auth_token.return_value = "token-w1"
        h_poll = handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        self.assertTrue(h_poll)
        claimed = self.handler.send_json.call_args[0][1].get("job")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], job_a_id)

        # Worker-1 polls again while Job A is active: MUST NOT receive Job B
        h_poll2 = handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        self.assertTrue(h_poll2)
        claimed2 = self.handler.send_json.call_args[0][1].get("job")
        self.assertIsNone(claimed2)

        # Job B must still be WAITING_FOR_DEPENDENCY
        jobs_db = load_jobs_db(self.paths)
        self.assertEqual(jobs_db["jobs"][job_b_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

    # -------------------------------------------------------------------------
    # Test B: A succeeds, artifact is transferred/available, B executes successfully
    # -------------------------------------------------------------------------
    def test_b_artifact_handoff_and_downstream_success(self):
        """When Job A succeeds producing an artifact, Job B unblocks, receives the artifact reference, and succeeds."""
        self._register_test_node("worker-producer", "token-prod")
        self._register_test_node("worker-consumer", "token-cons")

        wf_spec = {
            "name": "Artifact Handoff Pipeline",
            "steps": {
                "producer": {
                    "type": "echo",
                    "target": "worker-producer",
                    "outputs": [{"name": "dataset.csv", "filename": "dataset.csv"}]
                },
                "consumer": {
                    "type": "echo",
                    "target": "worker-consumer",
                    "depends_on": ["producer"],
                    "inputs": [{"step": "producer", "name": "dataset.csv"}]
                }
            }
        }

        # 1. Instantiate workflow
        self.handler.parse_auth_token.return_value = self.enrollment_token
        handle_job_post(self.handler, "/workflows", wf_spec, token=self.enrollment_token, query={})
        wf_rec = self.handler.send_json.call_args[0][1]["workflow"]
        wf_id = wf_rec["workflow_id"]
        prod_jid = wf_rec["steps"]["producer"]["job_id"]
        cons_jid = wf_rec["steps"]["consumer"]["job_id"]

        # 2. Producer claims job
        self.handler.parse_auth_token.return_value = "token-prod"
        handle_job_get(self.handler, "/nodes/worker-producer/jobs/next", {})
        claimed_prod = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(claimed_prod)
        self.assertEqual(claimed_prod["job_id"], prod_jid)

        # 3. Producer uploads artifact with SHA-256
        artifact_content = b"col1,col2,col3\n1,2,3\n4,5,6\n"
        art_sha = hashlib.sha256(artifact_content).hexdigest()
        art_b64 = base64.b64encode(artifact_content).decode("ascii")

        self.handler.parse_auth_token.return_value = "token-prod"
        upload_handled = handle_job_post(
            self.handler,
            "/artifacts/upload",
            {
                "job_id": prod_jid,
                "workflow_id": wf_id,
                "filename": "dataset.csv",
                "name": "dataset.csv",
                "content_base64": art_b64,
                "sha256": art_sha
            },
            token="token-prod",
            query={}
        )
        self.assertTrue(upload_handled)
        uploaded_art = self.handler.send_json.call_args[0][1]["artifact"]
        art_id = uploaded_art["artifact_id"]

        # 4. Producer completes job
        res_handled = handle_job_post(
            self.handler,
            f"/jobs/{prod_jid}/result",
            {
                "status": JOB_STATE_SUCCEEDED,
                "exit_code": 0,
                "artifacts": [uploaded_art]
            },
            token="token-prod",
            query={}
        )
        self.assertTrue(res_handled)

        # 5. Verify consumer job has unblocked to QUEUED with input_artifacts resolved
        jobs_db = load_jobs_db(self.paths)
        cons_job = jobs_db["jobs"][cons_jid]
        self.assertEqual(cons_job["status"], JOB_STATE_QUEUED)
        self.assertIsNone(cons_job["waiting_reason"])
        self.assertEqual(len(cons_job["input_artifacts"]), 1)
        resolved_in = cons_job["input_artifacts"][0]
        self.assertEqual(resolved_in["artifact_id"], art_id)
        self.assertEqual(resolved_in["sha256"], art_sha)
        self.assertEqual(resolved_in["filename"], "dataset.csv")

        # 6. Consumer worker polls and claims Job B
        self.handler.parse_auth_token.return_value = "token-cons"
        handle_job_get(self.handler, "/nodes/worker-consumer/jobs/next", {})
        claimed_cons = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(claimed_cons)
        self.assertEqual(claimed_cons["job_id"], cons_jid)

        # 7. Consumer downloads artifact and verifies integrity
        self.handler.parse_auth_token.return_value = "token-cons"
        handle_job_get(self.handler, f"/artifacts/{art_id}/metadata", {})
        meta_res = self.handler.send_json.call_args[0][1]
        self.assertTrue(meta_res.get("integrity_verified"))

        # 8. Consumer completes job
        handle_job_post(
            self.handler,
            f"/jobs/{cons_jid}/result",
            {"status": JOB_STATE_SUCCEEDED, "exit_code": 0},
            token="token-cons",
            query={}
        )

        # 9. Check workflow overall status is SUCCEEDED via GET /workflows/<id>
        self.handler.parse_auth_token.return_value = self.enrollment_token
        handle_job_get(self.handler, f"/workflows/{wf_id}", {})
        wf_resp = self.handler.send_json.call_args[0][1]
        wf_final = wf_resp["workflow"]
        self.assertEqual(wf_final["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_final["progress_percent"], 100.0)
        self.assertEqual(len(wf_resp["jobs_list"]), 2)

    # -------------------------------------------------------------------------
    # Test C: A fails: B and downstream descendants are blocked
    # -------------------------------------------------------------------------
    def test_c_dependency_failure_blocks_downstream_descendants(self):
        """When Job A fails, dependent Job B and transitive descendant Job C are marked FAILED with explicit reason."""
        self._register_test_node("worker-1", "token-w1")

        wf_spec = {
            "name": "Failing Chain Pipeline",
            "steps": {
                "stepA": {"type": "echo", "target": "worker-1", "max_attempts": 1, "retryable": False},
                "stepB": {"type": "echo", "target": "worker-1", "depends_on": ["stepA"]},
                "stepC": {"type": "echo", "target": "worker-1", "depends_on": ["stepB"]}
            }
        }
        self.handler.parse_auth_token.return_value = self.enrollment_token
        handle_job_post(self.handler, "/workflows", wf_spec, token=self.enrollment_token, query={})
        wf_rec = self.handler.send_json.call_args[0][1]["workflow"]
        wf_id = wf_rec["workflow_id"]
        a_id = wf_rec["steps"]["stepA"]["job_id"]
        b_id = wf_rec["steps"]["stepB"]["job_id"]
        c_id = wf_rec["steps"]["stepC"]["job_id"]

        # Step A fails terminally
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        handle_job_post(
            self.handler,
            f"/jobs/{a_id}/result",
            {"status": JOB_STATE_FAILED, "exit_code": 1, "attempt": 1, "stderr": "Simulation intentional failure"},
            token="token-w1",
            query={}
        )

        jobs_db = load_jobs_db(self.paths)
        workflows_db = load_workflows_db(self.paths)

        # Job B and Job C must both be FAILED
        self.assertEqual(jobs_db["jobs"][b_id]["status"], JOB_STATE_FAILED)
        self.assertIn("Upstream dependency failed", jobs_db["jobs"][b_id]["retry_reason"])
        self.assertIsNotNone(jobs_db["jobs"][b_id]["blocked_reason"])

        self.assertEqual(jobs_db["jobs"][c_id]["status"], JOB_STATE_FAILED)
        self.assertIsNotNone(jobs_db["jobs"][c_id]["blocked_reason"])

        # Entire workflow is FAILED
        self.assertEqual(workflows_db["workflows"][wf_id]["status"], JOB_STATE_FAILED)

        # Worker polls again: nothing must be claimable
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        self.assertIsNone(self.handler.send_json.call_args[0][1]["job"])

    # -------------------------------------------------------------------------
    # Test D: Invalid dependency IDs and cyclic graphs are rejected
    # -------------------------------------------------------------------------
    def test_d_invalid_dependencies_and_cycles_rejected_with_400(self):
        """Cyclic graphs, self-loops, undefined dependencies, duplicate IDs, and empty graphs return 400."""
        self.handler.parse_auth_token.return_value = self.enrollment_token

        # 1. Direct cycle: A -> B -> A
        cycle_spec = {
            "name": "Cycle Workflow",
            "steps": {
                "stepA": {"type": "echo", "depends_on": ["stepB"]},
                "stepB": {"type": "echo", "depends_on": ["stepA"]}
            }
        }
        handle_job_post(self.handler, "/workflows", cycle_spec, token=self.enrollment_token, query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)
        self.assertIn("cycle detected", self.handler.send_json.call_args[0][1]["error"].lower())

        # 2. Self-dependency: A -> A
        self_loop = {
            "name": "Self Loop",
            "steps": {"loopStep": {"type": "echo", "depends_on": ["loopStep"]}}
        }
        handle_job_post(self.handler, "/workflows", self_loop, token=self.enrollment_token, query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)
        self.assertIn("self-dependency", self.handler.send_json.call_args[0][1]["error"].lower())

        # 3. Undefined dependency: A -> Z
        missing_dep = {
            "name": "Missing Dep",
            "steps": {"stepA": {"type": "echo", "depends_on": ["nonExistentStep"]}}
        }
        handle_job_post(self.handler, "/workflows", missing_dep, token=self.enrollment_token, query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)
        self.assertIn("undefined", self.handler.send_json.call_args[0][1]["error"].lower())

        # 4. Duplicate step IDs in list format
        duplicate_ids = {
            "name": "Duplicate IDs",
            "steps": [
                {"id": "step1", "type": "echo"},
                {"id": "step1", "type": "echo"}
            ]
        }
        handle_job_post(self.handler, "/workflows", duplicate_ids, token=self.enrollment_token, query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)
        self.assertIn("duplicate", self.handler.send_json.call_args[0][1]["error"].lower())

        # 5. Empty workflow
        empty_wf = {"name": "Empty", "steps": {}}
        handle_job_post(self.handler, "/workflows", empty_wf, token=self.enrollment_token, query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)

        # Verify no rogue jobs were created in jobs_db
        jobs_db = load_jobs_db(self.paths)
        self.assertEqual(len(jobs_db.get("jobs", {})), 0)

    # -------------------------------------------------------------------------
    # Test E: Multiple independent branches progress concurrently
    # -------------------------------------------------------------------------
    def test_e_concurrent_independent_branches(self):
        """Two independent branches (A1->B1 and A2->B2) can progress concurrently across workers."""
        self._register_test_node("worker-1", "token-w1")
        self._register_test_node("worker-2", "token-w2")

        wf_spec = {
            "name": "Parallel Tracks",
            "failure_policy": "continue_on_failure",
            "steps": {
                "track1_a": {"type": "echo", "target": "worker-1"},
                "track1_b": {"type": "echo", "target": "worker-1", "depends_on": ["track1_a"]},
                "track2_a": {"type": "echo", "target": "worker-2"},
                "track2_b": {"type": "echo", "target": "worker-2", "depends_on": ["track2_a"]}
            }
        }
        self.handler.parse_auth_token.return_value = self.enrollment_token
        handle_job_post(self.handler, "/workflows", wf_spec, token=self.enrollment_token, query={})
        wf_rec = self.handler.send_json.call_args[0][1]["workflow"]
        wf_id = wf_rec["workflow_id"]
        t1a = wf_rec["steps"]["track1_a"]["job_id"]
        t1b = wf_rec["steps"]["track1_b"]["job_id"]
        t2a = wf_rec["steps"]["track2_a"]["job_id"]
        t2b = wf_rec["steps"]["track2_b"]["job_id"]

        # Worker 1 claims Track 1 root
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        job1 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(job1)
        self.assertEqual(job1["job_id"], t1a)

        # Worker 2 claims Track 2 root concurrently
        self.handler.parse_auth_token.return_value = "token-w2"
        handle_job_get(self.handler, "/nodes/worker-2/jobs/next", {})
        job2 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(job2)
        self.assertEqual(job2["job_id"], t2a)

        # Complete Track 1 root
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_post(self.handler, f"/jobs/{t1a}/result", {"status": JOB_STATE_SUCCEEDED, "exit_code": 0}, token="token-w1", query={})

        # Track 1 downstream step is unblocked to QUEUED, but Track 2 downstream step is still WAITING
        jobs_db = load_jobs_db(self.paths)
        self.assertEqual(jobs_db["jobs"][t1b]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][t2b]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # Worker 1 can immediately claim Track 1 downstream step while Track 2 root is still executing
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        next_job_w1 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(next_job_w1)
        self.assertEqual(next_job_w1["job_id"], t1b)

    # -------------------------------------------------------------------------
    # Test F: Repeated polling does not duplicate claims
    # -------------------------------------------------------------------------
    def test_f_repeated_polling_idempotency_prevents_duplicate_claims(self):
        """Concurrent or repeated polling does not dispatch the same job twice; lease_generation fences execution."""
        self._register_test_node("worker-1", "token-w1")
        self._register_test_node("worker-2", "token-w2")

        wf_spec = {
            "name": "Single Step Workflow",
            "steps": {"solo": {"type": "echo", "target": "worker-1"}}
        }
        self.handler.parse_auth_token.return_value = self.enrollment_token
        handle_job_post(self.handler, "/workflows", wf_spec, token=self.enrollment_token, query={})
        wf_rec = self.handler.send_json.call_args[0][1]["workflow"]
        solo_jid = wf_rec["steps"]["solo"]["job_id"]

        # 1. Worker 1 polls and claims
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        claim1 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNotNone(claim1)
        self.assertEqual(claim1["job_id"], solo_jid)
        self.assertEqual(claim1["lease_generation"], 1)

        # 2. Worker 2 polls: must get None
        self.handler.parse_auth_token.return_value = "token-w2"
        handle_job_get(self.handler, "/nodes/worker-2/jobs/next", {})
        claim2 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNone(claim2)

        # 3. Worker 1 polls again: must get None (already claimed)
        self.handler.parse_auth_token.return_value = "token-w1"
        handle_job_get(self.handler, "/nodes/worker-1/jobs/next", {})
        claim3 = self.handler.send_json.call_args[0][1]["job"]
        self.assertIsNone(claim3)

        # 4. Result submission completes job
        handle_job_post(self.handler, f"/jobs/{solo_jid}/result", {"status": JOB_STATE_SUCCEEDED, "exit_code": 0, "attempt": 1}, token="token-w1", query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 200)

        # Duplicate completion submission is idempotent (returns 200 with duplicate=True)
        handle_job_post(self.handler, f"/jobs/{solo_jid}/result", {"status": JOB_STATE_SUCCEEDED, "exit_code": 0, "attempt": 1}, token="token-w1", query={})
        self.assertEqual(self.handler.send_json.call_args[0][0], 200)
        self.assertTrue(self.handler.send_json.call_args[0][1].get("duplicate"))


if __name__ == "__main__":
    unittest.main()
