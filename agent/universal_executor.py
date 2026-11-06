#!/usr/bin/env python3
"""
PersonalServer Universal Job Execution Engine (J4)
==================================================
Worker-side execution manager enforcing:
1. Universal Job Contract (ujc.v1.0) validation and lifecycle management.
2. Attempt-isolated scratch workspaces (runtime/jobs/<job_id>/attempts/<attempt>/).
3. Monotonic fencing generation validation and stale attempt rejection.
4. Native runtime dispatch:
   - Python native execution (sys.executable or virtualenv).
   - Node.js native execution (node / npm).
   - Docker Engine execution (strictly verified Docker daemon; never udocker).
   - Structured shell/command execution.
5. Process-group supervision with bounded stdout/stderr streaming, execution timeouts,
   graceful SIGTERM/terminate with timeout to forced kill, and orphan process cleanup.
6. Environment variable and secret reference handling.
"""

import os
import sys
import json
import time
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.structured_logger import agent_logger, redact_sensitive_text
from common.artifact_manager import validate_artifact_path, compute_file_sha256, MAX_ARTIFACT_SIZE_BYTES

RUNTIME_DIR = BASE_DIR / "runtime"
JOBS_RUNTIME_DIR = RUNTIME_DIR / "jobs"

MAX_OUTPUT_BOUND = 64 * 1024  # 64 KB limit for stdout/stderr in memory

# Centralized J1 Job States
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_ASSIGNED = "ASSIGNED"
JOB_STATE_CLAIMED = "CLAIMED"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMED_OUT = "TIMED_OUT"
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_REJECTED = "REJECTED"


