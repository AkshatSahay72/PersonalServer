#!/usr/bin/env python3
"""
PersonalServer LocalWorker for Windows Development & Local Execution
====================================================================
Represents the local PC as a native, fully-compatible cluster worker node.
Features:
- Stable logical node ID and hardware/OS capability advertisement.
- Authenticated registration and heartbeat reporting via existing controller APIs.
- Allowlisted job execution via JobExecutor with strict timeout and output bounds.
- Graceful shutdown, cancellation handling, and secret redaction.
- Programmatic and CLI service lifecycle (start, stop, work, heartbeat, register).
"""

import sys
import os
import re
import json
import time
import socket
import platform
import secrets
import threading
import hashlib
import shutil
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Set, Tuple, List

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from common.arch_utils import get_canonical_architecture
from common.artifact_manager import validate_artifact_path, MAX_ARTIFACT_SIZE_BYTES
from config.platform_config import get_controller_paths, redact_sensitive_dict
from agent.job_executor import (
    JobExecutor,
    ALLOWLISTED_WORKLOADS,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_CANCELLED,
    JOB_STATE_REJECTED,
    detect_container_runtime,
    is_docker_daemon_ready
)
from agent.capability_prober import run_capability_probes


class LocalWorkerError(Exception):
    """Base exception for LocalWorker errors."""
    pass


class LocalWorkerAuthError(LocalWorkerError):
    """Authentication or authorization failure with controller."""
    pass


class LocalWorkerConfigError(LocalWorkerError):
    """Invalid or missing worker configuration."""
    pass


# Base safe workloads executable natively on Windows
BASE_SUPPORTED_WORKLOADS: Set[str] = {
    "echo",
    "system-info",
    "health-check",
    "node-status",
    "python-script",
    "failing-test",
    "timeout-test",
    "python",
    "nodejs",
    "docker",
    "shell",
    "universal"
}

BUILD_WORKLOADS: Set[str] = {
    "build-package",
    "build-test",
    "deploy-prepare",
    "app-deploy"
}

DOCKER_WORKLOADS: Set[str] = {
    "image-build",
    "image-tag",
    "image-inspect",
    "docker-deploy",
    "docker-build-deploy",
    "docker-stop",
    "docker-restart",
    "docker-remove",
    "docker-logs"
}




