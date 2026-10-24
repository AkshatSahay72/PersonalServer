#!/usr/bin/env python3
"""
PersonalServer Deterministic Worker Simulator
=============================================
Simulates multi-node cluster workers at the protocol boundary (HTTP REST APIs)
for integration testing of scheduler logic, lease management, health monitoring,
node eligibility, authentication, retries, and network partitions.

Simulated workers operate strictly through real controller endpoints and are
never advertised as physical devices or endowed with real OS execution powers.
"""

import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from enum import Enum
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scheduler.scheduler import (
    STATE_ONLINE,
    STATE_OFFLINE,
    STATE_DRAINING,
    STATE_DEBOARDING,
    STATE_REMOVED
)
from controller.controller import (
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


class WorkerBehavior(str, Enum):
    """Configurable simulation modes for worker failure injection."""
    NORMAL = "normal"
    FAIL_EXECUTION = "fail_execution"
    CRASH_ON_JOB = "crash_on_job"
    DISCONNECT_BEFORE_ACCEPT = "disconnect_before_accept"
    TIMEOUT_EXECUTION = "timeout_execution"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    DUPLICATE_COMPLETION = "duplicate_completion"
    DELAYED_HEARTBEAT = "delayed_heartbeat"
    MISSING_HEARTBEAT = "missing_heartbeat"


class SimulatedWorkerError(Exception):
    """Base exception for simulated worker runtime and protocol errors."""
    pass


class SimulatedWorkerAuthError(SimulatedWorkerError):
    """Raised when authentication fails with controller."""
    pass


class SimulatedNetworkError(SimulatedWorkerError):
    """Raised when simulated network partition or connection drop occurs."""
    pass


class SimulatedWorker:
    """
    Deterministic simulated cluster node that interacts with PersonalServer
    controller via real HTTP APIs at the network protocol boundary.
    """

    def __init__(
        self,
        node_id: str,
        controller_url: str,
        enrollment_token: str = "",
        name: Optional[str] = None,
        role: str = "compute",
        architecture: str = "amd64",
        resources: Optional[Dict[str, Any]] = None,
        capabilities: Optional[Dict[str, Any]] = None,
        behavior: WorkerBehavior = WorkerBehavior.NORMAL
    ):
        # Guarantee logical node ID clearly marks worker as simulated
        if not (node_id.startswith("sim-node-") or node_id.startswith("sim-")):
            self.node_id = f"sim-node-{node_id}"
        else:
            self.node_id = node_id

        self.controller_url = controller_url.rstrip("/")
        self.enrollment_token = enrollment_token.strip()
        self.name = name or f"Simulated-{self.node_id}"
        self.role = role
        self.architecture = architecture
        self.platform = "simulated"
        self.os_name = "SimulatedOS"

        self.resources = resources or {
            "cpu_cores": 4,
            "ram_mb": 8192,
            "storage_gb": 100
        }
        self.capabilities = capabilities or {
            "compute": True,
            "storage": True,
            "network": True,
            "container_runtime:docker": False
        }

        self.behavior = behavior
        self.auth_token: Optional[str] = None
        self.registered: bool = False
        self.network_partitioned: bool = False
        self.crashed: bool = False

        self.heartbeat_count: int = 0
        self.active_jobs: Dict[str, Dict[str, Any]] = {}
        self.completed_jobs: List[Dict[str, Any]] = []
        self.last_seen_timestamp: Optional[str] = None

    def get_metadata(self) -> Dict[str, Any]:
        """Returns registration payload."""
        return {
            "node_id": self.node_id,
            "name": self.name,
            "role": self.role,
            "platform": self.platform,
            "os": self.os_name,
            "architecture": self.architecture,
            "cpu_cores": int(self.resources.get("cpu_cores", 4)),
            "ram_mb": int(self.resources.get("ram_mb", 8192)),
            "storage_gb": int(self.resources.get("storage_gb", 100)),
            "capabilities": dict(self.capabilities)
        }

    def _check_network(self):
        """Simulates network drop or partition."""
        if self.network_partitioned:
            raise SimulatedNetworkError(f"Network partition active on node '{self.node_id}'. Connection refused.")
        if self.crashed:
            raise SimulatedWorkerError(f"Node '{self.node_id}' is in CRASHED state. Host unreachable.")

    # --------------------------------------------------------------------------
    # Protocol Operations
    # --------------------------------------------------------------------------

    def register(self, force: bool = False, custom_token: Optional[str] = None) -> Dict[str, Any]:
        """Registers this simulated worker with the controller."""
        self._check_network()

        token_to_use = custom_token if custom_token is not None else self.enrollment_token
        metadata = self.get_metadata()
        payload = json.dumps(metadata).encode("utf-8")
        url = f"{self.controller_url}/register"

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PersonalServer-SimulatedWorker/1.0"
        }
        if token_to_use:
            headers["Authorization"] = f"Bearer {token_to_use}"

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.auth_token = data.get("auth_token")
                self.registered = True
                return data
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SimulatedWorkerAuthError(f"Registration unauthorized (HTTP 401): {body_err}")
            raise SimulatedWorkerError(f"Registration failed (HTTP {e.code}): {body_err}")
        except Exception as e:
            raise SimulatedNetworkError(f"Network failure during registration: {e}")

    def send_heartbeat(
        self,
        timestamp: Optional[str] = None,
        services: Optional[Dict[str, str]] = None,
        workloads: Optional[Dict[str, Any]] = None,
        custom_auth: Optional[str] = None
    ) -> Dict[str, Any]:
        """Sends liveness heartbeat to the controller."""
        self._check_network()

        token = custom_auth if custom_auth is not None else (self.auth_token or self.enrollment_token)
        if not token:
            raise SimulatedWorkerAuthError("Cannot send heartbeat: No authentication token available.")

        now_iso = timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.last_seen_timestamp = now_iso

        payload = {
            "node_id": self.node_id,
            "status": "online",
            "timestamp": now_iso,
            "capabilities": dict(self.capabilities),
            "resources": dict(self.resources),
            "services": services or {"simulated_worker": "running"},
            "workloads": workloads or {"active_jobs": len(self.active_jobs)},
            "system": {
                "cpu_cores": self.resources.get("cpu_cores", 4),
                "cpu_percent": 10.0,
                "memory": f"{self.resources.get('ram_mb', 8192)}M",
                "storage": f"{self.resources.get('storage_gb', 100)}G",
                "uptime": "simulated"
            }
        }

        url = f"{self.controller_url}/heartbeat"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-SimulatedWorker/1.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.heartbeat_count += 1
                return data
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SimulatedWorkerAuthError(f"Heartbeat unauthorized (HTTP 401): {body_err}")
            raise SimulatedWorkerError(f"Heartbeat failed (HTTP {e.code}): {body_err}")
        except Exception as e:
            raise SimulatedNetworkError(f"Network failure during heartbeat: {e}")

    def switch_controller(self, new_url: str):
        """Switches this worker's active controller URL to support HA failover testing."""
        self.controller_url = new_url.rstrip("/")

    def auto_failover(self, candidate_urls: Optional[List[str]] = None) -> bool:
        """Finds active leader and switches controller URL."""
        from config.ha_manager import find_active_leader
        urls = candidate_urls or getattr(self, "candidate_controller_urls", [self.controller_url])
        leader_url, info = find_active_leader(urls)
        if leader_url and leader_url != self.controller_url:
            self.switch_controller(leader_url)
            return True
        return False

    def poll_and_claim_job(self, custom_auth: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Polls controller for next pending job for this node with atomic lease claim.
        """
        self._check_network()

        if self.behavior == WorkerBehavior.DISCONNECT_BEFORE_ACCEPT:
            self.network_partitioned = True
            raise SimulatedNetworkError(f"Simulated failure: Node '{self.node_id}' disconnected before accepting job.")

        token = custom_auth if custom_auth is not None else (self.auth_token or self.enrollment_token)
        if not token:
            raise SimulatedWorkerAuthError("Cannot poll for jobs: No auth token.")

        url = f"{self.controller_url}/nodes/{self.node_id}/jobs/next"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-SimulatedWorker/1.0"
            },
            method="GET"
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                job = data.get("job")
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SimulatedWorkerAuthError(f"Job polling unauthorized (HTTP 401): {body_err}")
            raise SimulatedWorkerError(f"Job poll failed (HTTP {e.code}): {body_err}")
        except Exception as e:
            raise SimulatedNetworkError(f"Network failure during job poll: {e}")

        if not job:
            return None

        job_id = job.get("job_id") or job.get("id")
        self.active_jobs[job_id] = job

        # Simulation: Crash immediately after lease is acquired
        if self.behavior == WorkerBehavior.CRASH_ON_JOB:
            self.crashed = True
            # Worker crashed! Keeps lease held on controller without submitting result.
            return job

        return job

    def upload_artifact(
        self,
        job_id: str,
        filename: str,
        content_bytes: bytes,
        name: Optional[str] = None,
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        custom_auth: Optional[str] = None
    ) -> Dict[str, Any]:
        """Uploads an artifact to the controller with SHA-256 calculation."""
        self._check_network()
        import hashlib
        import base64
        token = custom_auth if custom_auth is not None else (self.auth_token or self.enrollment_token)
        sha256 = hashlib.sha256(content_bytes).hexdigest()
        b64_content = base64.b64encode(content_bytes).decode("ascii")

        payload = {
            "job_id": job_id,
            "filename": filename,
            "name": name or filename,
            "content_base64": b64_content,
            "sha256": sha256,
            "workflow_id": workflow_id,
            "metadata": {
                "step_id": step_id,
                "node_id": self.node_id
            }
        }

        url = f"{self.controller_url}/artifacts/upload"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-SimulatedWorker/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("artifact", data)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise SimulatedWorkerError(f"Artifact upload failed (HTTP {e.code}): {err_body}")

    def download_and_verify_artifact(
        self,
        artifact_id: str,
        expected_sha256: Optional[str] = None,
        custom_auth: Optional[str] = None
    ) -> bytes:
        """Downloads an artifact from controller and verifies SHA-256."""
        self._check_network()
        import hashlib
        token = custom_auth if custom_auth is not None else (self.auth_token or self.enrollment_token)
        url = f"{self.controller_url}/artifacts/{artifact_id}/download"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-SimulatedWorker/1.0"
            },
            method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise SimulatedWorkerError(f"Artifact download failed (HTTP {e.code}): {err_body}")

        if expected_sha256:
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha.lower() != expected_sha256.lower():
                raise ValueError(f"Checksum mismatch for artifact {artifact_id}: expected {expected_sha256}, got {actual_sha}")
        return content

    def submit_job_result(
        self,
        job_id: str,
        status: str = JOB_STATE_SUCCEEDED,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        duration_ms: int = 15,
        started_at: Optional[str] = None,
        custom_auth: Optional[str] = None,
        attempt: Optional[int] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Submits completed/failed job execution result to the controller."""
        self._check_network()

        token = custom_auth if custom_auth is not None else (self.auth_token or self.enrollment_token)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        att = attempt
        if att is None and job_id in self.active_jobs:
            att = self.active_jobs[job_id].get("attempt")

        result_payload = {
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "started_at": started_at or now_iso,
            "finished_at": now_iso
        }
        if att is not None:
            result_payload["attempt"] = att
        if artifacts:
            result_payload["artifacts"] = artifacts

        url = f"{self.controller_url}/jobs/{job_id}/result"
        req = urllib.request.Request(
            url,
            data=json.dumps(result_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-SimulatedWorker/1.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SimulatedWorkerAuthError(f"Job result unauthorized (HTTP 401): {body_err}")
            raise SimulatedWorkerError(f"Job result submission failed (HTTP {e.code}): {body_err}")
        except Exception as e:
            raise SimulatedNetworkError(f"Network failure submitting result: {e}")

        # Remove from active jobs and record completion
        self.active_jobs.pop(job_id, None)
        self.completed_jobs.append({
            "job_id": job_id,
            "result": result_payload,
            "controller_response": data
        })

        # Test mode: Duplicate status submission
        if self.behavior == WorkerBehavior.DUPLICATE_COMPLETION:
            # Re-submit identical result to test idempotency
            try:
                dup_req = urllib.request.Request(
                    url,
                    data=json.dumps(result_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "PersonalServer-SimulatedWorker/1.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(dup_req, timeout=5) as dup_resp:
                    dup_data = json.loads(dup_resp.read().decode("utf-8"))
                    data["_duplicate_response"] = dup_data
            except Exception as dup_err:
                data["_duplicate_error"] = str(dup_err)

        return data

    def step_execute_job(self) -> Optional[Dict[str, Any]]:
        """
        Deterministic, synchronous single step execution:
        Claims next available job, downloads input artifacts (with SHA-256 validation),
        applies configured simulated behavior, uploads output artifacts, and submits the outcome.
        """
        job = self.poll_and_claim_job()
        if not job:
            return None

        job_id = job.get("job_id") or job.get("id")

        if self.behavior == WorkerBehavior.CRASH_ON_JOB:
            # Already set crashed = True in poll_and_claim_job
            return {"job_id": job_id, "action": "crashed_during_execution"}

        if self.behavior == WorkerBehavior.FAIL_EXECUTION:
            return self.submit_job_result(
                job_id,
                status=JOB_STATE_FAILED,
                exit_code=1,
                stderr="Simulated worker execution failure."
            )

        if self.behavior == WorkerBehavior.TIMEOUT_EXECUTION:
            return self.submit_job_result(
                job_id,
                status=JOB_STATE_TIMEOUT,
                exit_code=-1,
                stderr="Simulated job execution timed out."
            )

        if self.behavior == WorkerBehavior.UNSUPPORTED_CAPABILITY:
            return self.submit_job_result(
                job_id,
                status=JOB_STATE_REJECTED,
                exit_code=1,
                stderr=f"Capability required for job {job_id} not supported by simulated node."
            )

        # 1. Download and verify input artifacts if declared
        input_artifacts = job.get("input_artifacts") or []
        for in_art in input_artifacts:
            art_id = in_art.get("artifact_id")
            exp_sha = in_art.get("sha256")
            try:
                self.download_and_verify_artifact(art_id, expected_sha256=exp_sha)
            except Exception as e:
                return self.submit_job_result(
                    job_id,
                    status=JOB_STATE_FAILED,
                    exit_code=1,
                    stderr=f"Input artifact '{in_art.get('name')}' verification failed: {e}"
                )

        # 2. Upload declared output artifacts if specified in job parameters
        wf_id = job.get("workflow_id")
        step_id = job.get("workflow_step_id")
        declared_outputs = job.get("outputs") or job.get("parameters", {}).get("outputs") or job.get("parameters", {}).get("artifacts") or []
        produced_artifacts = []

        for out in declared_outputs:
            if isinstance(out, str):
                out_name = out
                out_fn = out
            elif isinstance(out, dict):
                out_name = out.get("name") or out.get("filename") or "output.dat"
                out_fn = out.get("filename") or out_name
            else:
                continue

            content = f"Simulated output from {self.node_id} for step {step_id or job_id} at {time.time()}".encode("utf-8")
            try:
                uploaded = self.upload_artifact(
                    job_id=job_id,
                    filename=out_fn,
                    content_bytes=content,
                    name=out_name,
                    workflow_id=wf_id,
                    step_id=step_id
                )
                produced_artifacts.append(uploaded)
            except Exception as e:
                return self.submit_job_result(
                    job_id,
                    status=JOB_STATE_FAILED,
                    exit_code=1,
                    stderr=f"Failed to upload simulated artifact '{out_fn}': {e}"
                )

        # Default: Normal success
        return self.submit_job_result(
            job_id,
            status=JOB_STATE_SUCCEEDED,
            exit_code=0,
            stdout=f"Simulated execution succeeded on node {self.node_id}.",
            artifacts=produced_artifacts if produced_artifacts else None
        )

    # --------------------------------------------------------------------------
    # Failure Injection & Recovery Controls
    # --------------------------------------------------------------------------

    def simulate_network_partition(self, partitioned: bool = True):
        """Simulates network disconnect or packet loss."""
        self.network_partitioned = partitioned

    def simulate_crash(self):
        """Simulates immediate worker process kill or unhandled crash."""
        self.crashed = True

    def simulate_recover(self):
        """Heals worker, restoring connectivity and operational state."""
        self.crashed = False
        self.network_partitioned = False


class SimulatedCluster:
    """
    Orchestrates multiple SimulatedWorker nodes to test complex cluster topology,
    resource scheduling, partitioning, and concurrent failure scenarios.
    """

    def __init__(self, controller_url: str, enrollment_token: str):
        self.controller_url = controller_url.rstrip("/")
        self.enrollment_token = enrollment_token
        self.workers: Dict[str, SimulatedWorker] = {}

    def add_worker(
        self,
        node_id: str,
        name: Optional[str] = None,
        role: str = "compute",
        architecture: str = "amd64",
        resources: Optional[Dict[str, Any]] = None,
        capabilities: Optional[Dict[str, Any]] = None,
        behavior: WorkerBehavior = WorkerBehavior.NORMAL
    ) -> SimulatedWorker:
        """Instantiates and registers a simulated worker in the cluster."""
        worker = SimulatedWorker(
            node_id=node_id,
            controller_url=self.controller_url,
            enrollment_token=self.enrollment_token,
            name=name,
            role=role,
            architecture=architecture,
            resources=resources,
            capabilities=capabilities,
            behavior=behavior
        )
        self.workers[worker.node_id] = worker
        return worker

    def get_worker(self, node_id: str) -> Optional[SimulatedWorker]:
        """Looks up worker by canonical or short node_id."""
        if node_id in self.workers:
            return self.workers[node_id]
        canon = f"sim-node-{node_id}"
        return self.workers.get(canon)

    def register_all(self) -> Dict[str, Dict[str, Any]]:
        """Registers all workers in the simulated cluster."""
        results = {}
        for nid, worker in self.workers.items():
            results[nid] = worker.register()
        return results

    def heartbeat_all(self, timestamp: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Broadcasts heartbeats from all active workers."""
        results = {}
        for nid, worker in self.workers.items():
            if not worker.crashed and not worker.network_partitioned:
                results[nid] = worker.send_heartbeat(timestamp=timestamp)
        return results

    def step_all(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """Polls and executes one job per worker in round-robin fashion."""
        results = {}
        for nid, worker in self.workers.items():
            if not worker.crashed and not worker.network_partitioned:
                results[nid] = worker.step_execute_job()
        return results

    def failover_all(self, new_controller_url: str):
        """Switches all simulated workers in the cluster to a new active controller."""
        for worker in self.workers.values():
            worker.switch_controller(new_controller_url)
