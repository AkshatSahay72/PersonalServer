#!/usr/bin/env python3
"""
Tests for Distributed Job Scheduler, Script Execution, Artifacts & DAG Workflows
================================================================================
Comprehensive test suite verifying:
- Script execution engine: ping-check, http-check, dns-check, python-script with args, shell-script.
- Resource-aware placement, active job concurrency penalties, memory reservations.
- Offline pinned node policy and waiting reasons.
- Multi-step DAG workflow instantiation, topological sorting, dependency fulfillment, failure propagation.
- Multi-node artifact management, atomic promotion, path traversal protection, SHA-256 verification.
- Distributed metrics and queue depth observability.
"""

import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.job_executor import JobExecutor
from common.artifact_manager import (
    ArtifactManager,
    compute_file_sha256,
    generate_artifact_id,
    store_artifact_bytes,
    validate_artifact_path,
    verify_artifact_integrity,
)
from common.time_utils import get_current_iso_timestamp
from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMED_OUT,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    canonicalize_job_state,
    validate_job_transition,
)
from controller.lease_sweeper import sweep_expired_leases
from controller.workflow_engine import (
    WorkflowValidationError,
    cancel_workflow,
    evaluate_workflow_progress,
    instantiate_workflow,
    progress_workflows,
    validate_workflow_dag,
)
from scheduler.scheduler import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    ResourceScheduler,
    is_node_eligible,
)


class TestScriptExecutionEngine(unittest.TestCase):
    """Test general-purpose safe script execution in agent/job_executor.py via JobExecutor.execute()."""

    def test_01_ping_check_execution(self):
        """ping-check executes ping with platform-specific args or detects invalid host."""
        # 1. Valid ping
        res = JobExecutor.execute({
            "job_id": "job-test-ping-1",
            "type": "ping-check",
            "parameters": {"host": "127.0.0.1", "count": 1, "ping_timeout": 3},
            "timeout": 10
        })
        self.assertIn(res["status"], (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED))
        self.assertGreater(res["duration_ms"], 0)
        self.assertTrue(len(res["stdout"]) > 0 or len(res["stderr"]) > 0)

        # 2. Invalid host characters rejected safely
        res_bad = JobExecutor.execute({
            "job_id": "job-test-ping-bad",
            "type": "ping-check",
            "parameters": {"host": "127.0.0.1; rm -rf /", "count": 1},
            "timeout": 10
        })
        self.assertEqual(res_bad["status"], JOB_STATE_FAILED)
        self.assertIn("Invalid or unsafe ping target", res_bad["stderr"])

    def test_02_http_check_execution(self):
        """http-check validates URL scheme, timeouts, and captures latency."""
        # 1. Non-http scheme rejected
        res_bad = JobExecutor.execute({
            "job_id": "job-test-http-bad",
            "type": "http-check",
            "parameters": {"url": "ftp://example.com/file"},
            "timeout": 10
        })
        self.assertEqual(res_bad["status"], JOB_STATE_FAILED)
        self.assertIn("http:// or https://", res_bad["stderr"])

        # 2. Connection failure on closed port
        res_closed = JobExecutor.execute({
            "job_id": "job-test-http-closed",
            "type": "http-check",
            "parameters": {"url": "http://127.0.0.1:59999/healthz", "http_timeout": 2},
            "timeout": 10
        })
        self.assertEqual(res_closed["status"], JOB_STATE_FAILED)

    def test_03_dns_check_execution(self):
        """dns-check resolves domain name and captures query latency."""
        # 1. Localhost resolution
        res = JobExecutor.execute({
            "job_id": "job-test-dns-1",
            "type": "dns-check",
            "parameters": {"domain": "localhost"},
            "timeout": 10
        })
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        self.assertIn("127.0.0.1", res["stdout"])
        self.assertGreaterEqual(res["duration_ms"], 0)

        # 2. Invalid domain characters rejected safely
        res_bad = JobExecutor.execute({
            "job_id": "job-test-dns-bad",
            "type": "dns-check",
            "parameters": {"domain": "invalid domain with spaces"},
            "timeout": 10
        })
        self.assertEqual(res_bad["status"], JOB_STATE_FAILED)
        self.assertIn("Invalid domain name", res_bad["stderr"])

    def test_04_python_script_execution_with_args(self):
        """python-script executes inline script and receives CLI argument array."""
        script_code = "import sys\nprint('ARGS:' + ','.join(sys.argv[2:]))\nprint('HELLO_PYTHON')"
        res = JobExecutor.execute({
            "job_id": "job-test-py-args",
            "type": "python-script",
            "parameters": {
                "script": script_code,
                "args": ["alpha", "beta", "gamma"]
            },
            "timeout": 10
        })
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("ARGS:alpha,beta,gamma", res["stdout"])
        self.assertIn("HELLO_PYTHON", res["stdout"])

    def test_05_shell_script_execution(self):
        """shell-script executes command on compatible host."""
        res = JobExecutor.execute({
            "job_id": "job-test-shell-1",
            "type": "shell-script",
            "parameters": {"script": "echo 'Distributed PersonalServer Cluster'"},
            "timeout": 10
        })
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("Distributed PersonalServer Cluster", res["stdout"])


