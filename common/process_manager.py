#!/usr/bin/env python3
"""
Safe Process Lifecycle Manager & Identity Contract
===================================================
Enforces isolated, safe lifecycle management for deployed application processes:
- Process identity contract stored in runtime/apps/<app_id>.json
- Strict infrastructure & Antigravity IDE protection (immune from termination)
- Passive liveness inspection (never uses kill signals to probe liveness)
- Process ownership verification prior to termination
- Clean port conflict detection
- Strictly scoped termination (no broad name matching, no process tree kill)
"""

import os
import sys
import json
import time
import socket
import signal
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, asdict

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"
APPS_RUNTIME_DIR = RUNTIME_DIR / "apps"
LOGS_DIR = BASE_DIR / "logs"

# Infrastructure and developer tool keywords that MUST NEVER be terminated
PROTECTED_CMD_KEYWORDS = {
    "antigravity",
    "code.exe",
    "code",
    "vscode",
    "node-agent.py",
    "controller.py",
    "app_router.py",
    "node-api/app.py",
    "sshd",
    "cloudflared",
    "tailscaled",
    "pytest",
}


class ProcessSecurityError(PermissionError):
    """Raised when an operation would target protected infrastructure or IDE processes."""
    pass


@dataclass
class ProcessIdentityRecord:
    app_id: str
    deployment_id: str
    pid: int
    port: int
    command: List[str]
    working_dir: str
    log_file: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessIdentityRecord":
        return cls(
            app_id=data.get("app_id", ""),
            deployment_id=data.get("deployment_id", ""),
            pid=int(data.get("pid", 0)),
            port=int(data.get("port", 0)),
            command=data.get("command", []),
            working_dir=data.get("working_dir", ""),
            log_file=data.get("log_file", ""),
            created_at=data.get("created_at", "")
        )