def probe_hardware_resources(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Probes real physical memory, CPU cores, and storage on the local PC.
    Uses Win32 API on Windows and /proc on Linux without third-party dependencies.
    """
    base_dir = base_dir or BASE_DIR
    cpu_cores = os.cpu_count() or 1
    ram_mb = 0

    # 1. Windows memory probing via GlobalMemoryStatusEx
    if sys.platform == "win32":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                ram_mb = int(round(stat.ullTotalPhys / (1024 * 1024)))
        except Exception:
            pass

    # 2. Linux /proc/meminfo fallback
    if ram_mb <= 0 and Path("/proc/meminfo").exists():
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            ram_mb = int(round(int(parts[1]) / 1024))
                        break
        except Exception:
            pass

    if ram_mb <= 0:
        ram_mb = 4096

    # 3. Disk storage
    import shutil
    storage_gb = 32
    try:
        check_path = str(base_dir) if base_dir.exists() else str(Path.home())
        total_b, _, _ = shutil.disk_usage(check_path)
        storage_gb = max(1, int(round(total_b / (1024 ** 3))))
    except Exception:
        pass

    return {
        "cpu_cores": cpu_cores,
        "ram_mb": ram_mb,
        "storage_gb": storage_gb
    }


class LocalWorker:
    """
    PersonalServer Local Worker Node.
    Registers with controller, streams heartbeats, and polls/executes assigned jobs.
    """

    def __init__(
        self,
        controller_url: Optional[str] = None,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
        role: Optional[str] = None,
        enrollment_token: Optional[str] = None,
        config_dir: Optional[Path] = None,
        runtime_dir: Optional[Path] = None,
        heartbeat_interval: float = 15.0,
        poll_interval: float = 2.0
    ):
        paths = get_controller_paths()
        self.config_dir = (config_dir or paths.config_dir).resolve()
        self.runtime_dir = (runtime_dir or paths.runtime_dir).resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.node_file = self.config_dir / "node.json"
        self.node_worker_file = self.config_dir / "node_worker.json"
        self.registration_file = self.runtime_dir / "registration.json"
        self.pid_file = self.runtime_dir / "node-worker.pid"
        self.control_file = self.runtime_dir / "node-worker.control"
        self.state_file = self.runtime_dir / "node-worker.state"

        # Role resolution (explicit argument -> node_worker.json -> "build")
        resolved_role = role
        if not resolved_role and self.node_worker_file.exists():
            try:
                nw_data = json.loads(self.node_worker_file.read_text(encoding="utf-8"))
                stored_role = nw_data.get("role")
                if stored_role and str(stored_role).strip() not in ("unknown", ""):
                    resolved_role = str(stored_role).strip()
            except Exception:
                pass
        self.role = resolved_role or "build"

        # Determine stable node ID
        self.node_id = self._resolve_stable_node_id(node_id)
        self.node_name = node_name or socket.gethostname() or "windows-local"

        # Controller URL resolution (env -> controller.json -> default)
        cfg_url = None
        ctrl_json = self.config_dir / "controller.json"
        if ctrl_json.exists():
            try:
                c_data = json.loads(ctrl_json.read_text(encoding="utf-8"))
                cfg_url = c_data.get("url")
            except Exception:
                pass

        self.controller_url = (
            controller_url
            or os.environ.get("PERSONALSERVER_CONTROLLER_URL")
            or cfg_url
            or "http://127.0.0.1:8000"
        ).rstrip("/")

        # HA candidate controllers
        env_candidates = os.environ.get("PERSONALSERVER_CONTROLLER_URLS", "")
        self.candidate_controller_urls = [
            u.strip().rstrip("/") for u in env_candidates.split(",") if u.strip()
        ]
        if self.controller_url not in self.candidate_controller_urls:
            self.candidate_controller_urls.append(self.controller_url)

        # Enrollment token resolution
        self.enrollment_token = (
            enrollment_token
            or os.environ.get("PERSONALSERVER_ENROLLMENT_TOKEN")
            or self._read_enrollment_token()
        )

        self.auth_token: Optional[str] = None
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval

        # Capability detection & verification probes
        self.advertised_capabilities = self._detect_capabilities()
        probe_data = run_capability_probes(self.advertised_capabilities)
        self.capabilities = probe_data["advertised_capabilities"]
        self.verified_capabilities = probe_data["verified_capabilities"]
        self.probe_results = probe_data["probe_results"]
        self.supported_workloads = set(BASE_SUPPORTED_WORKLOADS)
        if self.verified_capabilities.get("build") or self.capabilities.get("build"):
            self.supported_workloads.update(BUILD_WORKLOADS)
        if self.verified_capabilities.get("container_runtime:docker") or self.capabilities.get("container_runtime:docker"):
            self.supported_workloads.update(DOCKER_WORKLOADS)

        # Background thread control
        self._stop_event = threading.Event()
        self._drain_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._polling_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._draining = False
        self._current_process = None
        self._lock = threading.RLock()

        # Load existing registration state if present
        self._load_stored_registration()

        # Cleanup any orphan child processes from prior runs (J4)
        try:
            from agent.universal_executor import UniversalExecutionEngine
            UniversalExecutionEngine.cleanup_stale_processes()
        except Exception:
            pass

    def _resolve_stable_node_id(self, preferred_id: Optional[str]) -> str:
        """Resolves a stable logical node ID, reading from node_worker.json/node.json or generating a new one."""
        if preferred_id:
            return preferred_id.strip()

        env_id = os.environ.get("PERSONALSERVER_NODE_ID")
        if env_id:
            return env_id.strip()

        # Check dedicated worker node file first
        if self.node_worker_file.exists():
            try:
                data = json.loads(self.node_worker_file.read_text(encoding="utf-8"))
                stored_id = data.get("node_id")
                if stored_id and str(stored_id).strip() not in ("unknown", ""):
                    return stored_id.strip()
            except Exception:
                pass

        # Check node.json, but protect against hijacking phone controller identity on Windows
        if self.node_file.exists():
            try:
                data = json.loads(self.node_file.read_text(encoding="utf-8"))
                stored_id = data.get("node_id")
                # Do not reuse controller's node_id on Windows development PC
                if stored_id and str(stored_id).strip() not in ("unknown", "", "server-5387a86bf36116b1"):
                    return stored_id.strip()
            except Exception:
                pass

        # Generate a stable logical ID based on machine uuid or hex token
        import uuid
        stable_id = f"server-{secrets.token_hex(8)}"
        try:
            mac = uuid.getnode()
            if (mac >> 40) % 2 == 0:  # Valid non-random MAC
                mac_hex = f"{mac:012x}"
                stable_id = f"server-win-{mac_hex[:12]}"
        except Exception:
            pass

        # Persist to node_worker_file for stability
        try:
            self.node_worker_file.write_text(json.dumps({
                "node_id": stable_id,
                "name": socket.gethostname() or "windows-laptop",
                "role": getattr(self, "role", "build")
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

        return stable_id

    def _read_enrollment_token(self) -> Optional[str]:
        paths = get_controller_paths()
        token_f = paths.enrollment_token_file
        if token_f.exists():
            try:
                t = token_f.read_text(encoding="utf-8").strip()
                if t:
                    return t
            except Exception:
                pass
        return None

    def _detect_capabilities(self) -> Dict[str, bool]:
        """
        Probes real capabilities on this node.
        Build and deployment execution node profile:
        - Strictly honesty: no Linux/udocker on Windows.
        - EXPLICITLY EXCLUDES STORAGE: The laptop worker must NOT advertise storage
          or act as an artifact/cluster repository.
        - Advertises build toolchain: python3, git, build.
        - Advertises container runtime only when verified ready.
        """
        caps = {
            "compute": True,
            "storage": False,
            "network": True,
            "build": True,
            "python3": True
        }
        if shutil.which("git"):
            caps["git"] = True
        if is_docker_daemon_ready():
            caps["container_runtime:docker"] = True
        return caps

    def get_node_metadata(self) -> Dict[str, Any]:
        """Constructs the worker's node registration and telemetry payload."""
        hw = probe_hardware_resources()
        return {
            "node_id": self.node_id,
            "name": self.node_name,
            "role": self.role,
            "platform": sys.platform,
            "os": platform.system() or "Windows",
            "architecture": get_canonical_architecture(),
            "cpu_cores": hw["cpu_cores"],
            "ram_mb": hw["ram_mb"],
            "storage_gb": hw["storage_gb"],
            "capabilities": dict(self.capabilities),
            "advertised_capabilities": dict(self.advertised_capabilities),
            "verified_capabilities": dict(self.verified_capabilities),
            "probe_results": dict(self.probe_results),
            "supported_workloads": sorted(list(self.supported_workloads))
        }

    def _load_stored_registration(self):
        """Loads cached registration token if available."""
        if self.registration_file.exists():
            try:
                data = json.loads(self.registration_file.read_text(encoding="utf-8"))
                if data.get("node_id") == self.node_id:
                    self.auth_token = data.get("auth_token")
            except Exception:
                pass

    def _save_registration(self, auth_token: str):
        """Persists registration state to runtime storage."""
        self.auth_token = auth_token
        state = {
            "registered": True,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "controller_url": self.controller_url,
            "auth_token": auth_token,
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        try:
            self.registration_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Registration & Controller Communication
    # --------------------------------------------------------------------------

    def register(self, force: bool = False) -> Dict[str, Any]:
        """Registers this LocalWorker with the PersonalServer controller."""
        with self._lock:
            if not force and self.auth_token:
                # Test if registration is still recognized by sending a heartbeat
                try:
                    self.send_heartbeat()
                    return {"status": "registered", "node_id": self.node_id, "auth_token": self.auth_token}
                except LocalWorkerAuthError:
                    pass  # Re-registration required

            if not self.enrollment_token:
                raise LocalWorkerConfigError(
                    "Missing enrollment token. Please provide enrollment_token or ensure config/secrets/enrollment.token exists."
                )

            metadata = self.get_node_metadata()
            payload = json.dumps(metadata).encode("utf-8")
            url = f"{self.controller_url}/register"
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.enrollment_token}",
                    "User-Agent": "PersonalServer-LocalWorker/1.0"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    new_token = data.get("auth_token")
                    if not new_token:
                        raise LocalWorkerAuthError(f"Registration succeeded but controller returned no auth token: {data}")
                    self._save_registration(new_token)
                    return data
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                if e.code == 401:
                    raise LocalWorkerAuthError(f"Unauthorized: Controller rejected enrollment token (HTTP 401): {err_text}")
                raise LocalWorkerError(f"Registration failed with HTTP {e.code}: {err_text}")
            except Exception as e:
                raise LocalWorkerError(f"Failed to connect to controller at {self.controller_url}: {e}")

    def _failover_active_controller(self) -> bool:
        """Attempts to discover active cluster leader from candidates when primary fails."""
        try:
            from config.ha_manager import find_active_leader
            active_url, leader_info = find_active_leader(self.candidate_controller_urls, exclude_url=self.controller_url)
            if active_url:
                old_url = self.controller_url
                self.controller_url = active_url
                agent_logger.info(
                    "LocalWorker HA failover: switched controller from %s to %s (Term: %s)",
                    old_url, active_url, (leader_info or {}).get("term", "?")
                )
                return True
        except Exception:
            pass
        return False

    def send_heartbeat(self) -> Dict[str, Any]:
        """Sends liveness heartbeat and telemetry to the controller."""
        with self._lock:
            token = self.auth_token or self.enrollment_token
            if not token:
                raise LocalWorkerAuthError("Cannot send heartbeat: LocalWorker is not registered (no auth token).")

            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            hw = probe_hardware_resources()

            # Periodically refresh capability verification probes
            try:
                probe_data = run_capability_probes(self.advertised_capabilities)
                self.verified_capabilities = probe_data["verified_capabilities"]
                self.probe_results = probe_data["probe_results"]
            except Exception:
                pass

            payload = {
                "node_id": self.node_id,
                "status": "online",
                "timestamp": now_iso,
                "capabilities": dict(self.capabilities),
                "advertised_capabilities": dict(self.advertised_capabilities),
                "verified_capabilities": dict(self.verified_capabilities),
                "probe_results": dict(self.probe_results),
                "services": {"local_worker": "running"},
                "workloads": {"active_jobs": 0},
                "system": {
                    "cpu_cores": hw["cpu_cores"],
                    "cpu_percent": 0.0,
                    "memory": f"{hw['ram_mb']}M",
                    "storage": f"{hw['storage_gb']}G",
                    "uptime": "N/A"
                }
            }

            url = f"{self.controller_url}/heartbeat"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PersonalServer-LocalWorker/1.0"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                if e.code == 401:
                    raise LocalWorkerAuthError(f"Heartbeat unauthorized (HTTP 401): {err_text}")
                raise LocalWorkerError(f"Heartbeat failed with HTTP {e.code}: {err_text}")
            except Exception as e:
                if self._failover_active_controller():
                    try:
                        retry_url = f"{self.controller_url}/heartbeat"
                        retry_req = urllib.request.Request(
                            retry_url,
                            data=json.dumps(payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {token}",
                                "User-Agent": "PersonalServer-LocalWorker/1.0"
                            },
                            method="POST"
                        )
                        with urllib.request.urlopen(retry_req, timeout=8) as resp:
                            return json.loads(resp.read().decode("utf-8"))
                    except Exception as retry_err:
                        raise LocalWorkerError(f"Heartbeat failover retry error: {retry_err}")
                raise LocalWorkerError(f"Heartbeat connection error: {e}")

    # --------------------------------------------------------------------------
    # Job Polling & Execution
    # --------------------------------------------------------------------------

    def poll_and_execute_next(self) -> Optional[Dict[str, Any]]:
        """
        Polls controller for the next assigned job for this node, executes it,
        and posts the completed result back to the controller.
        Returns the completed result dictionary, or None if no job was waiting.
        """
        if self._draining:
            return None

        with self._lock:
            token = self.auth_token or self.enrollment_token
            if not token:
                raise LocalWorkerAuthError("Cannot poll for jobs: worker not registered.")

            fetch_url = f"{self.controller_url}/nodes/{self.node_id}/jobs/next"
            req = urllib.request.Request(
                fetch_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PersonalServer-LocalWorker/1.0"
                }
            )

            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    job = data.get("job")
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise LocalWorkerAuthError(f"Job polling unauthorized (HTTP 401): {e.reason}")
                raise LocalWorkerError(f"Job poll HTTP error {e.code}: {e.reason}")
            except Exception as e:
                if self._failover_active_controller():
                    try:
                        retry_fetch_url = f"{self.controller_url}/nodes/{self.node_id}/jobs/next"
                        retry_req = urllib.request.Request(
                            retry_fetch_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "User-Agent": "PersonalServer-LocalWorker/1.0"
                            }
                        )
                        with urllib.request.urlopen(retry_req, timeout=5) as resp:
                            data = json.loads(resp.read().decode("utf-8"))
                            job = data.get("job")
                    except Exception:
                        raise LocalWorkerError(f"Job poll connection error: {e}")
                else:
                    raise LocalWorkerError(f"Job poll connection error: {e}")

            if not job:
                return None

            job_id = job.get("job_id")
            job_type = job.get("type", "")
            attempt = int(job.get("attempt", 1))
            lease_gen = int(job.get("lease_generation") or 1)

            is_ujc = (
                job.get("schema_version") == "ujc.v1.0"
                or "spec" in job
                or job.get("runtime") in ("python", "nodejs", "docker", "shell", "native/python", "native/nodejs", "container/docker")
                or job_type in ("python", "nodejs", "docker", "shell", "universal")
            )

            # 1. Capability & Workload Validation: Verify workload is explicitly supported
            if not is_ujc and job_type not in self.supported_workloads:
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                err_result = {
                    "job_id": job_id,
                    "status": JOB_STATE_REJECTED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": (
                        f"Error: Workload type '{job_type}' is not supported by LocalWorker on {platform.system()} "
                        f"(Supported: {sorted(list(self.supported_workloads))})"
                    ),
                    "duration_ms": 0,
                    "started_at": now_iso,
                    "finished_at": now_iso
                }
                self._submit_job_result(job_id, err_result, token)
                return err_result

            # 2. Attempt Workspace Isolation (J4 §2)
            from agent.universal_executor import UniversalExecutionEngine
            try:
                sandbox_dir = UniversalExecutionEngine.get_attempt_workspace(job_id, attempt)
            except Exception:
                jobs_runtime_dir = self.runtime_dir / "jobs"
                sandbox_dir = (jobs_runtime_dir / job_id / "attempts" / str(attempt)).resolve()
                sandbox_dir.mkdir(parents=True, exist_ok=True)

            # Acknowledge assignment: notify controller of RUNNING status (J3.3 / J4: activates reservation)
            self._update_job_status(job_id, JOB_STATE_RUNNING, token, attempt=attempt, lease_gen=lease_gen)

            # 3. Input artifacts downloading
            input_artifacts = job.get("input_artifacts") or []
            if input_artifacts:
                dl_ok, dl_err = self._download_input_artifacts(input_artifacts, sandbox_dir, token)
                if not dl_ok:
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    fail_result = {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Input artifact resolution error: {dl_err}",
                        "duration_ms": 0,
                        "started_at": now_iso,
                        "finished_at": now_iso
                    }
                    self._submit_job_result(job_id, fail_result, token)
                    return fail_result

            # 4. Workload Execution via JobExecutor / UniversalExecutionEngine with cooperative cancellation watcher
            stop_cancel_watcher = threading.Event()

            def _watch_cancellation():
                check_url = f"{self.controller_url}/jobs/{job_id}"
                last_hb = time.time()
                while not stop_cancel_watcher.wait(timeout=1.0):
                    now_w = time.time()
                    try:
                        req = urllib.request.Request(
                            check_url,
                            headers={"Authorization": f"Bearer {token}", "User-Agent": "PersonalServer-LocalWorker/1.0"}
                        )
                        with urllib.request.urlopen(req, timeout=1.5) as resp:
                            j_data = json.loads(resp.read().decode("utf-8"))
                            curr_st = (j_data.get("job") or {}).get("status")
                            if curr_st == JOB_STATE_CANCELLED or (j_data.get("job") or {}).get("cancellation_requested"):
                                UniversalExecutionEngine.cancel_execution(job_id, reason="Cancelled via controller request")
                                JobExecutor.terminate_active_job(job_id, reason="Cancelled via controller request")
                                break
                    except Exception:
                        pass
                    # Heartbeat renewal every 15s keeps 45s execution lease active during long runs
                    if (now_w - last_hb) >= 15.0:
                        try:
                            self._update_job_status(job_id, JOB_STATE_RUNNING, token, attempt=attempt, lease_gen=lease_gen)
                            last_hb = now_w
                        except Exception:
                            pass

            watcher_thread = threading.Thread(target=_watch_cancellation, daemon=True)
            watcher_thread.start()
            try:
                exec_result = JobExecutor.execute(job, sandbox_dir=sandbox_dir)
            finally:
                stop_cancel_watcher.set()
                watcher_thread.join(timeout=0.2)

            # 5. Output artifact publication to authoritative controller artifact store
            if exec_result.get("status") == JOB_STATE_SUCCEEDED and exec_result.get("declared_outputs"):
                up_ok, uploaded_arts, up_err = self._upload_output_artifacts(exec_result["declared_outputs"], job, token)
                if not up_ok:
                    exec_result["status"] = JOB_STATE_FAILED
                    exec_result["exit_code"] = 1
                    exec_result["stderr"] = f"Output artifact publication error: {up_err}"
                else:
                    exec_result["artifacts"] = uploaded_arts

            # 5. Post result back to controller
            self._submit_job_result(job_id, exec_result, token)
            return exec_result

    def _download_input_artifacts(self, input_artifacts: list, sandbox_dir: Path, token: str) -> Tuple[bool, Optional[str]]:
        """Downloads required input artifacts from controller into the sandbox with streaming SHA-256 validation."""
        for art in input_artifacts:
            art_id = art.get("artifact_id")
            raw_dest_name = art.get("name") or art.get("filename")
            expected_sha = art.get("sha256")
            if not art_id or not raw_dest_name:
                return False, "Input artifact missing artifact_id or name/filename."
            try:
                dest_path = validate_artifact_path(sandbox_dir, raw_dest_name)
            except Exception as ve:
                return False, f"Invalid destination path for artifact '{raw_dest_name}': {ve}"

            download_url = f"{self.controller_url}/artifacts/{art_id}/download"
            req = urllib.request.Request(
                download_url,
                headers={"Authorization": f"Bearer {token}", "User-Agent": "PersonalServer-LocalWorker/1.0"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    with open(dest_path, "wb") as f:
                        hasher = hashlib.sha256()
                        bytes_read = 0
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            hasher.update(chunk)
                            bytes_read += len(chunk)
                            if bytes_read > MAX_ARTIFACT_SIZE_BYTES:
                                raise ValueError(f"Artifact exceeded maximum size limit of {MAX_ARTIFACT_SIZE_BYTES} bytes.")
                        actual_sha = hasher.hexdigest()
                        if expected_sha and actual_sha != expected_sha:
                            return False, f"Checksum mismatch for artifact '{raw_dest_name}': expected {expected_sha}, got {actual_sha}."
            except Exception as e:
                return False, f"Failed to download input artifact '{raw_dest_name}': {e}"
        return True, None

    def _upload_output_artifacts(self, declared_outputs: list, job: dict, token: str) -> Tuple[bool, list, Optional[str]]:
        """Streams output artifacts produced by the job to controller via /artifacts/upload/raw."""
        uploaded_records = []
        job_id = job.get("job_id", "")
        workflow_id = job.get("workflow_id", "")
        step_id = job.get("workflow_step_id", "")

        for out in declared_outputs:
            filename = out.get("filename")
            name = out.get("name") or filename
            filepath = Path(out.get("path"))
            sha256 = out.get("sha256")
            size_bytes = out.get("size_bytes", 0)

            if not filepath.exists() or not filepath.is_file():
                return False, uploaded_records, f"Output artifact file '{filepath}' missing on disk."

            query_str = urllib.parse.urlencode({
                "filename": filename,
                "name": name,
                "sha256": sha256,
                "job_id": job_id,
                "workflow_id": workflow_id,
                "step_id": step_id
            })
            upload_url = f"{self.controller_url}/artifacts/upload/raw?{query_str}"

            try:
                with open(filepath, "rb") as f:
                    file_bytes = f.read()

                req = urllib.request.Request(
                    upload_url,
                    data=file_bytes,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size_bytes),
                        "X-Artifact-Filename": filename,
                        "X-Artifact-Name": name,
                        "X-Artifact-Sha256": sha256,
                        "X-Job-Id": job_id,
                        "X-Workflow-Id": workflow_id,
                        "X-Workflow-Step-Id": step_id,
                        "User-Agent": "PersonalServer-LocalWorker/1.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    art_rec = resp_data.get("artifact")
                    if art_rec:
                        uploaded_records.append(art_rec)
            except Exception as e:
                return False, uploaded_records, f"Failed to upload artifact '{filename}': {e}"

        return True, uploaded_records, None

    def _update_job_status(self, job_id: str, status: str, token: str, attempt: int = 1, lease_gen: int = 1):
        """Notifies controller of a job status transition (e.g. RUNNING acknowledgment / lease renewal)."""
        status_url = f"{self.controller_url}/jobs/{job_id}/status"
        payload = {
            "status": status,
            "attempt": attempt,
            "lease_generation": lease_gen,
            "fencing_token": lease_gen
        }
        req = urllib.request.Request(
            status_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-LocalWorker/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception:
            pass

    def _submit_job_result(self, job_id: str, result_dict: Dict[str, Any], token: str):
        """Submits the completed or failed job execution result to the controller."""
        # Redact any accidental secrets from the result payload
        safe_result = redact_sensitive_dict(result_dict)

        result_url = f"{self.controller_url}/jobs/{job_id}/result"
        req = urllib.request.Request(
            result_url,
            data=json.dumps(safe_result).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "PersonalServer-LocalWorker/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise LocalWorkerError(f"Failed to submit result for job {job_id} (HTTP {e.code}): {e.reason}")
        except Exception as e:
            if self._failover_active_controller():
                try:
                    retry_result_url = f"{self.controller_url}/jobs/{job_id}/result"
                    retry_req = urllib.request.Request(
                        retry_result_url,
                        data=json.dumps(safe_result).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {token}",
                            "User-Agent": "PersonalServer-LocalWorker/1.0"
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(retry_req, timeout=10) as resp:
                        resp.read()
                        return
                except Exception:
                    pass
            raise LocalWorkerError(f"Connection error submitting result for job {job_id}: {e}")

    # --------------------------------------------------------------------------
    # Lifecycle Threads
    # --------------------------------------------------------------------------

    def start(self):
        """Starts background heartbeat and job polling threads."""
        with self._lock:
            if self._is_running:
                return

            self.register()
            self._stop_event.clear()
            self._is_running = True

            self._heartbeat_thread = threading.Thread(target=self._run_heartbeat_loop, daemon=True, name="LocalWorker-Heartbeat")
            self._polling_thread = threading.Thread(target=self._run_polling_loop, daemon=True, name="LocalWorker-Polling")

            self._heartbeat_thread.start()
            self._polling_thread.start()

    def is_draining(self) -> bool:
        """Returns True if the worker is currently draining and rejecting new work."""
        return self._draining

    def check_control_signal(self) -> Optional[Dict[str, Any]]:
        """Checks for external IPC control signals (drain / stop)."""
        if not self.control_file.exists():
            return None
        try:
            raw = self.control_file.read_text(encoding="utf-8").strip()
            data = json.loads(raw) if raw else {}
            action = data.get("action")
            if action == "drain":
                grace = float(data.get("grace_period", 15.0))
                threading.Thread(target=self.drain, args=(grace,), daemon=True, name="LocalWorker-Drain").start()
                return data
            elif action == "stop":
                threading.Thread(target=self.stop, daemon=True, name="LocalWorker-Stop").start()
                return data
        except Exception:
            pass
        return None

    def drain(self, grace_period: float = 15.0) -> bool:
        """
        Drains the worker safely:
        1. Immediately halts claiming new jobs.
        2. Updates state to DRAINING.
        3. Waits for active jobs to finish within grace_period.
        4. If active jobs exceed grace_period, terminates them cooperatively so controller lease recovery handles them.
        5. Stops worker threads cleanly.
        """
        with self._lock:
            if self._draining or not self._is_running:
                return False
            self._draining = True
            self._drain_event.set()

        self._write_state("DRAINING")

        # Wait for active jobs to finish up to grace_period
        deadline = time.time() + max(1.0, grace_period)
        while time.time() < deadline:
            active_cnt = JobExecutor.get_active_job_count()
            if active_cnt == 0:
                break
            time.sleep(0.25)

        # If any jobs still active after grace period, terminate them
        remaining_jobs = JobExecutor.get_active_job_ids()
        for jid in remaining_jobs:
            try:
                JobExecutor.terminate_active_job(jid, reason=f"Worker shutdown drain timeout ({grace_period}s)")
            except Exception:
                pass

        # Cleanly stop worker threads
        self.stop(timeout=5.0)
        self._write_state("OFF")
        try:
            self.control_file.unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def _write_state(self, state: str):
        """Persists real-time state for external CLI and diagnostic inspection."""
        try:
            self.state_file.write_text(json.dumps({
                "state": state,
                "pid": os.getpid() if state != "OFF" else None,
                "node_id": self.node_id,
                "controller_url": self.controller_url,
                "active_jobs": JobExecutor.get_active_job_count() if state != "OFF" else 0,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    def stop(self, timeout: float = 5.0):
        """Stops the worker threads and cleans up running tasks."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()
            self._is_running = False

            if self._heartbeat_thread and self._heartbeat_thread.is_alive():
                self._heartbeat_thread.join(timeout=timeout)
            if self._polling_thread and self._polling_thread.is_alive():
                self._polling_thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._is_running

    def _run_heartbeat_loop(self):
        while not self._stop_event.is_set():
            try:
                self.send_heartbeat()
            except Exception as e:
                # Log or handle reconnection gracefully
                pass
            self._stop_event.wait(self.heartbeat_interval)

    def _run_polling_loop(self):
        while not self._stop_event.is_set() and not self._draining:
            self.check_control_signal()
            if self._draining:
                break
            try:
                self.poll_and_execute_next()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval)


# ------------------------------------------------------------------------------
# CLI Entrypoint
# ------------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PersonalServer LocalWorker for Windows Development")
    subparsers = parser.add_subparsers(dest="action", help="Worker action")

    subparsers.add_parser("register", help="Register this node with the controller")
    subparsers.add_parser("heartbeat", help="Send a single heartbeat to the controller")
    subparsers.add_parser("work", help="Poll and execute the next assigned job")
    subparsers.add_parser("status", help="Print local worker node metadata and run status")
    subparsers.add_parser("run", help="Run worker daemon in the foreground")
    subparsers.add_parser("stop", help="Safely drain and stop a running local worker")

    parser.add_argument("--controller", help="Controller URL override")
    parser.add_argument("--node-id", help="Node ID override")
    parser.add_argument("--token", help="Enrollment token override")

    args = parser.parse_args()

    worker = LocalWorker(
        controller_url=args.controller,
        node_id=args.node_id,
        enrollment_token=args.token
    )

    action = args.action or "status"

    if action == "status":
        meta = worker.get_node_metadata()
        is_running = False
        running_pid = None
        if worker.pid_file.exists():
            try:
                raw = worker.pid_file.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    chk_pid = int(raw)
                    from scripts.doctor import check_process_alive
                    if check_process_alive(chk_pid, pattern="local_worker"):
                        is_running = True
                        running_pid = chk_pid
            except Exception:
                pass

        print("==========================================")
        print(" PersonalServer LocalWorker Metadata")
        print("==========================================")
        print(f"Status:       {'RUNNING (PID: ' + str(running_pid) + ')' if is_running else 'OFF'}")
        print(f"Node ID:      {meta['node_id']}")
        print(f"Name:         {meta['name']}")
        print(f"Platform:     {meta['platform']} ({meta['os']})")
        print(f"Architecture: {meta['architecture']}")
        print(f"CPU Cores:    {meta['cpu_cores']}")
        print(f"Role:         {meta.get('role', 'build')}")
        print(f"Capabilities: {[k for k, v in meta['capabilities'].items() if v]}")
        print(f"Workloads:    {meta['supported_workloads']}")
        print(f"Registered:   {bool(worker.auth_token)}")
        print("==========================================")
        return 0

    elif action == "stop":
        if not worker.pid_file.exists():
            print("LocalWorker is not running.")
            return 0
        raw_pid = worker.pid_file.read_text(encoding="utf-8").strip()
        if not raw_pid.isdigit():
            print("LocalWorker is not running (stale PID file cleaned).")
            worker.pid_file.unlink(missing_ok=True)
            return 0
        target_pid = int(raw_pid)
        from scripts.doctor import check_process_alive
        if not check_process_alive(target_pid, pattern="local_worker"):
            print(f"LocalWorker (PID: {target_pid}) is not running.")
            worker.pid_file.unlink(missing_ok=True)
            return 0

        print(f"Stopping LocalWorker (PID: {target_pid}) safely...")
        try:
            worker.control_file.write_text(json.dumps({"action": "drain", "grace_period": 10.0}), encoding="utf-8")
        except Exception as e:
            print(f"Failed to write drain control signal: {e}", file=sys.stderr)

        t0 = time.time()
        stopped = False
        while time.time() - t0 < 10.0:
            time.sleep(0.5)
            if not check_process_alive(target_pid, pattern="local_worker"):
                stopped = True
                break

        if stopped:
            print(f"LocalWorker (PID: {target_pid}) stopped cleanly.")
            worker.pid_file.unlink(missing_ok=True)
            return 0
        else:
            print(f"LocalWorker (PID: {target_pid}) did not exit within drain window; terminating...")
            try:
                if sys.platform == "win32":
                    import ctypes
                    k32 = ctypes.windll.kernel32
                    h = k32.OpenProcess(0x0001, False, target_pid)
                    if h:
                        k32.TerminateProcess(h, 1)
                        k32.CloseHandle(h)
                else:
                    os.kill(target_pid, 9)
                worker.pid_file.unlink(missing_ok=True)
                print(f"LocalWorker (PID: {target_pid}) terminated.")
            except Exception as e:
                print(f"Failed to terminate worker: {e}", file=sys.stderr)
                return 1
        return 0

    elif action == "register":
        res = worker.register(force=True)
        print(f"Successfully registered LocalWorker: {res.get('node_id')}")
        return 0

    elif action == "heartbeat":
        res = worker.send_heartbeat()
        print(f"Heartbeat OK at {res.get('timestamp')}")
        return 0

    elif action == "work":
        res = worker.poll_and_execute_next()
        if res:
            print(f"Executed job {res.get('job_id')} with status: {res.get('status')} (Exit: {res.get('exit_code')})")
        else:
            print("No jobs pending for this node.")
        return 0

    elif action == "run":
        curr_pid = os.getpid()
        # Duplicate start prevention
        if worker.pid_file.exists():
            try:
                raw_pid = worker.pid_file.read_text(encoding="utf-8").strip()
                if raw_pid.isdigit():
                    old_pid = int(raw_pid)
                    if old_pid != curr_pid:
                        try:
                            from scripts.doctor import check_process_alive
                            if check_process_alive(old_pid, pattern="local_worker"):
                                print(f"Error: LocalWorker is already running under PID {old_pid}", file=sys.stderr)
                                return 1
                        except Exception:
                            pass
            except Exception:
                pass

        # Write current PID
        curr_pid = os.getpid()
        try:
            worker.pid_file.write_text(f"{curr_pid}\n", encoding="utf-8")
        except Exception:
            pass

        print(f"Starting LocalWorker (PID: {curr_pid}) for node '{worker.node_id}' against {worker.controller_url}...")
        worker.start()
        worker._write_state("ON")
        print("LocalWorker is running. Press Ctrl+C or send drain signal to stop.")

        try:
            while worker.is_running() and not worker.is_draining():
                worker.check_control_signal()
                time.sleep(0.5)

            if worker.is_draining():
                print("LocalWorker is draining active jobs...")
                while worker.is_running():
                    time.sleep(0.2)
        except (KeyboardInterrupt, SystemExit):
            print("\nStopping LocalWorker cleanly...")
            worker.drain(grace_period=5.0)
        finally:
            worker.stop()
            worker._write_state("OFF")
            try:
                if worker.pid_file.exists() and worker.pid_file.read_text(encoding="utf-8").strip() == str(curr_pid):
                    worker.pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            print("LocalWorker stopped cleanly.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
