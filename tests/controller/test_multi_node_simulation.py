#!/usr/bin/env python3
"""
PersonalServer Multi-Node Integration Test Suite
================================================
Deterministic multi-node simulation tests exercising the real PersonalServer
controller, registry, authentication, resource scheduler, lease management,
and retry lifecycle at the network protocol boundary.

Scenarios Tested:
1. Register multiple workers with different capabilities & roles.
2. Authenticate workers and reject invalid credentials.
3. Expire stale heartbeats and mark workers unavailable (OFFLINE).
4. Schedule only to eligible workers (capability, role, and resource filtering).
5. Verify that an unavailable worker receives no new work.
6. Simulate execution failure and verify exponential retry according to cluster rules.
7. Restart controller and recover persisted node/job state.
8. Verify that a job is NOT silently marked successful after a worker crash.
9. Test stale worker reports and idempotent duplicate job completion.
10. Verify cluster recovery after a simulated network partition.
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

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths, get_controller_paths
from controller.controller import (
    ControllerHandler,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    compute_node_liveness,
    sweep_expired_leases,
    STATE_ONLINE,
    STATE_OFFLINE,
    STATE_DRAINING,
    STATE_REMOVED,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_CANCELLED,
    JOB_STATE_REJECTED,
    JOB_STATE_RECOVERING
)
from scheduler.scheduler import ResourceScheduler
from agent.simulated_worker import (
    SimulatedWorker,
    SimulatedCluster,
    WorkerBehavior,
    SimulatedWorkerAuthError,
    SimulatedWorkerError,
    SimulatedNetworkError
)


class TestMultiNodeSimulation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Create isolated temporary directory
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_multi_node_sim_"))
        cls.data_dir = cls.test_dir / "data"
        cls.config_dir = cls.test_dir / "config"
        cls.secrets_dir = cls.config_dir / "secrets"
        cls.runtime_dir = cls.test_dir / "runtime"
        cls.logs_dir = cls.test_dir / "logs"
        cls.storage_dir = cls.test_dir / "storage"

        for d in (cls.data_dir, cls.secrets_dir, cls.runtime_dir, cls.logs_dir, cls.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        cls.enrollment_token = "cluster-secret-enrollment-token-xyz"
        (cls.secrets_dir / "enrollment.token").write_text(cls.enrollment_token + "\n", encoding="utf-8")

        # Activate paths override
        cls.paths_ctx = override_controller_paths(
            data_dir=cls.data_dir,
            config_dir=cls.config_dir,
            secrets_dir=cls.secrets_dir,
            runtime_dir=cls.runtime_dir,
            logs_dir=cls.logs_dir,
            storage_dir=cls.storage_dir
        )
        cls.paths = cls.paths_ctx.__enter__()

        # Start controller on ephemeral port
        ControllerHandler.heartbeat_timeout = 3  # short timeout for deterministic offline tests
        cls.httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        cls.server_port = cls.httpd.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.server_port}"

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass
        try:
            cls.paths_ctx.__exit__(None, None, None)
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _api_post(self, path: str, data: dict, token: str = None) -> tuple:
        url = f"{self.controller_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            res_body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(res_body)
            except Exception:
                return e.code, {"error": res_body}

    def _api_get(self, path: str, token: str = None) -> tuple:
        url = f"{self.controller_url}{path}"
        req_headers = {}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            res_body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(res_body)
            except Exception:
                return e.code, {"error": res_body}

    # --------------------------------------------------------------------------
    # Scenario 1: Register Multiple Workers with Different Capabilities
    # --------------------------------------------------------------------------
    def test_01_register_multiple_workers_with_different_capabilities(self):
        """Register multiple workers with heterogeneous roles, architectures, and capabilities."""
        cluster = SimulatedCluster(self.controller_url, self.enrollment_token)

        # Worker Alpha: General compute, amd64
        w_alpha = cluster.add_worker(
            "alpha",
            name="Simulated Compute Node",
            role="compute",
            architecture="amd64",
            resources={"cpu_cores": 8, "ram_mb": 16384, "storage_gb": 200},
            capabilities={"compute": True, "storage": False, "network": True}
        )

        # Worker Beta: Dedicated storage, arm64
        w_beta = cluster.add_worker(
            "beta",
            name="Simulated Storage Node",
            role="storage",
            architecture="arm64",
            resources={"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 1000},
            capabilities={"compute": False, "storage": True, "network": True}
        )

        # Worker Gamma: Docker container runtime node, amd64
        w_gamma = cluster.add_worker(
            "gamma",
            name="Simulated Docker Node",
            role="compute",
            architecture="amd64",
            resources={"cpu_cores": 16, "ram_mb": 32768, "storage_gb": 500},
            capabilities={"compute": True, "storage": True, "container_runtime:docker": True}
        )

        reg_results = cluster.register_all()
        self.assertEqual(len(reg_results), 3)

        # Verify all three acquired valid tokens
        for nid, worker in cluster.workers.items():
            self.assertTrue(worker.registered)
            self.assertTrue(bool(worker.auth_token))

        # Check controller database
        nodes_db = load_nodes_db()
        nodes = nodes_db.get("nodes", {})

        self.assertIn("sim-node-alpha", nodes)
        self.assertEqual(nodes["sim-node-alpha"]["architecture"], "amd64")
        self.assertEqual(nodes["sim-node-alpha"]["role"], "compute")
        self.assertTrue(nodes["sim-node-alpha"]["capabilities"]["compute"])
        self.assertFalse(nodes["sim-node-alpha"]["capabilities"]["storage"])

        self.assertIn("sim-node-beta", nodes)
        self.assertEqual(nodes["sim-node-beta"]["architecture"], "arm64")
        self.assertEqual(nodes["sim-node-beta"]["role"], "storage")
        self.assertTrue(nodes["sim-node-beta"]["capabilities"]["storage"])

        self.assertIn("sim-node-gamma", nodes)
        self.assertTrue(nodes["sim-node-gamma"]["capabilities"]["container_runtime:docker"])

    # --------------------------------------------------------------------------
    # Scenario 2: Authenticate Workers and Reject Invalid Credentials
    # --------------------------------------------------------------------------
    def test_02_authenticate_workers_and_reject_invalid_credentials(self):
        """Verify strict authentication enforcement across registration, heartbeat, claim, and result."""
        # 1. Invalid enrollment token on registration
        bad_worker = SimulatedWorker(
            "bad-auth",
            controller_url=self.controller_url,
            enrollment_token="invalid-enrollment-key"
        )
        with self.assertRaises(SimulatedWorkerAuthError) as ctx:
            bad_worker.register()
        self.assertIn("401", str(ctx.exception))

        # 2. Valid worker registered, but sends heartbeat with forged auth token
        worker = SimulatedWorker(
            "auth-test",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token
        )
        worker.register()
        self.assertTrue(worker.registered)

        # Forged token on heartbeat
        with self.assertRaises(SimulatedWorkerAuthError):
            worker.send_heartbeat(custom_auth="forged-auth-token-123")

        # Forged token on job polling
        with self.assertRaises(SimulatedWorkerAuthError):
            worker.poll_and_claim_job(custom_auth="forged-auth-token-123")

        # Forged token on job result (submit real job first so job exists in registry)
        status_sub, job_sub = self._api_post(
            "/jobs",
            {"type": "echo", "target": worker.node_id, "parameters": {"message": "auth test"}},
            token=self.enrollment_token
        )
        self.assertEqual(status_sub, 200)
        real_job_id = job_sub["job_id"]
        with self.assertRaises(SimulatedWorkerAuthError):
            worker.submit_job_result(real_job_id, custom_auth="forged-auth-token-123")

    # --------------------------------------------------------------------------
    # Scenario 3: Expire Stale Heartbeats and Mark Workers Unavailable
    # --------------------------------------------------------------------------
    def test_03_expire_stale_heartbeats_and_mark_workers_unavailable(self):
        """Simulate missing heartbeats and verify node transitions to OFFLINE deterministically."""
        worker = SimulatedWorker(
            "heartbeat-expiry",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token
        )
        worker.register()
        hb = worker.send_heartbeat()
        self.assertIn("timestamp", hb)

        # Verify currently online
        nodes_db = load_nodes_db()
        node_record = nodes_db["nodes"][worker.node_id]
        liveness = compute_node_liveness(node_record, timeout_seconds=3)
        self.assertEqual(liveness, STATE_ONLINE)

        # Deterministic simulation: backdate last_seen by 10 seconds
        node_record["last_seen"] = "2020-01-01T00:00:00Z"
        save_nodes_db(nodes_db)

        # Check API & Scheduler view
        status, nodes_resp = self._api_get("/nodes")
        self.assertEqual(status, 200)
        node_api = next((n for n in nodes_resp["nodes"] if n["node_id"] == worker.node_id), None)
        self.assertIsNotNone(node_api)
        self.assertEqual(node_api["status"], STATE_OFFLINE)

    # --------------------------------------------------------------------------
    # Scenario 4: Schedule Only to Eligible Workers
    # --------------------------------------------------------------------------
    def test_04_schedule_only_to_eligible_workers(self):
        """Verify ResourceScheduler filters by capabilities, roles, and hardware requirements."""
        cluster = SimulatedCluster(self.controller_url, self.enrollment_token)

        w_compute = cluster.add_worker("sched-comp", role="compute", capabilities={"compute": True, "storage": False})
        w_storage = cluster.add_worker("sched-stor", role="storage", capabilities={"compute": False, "storage": True})
        w_docker = cluster.add_worker("sched-dock", role="compute", capabilities={"compute": True, "container_runtime:docker": True})

        cluster.register_all()
        cluster.heartbeat_all()

        # Job A: Requires docker container runtime
        status_a, res_a = self._api_post(
            "/jobs",
            {
                "type": "docker-deploy",
                "target": "auto",
                "requirements": {"capabilities": ["container_runtime:docker"]},
                "parameters": {"app_id": "test-app", "image": "redis:alpine"}
            },
            token=self.enrollment_token
        )
        self.assertEqual(status_a, 200)
        # Selected node MUST be a docker-capable node and NEVER a non-docker node
        self.assertIn(res_a["target_node"], ["sim-node-sched-dock", "sim-node-gamma"])
        self.assertNotIn(res_a["target_node"], ["sim-node-sched-comp", "sim-node-sched-stor", "sim-node-alpha", "sim-node-beta"])

        # Job B: Explicit role storage requirement
        status_b, res_b = self._api_post(
            "/jobs",
            {
                "type": "system-info",
                "target": "auto",
                "requirements": {"role": "storage"}
            },
            token=self.enrollment_token
        )
        self.assertEqual(status_b, 200)
        self.assertIn(res_b["target_node"], ["sim-node-sched-stor", "sim-node-beta"])
        self.assertNotIn(res_b["target_node"], ["sim-node-sched-comp", "sim-node-sched-dock", "sim-node-alpha", "sim-node-gamma"])

    # --------------------------------------------------------------------------
    # Scenario 5: Verify That an Unavailable Worker Receives No New Work
    # --------------------------------------------------------------------------
    def test_05_unavailable_worker_receives_no_new_work(self):
        """Verify OFFLINE or DRAINING workers are rejected for new job assignment."""
        worker = SimulatedWorker(
            "unavail-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token
        )
        worker.register()

        # Simulate offline state
        nodes_db = load_nodes_db()
        nodes_db["nodes"][worker.node_id]["last_seen"] = "2020-01-01T00:00:00Z"
        save_nodes_db(nodes_db)

        # 1. Explicit target submission to offline node must be rejected
        status, res = self._api_post(
            "/jobs",
            {
                "type": "echo",
                "target": worker.node_id,
                "parameters": {"message": "Test"}
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 400)
        self.assertEqual(res["status"], JOB_STATE_REJECTED)
        self.assertIn("OFFLINE", res["error"])

        # 2. Auto scheduler must never select this node
        sched_res = ResourceScheduler.select_node(
            job_requirements={"capabilities": ["compute"]},
            nodes_db=load_nodes_db(),
            timeout_seconds=3
        )
        self.assertNotEqual(sched_res["selected_node"], worker.node_id)
        self.assertIn(worker.node_id, sched_res["rejected"])

    # --------------------------------------------------------------------------
    # Scenario 6: Simulate Execution Failure and Retry Lifecycle
    # --------------------------------------------------------------------------
    def test_06_simulate_execution_failure_and_retry(self):
        """Simulate task failure and verify incremental retry count up to max_attempts."""
        worker = SimulatedWorker(
            "retry-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token,
            behavior=WorkerBehavior.FAIL_EXECUTION
        )
        worker.register()
        worker.send_heartbeat()

        # Submit job with max_attempts: 2
        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "failing-test",
                "target": worker.node_id,
                "max_attempts": 2,
                "timeout": 10
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        # Attempt 1: Worker claims and fails
        step1 = worker.step_execute_job()
        self.assertIsNotNone(step1)

        # Job should now be in RECOVERING with attempt = 2
        _, job_after_att1 = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_after_att1["job"]["status"], JOB_STATE_RECOVERING)
        self.assertEqual(job_after_att1["job"]["attempt"], 2)

        # Attempt 2: Worker claims retry and fails again
        step2 = worker.step_execute_job()
        self.assertIsNotNone(step2)

        # Maximum attempts reached: Job transitions to terminal FAILED
        _, job_final = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_final["job"]["status"], JOB_STATE_FAILED)
        self.assertEqual(job_final["job"]["attempt"], 2)
        self.assertEqual(len(job_final["job"]["attempts_history"]), 2)

    # --------------------------------------------------------------------------
    # Scenario 7: Restart Controller and Recover Persisted Node/Job State
    # --------------------------------------------------------------------------
    def test_07_restart_controller_and_recover_persisted_state(self):
        """Simulate controller shutdown and reboot, verifying persisted DB state recovery."""
        worker = SimulatedWorker(
            "reboot-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token
        )
        worker.register()
        worker.send_heartbeat()

        status, job_sub = self._api_post(
            "/jobs",
            {"type": "echo", "target": worker.node_id, "parameters": {"message": "Survive reboot"}},
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = job_sub["job_id"]

        # 1. Stop current controller HTTP server
        self.httpd.shutdown()
        self.httpd.server_close()

        # 2. Start a fresh controller HTTP server against the identical persisted paths
        new_httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        new_port = new_httpd.server_address[1]
        new_url = f"http://127.0.0.1:{new_port}"
        new_thread = threading.Thread(target=new_httpd.serve_forever, daemon=True)
        new_thread.start()

        try:
            # Point worker to new controller port
            worker.controller_url = new_url

            # Verify persisted node still recognized and authenticated
            hb = worker.send_heartbeat()
            self.assertIn("timestamp", hb)

            # Verify job is still queued and claimable
            job = worker.poll_and_claim_job()
            self.assertIsNotNone(job)
            self.assertEqual(job["job_id"], job_id)

            # Complete job on new controller instance
            res = worker.submit_job_result(job_id, status=JOB_STATE_SUCCEEDED, stdout="Success post-reboot")
            self.assertIn("status", res)
        finally:
            # Restore class server for remaining tests
            new_httpd.shutdown()
            new_httpd.server_close()
            self.httpd = HTTPServer(("127.0.0.1", self.server_port), ControllerHandler)
            self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.server_thread.start()

    # --------------------------------------------------------------------------
    # Scenario 8: Job is NOT Silently Marked Successful After Worker Crash
    # --------------------------------------------------------------------------
    def test_08_job_not_silently_marked_successful_after_worker_crash(self):
        """Worker claims lease and crashes. Controller lease sweep detects failure and recovers."""
        worker = SimulatedWorker(
            "crash-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token,
            behavior=WorkerBehavior.CRASH_ON_JOB
        )
        worker.register()
        worker.send_heartbeat()

        status, submit_res = self._api_post(
            "/jobs",
            {
                "type": "timeout-test",
                "target": worker.node_id,
                "max_attempts": 1,
                "timeout": 5
            },
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        # Worker claims job and crashes immediately
        claimed = worker.poll_and_claim_job()
        self.assertIsNotNone(claimed)
        self.assertTrue(worker.crashed)

        # Worker stops sending heartbeats. Deterministically simulate offline worker
        nodes_db = load_nodes_db()
        nodes_db["nodes"][worker.node_id]["last_seen"] = "2020-01-01T00:00:00Z"
        save_nodes_db(nodes_db)

        # Trigger controller lease sweep
        jobs_db = load_jobs_db()
        sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=3)
        save_jobs_db(jobs_db)

        # Verify job is NEVER marked SUCCEEDED
        _, job_final = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        final_status = job_final["job"]["status"]
        self.assertNotEqual(final_status, JOB_STATE_SUCCEEDED)
        self.assertEqual(final_status, JOB_STATE_FAILED)
        self.assertIn("went OFFLINE", job_final["job"]["retry_reason"])

    # --------------------------------------------------------------------------
    # Scenario 9: Stale Worker Reports and Duplicate Job Completion
    # --------------------------------------------------------------------------
    def test_09_stale_worker_reports_and_duplicate_job_completion(self):
        """Verify idempotent handling of duplicate completion reports without state corruption."""
        worker = SimulatedWorker(
            "duplicate-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token,
            behavior=WorkerBehavior.DUPLICATE_COMPLETION
        )
        worker.register()
        worker.send_heartbeat()

        status, submit_res = self._api_post(
            "/jobs",
            {"type": "echo", "target": worker.node_id, "parameters": {"message": "Idempotency test"}},
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        # Worker claims and submits result (which triggers duplicate submission)
        step_res = worker.step_execute_job()
        self.assertIsNotNone(step_res)

        # Verify initial submission was accepted
        self.assertEqual(step_res.get("status"), "ok")

        # Verify duplicate report was handled idempotently
        self.assertIn("_duplicate_response", step_res)
        dup_resp = step_res["_duplicate_response"]
        self.assertTrue(dup_resp.get("duplicate"))
        self.assertIn("terminal state", dup_resp.get("message"))

        # Verify controller state remains SUCCEEDED and attempt history has not been duplicated
        _, job_check = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_check["job"]["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(len(job_check["job"]["attempts_history"]), 1)

    # --------------------------------------------------------------------------
    # Scenario 10: Cluster Recovery After Simulated Network Partition
    # --------------------------------------------------------------------------
    def test_10_cluster_recovery_after_simulated_network_partition(self):
        """Simulate network partition causing worker to go offline, then heal partition and resume work."""
        worker = SimulatedWorker(
            "partition-worker",
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token
        )
        worker.register()
        worker.send_heartbeat()

        # 1. Enact network partition
        worker.simulate_network_partition(True)

        with self.assertRaises(SimulatedNetworkError):
            worker.send_heartbeat()

        # Controller detects heartbeat timeout
        nodes_db = load_nodes_db()
        nodes_db["nodes"][worker.node_id]["last_seen"] = "2020-01-01T00:00:00Z"
        save_nodes_db(nodes_db)

        # Verify node is offline
        self.assertEqual(compute_node_liveness(nodes_db["nodes"][worker.node_id], timeout_seconds=3), STATE_OFFLINE)

        # 2. Heal network partition
        worker.simulate_network_partition(False)

        # Worker reconnects and sends heartbeat
        hb_res = worker.send_heartbeat()
        self.assertIn("timestamp", hb_res)

        # Verify node is back ONLINE
        nodes_db_fresh = load_nodes_db()
        self.assertEqual(compute_node_liveness(nodes_db_fresh["nodes"][worker.node_id], timeout_seconds=3), STATE_ONLINE)

        # Worker can now accept and execute new jobs
        status, submit_res = self._api_post(
            "/jobs",
            {"type": "echo", "target": worker.node_id, "parameters": {"message": "Healed!"}},
            token=self.enrollment_token
        )
        self.assertEqual(status, 200)
        job_id = submit_res["job_id"]

        exec_res = worker.step_execute_job()
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res.get("status"), "ok")

        _, job_final = self._api_get(f"/jobs/{job_id}", token=self.enrollment_token)
        self.assertEqual(job_final["job"]["status"], JOB_STATE_SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