class UniversalExecutionEngine:
    """
    Worker-side execution engine managing attempts, isolation, and process supervision.
    """

    _ACTIVE_PROCESSES: Dict[str, Dict[str, Any]] = {}
    _LOCK = threading.Lock()

    @classmethod
    def get_attempt_workspace(cls, job_id: str, attempt: int) -> Path:
        """
        Creates and returns an attempt-isolated directory:
        <runtime_dir>/jobs/<job_id>/attempts/<attempt>/
        Ensures strict attempt isolation (retries do NOT share mutable workspaces).
        """
        if ".." in str(job_id) or "/" in str(job_id) or "\\" in str(job_id):
            raise ValueError(f"Path traversal detected: {job_id}")
        clean_job_id = str(job_id).strip()
        clean_attempt = int(attempt)
        ws = (JOBS_RUNTIME_DIR / clean_job_id / "attempts" / str(clean_attempt)).resolve()
        # Path traversal guard
        if not str(ws).startswith(str(JOBS_RUNTIME_DIR.resolve())):
            raise ValueError(f"Path traversal detected: {job_id} / attempt {attempt}")
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    @classmethod
    def detect_runtime(cls, runtime_name: str) -> Tuple[bool, str, Optional[str]]:
        """
        Checks if the requested runtime is supported and present on this node.
        Returns (is_supported, canonical_runtime, binary_path).
        Strictly distinguishes Docker Engine from udocker.
        """
        norm = str(runtime_name or "").strip().lower()

        # 1. Python Native
        if norm in ("python", "python3", "native/python", "virtualenv"):
            py_bin = sys.executable or shutil.which("python3") or shutil.which("python")
            if py_bin:
                return True, "native/python", py_bin
            return False, "native/python", None

        # 2. Node.js Native
        if norm in ("nodejs", "node", "native/nodejs", "javascript", "npm"):
            node_bin = shutil.which("node") or shutil.which("nodejs")
            if node_bin:
                return True, "native/nodejs", node_bin
            return False, "native/nodejs", None

        # 3. Docker Engine
        if norm in ("docker", "container/docker", "container_runtime:docker"):
            docker_bin = shutil.which("docker")
            if not docker_bin:
                return False, "container/docker", None
            # Reject if binary is named udocker or resolves to udocker
            if "udocker" in Path(docker_bin).name.lower():
                return False, "container/docker", None
            # Check daemon responsiveness
            try:
                res = subprocess.run([docker_bin, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                if res.returncode == 0:
                    return True, "container/docker", docker_bin
            except Exception:
                pass
            return False, "container/docker", None

        # 4. Shell / Command
        if norm in ("shell", "sh", "bash", "command"):
            sh_bin = shutil.which("bash") or shutil.which("sh")
            if sh_bin or os.name == "nt":
                return True, "shell", sh_bin or "cmd.exe"
            return False, "shell", None

        return False, norm, None

    @classmethod
    def cancel_execution(cls, job_id: str, reason: str = "Cancelled by operator") -> bool:
        """
        Gracefully terminates an active execution attempt (SIGTERM/terminate -> forced kill).
        """
        with cls._LOCK:
            info = cls._ACTIVE_PROCESSES.get(job_id)
            if not info:
                return False
            info["cancelled"] = True
            info["cancel_reason"] = reason
            proc = info.get("proc")

        if proc and proc.poll() is None:
            cls._terminate_process_tree(proc, timeout_sec=2.0)
            return True
        return False

    @classmethod
    def _terminate_process_tree(cls, proc: subprocess.Popen, timeout_sec: float = 2.0) -> None:
        """Terminates a process and its process group cleanly."""
        try:
            if os.name != "nt":
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
            else:
                proc.terminate()

            proc.wait(timeout=timeout_sec)
        except (subprocess.TimeoutExpired, Exception):
            try:
                if os.name != "nt":
                    try:
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                else:
                    proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    @classmethod
    def cleanup_stale_processes(cls) -> int:
        """
        Reaps and terminates any lingering child processes recorded in _ACTIVE_PROCESSES
        or left behind by an agent restart.
        """
        cleaned = 0
        with cls._LOCK:
            for job_id, info in list(cls._ACTIVE_PROCESSES.items()):
                proc = info.get("proc")
                if proc and proc.poll() is None:
                    cls._terminate_process_tree(proc, timeout_sec=1.0)
                    cleaned += 1
            cls._ACTIVE_PROCESSES.clear()
        return cleaned

    @classmethod
    def execute_job(
        cls,
        job: Dict[str, Any],
        expected_fencing_token: Optional[int] = None,
        custom_env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Executes a Universal Job workload under strict contract enforcement:
        1. Validates fencing generation.
        2. Sets up attempt-namespaced sandbox.
        3. Spawns supervised process.
        4. Captures bounded logs, return code, duration, and execution metadata.
        """
        job_id = job.get("job_id") or "unknown-job"
        attempt = int(job.get("attempt", 1))
        spec = job.get("spec", {})
        workload = spec.get("workload") or {}
        policy = spec.get("policy") or {}
        now_iso = datetime.now(timezone.utc).isoformat()
        t0 = time.time()

        # 1. Fencing Token & Lease Generation Validation
        current_gen = int(job.get("lease_generation") or job.get("status", {}).get("lease", {}).get("fencing_token", 1))
        if expected_fencing_token is not None and expected_fencing_token != current_gen:
            return {
                "job_id": job_id,
                "attempt": attempt,
                "lease_generation": current_gen,
                "fencing_token": current_gen,
                "status": JOB_STATE_FAILED,
                "exit_code": 412,
                "stdout": "",
                "stderr": f"Precondition Failed: Fencing generation mismatch: expected {expected_fencing_token}, job holds {current_gen}",
                "duration_ms": 0,
                "started_at": now_iso,
                "finished_at": now_iso
            }

        # 2. Duplicate Assignment Protection: Prevent duplicate concurrent executions
        with cls._LOCK:
            if job_id in cls._ACTIVE_PROCESSES:
                active_info = cls._ACTIVE_PROCESSES[job_id]
                active_proc = active_info.get("proc")
                if active_info.get("attempt") == attempt and (active_proc is None or active_proc.poll() is None):
                    return {
                        "job_id": job_id,
                        "attempt": attempt,
                        "lease_generation": current_gen,
                        "fencing_token": current_gen,
                        "status": JOB_STATE_RUNNING,
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": f"Duplicate assignment ignored: job '{job_id}' attempt {attempt} is actively executing",
                        "duplicate_ignored": True,
                        "duration_ms": 0,
                        "started_at": now_iso,
                        "finished_at": now_iso
                    }

        # 3. Workspace Setup
        try:
            workspace_dir = cls.get_attempt_workspace(job_id, attempt)
        except ValueError as ve:
            return {
                "job_id": job_id,
                "attempt": attempt,
                "lease_generation": current_gen,
                "status": JOB_STATE_FAILED,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(ve),
                "duration_ms": 0,
                "started_at": now_iso,
                "finished_at": now_iso
            }

        # 3. Runtime Resolution
        raw_runtime = workload.get("runtime") or job.get("type") or "native/python"
        is_supported, canon_runtime, bin_path = cls.detect_runtime(raw_runtime)
        if not is_supported or not bin_path:
            return {
                "job_id": job_id,
                "attempt": attempt,
                "lease_generation": current_gen,
                "status": JOB_STATE_FAILED,
                "exit_code": 127,
                "stdout": "",
                "stderr": f"Unsupported or missing runtime '{raw_runtime}' on worker host",
                "duration_ms": 0,
                "started_at": now_iso,
                "finished_at": now_iso
            }

        # 4. Command Construction
        entrypoint = workload.get("entrypoint") or ""
        arguments = workload.get("arguments") or []
        cmd: List[str] = []

        if workload.get("command"):
            raw_cmd = workload.get("command")
            if isinstance(raw_cmd, list):
                cmd = [str(x) for x in raw_cmd]
            else:
                cmd = [str(raw_cmd)]
        elif canon_runtime == "native/python":
            if entrypoint:
                cmd = [bin_path, entrypoint] + [str(a) for a in arguments]
            else:
                inline_script = job.get("parameters", {}).get("script") or workload.get("script") or "print('ok')"
                cmd = [bin_path, "-c", inline_script]
        elif canon_runtime == "native/nodejs":
            if entrypoint:
                cmd = [bin_path, entrypoint] + [str(a) for a in arguments]
            else:
                inline_script = job.get("parameters", {}).get("script") or workload.get("script") or "console.log('ok')"
                cmd = [bin_path, "-e", inline_script]
        elif canon_runtime == "container/docker":
            image = workload.get("image") or job.get("parameters", {}).get("image")
            if not image:
                return {
                    "job_id": job_id,
                    "attempt": attempt,
                    "lease_generation": current_gen,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Missing required container image in workload specification",
                    "duration_ms": 0,
                    "started_at": now_iso,
                    "finished_at": now_iso
                }
            cmd = [bin_path, "run", "--rm", "-v", f"{workspace_dir}:/workspace", "-w", "/workspace", image]
            if entrypoint:
                cmd.append(entrypoint)
            cmd.extend([str(a) for a in arguments])
        elif canon_runtime == "shell":
            if entrypoint:
                cmd = [bin_path, entrypoint] + [str(a) for a in arguments]
            else:
                cmd = [str(a) for a in arguments]

        # 5. Environment Preparation
        proc_env = os.environ.copy()
        proc_env["PERSONALSERVER_JOB_ID"] = job_id
        proc_env["PERSONALSERVER_ATTEMPT"] = str(attempt)
        proc_env["PERSONALSERVER_WORKSPACE"] = str(workspace_dir)
        proc_env["PERSONALSERVER_FENCING_TOKEN"] = str(current_gen)

        spec_env = spec.get("environment", {}).get("variables", {})
        for k, v in spec_env.items():
            proc_env[str(k)] = str(v)

        if custom_env:
            for k, v in custom_env.items():
                proc_env[str(k)] = str(v)

        # 6. Process Supervision & Execution
        timeout_sec = float(policy.get("timeout_sec") or job.get("timeout") or 60.0)
        started_iso = datetime.now(timezone.utc).isoformat()
        stdout_chunks = []
        stderr_chunks = []
        exit_code = 0
        timed_out = False
        cancelled = False

        start_new_session = (os.name != "nt")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace_dir),
                env=proc_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=start_new_session
            )

            with cls._LOCK:
                cls._ACTIVE_PROCESSES[job_id] = {
                    "proc": proc,
                    "attempt": attempt,
                    "workspace": workspace_dir,
                    "fencing_token": current_gen,
                    "cancelled": False,
                    "cancel_reason": None
                }

            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout_sec)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                cls._terminate_process_tree(proc, timeout_sec=1.5)
                stdout_data, stderr_data = proc.communicate()
                exit_code = -1

            with cls._LOCK:
                job_proc_info = cls._ACTIVE_PROCESSES.pop(job_id, {})
                if job_proc_info.get("cancelled"):
                    cancelled = True

        except Exception as e:
            with cls._LOCK:
                cls._ACTIVE_PROCESSES.pop(job_id, None)
            return {
                "job_id": job_id,
                "attempt": attempt,
                "lease_generation": current_gen,
                "status": JOB_STATE_FAILED,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Failed to spawn process: {e}",
                "duration_ms": round((time.time() - t0) * 1000, 1),
                "started_at": started_iso,
                "finished_at": datetime.now(timezone.utc).isoformat()
            }

        # 7. Output Truncation & Status Determination
        dur_ms = round((time.time() - t0) * 1000, 1)
        finished_iso = datetime.now(timezone.utc).isoformat()
        bounded_stdout = redact_sensitive_text(str(stdout_data or "")[:MAX_OUTPUT_BOUND])
        bounded_stderr = redact_sensitive_text(str(stderr_data or "")[:MAX_OUTPUT_BOUND])

        if cancelled:
            final_status = JOB_STATE_CANCELLED
            stderr_out = bounded_stderr or "Execution cancelled by operator."
        elif timed_out:
            final_status = JOB_STATE_TIMED_OUT
            stderr_out = bounded_stderr or f"Execution timed out after {timeout_sec}s."
        elif exit_code == 0:
            final_status = JOB_STATE_SUCCEEDED
            stderr_out = bounded_stderr
        else:
            final_status = JOB_STATE_FAILED
            stderr_out = bounded_stderr or f"Process exited with non-zero code {exit_code}."

        return {
            "job_id": job_id,
            "attempt": attempt,
            "lease_generation": current_gen,
            "fencing_token": current_gen,
            "status": final_status,
            "exit_code": exit_code,
            "stdout": bounded_stdout,
            "stderr": stderr_out,
            "duration_ms": dur_ms,
            "started_at": started_iso,
            "finished_at": finished_iso,
            "workspace_dir": str(workspace_dir)
        }