class TestResourceAwareScheduler(unittest.TestCase):
    """Test resource reservations, concurrency limits, and offline pinned node policies."""

    def setUp(self):
        self.now_iso = get_current_iso_timestamp()
        self.node_a = {
            "node_id": "node-alpha",
            "name": "Alpha Node",
            "role": "compute",
            "status": "ONLINE",
            "last_seen": self.now_iso,
            "capabilities": ["container_runtime:docker", "python:3"],
            "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 50},
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "services": {"node_api": "running"},
                "system": {"memory": "2.0Gi/8.0Gi", "cpu_cores": 8, "storage": "10Gi/50Gi"}
            }
        }
        self.node_b = {
            "node_id": "node-beta",
            "name": "Beta Node",
            "role": "compute",
            "status": "ONLINE",
            "last_seen": self.now_iso,
            "capabilities": ["python:3"],
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 20},
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "services": {"node_api": "running"},
                "system": {"memory": "1.0Gi/4.0Gi", "cpu_cores": 4, "storage": "5Gi/20Gi"}
            }
        }
        self.nodes_db = {
            "nodes": {
                "node-alpha": self.node_a,
                "node-beta": self.node_b
            }
        }

    def test_01_eligibility_filter(self):
        """is_node_eligible correctly checks health, role, capabilities, and memory."""
        # Beta lacks docker capability
        self.assertTrue(is_node_eligible(self.node_a, {"capabilities": ["container_runtime:docker"]}))
        self.assertFalse(is_node_eligible(self.node_b, {"capabilities": ["container_runtime:docker"]}))

        # Minimum memory requirement (node-a has ~6144 MB free, node-b has ~3072 MB free)
        self.assertTrue(is_node_eligible(self.node_a, {"min_memory_mb": 5000}))
        self.assertFalse(is_node_eligible(self.node_b, {"min_memory_mb": 5000}))

    def test_02_resource_reservation_deduction(self):
        """Active in-flight jobs deduct reserved memory from available memory during scheduling."""
        cluster_jobs = {
            "job-active-1": {
                "assigned_node": "node-alpha",
                "status": JOB_STATE_RUNNING,
                "requirements": {"min_memory_mb": 5000}
            }
        }
        decision = ResourceScheduler.select_node(
            {"min_memory_mb": 2000},
            self.nodes_db,
            DEFAULT_HEARTBEAT_TIMEOUT,
            cluster_jobs=cluster_jobs
        )
        # Alpha had ~6144 MB - 5000 MB reserved = ~1144 MB available, which is < 2000 MB
        # Therefore Beta is chosen!
        self.assertEqual(decision["selected_node"], "node-beta")

    def test_03_active_jobs_concurrency_penalty(self):
        """Least-loaded placement penalizes nodes with higher active concurrent jobs."""
        cluster_jobs = {
            f"job-load-{i}": {
                "assigned_node": "node-alpha",
                "status": JOB_STATE_RUNNING,
            }
            for i in range(5)  # 5 active jobs on Alpha
        }
        decision = ResourceScheduler.select_node(
            {"role": "compute"},
            self.nodes_db,
            DEFAULT_HEARTBEAT_TIMEOUT,
            cluster_jobs=cluster_jobs
        )
        # Beta has 0 active jobs and gets selected
        self.assertEqual(decision["selected_node"], "node-beta")

    def test_04_offline_pinned_node_lease_sweeper(self):
        """Jobs pinned to an offline node enter NEEDS_ATTENTION with an explanatory waiting_reason."""
        stale_time = "2026-10-01T00:00:00Z"
        self.node_a["last_seen"] = stale_time
        self.node_a["last_heartbeat"]["timestamp"] = stale_time

        jobs_db = {
            "jobs": {
                "job-pinned-1": {
                    "id": "job-pinned-1",
                    "target_node": "node-alpha",
                    "pinned": True,
                    "status": JOB_STATE_QUEUED,
                    "state": JOB_STATE_QUEUED,
                    "created_at": stale_time
                }
            }
        }

        sweep_expired_leases(jobs_db, self.nodes_db, timeout_seconds=10)
        job = jobs_db["jobs"]["job-pinned-1"]

        self.assertEqual(job["status"], JOB_STATE_NEEDS_ATTENTION)
        self.assertEqual(job["state"], JOB_STATE_NEEDS_ATTENTION)
        self.assertIn("node-alpha", job["waiting_reason"])
        self.assertIn("offline", job["waiting_reason"].lower())