class SafeProcessLifecycleManager:
    """Manages application process execution, identity verification, and safe termination."""

    @staticmethod
    def _ensure_runtime_dir() -> Path:
        APPS_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        return APPS_RUNTIME_DIR

    @staticmethod
    def get_record_path(app_id: str) -> Path:
        clean_id = "".join(c for c in app_id if c.isalnum() or c in ("-", "_"))
        return SafeProcessLifecycleManager._ensure_runtime_dir() / f"{clean_id}.json"

    @staticmethod
    def write_record(record: ProcessIdentityRecord) -> Path:
        path = SafeProcessLifecycleManager.get_record_path(record.app_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record.to_dict(), f, indent=2)
        return path

    @staticmethod
    def read_record(app_id: str) -> Optional[ProcessIdentityRecord]:
        path = SafeProcessLifecycleManager.get_record_path(app_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return ProcessIdentityRecord.from_dict(data)
        except Exception:
            return None

    @staticmethod
    def remove_record(app_id: str) -> None:
        path = SafeProcessLifecycleManager.get_record_path(app_id)
        if path.exists():
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def check_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """Verifies if the specified port is free on the host."""
        if not (1 <= port <= 65535):
            return False
        # Test if port is currently listening
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                return False
        # Test if address can be bound
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False

    @staticmethod
    def is_pid_active(pid: int) -> bool:
        """Passively checks if a process with PID is active without sending signals."""
        if pid <= 0:
            return False

        if sys.platform == "win32":
            try:
                res = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=3
                )
                return str(pid) in res.stdout
            except Exception:
                return False
        else:
            return Path(f"/proc/{pid}").exists()

    @staticmethod
    def get_process_cmdline(pid: int) -> str:
        """Safely inspects process command line for identification."""
        if pid <= 0:
            return ""
        try:
            if sys.platform == "win32":
                res = subprocess.run(
                    ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=3
                )
                return res.stdout.strip()
            else:
                cmd_file = Path(f"/proc/{pid}/cmdline")
                if cmd_file.exists():
                    return cmd_file.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except Exception:
            pass
        return ""

    @staticmethod
    def verify_infrastructure_immunity(pid: int, candidate_cmd: str = "") -> None:
        """
        Validates that a process ID does NOT belong to the Antigravity IDE,
        current process, parent process, or critical server infrastructure.
        Raises ProcessSecurityError if target is protected.
        """
        if pid <= 1:
            raise ProcessSecurityError(f"PID {pid} is system init/kernel; termination strictly forbidden.")

        curr_pid = os.getpid()
        if pid == curr_pid:
            raise ProcessSecurityError(f"PID {pid} is the executing process; self-termination strictly forbidden.")

        if hasattr(os, "getppid"):
            try:
                ppid = os.getppid()
            except Exception:
                ppid = None
            if ppid is not None and pid == ppid:
                raise ProcessSecurityError(f"PID {pid} is parent process of executor; termination strictly forbidden.")

        # Inspect command line text
        cmd_text = (candidate_cmd or SafeProcessLifecycleManager.get_process_cmdline(pid)).lower()
        for kw in PROTECTED_CMD_KEYWORDS:
            if kw in cmd_text:
                raise ProcessSecurityError(
                    f"PID {pid} matches protected infrastructure or IDE pattern '{kw}'; operation refused."
                )

    @staticmethod
    def verify_process_ownership(app_id: str, pid: int, record: Optional[ProcessIdentityRecord] = None) -> Tuple[bool, str]:
        """
        Verifies that a running OS process actually belongs to the application deployment record.
        Prevents terminating unrelated processes if PIDs were recycled by the OS.
        """
        rec = record or SafeProcessLifecycleManager.read_record(app_id)
        if not rec:
            return False, f"No process record found for application '{app_id}'."

        if rec.pid != pid:
            return False, f"PID mismatch: record specifies {rec.pid}, but target is {pid}."

        if not SafeProcessLifecycleManager.is_pid_active(pid):
            return False, f"Process {pid} is not active."

        # Verify command signature where possible
        cmd_text = SafeProcessLifecycleManager.get_process_cmdline(pid)
        if cmd_text and rec.command:
            # Check if at least one unique command component matches
            expected_tokens = [os.path.basename(c).lower() for c in rec.command if c]
            if not any(token in cmd_text.lower() for token in expected_tokens if len(token) > 2):
                return False, f"Process ownership conflict: PID {pid} command line does not match deployment contract."

        return True, "Ownership verified."

    @staticmethod
    def start_process(
        app_id: str,
        deployment_id: str,
        cmd: List[str],
        cwd: Path,
        port: int,
        env: Dict[str, str],
        log_file: Optional[Path] = None
    ) -> ProcessIdentityRecord:
        """
        Launches an application process in safe background isolation with an explicit identity contract.
        """
        if not SafeProcessLifecycleManager.check_port_available(port):
            raise OSError(f"Port conflict: Port {port} is already in use by another service.")

        # Ensure working directory exists
        cwd.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        target_log = log_file or (LOGS_DIR / f"app_{app_id}.log")
        log_fd = open(target_log, "a", encoding="utf-8")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags
        )
        log_fd.close()

        record = ProcessIdentityRecord(
            app_id=app_id,
            deployment_id=deployment_id,
            pid=proc.pid,
            port=port,
            command=cmd,
            working_dir=str(cwd),
            log_file=str(target_log),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        )
        SafeProcessLifecycleManager.write_record(record)
        return record

    @staticmethod
    def stop_process(app_id: str, timeout_sec: float = 5.0) -> Tuple[bool, str]:
        """
        Gracefully stops a managed application process:
        - Verifies infrastructure immunity (never touches IDE or servers)
        - Verifies process ownership
        - Sends targeted graceful shutdown signal
        - Cleans up runtime identity record
        """
        record = SafeProcessLifecycleManager.read_record(app_id)
        if not record:
            return True, "No active process record found for application."

        pid = record.pid
        if not SafeProcessLifecycleManager.is_pid_active(pid):
            SafeProcessLifecycleManager.remove_record(app_id)
            return True, f"Process {pid} was already stopped."

        # 1. Immune Infrastructure Check
        SafeProcessLifecycleManager.verify_infrastructure_immunity(pid, " ".join(record.command))

        # 2. Ownership Verification Check
        valid, reason = SafeProcessLifecycleManager.verify_process_ownership(app_id, pid, record)
        if not valid:
            return False, f"Refusing to stop process: {reason}"

        # 3. Graceful Targeted Signal
        try:
            if sys.platform == "win32":
                # Single-PID graceful terminate without /T or /F
                subprocess.run(
                    ["taskkill", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=timeout_sec
                )
            else:
                os.kill(pid, signal.SIGTERM)

            # Wait for exit up to timeout_sec
            t0 = time.time()
            while time.time() - t0 < timeout_sec:
                if not SafeProcessLifecycleManager.is_pid_active(pid):
                    break
                time.sleep(0.2)

            if SafeProcessLifecycleManager.is_pid_active(pid):
                # If still active, send final targeted kill
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=2
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)

        except Exception as e:
            return False, f"Error stopping process {pid}: {e}"

        SafeProcessLifecycleManager.remove_record(app_id)
        return True, f"Process {pid} stopped successfully."
