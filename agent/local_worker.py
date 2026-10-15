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
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, Set

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

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
    "timeout-test"
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


def get_canonical_architecture() -> str:
    """Normalize local CPU architecture to cluster standard."""
    machine = platform.machine().strip().lower()
    mapping = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "x64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv7l": "armv7",
        "i386": "386",
        "x86": "386"
    }
    return mapping.get(machine, machine)


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
        role: str = "compute",
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
        self.registration_file = self.runtime_dir / "registration.json"

        # Determine stable node ID
        self.node_id = self._resolve_stable_node_id(node_id)
        self.node_name = node_name or socket.gethostname() or "windows-local"
        self.role = role

        # Controller URL resolution
        self.controller_url = (
            controller_url
            or os.environ.get("PERSONALSERVER_CONTROLLER_URL")
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

        # Capability detection
        self.capabilities = self._detect_capabilities()
        self.supported_workloads = set(BASE_SUPPORTED_WORKLOADS)
        if self.capabilities.get("container_runtime:docker"):
            self.supported_workloads.update(DOCKER_WORKLOADS)

        # Background thread control
        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._polling_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._current_process = None
        self._lock = threading.RLock()

        # Load existing registration state if present
        self._load_stored_registration()

    def _resolve_stable_node_id(self, preferred_id: Optional[str]) -> str:
        """Resolves a stable logical node ID, reading from node.json or persisting a new one."""
        if preferred_id:
            return preferred_id.strip()

        env_id = os.environ.get("PERSONALSERVER_NODE_ID")
        if env_id:
            return env_id.strip()

        if self.node_file.exists():
            try:
                data = json.loads(self.node_file.read_text(encoding="utf-8"))
                stored_id = data.get("node_id")
                if stored_id and str(stored_id).strip() not in ("unknown", ""):
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
                stable_id = f"server-{mac_hex[:16]}"
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
        """Probes real capabilities on this node. Strictly honesty: no Linux/udocker on Windows."""
        caps = {
            "compute": True,
            "storage": True,
            "network": True
        }
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

            payload = {
                "node_id": self.node_id,
                "status": "online",
                "timestamp": now_iso,
                "capabilities": dict(self.capabilities),
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

            # 1. Capability & Workload Validation: Verify workload is explicitly supported
            if job_type not in self.supported_workloads:
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

            # 2. Workload Execution via JobExecutor with cooperative cancellation watcher
            stop_cancel_watcher = threading.Event()

            def _watch_cancellation():
                check_url = f"{self.controller_url}/jobs/{job_id}"
                while not stop_cancel_watcher.wait(timeout=1.0):
                    try:
                        req = urllib.request.Request(
                            check_url,
                            headers={"Authorization": f"Bearer {token}", "User-Agent": "PersonalServer-LocalWorker/1.0"}
                        )
                        with urllib.request.urlopen(req, timeout=1.5) as resp:
                            j_data = json.loads(resp.read().decode("utf-8"))
                            curr_st = (j_data.get("job") or {}).get("status")
                            if curr_st == JOB_STATE_CANCELLED or (j_data.get("job") or {}).get("cancellation_requested"):
                                JobExecutor.terminate_active_job(job_id, reason="Cancelled via controller request")
                                break
                    except Exception:
                        pass

            watcher_thread = threading.Thread(target=_watch_cancellation, daemon=True)
            watcher_thread.start()
            try:
                exec_result = JobExecutor.execute(job)
            finally:
                stop_cancel_watcher.set()
                watcher_thread.join(timeout=0.2)

            # 3. Post result back to controller
            self._submit_job_result(job_id, exec_result, token)
            return exec_result

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
        while not self._stop_event.is_set():
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
    subparsers.add_parser("status", help="Print local worker node metadata")
    subparsers.add_parser("run", help="Run worker daemon in the foreground")

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
        print("==========================================")
        print(" PersonalServer LocalWorker Metadata")
        print("==========================================")
        print(f"Node ID:      {meta['node_id']}")
        print(f"Name:         {meta['name']}")
        print(f"Platform:     {meta['platform']} ({meta['os']})")
        print(f"Architecture: {meta['architecture']}")
        print(f"CPU Cores:    {meta['cpu_cores']}")
        print(f"Memory:       {meta['ram_mb']} MB")
        print(f"Capabilities: {list(meta['capabilities'].keys())}")
        print(f"Workloads:    {meta['supported_workloads']}")
        print(f"Registered:   {bool(worker.auth_token)}")
        print("==========================================")
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
        print(f"Starting LocalWorker for node '{worker.node_id}' against {worker.controller_url}...")
        worker.start()
        print("LocalWorker is running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping LocalWorker...")
            worker.stop()
            print("LocalWorker stopped cleanly.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
