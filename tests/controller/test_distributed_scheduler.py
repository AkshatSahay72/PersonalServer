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
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from config.platform_config import override_controller_paths
from agent.job_executor import JobExecutor
from common.artifact_manager import (
    ArtifactManager,
    compute_file_sha256,
    generate_artifact_id,
    store_artifact_bytes,
    store_artifact_stream,
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
from controller.http.job_routes import (
    handle_job_get,
    handle_job_post,
    handle_job_delete,
)
from controller.onboarding import get_or_create_enrollment_token
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

        # 2. Connection failure on closed port with explicit allow_internal=True
        res_closed = JobExecutor.execute({
            "job_id": "job-test-http-closed",
            "type": "http-check",
            "parameters": {"url": "http://127.0.0.1:59999/healthz", "http_timeout": 2, "allow_internal": True},
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

    def test_06_http_check_ssrf_protection(self):
        """http-check blocks loopback, private, link-local, and cloud metadata targets unless allow_internal=True."""
        blocked_targets = [
            "http://127.0.0.1:8080/admin",
            "http://10.0.0.5/api/v1",
            "http://172.16.0.1/status",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
        ]
        for url in blocked_targets:
            res = JobExecutor.execute({
                "job_id": f"job-test-ssrf-{hash(url)}",
                "type": "http-check",
                "parameters": {"url": url},
                "timeout": 5
            })
            self.assertEqual(res["status"], JOB_STATE_FAILED, f"Target {url} should have been blocked")
            self.assertIn("Security violation (SSRF)", res["stderr"])

        # When allow_internal is explicitly True, pre-validation allows attempt (fails with connection error on unopen port)
        res_allowed = JobExecutor.execute({
            "job_id": "job-test-ssrf-allowed",
            "type": "http-check",
            "parameters": {"url": "http://127.0.0.1:59998/test", "allow_internal": True, "http_timeout": 1},
            "timeout": 5
        })
        self.assertEqual(res_allowed["status"], JOB_STATE_FAILED)
        self.assertNotIn("Security violation (SSRF)", res_allowed["stderr"])

    def test_07_shell_script_safe_arguments(self):
        """shell-script passes argument array as distinct data elements without command interpolation."""
        test_args = ["first", "second with spaces", "; rm -rf /", "$env:SECRET"]
        script = "import sys\nfor a in sys.argv[1:]: print(f'ARG>>{a}<<')" if sys.platform != "win32" else "param($a, $b, $c, $d); Write-Output \"ARG>>$a<<\"; Write-Output \"ARG>>$b<<\"; Write-Output \"ARG>>$c<<\"; Write-Output \"ARG>>$d<<\""
        res = JobExecutor.execute({
            "job_id": "job-test-safe-args",
            "type": "shell-script",
            "parameters": {
                "script": script,
                "args": test_args
            },
            "timeout": 10
        })
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        for arg in test_args:
            self.assertIn(f"ARG>>{arg}<<", res["stdout"])


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

    def test_05_resource_filtering_cpu_and_storage(self):
        """Scheduler accurately filters by minimum CPU cores and storage requirements."""
        # Node Alpha has 8 cores, 40GB free storage. Node Beta has 4 cores, 15GB free storage.
        self.assertTrue(is_node_eligible(self.node_a, {"min_cpu_cores": 6}))
        self.assertFalse(is_node_eligible(self.node_b, {"min_cpu_cores": 6}))

        self.assertTrue(is_node_eligible(self.node_a, {"min_storage_gb": 30}))
        self.assertFalse(is_node_eligible(self.node_b, {"min_storage_gb": 30}))

        # Select node requiring 6 cores: Alpha chosen, Beta rejected for CPU
        decision = ResourceScheduler.select_node({"min_cpu_cores": 6}, self.nodes_db)
        self.assertEqual(decision["selected_node"], "node-alpha")
        self.assertIn("Insufficient CPU cores", decision["rejected"]["node-beta"])

    def test_06_insufficient_resources_keeps_job_queued_with_reason(self):
        """When no worker meets resource requirements, select_node returns None with explicit reasons."""
        # Request 16GB RAM when Alpha has ~6GB and Beta has ~3GB
        decision = ResourceScheduler.select_node({"min_memory_mb": 16000}, self.nodes_db)
        self.assertIsNone(decision["selected_node"])
        self.assertEqual(decision["score"], 0.0)
        self.assertIn("No eligible nodes met the requirements", decision["reason"])
        self.assertIn("node-alpha", decision["rejected"])
        self.assertIn("node-beta", decision["rejected"])
        self.assertIn("Insufficient available memory", decision["rejected"]["node-alpha"])

    def test_07_unavailable_workers_filtered_with_clear_reasons(self):
        """Offline and draining nodes are rejected with distinct, actionable reasons."""
        self.node_a["status"] = "DRAINING"
        stale_time = "2026-10-01T00:00:00Z"
        self.node_b["last_seen"] = stale_time
        self.node_b["last_heartbeat"]["timestamp"] = stale_time

        decision = ResourceScheduler.select_node({"role": "compute"}, self.nodes_db, timeout_seconds=10)
        self.assertIsNone(decision["selected_node"])
        self.assertIn("DRAINING", decision["rejected"]["node-alpha"])
        self.assertIn("OFFLINE", decision["rejected"]["node-beta"])

    def test_08_lease_sweeper_dispatches_pending_auto_job_when_worker_becomes_available(self):
        """Lease sweeper evaluates unassigned pending auto-scheduled jobs and assigns eligible workers."""
        jobs_db = {
            "jobs": {
                "job-pending-auto": {
                    "job_id": "job-pending-auto",
                    "target": "auto",
                    "target_node": None,
                    "assigned_node": None,
                    "status": JOB_STATE_QUEUED,
                    "requirements": {"min_memory_mb": 5000},
                    "waiting_reason": "Awaiting eligible worker"
                }
            }
        }
        # Node Alpha is currently online with ~6144MB free
        changed = sweep_expired_leases(jobs_db, self.nodes_db, timeout_seconds=60)
        self.assertTrue(changed)
        job = jobs_db["jobs"]["job-pending-auto"]
        self.assertEqual(job["target_node"], "node-alpha")
        self.assertEqual(job["assigned_node"], "node-alpha")
        self.assertIsNone(job["waiting_reason"])
        self.assertEqual(job["scheduler"]["mode"], "resource-aware")



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
        """Cancelling a workflow transitions non-terminal child jobs to CANCELLED and preserves SUCCEEDED steps."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Cancel Pipeline",
            "steps": {
                "step1": {"type": "echo"},
                "step2": {"type": "echo", "depends_on": ["step1"]},
                "step3": {"type": "echo", "depends_on": ["step2"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        s1_job = wf_rec["steps"]["step1"]["job_id"]
        s2_job = wf_rec["steps"]["step2"]["job_id"]
        s3_job = wf_rec["steps"]["step3"]["job_id"]

        # Step 1 succeeded before cancellation
        jobs_db["jobs"][s1_job]["status"] = JOB_STATE_SUCCEEDED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}
        cancel_workflow(wf_rec["workflow_id"], workflows_db, jobs_db, reason="User cancellation")

        self.assertEqual(wf_rec["status"], JOB_STATE_CANCELLED)
        # Succeeded step MUST NOT be corrupted to CANCELLED
        self.assertEqual(jobs_db["jobs"][s1_job]["status"], JOB_STATE_SUCCEEDED)
        # Non-terminal steps must be CANCELLED
        self.assertEqual(jobs_db["jobs"][s2_job]["status"], JOB_STATE_CANCELLED)
        self.assertEqual(jobs_db["jobs"][s3_job]["status"], JOB_STATE_CANCELLED)

    def test_05_multi_tier_dependency_cascade(self):
        """When root step fails, downstream steps at arbitrary depths (A -> B -> C) fail in one pass."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Cascade Pipeline",
            "steps": {
                "stepA": {"type": "echo"},
                "stepB": {"type": "echo", "depends_on": ["stepA"]},
                "stepC": {"type": "echo", "depends_on": ["stepB"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        a_job = wf_rec["steps"]["stepA"]["job_id"]
        b_job = wf_rec["steps"]["stepB"]["job_id"]
        c_job = wf_rec["steps"]["stepC"]["job_id"]

        # Step A fails
        jobs_db["jobs"][a_job]["status"] = JOB_STATE_FAILED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        progress_workflows(workflows_db, jobs_db)

        # Both B and C must transition to FAILED in a single evaluation
        self.assertEqual(jobs_db["jobs"][b_job]["status"], JOB_STATE_FAILED)
        self.assertEqual(jobs_db["jobs"][c_job]["status"], JOB_STATE_FAILED)
        self.assertEqual(wf_rec["status"], JOB_STATE_FAILED)

    def test_06_fail_fast_policy_cancels_queued_siblings(self):
        """Under default fail_fast policy, failure in one track cancels sibling tracks (no stranded jobs)."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "Parallel FailFast Pipeline",
            "failure_policy": "fail_fast",
            "steps": {
                "track1_a": {"type": "echo"},
                "track1_b": {"type": "echo", "depends_on": ["track1_a"]},
                "track2_a": {"type": "echo"},
                "track2_b": {"type": "echo", "depends_on": ["track2_a"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        t1a = wf_rec["steps"]["track1_a"]["job_id"]
        t1b = wf_rec["steps"]["track1_b"]["job_id"]
        t2a = wf_rec["steps"]["track2_a"]["job_id"]
        t2b = wf_rec["steps"]["track2_b"]["job_id"]

        # Track 2 root is queued; Track 1 root fails
        self.assertEqual(jobs_db["jobs"][t2a]["status"], JOB_STATE_QUEUED)
        jobs_db["jobs"][t1a]["status"] = JOB_STATE_FAILED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        progress_workflows(workflows_db, jobs_db)

        # Track 2 queued job and waiting job must be CANCELLED by fail-fast, not left stranded!
        self.assertEqual(jobs_db["jobs"][t2a]["status"], JOB_STATE_CANCELLED)
        self.assertEqual(jobs_db["jobs"][t2b]["status"], JOB_STATE_CANCELLED)
        # Track 1 downstream step failed due to upstream failure
        self.assertEqual(jobs_db["jobs"][t1b]["status"], JOB_STATE_FAILED)
        self.assertEqual(wf_rec["status"], JOB_STATE_FAILED)

    def test_07_continue_on_failure_policy(self):
        """Under continue_on_failure, independent tracks proceed to completion while failed branch stops."""
        jobs_db = {"jobs": {}}
        wf_spec = {
            "name": "ContinueOnFailure Pipeline",
            "failure_policy": "continue_on_failure",
            "steps": {
                "track1_a": {"type": "echo"},
                "track1_b": {"type": "echo", "depends_on": ["track1_a"]},
                "track2_a": {"type": "echo"},
                "track2_b": {"type": "echo", "depends_on": ["track2_a"]}
            }
        }
        wf_rec, _ = instantiate_workflow(wf_spec, jobs_db)
        t1a = wf_rec["steps"]["track1_a"]["job_id"]
        t1b = wf_rec["steps"]["track1_b"]["job_id"]
        t2a = wf_rec["steps"]["track2_a"]["job_id"]
        t2b = wf_rec["steps"]["track2_b"]["job_id"]

        # Track 1 fails, Track 2 succeeds
        jobs_db["jobs"][t1a]["status"] = JOB_STATE_FAILED
        jobs_db["jobs"][t2a]["status"] = JOB_STATE_SUCCEEDED
        workflows_db = {"workflows": {wf_rec["workflow_id"]: wf_rec}}

        progress_workflows(workflows_db, jobs_db)

        # t1b should fail, but t2b should become QUEUED!
        self.assertEqual(jobs_db["jobs"][t1b]["status"], JOB_STATE_FAILED)
        self.assertEqual(jobs_db["jobs"][t2b]["status"], JOB_STATE_QUEUED)
        self.assertEqual(wf_rec["status"], "RUNNING")

        # Now complete t2b
        jobs_db["jobs"][t2b]["status"] = JOB_STATE_SUCCEEDED
        progress_workflows(workflows_db, jobs_db)

        self.assertEqual(wf_rec["status"], JOB_STATE_FAILED)
        self.assertEqual(wf_rec["completed_steps"], 2)


class TestHttpJobRoutesSecurity(unittest.TestCase):
    """Test authentication, lease fencing, streaming uploads, and deletion guards on HTTP endpoints."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_routes_sec_")
        self.data_dir = Path(self.tmp_dir) / "data"
        self.config_dir = Path(self.tmp_dir) / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = Path(self.tmp_dir) / "runtime"
        self.logs_dir = Path(self.tmp_dir) / "logs"
        self.storage_dir = Path(self.tmp_dir) / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.enrollment_token = "test-enrollment-secret-token-routes"
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

        # Mock handler
        self.handler = MagicMock()
        self.handler.current_correlation_id = "corr-test-routes"
        self.handler.send_json = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = io.BytesIO()
        self.handler.headers = {}
        self.handler.heartbeat_timeout = 60
        self.handler.is_authenticated_admin_or_node.return_value = False

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_unauthenticated_post_jobs_rejected_401(self):
        """POST /jobs with no token returns 401 Unauthorized."""
        self.handler.parse_auth_token.return_value = ""
        handled = handle_job_post(self.handler, "/jobs", {"type": "echo", "parameters": {"message": "hi"}}, token="", query={})
        self.assertTrue(handled)
        self.handler.send_json.assert_called_with(401, {"error": "Unauthorized: Authentication required to submit jobs"})

    def test_02_authenticated_post_jobs_accepted_200(self):
        """POST /jobs with valid enrollment token returns 200 with lease_generation 0."""
        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True
        handled = handle_job_post(self.handler, "/jobs", {"type": "echo", "parameters": {"message": "hi"}}, token=self.enrollment_token, query={})
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_data = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 200)
        self.assertEqual(call_data["job"]["lease_generation"], 0)

    def test_03_unauthenticated_artifact_endpoints_rejected_401(self):
        """GET /artifacts and GET /artifacts/<id>/download with no token return 401."""
        self.handler.parse_auth_token.return_value = ""
        # 1. List
        handled = handle_job_get(self.handler, "/artifacts", {})
        self.assertTrue(handled)
        self.handler.send_json.assert_called_with(401, {"error": "Unauthorized: Authentication required to list artifacts"})

        # 2. Download
        handled2 = handle_job_get(self.handler, "/artifacts/art-test-123/download", {})
        self.assertTrue(handled2)
        self.handler.send_json.assert_called_with(401, {"error": "Unauthorized: Authentication required to download artifacts"})

    def test_04_artifact_upload_base64_size_limit_413(self):
        """POST /artifacts/upload rejects base64 bodies larger than 64MB with 413 Payload Too Large."""
        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True
        fake_oversized_b64 = "A" * (65 * 1024 * 1024)
        handled = handle_job_post(
            self.handler,
            "/artifacts/upload",
            {"filename": "huge.bin", "content_base64": fake_oversized_b64},
            token=self.enrollment_token,
            query={}
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        self.assertEqual(call_code, 413)

    def test_05_lease_fencing_token_rejects_superseded_generation(self):
        """POST /jobs/<id>/result rejects stale lease generations with 409 Conflict."""
        # Create a job with lease_generation = 2
        from controller.stores import load_jobs_db, save_jobs_db, load_nodes_db, save_nodes_db
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        test_job_id = "job-fencing-test-1"
        test_node_id = "node-fencing-worker"
        nodes_db.setdefault("nodes", {})[test_node_id] = {"auth_token": "worker-token-xyz"}
        save_nodes_db(nodes_db)

        jobs_db.setdefault("jobs", {})[test_job_id] = {
            "job_id": test_job_id,
            "target_node": test_node_id,
            "status": JOB_STATE_RUNNING,
            "attempt": 1,
            "lease_generation": 2
        }
        save_jobs_db(jobs_db)

        self.handler.is_authenticated_admin_or_node.return_value = True
        # Worker submits result with superseded lease_generation = 1
        handled = handle_job_post(
            self.handler,
            f"/jobs/{test_job_id}/result",
            {"status": JOB_STATE_SUCCEEDED, "lease_generation": 1},
            token="worker-token-xyz",
            query={}
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 409)
        self.assertTrue(call_body.get("superseded"))

    def test_06_lease_renewal_extends_expiry(self):
        """POST /jobs/<id>/lease/renew extends lease_expires_at for active job."""
        from controller.stores import load_jobs_db, save_jobs_db, load_nodes_db, save_nodes_db
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        test_job_id = "job-renew-test-1"
        test_node_id = "node-renew-worker"
        nodes_db.setdefault("nodes", {})[test_node_id] = {"auth_token": "worker-token-abc"}
        save_nodes_db(nodes_db)

        now_epoch = time.time()
        jobs_db.setdefault("jobs", {})[test_job_id] = {
            "job_id": test_job_id,
            "target_node": test_node_id,
            "status": JOB_STATE_RUNNING,
            "lease_generation": 1,
            "lease_expires_at": now_epoch + 10
        }
        save_jobs_db(jobs_db)

        handled = handle_job_post(
            self.handler,
            f"/jobs/{test_job_id}/lease/renew",
            {"lease_generation": 1, "extension_sec": 120},
            token="worker-token-abc",
            query={}
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 200)
        self.assertGreater(call_body["lease_expires_at"], now_epoch + 100)

    def test_07_job_deletion_guards_active_and_workflow_steps(self):
        """DELETE /jobs/<id> rejects deleting active jobs or steps of active workflows with 409."""
        from controller.stores import load_jobs_db, save_jobs_db, load_workflows_db, save_workflows_db
        jobs_db = load_jobs_db()
        workflows_db = load_workflows_db()

        # 1. Active waiting job cannot be deleted
        jobs_db.setdefault("jobs", {})["job-waiting-del"] = {
            "job_id": "job-waiting-del",
            "status": JOB_STATE_WAITING_FOR_DEPENDENCY
        }
        # 2. Step belonging to active workflow cannot be deleted
        jobs_db.setdefault("jobs", {})["job-wf-step-del"] = {
            "job_id": "job-wf-step-del",
            "workflow_id": "wf-active-guard",
            "status": JOB_STATE_SUCCEEDED
        }
        workflows_db.setdefault("workflows", {})["wf-active-guard"] = {
            "workflow_id": "wf-active-guard",
            "status": "RUNNING"
        }
        save_jobs_db(jobs_db)
        save_workflows_db(workflows_db)

        self.handler.is_authenticated_admin_or_node.return_value = True

        # Try delete waiting job
        h1 = handle_job_delete(self.handler, "/jobs/job-waiting-del", token=self.enrollment_token)
        self.assertTrue(h1)
        self.assertEqual(self.handler.send_json.call_args[0][0], 409)

        # Try delete workflow step
        h2 = handle_job_delete(self.handler, "/jobs/job-wf-step-del", token=self.enrollment_token)
        self.assertTrue(h2)
        self.assertEqual(self.handler.send_json.call_args[0][0], 409)

    def test_08_raw_streaming_artifact_upload_success_and_checksum_verification(self):
        """POST /artifacts/upload/raw streams content to disk and enforces SHA-256 integrity."""
        raw_content = b"Binary stream payload for testing personal server artifacts."
        hasher = hashlib.sha256()
        hasher.update(raw_content)
        correct_sha = hasher.hexdigest()

        self.handler.is_authenticated_admin_or_node.return_value = True
        self.handler.rfile = io.BytesIO(raw_content)
        self.handler.headers = {
            "Content-Length": str(len(raw_content)),
            "X-Artifact-Filename": "streamed_data.bin",
            "X-Artifact-Sha256": correct_sha
        }

        # 1. Successful upload with valid sha256
        handled = handle_job_post(self.handler, "/artifacts/upload/raw", None, token=self.enrollment_token, query={})
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 201)
        self.assertEqual(call_body["status"], "UPLOADED")
        self.assertEqual(call_body["artifact"]["sha256"], correct_sha)
        self.assertEqual(call_body["artifact"]["size_bytes"], len(raw_content))

        # 2. Checksum mismatch fails and cleans up temp files
        self.handler.rfile = io.BytesIO(raw_content)
        self.handler.headers["X-Artifact-Sha256"] = "badc0ffee" * 7 + "0"
        handled_bad = handle_job_post(self.handler, "/artifacts/upload/raw", None, token=self.enrollment_token, query={})
        self.assertTrue(handled_bad)
        self.assertEqual(self.handler.send_json.call_args[0][0], 400)
        self.assertIn("Checksum mismatch", self.handler.send_json.call_args[0][1]["error"])

    def test_09_unauthorized_token_cannot_delete_jobs(self):
        """DELETE /jobs/<id> without valid admin credentials returns 401 Unauthorized."""
        self.handler.is_authenticated_admin_or_node.return_value = False
        handled = handle_job_delete(self.handler, "/jobs/some-job-id", token="unauthorized-worker-token")
        self.assertTrue(handled)
        self.assertEqual(self.handler.send_json.call_args[0][0], 401)

    def test_10_allow_internal_requires_admin_credentials(self):
        """POST /jobs with allow_internal=True is rejected with 403 Forbidden when called by a worker token."""
        self.handler.is_authenticated_admin_or_node.return_value = True
        # 1. Worker token attempting allow_internal is rejected
        handled = handle_job_post(
            self.handler,
            "/jobs",
            {"type": "http-check", "parameters": {"url": "http://127.0.0.1:8080", "allow_internal": True}},
            token="worker-node-token-123",
            query={}
        )
        self.assertTrue(handled)
        self.assertEqual(self.handler.send_json.call_args[0][0], 403)
        self.assertIn("requires administrator credentials", self.handler.send_json.call_args[0][1]["error"])

        # 2. Administrator token is permitted
        handled_admin = handle_job_post(
            self.handler,
            "/jobs",
            {"type": "http-check", "parameters": {"url": "http://127.0.0.1:8080", "allow_internal": True}},
            token=self.enrollment_token,
            query={}
        )
        self.assertTrue(handled_admin)
        self.assertEqual(self.handler.send_json.call_args[0][0], 200)

    def test_11_insufficient_resources_keeps_job_queued_with_waiting_reason(self):
        """Submitting job with unsatisfiable requirements leaves job QUEUED with descriptive waiting_reason."""
        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True

        handled = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "echo",
                "parameters": {"message": "heavy-job"},
                "requirements": {"min_memory_mb": 128000},
                "target": "auto"
            },
            token=self.enrollment_token,
            query={}
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 200)
        self.assertEqual(call_body["status"], JOB_STATE_QUEUED)
        self.assertIsNone(call_body["target_node"])
        self.assertEqual(call_body["scheduler"]["mode"], "resource-aware-pending")
        self.assertIn("Awaiting eligible worker", call_body["job"]["waiting_reason"])

    def test_12_worker_poll_claims_only_when_eligible(self):
        """Worker polling /jobs/next claims auto-scheduled job only if node satisfies requirements."""
        from controller.stores import load_jobs_db, save_jobs_db, load_nodes_db, save_nodes_db
        jobs_db = load_jobs_db(self.paths)
        nodes_db = load_nodes_db(self.paths)

        # Register weak worker (1 core, 512MB RAM) and strong worker (8 cores, 8192MB RAM)
        nodes_db.setdefault("nodes", {})["node-weak"] = {
            "node_id": "node-weak",
            "name": "Weak Node",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "token-weak",
            "last_seen": get_current_iso_timestamp(),
            "resources": {"cpu_cores": 1, "ram_mb": 512, "storage_gb": 10},
            "last_heartbeat": {"system": {"cpu_cores": 1, "memory": "100M/512M", "storage": "2G/10G"}}
        }
        nodes_db.setdefault("nodes", {})["node-strong"] = {
            "node_id": "node-strong",
            "name": "Strong Node",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "token-strong",
            "last_seen": get_current_iso_timestamp(),
            "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 100},
            "last_heartbeat": {"system": {"cpu_cores": 8, "memory": "2Gi/8Gi", "storage": "20G/100G"}}
        }
        save_nodes_db(nodes_db, self.paths)

        # Queue job requiring 4 cores
        test_job_id = "job-req-4cores"
        jobs_db.setdefault("jobs", {})[test_job_id] = {
            "job_id": test_job_id,
            "target": "auto",
            "target_node": None,
            "assigned_node": None,
            "status": JOB_STATE_QUEUED,
            "requirements": {"min_cpu_cores": 4},
            "timeout": 30
        }
        save_jobs_db(jobs_db, self.paths)

        # 1. Weak worker polls -> cannot claim
        self.handler.parse_auth_token.return_value = "token-weak"
        self.handler.is_authenticated_admin_or_node.return_value = False
        h1 = handle_job_get(self.handler, "/nodes/node-weak/jobs/next", {})
        self.assertTrue(h1)
        res_weak = self.handler.send_json.call_args[0][1]
        self.assertIsNone(res_weak.get("job"))

        # 2. Strong worker polls -> successfully claims
        self.handler.parse_auth_token.return_value = "token-strong"
        h2 = handle_job_get(self.handler, "/nodes/node-strong/jobs/next", {})
        self.assertTrue(h2)
        res_strong = self.handler.send_json.call_args[0][1]
        self.assertIsNotNone(res_strong.get("job"))
        claimed_job = res_strong["job"]
        self.assertEqual(claimed_job["job_id"], test_job_id)
        self.assertEqual(claimed_job["target_node"], "node-strong")
        self.assertEqual(claimed_job["status"], JOB_STATE_CLAIMED)
        self.assertIsNone(claimed_job["waiting_reason"])

    def test_13_explicit_target_requirements_mismatch_rejected(self):
        """Explicitly targeting a node that does not satisfy requirements returns 400."""
        from controller.stores import load_nodes_db, save_nodes_db
        nodes_db = load_nodes_db(self.paths)
        nodes_db.setdefault("nodes", {})["node-under-resourced"] = {
            "node_id": "node-under-resourced",
            "name": "Under Resourced",
            "role": "compute",
            "status": "ONLINE",
            "auth_token": "token-under",
            "last_seen": get_current_iso_timestamp(),
            "resources": {"cpu_cores": 1, "ram_mb": 512, "storage_gb": 10},
            "last_heartbeat": {"system": {"cpu_cores": 1, "memory": "200M/512M", "storage": "2G/10G"}}
        }
        save_nodes_db(nodes_db, self.paths)

        self.handler.parse_auth_token.return_value = self.enrollment_token
        self.handler.is_authenticated_admin_or_node.return_value = True

        handled = handle_job_post(
            self.handler,
            "/jobs",
            {
                "type": "echo",
                "parameters": {"message": "hello"},
                "requirements": {"min_cpu_cores": 4},
                "target": "node-under-resourced"
            },
            token=self.enrollment_token,
            query={}
        )
        self.assertTrue(handled)
        call_code = self.handler.send_json.call_args[0][0]
        call_body = self.handler.send_json.call_args[0][1]
        self.assertEqual(call_code, 400)
        self.assertIn("does not satisfy job requirements", call_body["error"])
        self.assertIn("Insufficient CPU cores", call_body["error"])


if __name__ == "__main__":
    unittest.main()

