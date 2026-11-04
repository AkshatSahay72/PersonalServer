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
from typing import Optional, Dict, Any, Tuple, List, Union
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

# Generic process/interpreter tokens that cannot establish application ownership on their own
GENERIC_COMMAND_TOKENS = {
    "python",
    "python3",
    "python.exe",
    "python3.exe",
    "pythonw",
    "pythonw.exe",
    "py",
    "py.exe",
    "pypy",
    "pypy3",
    "sh",
    "bash",
    "zsh",
    "dash",
    "ash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "node",
    "node.exe",
    "npm",
    "npx",
    "-u",
    "-m",
    "-c",
    "-b",
    "-v",
    "-o",
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
    start_time: Optional[Union[int, float, str]] = None

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
            created_at=data.get("created_at", ""),
            start_time=data.get("start_time", None)
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
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
            proc_path = Path(f"/proc/{pid}")
            if not proc_path.exists():
                return False
            try:
                # If process is in zombie state ('Z'), it is no longer active
                stat_file = proc_path / "stat"
                if stat_file.exists():
                    content = stat_file.read_text(encoding="utf-8", errors="ignore").strip()
                    rparen = content.rfind(")")
                    if rparen != -1:
                        rest = content[rparen + 1:].strip().split()
                        if rest and rest[0] == "Z":
                            return False
            except Exception:
                pass
            return True

    @staticmethod
    def get_process_start_time(pid: int) -> Optional[Union[int, str]]:
        """
        Retrieves the OS process start-time identity to detect PID recycling:
        - On Linux/Android (Termux): Extracts field 22 (starttime clock ticks) from /proc/{pid}/stat.
        - On Windows: Uses GetProcessTimes via ctypes (or PowerShell CIM fallback) to get creation time ticks.
        Returns None if PID does not exist, stat file is unreadable/malformed, or access is denied.
        """
        if pid <= 0:
            return None

        if sys.platform == "win32":
            # 1. High-performance Win32 API via ctypes (zero subprocess overhead)
            try:
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.windll.kernel32
                # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h_proc = kernel32.OpenProcess(0x1000, False, pid)
                if h_proc:
                    try:
                        c_time = wintypes.FILETIME()
                        e_time = wintypes.FILETIME()
                        k_time = wintypes.FILETIME()
                        u_time = wintypes.FILETIME()
                        if kernel32.GetProcessTimes(
                            h_proc,
                            ctypes.byref(c_time),
                            ctypes.byref(e_time),
                            ctypes.byref(k_time),
                            ctypes.byref(u_time),
                        ):
                            return (c_time.dwHighDateTime << 32) | c_time.dwLowDateTime
                    finally:
                        kernel32.CloseHandle(h_proc)
            except Exception:
                pass

            # 2. Fallback to PowerShell CIM
            try:
                ps_cmd = f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CreationDate.ToFileTime()"
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=3
                )
                val = res.stdout.strip()
                if val and val.isdigit():
                    return int(val)
            except Exception:
                pass
            return None
        else:
            # Linux / Android (Termux) via /proc/{pid}/stat
            stat_path = Path(f"/proc/{pid}/stat")
            if not stat_path.exists():
                return None
            try:
                content = stat_path.read_text(encoding="utf-8", errors="ignore").strip()
                rparen = content.rfind(")")
                if rparen == -1:
                    return None
                rest = content[rparen + 1:].strip().split()
                # rest[0] = state (field 3), rest[19] = starttime (field 22 in /proc/{pid}/stat)
                if len(rest) > 19:
                    return int(rest[19])
            except Exception:
                return None
            return None

    @staticmethod
    def get_process_cmdline(pid: int) -> str:
        """Safely inspects process command line for identification without deprecated WMIC."""
        if pid <= 0:
            return ""
        try:
            if sys.platform == "win32":
                # 1. Use supported PowerShell CIM query
                try:
                    ps_cmd = f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"
                    res = subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                        capture_output=True,
                        text=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=3
                    )
                    output = res.stdout.strip()
                    if output:
                        return output
                except Exception:
                    pass

                # 2. Fallback to WMIC if available
                try:
                    res = subprocess.run(
                        ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                        capture_output=True,
                        text=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=3
                    )
                    lines = [line.strip() for line in res.stdout.splitlines() if line.strip() and not line.strip().lower().startswith("commandline")]
                    if lines:
                        return " ".join(lines)
                except Exception:
                    pass
                return ""
            else:
                cmd_file = Path(f"/proc/{pid}/cmdline")
                if cmd_file.exists():
                    return cmd_file.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
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
        Fail-closed: returns (False, reason) if anything cannot be verified or conflicts.
        Prevents terminating unrelated processes if PIDs were recycled by the OS.
        """
        rec = record or SafeProcessLifecycleManager.read_record(app_id)
        if not rec:
            return False, f"No process record found for application '{app_id}'."

        if rec.pid != pid:
            return False, f"PID mismatch: record specifies {rec.pid}, but target is {pid}."

        if not SafeProcessLifecycleManager.is_pid_active(pid):
            return False, f"Process {pid} is not active."

        # 1. Process start-time identity check (detects recycled PID)
        if rec.start_time is None:
            return False, f"Ownership unverified: process record for '{app_id}' lacks start-time metadata."

        curr_start_time = SafeProcessLifecycleManager.get_process_start_time(pid)
        if curr_start_time is None:
            return False, f"Ownership unverified: unable to determine start time for live process {pid}."

        if str(curr_start_time) != str(rec.start_time):
            return False, (
                f"PID reuse detected: live process {pid} start time ({curr_start_time}) "
                f"does not match deployment record ({rec.start_time})."
            )

        # 2. Command line signature check (fail-closed)
        cmd_text = SafeProcessLifecycleManager.get_process_cmdline(pid).strip()
        if not cmd_text:
            return False, f"Ownership unverified: process {pid} command line is inaccessible or empty."

        # Extract meaningful tokens from the record (rejecting generic tokens like 'python', 'sh', flags)
        meaningful_tokens: List[str] = []
        system_dir_names = {"bin", "usr", "local", "programs", "appdata", "windows", "system32", "lib", "site-packages"}
        if rec.command:
            for i, arg in enumerate(rec.command):
                if not arg or arg.startswith("-"):
                    continue
                base = os.path.basename(arg).lower()
                # Skip generic interpreters and launchers
                if (
                    base in GENERIC_COMMAND_TOKENS
                    or base.startswith("python")
                    or base.startswith("pypy")
                    or (i == 0 and ("python" in base or "node" in base))
                ):
                    continue
                if len(base) > 2 and base not in meaningful_tokens:
                    meaningful_tokens.append(base)

                # Check for distinctive application path components (avoiding system paths)
                if ("/" in arg or "\\" in arg) and (i > 0 or not base.startswith("python")):
                    try:
                        parts = Path(arg).parts
                        for p in parts:
                            p_lower = p.lower()
                            if (
                                p_lower not in GENERIC_COMMAND_TOKENS
                                and len(p_lower) > 3
                                and not p_lower.startswith("python")
                                and p_lower not in system_dir_names
                            ):
                                if p_lower not in meaningful_tokens:
                                    meaningful_tokens.append(p_lower)
                    except Exception:
                        pass

        # If the command only had generic tokens, fall back to app_id and working dir name
        if rec.app_id and len(rec.app_id) > 2:
            app_id_lower = rec.app_id.lower()
            if app_id_lower not in meaningful_tokens:
                meaningful_tokens.append(app_id_lower)

        if rec.working_dir:
            cwd_base = os.path.basename(rec.working_dir).lower()
            if (
                cwd_base
                and cwd_base not in GENERIC_COMMAND_TOKENS
                and not cwd_base.startswith("python")
                and cwd_base not in system_dir_names
                and len(cwd_base) > 2
            ):
                if cwd_base not in meaningful_tokens:
                    meaningful_tokens.append(cwd_base)

        cmd_text_lower = cmd_text.lower()
        matches = [token for token in meaningful_tokens if token in cmd_text_lower]
        if not matches:
            return False, (
                f"Process ownership conflict: PID {pid} command line does not match deployment contract "
                f"(expected tokens: {meaningful_tokens})."
            )

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
        # Wait up to 2.5 seconds for port to clear if recently released
        t_port = time.time()
        while time.time() - t_port < 2.5:
            if SafeProcessLifecycleManager.check_port_available(port):
                break
            time.sleep(0.1)

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

        # Capture process creation start-time identity immediately after spawn
        start_time = SafeProcessLifecycleManager.get_process_start_time(proc.pid)

        record = ProcessIdentityRecord(
            app_id=app_id,
            deployment_id=deployment_id,
            pid=proc.pid,
            port=port,
            command=cmd,
            working_dir=str(cwd),
            log_file=str(target_log),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            start_time=start_time
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
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass

            # Wait for exit up to timeout_sec
            t0 = time.time()
            while time.time() - t0 < timeout_sec:
                if sys.platform != "win32":
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
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
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
                time.sleep(0.3)

        except Exception as e:
            return False, f"Error stopping process {pid}: {e}"

        SafeProcessLifecycleManager.remove_record(app_id)
        return True, f"Process {pid} stopped successfully."