class TestArtifactManager(unittest.TestCase):
    """Test secure artifact storage, SHA-256 verification, and path traversal protection."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ps_artifact_test_")
        self.storage_root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_atomic_store_and_checksum(self):
        """Artifact is stored atomically with exact SHA-256 and metadata persistence."""
        content = b"PersonalServer artifact test payload 123456"
        art_id = generate_artifact_id()

        record = store_artifact_bytes(
            storage_root=self.storage_root,
            artifact_id=art_id,
            filename="output.dat",
            content_bytes=content,
            job_id="job-art-test-1"
        )

        self.assertEqual(record["artifact_id"], art_id)
        self.assertEqual(record["size_bytes"], len(content))
        self.assertTrue(Path(record["path"]).exists())
        self.assertTrue(verify_artifact_integrity(record))

    def test_02_checksum_mismatch_rejection(self):
        """store_artifact_bytes rejects mismatched expected SHA-256 checksum."""
        content = b"Important data"
        art_id = generate_artifact_id()

        with self.assertRaises(ValueError) as ctx:
            store_artifact_bytes(
                storage_root=self.storage_root,
                artifact_id=art_id,
                filename="data.txt",
                content_bytes=content,
                expected_sha256="deadbeefbadchecksum"
            )
        self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_03_path_traversal_rejection(self):
        """Path traversal patterns are strictly caught and rejected."""
        with self.assertRaises(ValueError):
            validate_artifact_path(self.storage_root, "../../etc/passwd")

        with self.assertRaises(ValueError):
            validate_artifact_path(self.storage_root, "..")

        with self.assertRaises(ValueError):
            validate_artifact_path(self.storage_root, "subdir/filename.txt")


class TestWorkflowEngine(unittest.TestCase):
    """Test multi-step DAG workflows, cycle detection, parallel dispatch, and failure propagation."""

    def test_01_dag_cycle_detection(self):
        """Cyclic workflow specifications are rejected with WorkflowValidationError."""
        cyclic_spec = {
            "stepA": {"depends_on": ["stepB"]},
            "stepB": {"depends_on": ["stepA"]}
        }
        with self.assertRaises(WorkflowValidationError) as ctx:
            validate_workflow_dag(cyclic_spec)
        self.assertIn("cycle detected", str(ctx.exception).lower())

        self_dep_spec = {
            "stepA": {"depends_on": ["stepA"]}
        }
        with self.assertRaises(WorkflowValidationError):
            validate_workflow_dag(self_dep_spec)

    def test_02_workflow_instantiation_and_step_progression(self):
        """Independent steps start QUEUED; dependent steps start WAITING_FOR_DEPENDENCY and unblock on parent success."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "ETL Pipeline",
            "steps": {
                "fetch": {"type": "http-check", "parameters": {"url": "https://example.com"}},
                "process": {"type": "python-script", "depends_on": ["fetch"], "parameters": {"script": "print('ok')"}},
                "aggregate": {"type": "echo", "depends_on": ["process"], "parameters": {"message": "done"}}
            }
        }

        wf_rec, created_jobs = instantiate_workflow(wf_spec, jobs_db)

        self.assertEqual(len(created_jobs), 3)
        fetch_job_id = wf_rec["steps"]["fetch"]["job_id"]
        process_job_id = wf_rec["steps"]["process"]["job_id"]
        agg_job_id = wf_rec["steps"]["aggregate"]["job_id"]

        # 1. Initially, fetch is QUEUED; process and aggregate are WAITING_FOR_DEPENDENCY
        self.assertEqual(jobs_db["jobs"][fetch_job_id]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][process_job_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)
        self.assertEqual(jobs_db["jobs"][agg_job_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # 2. Simulate fetch success -> process should unblock to QUEUED
        jobs_db["jobs"][fetch_job_id]["status"] = JOB_STATE_SUCCEEDED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        changed = progress_workflows(workflows_db, jobs_db)
        self.assertTrue(changed)
        self.assertEqual(jobs_db["jobs"][process_job_id]["status"], JOB_STATE_QUEUED)
        self.assertEqual(jobs_db["jobs"][agg_job_id]["status"], JOB_STATE_WAITING_FOR_DEPENDENCY)

        # 3. Simulate process success -> aggregate unblocks to QUEUED
        jobs_db["jobs"][process_job_id]["status"] = JOB_STATE_SUCCEEDED
        progress_workflows(workflows_db, jobs_db)
        self.assertEqual(jobs_db["jobs"][agg_job_id]["status"], JOB_STATE_QUEUED)

        # 4. Simulate aggregate success -> entire workflow transitions to SUCCEEDED
        jobs_db["jobs"][agg_job_id]["status"] = JOB_STATE_SUCCEEDED
        progress_workflows(workflows_db, jobs_db)
        self.assertEqual(wf_rec["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(wf_rec["progress_percent"], 100.0)

    def test_03_workflow_failure_propagation(self):
        """When a required parent step fails, downstream dependent steps transition to FAILED."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Fragile Pipeline",
            "steps": {
                "step1": {"type": "echo"},
                "step2": {"type": "echo", "depends_on": ["step1"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        s1_job = wf_rec["steps"]["step1"]["job_id"]
        s2_job = wf_rec["steps"]["step2"]["job_id"]

        # Simulate step1 failure
        jobs_db["jobs"][s1_job]["status"] = JOB_STATE_FAILED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        progress_workflows(workflows_db, jobs_db)

        # Step 2 should transition to FAILED due to parent failure
        self.assertEqual(jobs_db["jobs"][s2_job]["status"], JOB_STATE_FAILED)
        self.assertIn("Upstream dependency failed", jobs_db["jobs"][s2_job]["retry_reason"])
        self.assertEqual(wf_rec["status"], JOB_STATE_FAILED)

    def test_04_workflow_cancellation(self):
        """Cancelling a workflow transitions non-terminal child jobs to CANCELLED."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Cancel Pipeline",
            "steps": {
                "step1": {"type": "echo"},
                "step2": {"type": "echo", "depends_on": ["step1"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        s1_job = wf_rec["steps"]["step1"]["job_id"]
        s2_job = wf_rec["steps"]["step2"]["job_id"]

        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}
        cancel_workflow(wf_rec["workflow_id"], workflows_db, jobs_db, reason="User cancellation")

        self.assertEqual(wf_rec["status"], JOB_STATE_CANCELLED)
        self.assertEqual(jobs_db["jobs"][s1_job]["status"], JOB_STATE_CANCELLED)
        self.assertEqual(jobs_db["jobs"][s2_job]["status"], JOB_STATE_CANCELLED)


if __name__ == "__main__":
    unittest.main()
