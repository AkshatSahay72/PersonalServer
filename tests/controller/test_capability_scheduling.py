#!/usr/bin/env python3
"""
Tests for Capability-Based Scheduling and Verified vs. Advertised Capabilities (V1.3)
=====================================================================================
Covers:
1. Hardware-independent worker selection via declared capabilities (capability:<cap>, cap:<cap>, role:<role>).
2. Distinguishing advertised capabilities from capabilities verified by the worker.
3. Strict verification enforcement with require_verified and verified_capabilities.
4. Unsupported capabilities and queuing with waiting reasons.
5. Backward compatibility for legacy jobs (target='auto', explicit node IDs) and legacy node heartbeats.
6. Multi-step DAG workflow execution with capability-targeted steps.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from config.platform_config import override_controller_paths
from common.time_utils import get_current_iso_timestamp
from controller.constants import (
    JOB_STATE_ASSIGNED,
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
from controller.http.node_routes import (
    handle_node_post,
)
from controller.lease_sweeper import sweep_expired_leases
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db, save_nodes_db
from controller.workflow_engine import (
    evaluate_workflow_progress,
    instantiate_workflow,
)
from scheduler.scheduler import (
    ResourceScheduler,
    extract_node_capabilities,
    is_node_eligible,
)


class TestCapabilityBasedScheduling(unittest.TestCase):
    """Test suite for capability-based scheduling interfaces and logic."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_cap_sched_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "test-enrollment-secret-cap"
        (self.secrets_dir / "enrollment.token").write_text(self.enrollment_token + "\n", encoding="utf-8")

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir,
        )
        self.paths = self.paths_ctx.__enter__()

        # Mock HTTP handler for routing tests
        self.handler = MagicMock()
        self.handler.current_correlation_id = "corr-test-cap"
        self.handler.send_json = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = io.BytesIO()
        self.handler.headers = {}
        self.handler.heartbeat_timeout = 60
        self.handler.is_authenticated_admin_or_node.return_value = True

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Capability Extraction & Distinction (Advertised vs Verified)
    # -------------------------------------------------------------------------

    def test_01_extract_node_capabilities_distinction(self):
        """extract_node_capabilities distinguishes advertised from verified capabilities."""
        node = {
            "node_id": "test-node-1",
            "capabilities": {
                "container_runtime:docker": True,
                "gpu:cuda": True,
            },
            "verified_capabilities": {
                "container_runtime:docker": True,
            },
            "advertised_capabilities": {
                "container_runtime:docker": True,
                "gpu:cuda": True,
                "experimental:feature": True,
            },
        }
        adv, ver = extract_node_capabilities(node)
        self.assertIn("gpu:cuda", adv)
        self.assertIn("experimental:feature", adv)
        self.assertIn("container_runtime:docker", adv)

        # Docker was verified, but gpu:cuda was only advertised!
        self.assertIn("container_runtime:docker", ver)
        self.assertNotIn("gpu:cuda", ver)
        self.assertNotIn("experimental:feature", ver)

    def test_02_legacy_node_capabilities_treated_as_advertised(self):
        """Nodes reporting legacy flat capabilities are treated as advertised, not automatically verified."""
        legacy_node = {
            "node_id": "legacy-phone-vivo",
            "capabilities": ["compute", "python3", "storage"],
        }
        adv, ver = extract_node_capabilities(legacy_node)
        self.assertIn("compute", adv)
        self.assertIn("python3", adv)
        self.assertIn("storage", adv)
        # Should NOT claim verification merely because it was reported
        self.assertEqual(len(ver), 0)

    # -------------------------------------------------------------------------
    # 2. Scheduler Eligibility Matching & Verification Enforcement
    # -------------------------------------------------------------------------

    def test_03_require_verified_blocks_unverified_capability(self):
        """Job with require_verified: True rejects nodes having advertised but unverified capability."""
        now_iso = get_current_iso_timestamp()

        unverified_worker = {
            "node_id": "worker-unverified",
            "status": "ONLINE",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"docker": True},
            "advertised_capabilities": {"docker": True},
            "verified_capabilities": {},  # Worker did not verify docker!
        }
        verified_worker = {
            "node_id": "worker-verified",
            "status": "ONLINE",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"docker": True},
            "advertised_capabilities": {"docker": True},
            "verified_capabilities": {"docker": True},  # Worker verified docker
        }

        # 1. Standard job (backward compatible) accepts unverified worker
        std_job = {
            "requirements": {
                "capabilities": ["docker"],
            }
        }
        ok_std, reason_std = ResourceScheduler.is_node_eligible(unverified_worker, std_job, timeout_seconds=60)
        self.assertTrue(ok_std)
        self.assertEqual(reason_std, "Eligible")

        # 2. Strict job with require_verified rejects unverified worker
        strict_job = {
            "requirements": {
                "capabilities": ["docker"],
                "require_verified": True,
            }
        }
        ok_strict, reason_strict = ResourceScheduler.is_node_eligible(unverified_worker, strict_job, timeout_seconds=60)
        self.assertFalse(ok_strict)
        self.assertIn("Capabilities advertised but unverified by worker", reason_strict)
        self.assertIn("docker", reason_strict)

        # 3. Strict job accepts verified worker
        ok_ver, reason_ver = ResourceScheduler.is_node_eligible(verified_worker, strict_job, timeout_seconds=60)
        self.assertTrue(ok_ver)
        self.assertEqual(reason_ver, "Eligible")

    def test_04_explicit_verified_capabilities_requirement(self):
        """Job with verified_capabilities list enforces verification specifically on requested caps."""
        now_iso = get_current_iso_timestamp()

        node = {
            "node_id": "worker-mixed",
            "status": "ONLINE",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"python3": True, "gpu": True},
            "advertised_capabilities": {"python3": True, "gpu": True},
            "verified_capabilities": {"python3": True},  # Only python3 verified
        }

        # Demanding python3 verified passes
        job1 = {"requirements": {"verified_capabilities": ["python3"]}}
        ok1, _ = ResourceScheduler.is_node_eligible(node, job1, timeout_seconds=60)
        self.assertTrue(ok1)

        # Demanding gpu verified fails
        job2 = {"requirements": {"verified_capabilities": ["gpu"]}}
        ok2, reason2 = ResourceScheduler.is_node_eligible(node, job2, timeout_seconds=60)
        self.assertFalse(ok2)
        self.assertIn("advertised but unverified by worker", reason2)

    # -------------------------------------------------------------------------
    # 3. HTTP Job Submission with Hardware-Independent Capability Targeting
    # -------------------------------------------------------------------------

    def test_05_job_submission_with_target_capability(self):
        """POST /jobs with target='capability:<cap>' selects worker by capability without hardcoding node_id."""
        nodes_db = load_nodes_db(self.paths)
        now_iso = get_current_iso_timestamp()

        # Node A: has container_runtime:udocker
        nodes_db.setdefault("nodes", {})["node-udocker"] = {
            "node_id": "node-udocker",
            "name": "UDocker Runner",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-udocker",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 20},
            "capabilities": {"container_runtime:udocker": True},
            "verified_capabilities": {"container_runtime:udocker": True},
        }

        # Node B: only has basic python
        nodes_db["nodes"]["node-basic"] = {
            "node_id": "node-basic",
            "name": "Basic Runner",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-basic",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 16384, "storage_gb": 100},  # Bigger resources!
            "capabilities": {"python3": True},
            "verified_capabilities": {"python3": True},
        }
        save_nodes_db(nodes_db, self.paths)

        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True

        handled = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "shell-script",
                "parameters": {"command": "echo test"},
                "target": "capability:container_runtime:udocker",
            },
            token=self.enrollment_token,
            query={},
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 200)

        job = call_body["job"]
        self.assertEqual(job["target"], "capability:container_runtime:udocker")
        # Should be scheduled directly to node-udocker despite node-basic having more CPU/RAM!
        self.assertEqual(job["target_node"], "node-udocker")
        self.assertEqual(job["status"], JOB_STATE_QUEUED)
        self.assertEqual(job["scheduler"]["mode"], "capability-aware")
        self.assertIn("container_runtime:udocker", job["requirements"]["capabilities"])

    def test_06_unsupported_capability_remains_queued_with_waiting_reason(self):
        """Job targeting an unsupported capability remains QUEUED with a descriptive waiting_reason."""
        nodes_db = load_nodes_db(self.paths)
        now_iso = get_current_iso_timestamp()

        nodes_db.setdefault("nodes", {})["node-standard"] = {
            "node_id": "node-standard",
            "name": "Standard Node",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-std",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 20},
            "capabilities": {"compute": True},
        }
        save_nodes_db(nodes_db, self.paths)

        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True

        handled = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "shell-script",
                "parameters": {"command": "echo test"},
                "target": "capability:quantum_accelerator",
            },
            token=self.enrollment_token,
            query={},
        )
        self.assertTrue(handled)
        job = self.handler.send_json.call_args[0][1]["job"]
        self.assertEqual(job["status"], JOB_STATE_QUEUED)
        self.assertIsNone(job["target_node"])
        self.assertIsNotNone(job["waiting_reason"])
        self.assertIn("quantum_accelerator", job["waiting_reason"])

    def test_07_worker_polling_enforces_capability_match(self):
        """Worker cannot claim a capability-targeted job if it lacks the required capability."""
        nodes_db = load_nodes_db(self.paths)
        jobs_db = load_jobs_db(self.paths)
        now_iso = get_current_iso_timestamp()

        # Register two online workers
        nodes_db.setdefault("nodes", {})["worker-ai"] = {
            "node_id": "worker-ai",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-ai",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"npu": True},
            "verified_capabilities": {"npu": True},
        }
        nodes_db["nodes"]["worker-plain"] = {
            "node_id": "worker-plain",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-plain",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"cpu": True},
            "verified_capabilities": {"cpu": True},
        }
        save_nodes_db(nodes_db, self.paths)

        # Enqueue capability-targeted job
        job_id = "job-cap-ai-1"
        jobs_db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "target": "capability:npu",
            "target_node": None,
            "assigned_node": None,
            "status": JOB_STATE_QUEUED,
            "requirements": {"capabilities": ["npu"]},
            "timeout": 30,
        }
        save_jobs_db(jobs_db, self.paths)

        # 1. Plain worker polls -> Cannot claim
        self.handler.parse_auth_token.return_value = "tok-plain"
        self.handler.is_authenticated_admin_or_node.return_value = False
        h1 = handle_job_get(self.handler, "/nodes/worker-plain/jobs/next", {})
        self.assertTrue(h1)
        res1 = self.handler.send_json.call_args[0][1]
        self.assertIsNone(res1.get("job"))

        # 2. AI worker polls -> Claims job
        self.handler.parse_auth_token.return_value = "tok-ai"
        h2 = handle_job_get(self.handler, "/nodes/worker-ai/jobs/next", {})
        self.assertTrue(h2)
        res2 = self.handler.send_json.call_args[0][1]
        self.assertIsNotNone(res2.get("job"))
        self.assertEqual(res2["job"]["job_id"], job_id)
        self.assertEqual(res2["job"]["target_node"], "worker-ai")
        self.assertEqual(res2["job"]["status"], JOB_STATE_CLAIMED)

    # -------------------------------------------------------------------------
    # 4. DAG Workflows with Capability-Based Step Targeting
    # -------------------------------------------------------------------------

    def test_08_dag_workflow_capability_targeting(self):
        """DAG workflows allow steps to target capabilities (target='capability:...') instead of device IDs."""
        nodes_db = load_nodes_db(self.paths)
        now_iso = get_current_iso_timestamp()

        # Step 1 needs data-generator capability
        nodes_db.setdefault("nodes", {})["phone-vivo"] = {
            "node_id": "phone-vivo",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-vivo",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50},
            "capabilities": {"sensor:camera": True, "compute": True},
            "verified_capabilities": {"sensor:camera": True},
        }
        # Step 2 needs ml-processor capability
        nodes_db["nodes"]["phone-realme"] = {
            "node_id": "phone-realme",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-realme",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 100},
            "capabilities": {"ml:tflite": True, "compute": True},
            "verified_capabilities": {"ml:tflite": True},
        }
        save_nodes_db(nodes_db, self.paths)

        dag_spec = {
            "name": "wf-capability-demo",
            "steps": [
                {
                    "step_id": "step-capture",
                    "type": "shell-script",
                    "target": "capability:sensor:camera",
                    "parameters": {"command": "echo capturing frame"},
                    "dependencies": [],
                },
                {
                    "step_id": "step-infer",
                    "type": "shell-script",
                    "target": "capability:ml:tflite",
                    "parameters": {"command": "echo inferring model"},
                    "dependencies": ["step-capture"],
                },
            ],
        }

        # 1. Instantiate workflow
        jobs_db = load_jobs_db(self.paths)
        inst_wf, created_jobs = instantiate_workflow(dag_spec, jobs_db)
        save_jobs_db(jobs_db, self.paths)

        self.assertIsNotNone(inst_wf.get("workflow_id"))
        wf_id = inst_wf["workflow_id"]

        # Step 1 should be QUEUED and have target_node assigned or pending
        step1_info = inst_wf["steps"]["step-capture"]
        step1_job = jobs_db["jobs"][step1_info["job_id"]]
        self.assertEqual(step1_job["target"], "capability:sensor:camera")
        self.assertIn("sensor:camera", step1_job["requirements"]["capabilities"])
        self.assertEqual(step1_job["status"], JOB_STATE_QUEUED)

        # Step 2 should be WAITING_FOR_DEPENDENCY
        step2_info = inst_wf["steps"]["step-infer"]
        step2_job = jobs_db["jobs"][step2_info["job_id"]]
        self.assertEqual(step2_job["target"], "capability:ml:tflite")
        self.assertEqual(step2_job["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # 2. Simulate Step 1 completion
        step1_job["status"] = JOB_STATE_SUCCEEDED
        save_jobs_db(jobs_db, self.paths)

        # 3. Evaluate progress -> Step 2 unblocks
        progressed = evaluate_workflow_progress(inst_wf, jobs_db)
        save_jobs_db(jobs_db, self.paths)
        self.assertTrue(progressed)

        step2_unblocked = jobs_db["jobs"][step2_info["job_id"]]
        self.assertEqual(step2_unblocked["status"], JOB_STATE_QUEUED)

        # ResourceScheduler matches Step 2 requirements to phone-realme
        decision = ResourceScheduler.select_node(step2_unblocked.get("requirements", {}), nodes_db.get("nodes", {}))
        self.assertEqual(decision["selected_node"], "phone-realme")

    # -------------------------------------------------------------------------
    # 5. Backward Compatibility (Auto, Pinned IDs, Legacy Heartbeats)
    # -------------------------------------------------------------------------

    def test_09_backward_compatibility_existing_jobs_and_phones(self):
        """Preserves legacy behavior: explicit device pinning and target='auto' function without change."""
        nodes_db = load_nodes_db(self.paths)
        now_iso = get_current_iso_timestamp()

        nodes_db.setdefault("nodes", {})["server-5387a86bf36116b1"] = {
            "node_id": "server-5387a86bf36116b1",
            "name": "Vivo Y31",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-vivo-legacy",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 100},
            "capabilities": ["compute", "python3"],
        }
        save_nodes_db(nodes_db, self.paths)

        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True

        # 1. Explicit pinning by device ID still pins directly
        handled_pin = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "echo",
                "parameters": {"message": "pinned"},
                "target": "server-5387a86bf36116b1",
            },
            token=self.enrollment_token,
            query={},
        )
        self.assertTrue(handled_pin)
        job_pinned = self.handler.send_json.call_args[0][1]["job"]
        self.assertEqual(job_pinned["target"], "server-5387a86bf36116b1")
        self.assertEqual(job_pinned["target_node"], "server-5387a86bf36116b1")
        self.assertEqual(job_pinned["scheduler"]["mode"], "explicit")

        # 2. Target='auto' still works and selects highest-scoring node
        handled_auto = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "echo",
                "parameters": {"message": "auto"},
                "target": "auto",
            },
            token=self.enrollment_token,
            query={},
        )
        self.assertTrue(handled_auto)
        job_auto = self.handler.send_json.call_args[0][1]["job"]
        self.assertEqual(job_auto["target_node"], "server-5387a86bf36116b1")
        self.assertEqual(job_auto["scheduler"]["mode"], "resource-aware")

    # -------------------------------------------------------------------------
    # 6. Dynamic Lease Sweeper Scheduling for Capability Targets
    # -------------------------------------------------------------------------

    def test_10_lease_sweeper_schedules_pending_capability_job(self):
        """Lease sweeper evaluates capability-targeted QUEUED jobs when an eligible node appears."""
        nodes_db = load_nodes_db(self.paths)
        jobs_db = load_jobs_db(self.paths)

        # Initially no nodes online
        job_id = "job-pending-cap-1"
        jobs_db.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "target": "capability:gpu",
            "target_node": None,
            "assigned_node": None,
            "status": JOB_STATE_QUEUED,
            "requirements": {"capabilities": ["gpu"]},
            "scheduler": {"mode": "capability-pending"},
            "timeout": 30,
        }
        save_jobs_db(jobs_db, self.paths)

        # Run sweeper: no eligible node, remains queued
        res1 = sweep_expired_leases(jobs_db, nodes_db)
        self.assertEqual(jobs_db["jobs"][job_id]["status"], JOB_STATE_QUEUED)
        self.assertIsNone(jobs_db["jobs"][job_id].get("target_node"))

        # Now an eligible node registers
        now_iso = get_current_iso_timestamp()
        nodes_db.setdefault("nodes", {})["gpu-node"] = {
            "node_id": "gpu-node",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "tok-gpu",
            "last_seen": now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 16384, "storage_gb": 100},
            "capabilities": {"gpu": True},
            "verified_capabilities": {"gpu": True},
        }
        save_nodes_db(nodes_db, self.paths)

        # Run sweeper again: job should be assigned to gpu-node!
        res2 = sweep_expired_leases(jobs_db, nodes_db)
        updated_job = jobs_db["jobs"][job_id]
        self.assertEqual(updated_job["status"], JOB_STATE_QUEUED)
        self.assertEqual(updated_job["target_node"], "gpu-node")
        self.assertEqual(updated_job["scheduler"]["mode"], "capability-aware")


if __name__ == "__main__":
    unittest.main()
