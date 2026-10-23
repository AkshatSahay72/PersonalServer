#!/usr/bin/env python3
"""
PersonalServer Unified Diagnostic Engine
========================================
Comprehensive, read-only diagnostic CLI for PersonalServer nodes and controller hosts.
Detects local platform, verifies processes, ports, endpoints, Tailscale, SSH,
Cloudflare Tunnel, and logs, producing actionable reports with zero side-effects.

Usage:
  python scripts/doctor.py                # Run diagnostics and print summary
  python scripts/doctor.py --json         # Machine-readable JSON output
  python scripts/doctor.py --save-report  # Save sanitized report to runtime/reports/
  python scripts/doctor.py -v             # Verbose check details

Exit codes:
  0: HEALTHY               (All applicable components operational)
  1: DEGRADED              (System operational with warnings/degraded elements)
  2: NEEDS MANUAL ACTION   (Critical failure requiring user intervention)
"""

import os
import sys
import json
import time
import re
import shutil
import socket
import platform
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Ensure agent directory is in sys.path for importing node-agent if needed
AGENT_DIR = BASE_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# Safe import of structured logger redaction
try:
    from config.structured_logger import redact_sensitive_text, redact_sensitive_data
except Exception:
    def redact_sensitive_text(text: Any) -> str:
        return str(text) if text is not None else ""
    def redact_sensitive_data(val: Any) -> Any:
        return val

# Common directories and files
RUNTIME_DIR = BASE_DIR / "runtime"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
REPORTS_DIR = RUNTIME_DIR / "reports"

NODE_JSON = CONFIG_DIR / "node.json"
NODE_CONF = CONFIG_DIR / "node.conf"
CONTROLLER_JSON = CONFIG_DIR / "controller.json"

NODE_API_PID_FILE = RUNTIME_DIR / "node-api.pid"
NODE_WORKER_PID_FILE = RUNTIME_DIR / "node-worker.pid"
CLOUDFLARED_PID_FILE = RUNTIME_DIR / "cloudflared.pid"
CONTROLLER_PID_FILE = RUNTIME_DIR / "controller.pid"
ROUTER_PID_FILE = RUNTIME_DIR / "app-router.pid"
WATCHDOG_PID_FILE = RUNTIME_DIR / "watchdog.pid"
RECOVERY_LOCK_FILE = RUNTIME_DIR / "recovery.lock"
RECOVERY_STATE_FILE = RUNTIME_DIR / "recovery_state.json"

NODE_API_LOG = LOGS_DIR / "node-api.log"
NODE_WORKER_LOG = LOGS_DIR / "node-worker.log"
CLOUDFLARED_LOG = LOGS_DIR / "cloudflared.log"
CONTROLLER_LOG = LOGS_DIR / "controller.log"
ROUTER_LOG = LOGS_DIR / "app-router.log"
WATCHDOG_LOG = LOGS_DIR / "watchdog.log"

STATUS_HEALTHY = "HEALTHY"
STATUS_RECOVERED = "RECOVERED"
STATUS_DEGRADED = "DEGRADED"
STATUS_ACTION = "NEEDS MANUAL ACTION"

MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_WINDOW_SECONDS = 600


# ==============================================================================
# Helper Utilities
# ==============================================================================

def run_cmd_safe(cmd: List[str], timeout: float = 3.0) -> str:
    """Run command safely with strict timeout and no shell interpretation."""
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False
        )
        return res.stdout.strip()
    except Exception:
        return ""


def check_tcp_port(host: str, port: int, timeout: float = 1.5) -> bool:
    """Checks if a TCP port is open and listening."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def is_termux_environment() -> bool:
    """Detects whether code is running within the Android Termux environment."""
    if "com.termux" in os.environ.get("PREFIX", ""):
        return True
    if os.environ.get("ANDROID_ROOT"):
        return True
    return os.path.exists("/data/data/com.termux")


def http_get_json(url: str, timeout: float = 2.5) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Issues a simple HTTP GET request and attempts to decode JSON."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers={"User-Agent": "PersonalServerDoctor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                return status, data, body
            except Exception:
                return status, None, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return e.code, None, body
    except Exception as e:
        return 0, None, str(e)


probe_http_endpoint = http_get_json


# ==============================================================================
# Process Inspection
# ==============================================================================

def check_process_alive(pid_int: int, pattern: Optional[str] = None) -> bool:
    """
    Cross-platform process liveness and command-line pattern matching.
    Reuses node-agent Win32/CIM query on Windows or os.kill/ps on Linux/Termux.
    """

    if pid_int <= 0:
        return False

    if sys.platform == "win32":
        try:
            # Delegate to node-agent Windows API implementation
            import importlib.util
            agent_script = BASE_DIR / "agent" / "node-agent.py"
            if agent_script.exists():
                spec = importlib.util.spec_from_file_location("node_agent_doc", str(agent_script))
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod._is_pid_alive_windows(pid_int, pattern=pattern)
        except Exception:
            pass

        # Built-in Windows fallback via tasklist/PowerShell
        try:
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid_int}').CommandLine"]
            out = run_cmd_safe(cmd, timeout=3.0)
            if not out:
                return False
            if pattern:
                return pattern.lower().replace("\\", "/") in out.lower().replace("\\", "/")
            return True
        except Exception:
            return False

    # POSIX / Termux
    try:
        os.kill(pid_int, 0)
    except OSError:
        return False

    if pattern:
        args = run_cmd_safe(["ps", "-p", str(pid_int), "-o", "args="], timeout=2.0)
        return pattern in args

    return True


is_pid_alive = check_process_alive


def inspect_pid_file(pid_path: Path, pattern: str) -> Dict[str, Any]:
    """Inspects a PID file, validates its integer content, and tests process liveness."""
    if not pid_path.exists():
        # Check if process is running untracked
        untracked_pid = find_untracked_process(pattern)
        if untracked_pid:
            return {
                "status": "RUNNING_UNTRACKED",
                "pid": untracked_pid,
                "pid_file": str(pid_path.relative_to(BASE_DIR)),
                "message": f"Process running without PID file (PID: {untracked_pid})"
            }
        return {
            "status": "STOPPED",
            "pid": None,
            "pid_file": str(pid_path.relative_to(BASE_DIR)),
            "message": "Service stopped (PID file missing)"
        }

    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except Exception:
        return {
            "status": "INVALID_PID_FILE",
            "pid": None,
            "pid_file": str(pid_path.relative_to(BASE_DIR)),
            "message": f"Corrupt PID file at {pid_path.name}"
        }

    if check_process_alive(pid, pattern=pattern):
        return {
            "status": "RUNNING",
            "pid": pid,
            "pid_file": str(pid_path.relative_to(BASE_DIR)),
            "message": f"Process running (PID: {pid})"
        }
    else:
        # Check if running under another PID
        alt_pid = find_untracked_process(pattern)
        if alt_pid:
            return {
                "status": "STALE_PID_FILE",
                "pid": pid,
                "actual_pid": alt_pid,
                "pid_file": str(pid_path.relative_to(BASE_DIR)),
                "message": f"Stale PID file (PID {pid} dead, but process alive at PID {alt_pid})"
            }
        return {
            "status": "STALE_PID_FILE",
            "pid": pid,
            "pid_file": str(pid_path.relative_to(BASE_DIR)),
            "message": f"Stale PID file (Process {pid} is dead)"
        }


def find_untracked_process(pattern: str) -> Optional[int]:
    """Finds an untracked process PID matching command pattern."""
    if sys.platform == "win32":
        try:
            pat_norm = pattern.lower().replace("\\", "/")
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{pat_norm}*' }} | Select-Object -ExpandProperty ProcessId -First 1"]
            res = run_cmd_safe(cmd, timeout=3.0)
            if res and res.isdigit():
                return int(res)
        except Exception:
            pass
        return None

    # POSIX / Termux
    out = run_cmd_safe(["pgrep", "-f", pattern], timeout=2.0)
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            p = int(line)
            if p != os.getpid():
                return p
    return None


# ==============================================================================
# Recovery Locking, Circuit Breaker & Process Management
# ==============================================================================

class RecoveryLock:
    """Guarantees mutually exclusive recovery and watchdog operations."""

    def __init__(self, lock_file: Path = RECOVERY_LOCK_FILE):
        self.lock_file = lock_file
        self.acquired = False

    def acquire(self) -> Tuple[bool, str]:
        if self.lock_file.exists():
            try:
                data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                pid = data.get("pid")
                if pid and check_process_alive(pid):
                    return False, f"Recovery already in progress by PID {pid} (since {data.get('time', 'unknown')})"
            except Exception:
                pass
            # Stale lock file, remove it safely
            try:
                self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "time": datetime.now(timezone.utc).isoformat(),
                "args": sys.argv
            }
            self.lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.acquired = True
            return True, "Lock acquired"
        except Exception as e:
            return False, f"Failed to acquire recovery lock: {e}"

    def release(self):
        if self.acquired and self.lock_file.exists():
            try:
                self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            self.acquired = False

    def __enter__(self):
        ok, msg = self.acquire()
        if not ok:
            raise RuntimeError(msg)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class CircuitBreaker:
    """Bounds recovery attempts and trips if a service fails repeated restarts."""

    def __init__(
        self,
        state_file: Path = RECOVERY_STATE_FILE,
        max_attempts: int = MAX_RECOVERY_ATTEMPTS,
        window_sec: int = RECOVERY_WINDOW_SECONDS
    ):
        self.state_file = state_file
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"services": {}}

    def _save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except Exception:
            pass

    def can_attempt(self, service: str) -> Tuple[bool, int, str]:
        svc_data = self.state.setdefault("services", {}).get(service, {})
        count = svc_data.get("count", 0)
        last_attempt = svc_data.get("last_attempt", 0)
        now = time.time()

        if (now - last_attempt) > self.window_sec:
            # Window expired, reset counter
            self.state.setdefault("services", {})[service] = {
                "count": 0,
                "last_attempt": now,
                "tripped": False
            }
            self._save()
            return True, 0, "Window clear"

        if count >= self.max_attempts:
            return False, count, f"Circuit breaker tripped: {count} failed recovery attempts within {self.window_sec}s"
        return True, count, f"Attempts: {count}/{self.max_attempts}"

    def record_attempt(self, service: str, success: bool):
        svc_data = self.state.setdefault("services", {}).setdefault(service, {"count": 0, "last_attempt": 0, "tripped": False})
        now = time.time()
        svc_data["last_attempt"] = now
        if success:
            svc_data["count"] = 0
            svc_data["tripped"] = False
        else:
            svc_data["count"] = svc_data.get("count", 0) + 1
            if svc_data["count"] >= self.max_attempts:
                svc_data["tripped"] = True
        self._save()

    def reset(self, service: Optional[str] = None):
        if service:
            self.state.setdefault("services", {}).pop(service, None)
        else:
            self.state = {"services": {}}
        self._save()


def safe_terminate_process(pid_int: int, pattern: str, timeout: float = 5.0) -> bool:
    """
    Terminates a process safely after verifying its command-line identity.
    Sends SIGTERM/graceful termination first, then SIGKILL only if confirmed matching.
    """
    if pid_int <= 0:
        return True
    if not check_process_alive(pid_int, pattern):
        return True  # Already dead or not matching

    if sys.platform == "win32":
        try:
            # Graceful termination first
            subprocess.run(
                ["taskkill", "/PID", str(pid_int)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
            start_t = time.time()
            while time.time() - start_t < timeout:
                if not check_process_alive(pid_int):
                    return True
                time.sleep(0.25)
            # Force kill if still alive
            if check_process_alive(pid_int, pattern):
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid_int)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
                time.sleep(0.5)
            return not check_process_alive(pid_int)
        except Exception:
            return False

    # POSIX / Termux
    try:
        import signal
        os.kill(pid_int, signal.SIGTERM)
        start_t = time.time()
        while time.time() - start_t < timeout:
            if not check_process_alive(pid_int):
                return True
            time.sleep(0.25)
        # Force kill
        if check_process_alive(pid_int, pattern):
            os.kill(pid_int, signal.SIGKILL)
            time.sleep(0.5)
        return not check_process_alive(pid_int)
    except OSError:
        return True
    except Exception:
        return False


def spawn_background_process(cmd: List[str], log_file: Path, cwd: Path) -> Optional[int]:
    """Spawns a detached background process redirecting output to log_file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    out_f = open(log_file, "a", encoding="utf-8", errors="replace")
    try:
        if sys.platform == "win32":
            # 0x00000008 = DETACHED_PROCESS, 0x00000200 = CREATE_NEW_PROCESS_GROUP
            flags = 0x00000008 | 0x00000200
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=out_f,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True
            )
            return proc.pid
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=out_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True
            )
            return proc.pid
    except Exception:
        return None


# ==============================================================================
# Diagnostic Engine
# ==============================================================================

class DiagnosticEngine:
    """Executes safe, read-only diagnostic audits across local services."""

    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = base_dir
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.results: Dict[str, Any] = {}
        self.summary_status = STATUS_HEALTHY
        self.manual_actions: List[str] = []
        self.warnings: List[str] = []

    def detect_platform(self) -> Dict[str, Any]:
        """Detects and returns platform metadata."""
        env = self.detect_platform_and_role()
        return {
            "os": platform.system(),
            "platform": env["platform"],
            "architecture": env["architecture"],
            "python_version": platform.python_version(),
            "is_termux": env["is_termux"]
        }

    def read_node_identity(self) -> Dict[str, Any]:
        """Reads node identity configuration from disk."""
        if NODE_JSON.exists():
            try:
                with open(NODE_JSON, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def is_controller_host(self) -> bool:
        """Determines if local host is designated as a controller."""
        return (self.base_dir / "controller" / "controller.py").exists()

    def get_applicable_services(self) -> List[str]:
        """Returns the list of service names applicable to this host."""
        env = self.detect_platform_and_role()
        identity = self.read_node_identity()
        role = identity.get("role", env.get("configured_role", "node"))
        is_controller = self.is_controller_host()

        services = []
        is_termux = env["is_termux"]
        if is_termux or sys.platform.startswith("linux"):
            services.append("ssh")
        services.append("tailscale")

        if is_controller:
            services.append("controller_router")

        if (not is_controller) or role in ("node", "compute", "storage", "ingress") or env.get("has_node_api_files"):
            services.append("node_api")
            services.append("node_worker")

        if env.get("has_cloudflared") or (SECRETS_DIR / "cloudflared.token").exists():
            services.append("cloudflare_tunnel")

        return services

    def check_service_process(self, pid_file: Path, pattern: str) -> Dict[str, Any]:
        """Inspects a PID file and verifies running process."""
        return inspect_pid_file(pid_file, pattern)

    def find_untracked_process(self, pattern: str) -> Optional[int]:
        """Finds untracked process matching command pattern."""
        return find_untracked_process(pattern)

    def detect_platform_and_role(self) -> Dict[str, Any]:
        """Detects OS platform, Termux environment, and configured node role."""
        is_termux = is_termux_environment()
        os_name = "termux" if is_termux else sys.platform

        node_id = "unknown"
        node_name = platform.node()
        role = "unknown"

        # Check node configuration
        if NODE_JSON.exists():
            try:
                with open(NODE_JSON, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    node_id = d.get("node_id", node_id)
                    node_name = d.get("name", node_name)
                    role = d.get("role", role)
            except Exception:
                pass
        elif NODE_CONF.exists():
            try:
                with open(NODE_CONF, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("NODE_ID="):
                            node_id = line.split("=", 1)[1].strip()
                        elif line.startswith("NODE_NAME="):
                            node_name = line.split("=", 1)[1].strip()
                        elif line.startswith("NODE_ROLE="):
                            role = line.split("=", 1)[1].strip()
            except Exception:
                pass

        has_controller_files = (self.base_dir / "controller" / "controller.py").exists()
        has_node_api_files = (self.base_dir / "services" / "node-api" / "app.py").exists()
        has_cloudflared = bool(shutil.which("cloudflared")) or (SECRETS_DIR / "cloudflared.token").exists()

        return {
            "platform": os_name,
            "is_termux": is_termux,
            "architecture": platform.machine(),
            "hostname": node_name,
            "node_id": node_id,
            "configured_role": role,
            "has_controller_files": has_controller_files,
            "has_node_api_files": has_node_api_files,
            "has_cloudflared": has_cloudflared,
        }

    def check_system_resources(self, is_termux: bool) -> Dict[str, Any]:
        """Checks available disk space, memory, and Android wake-lock status."""
        data: Dict[str, Any] = {"status": STATUS_HEALTHY, "details": {}}

        # Disk space
        try:
            target_path = Path.home() if is_termux else self.base_dir
            usage = shutil.disk_usage(target_path)
            free_gb = round(usage.free / (1024 ** 3), 2)
            total_gb = round(usage.total / (1024 ** 3), 2)
            used_pct = round((usage.used / usage.total) * 100, 1)

            data["details"]["disk"] = {
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_pct": used_pct,
            }

            if used_pct >= 95.0 or free_gb < 1.0:
                data["status"] = STATUS_ACTION
                self.manual_actions.append(f"Disk space critically low: {free_gb} GB free ({used_pct}% used). Free disk space on {target_path}.")
            elif used_pct >= 85.0:
                data["status"] = STATUS_DEGRADED
                self.warnings.append(f"Disk space warning: {used_pct}% used ({free_gb} GB free).")
        except Exception as e:
            data["details"]["disk"] = {"error": str(e)}

        # Memory (POSIX / Termux)
        if Path("/proc/meminfo").exists():
            try:
                meminfo = {}
                with open("/proc/meminfo", "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split(":")
                        if len(parts) == 2:
                            meminfo[parts[0].strip()] = parts[1].strip()

                total_kb = int(meminfo.get("MemTotal", "0 kB").split()[0])
                avail_kb = int(meminfo.get("MemAvailable", meminfo.get("MemFree", "0 kB")).split()[0])
                if total_kb > 0:
                    avail_mb = round(avail_kb / 1024, 1)
                    used_pct = round(((total_kb - avail_kb) / total_kb) * 100, 1)
                    data["details"]["memory"] = {
                        "available_mb": avail_mb,
                        "used_pct": used_pct
                    }
                    if used_pct >= 95.0:
                        data["status"] = STATUS_DEGRADED
                        self.warnings.append(f"Memory pressure high: {used_pct}% used ({avail_mb} MB available).")
            except Exception:
                pass

        # Android Termux Wake-Lock check
        if is_termux:
            wake_lock_tool = shutil.which("termux-wake-lock")
            data["details"]["termux_wake_lock"] = {
                "installed": bool(wake_lock_tool)
            }
            if not wake_lock_tool:
                data["status"] = STATUS_DEGRADED
                self.warnings.append("termux-wake-lock utility missing. Background tasks may be killed by Android Doze.")

        return data

    def check_configuration_files(self, env: Dict[str, Any]) -> Dict[str, Any]:
        """Checks existence and permissions of required configuration files."""
        res: Dict[str, Any] = {"status": STATUS_HEALTHY, "details": {}}

        # Node identity file
        has_identity = NODE_JSON.exists() or NODE_CONF.exists()
        res["details"]["node_identity"] = {
            "exists": has_identity,
            "path": "config/node.json" if NODE_JSON.exists() else ("config/node.conf" if NODE_CONF.exists() else None)
        }
        if not has_identity and not env.get("has_controller_files"):
            res["status"] = STATUS_ACTION
            self.manual_actions.append("Node identity missing. Run 'python agent/node-agent.py info' or create config/node.json.")

        # Enrollment token check
        enrollment_token = SECRETS_DIR / "enrollment.token"
        has_token = enrollment_token.exists() and enrollment_token.stat().st_size > 0
        res["details"]["enrollment_token"] = {
            "exists": has_token,
            "path": "config/secrets/enrollment.token"
        }

        # Cloudflare token check
        if env.get("has_cloudflared"):
            cf_token = SECRETS_DIR / "cloudflared.token"
            has_cf = cf_token.exists() and cf_token.stat().st_size > 0
            res["details"]["cloudflared_token"] = {
                "exists": has_cf,
                "path": "config/secrets/cloudflared.token"
            }
            if not has_cf:
                res["status"] = STATUS_ACTION
                self.manual_actions.append("Cloudflare Tunnel token missing. Place token in config/secrets/cloudflared.token.")

        return res

    def check_ssh_service(self, is_termux: bool) -> Dict[str, Any]:
        """Verifies SSH service availability (port 8022 for Termux)."""
        port = 8022 if is_termux else 22
        listening = check_tcp_port("127.0.0.1", port, timeout=1.5)

        data = {
            "status": STATUS_HEALTHY if listening else STATUS_ACTION,
            "port": port,
            "listening": listening,
            "message": f"SSH listening on 127.0.0.1:{port}" if listening else f"SSH not responding on port {port}"
        }

        if not listening:
            if is_termux:
                self.manual_actions.append(f"Termux OpenSSH is not running on port {port}. Start it by running: sshd")
            else:
                self.warnings.append(f"SSH port {port} is not listening locally.")
                data["status"] = STATUS_DEGRADED

        return data

    def check_tailscale(self) -> Dict[str, Any]:
        """Checks Tailscale status and peer reachability without mutating state."""
        tailscale_bin = shutil.which("tailscale")
        if not tailscale_bin:
            return {
                "status": STATUS_DEGRADED,
                "installed": False,
                "message": "tailscale CLI not found on PATH (may be running as Android VPN App)"
            }

        # Run tailscale status --json
        raw = run_cmd_safe(["tailscale", "status", "--json"], timeout=2.5)
        if not raw:
            # Fallback to plain tailscale status
            plain = run_cmd_safe(["tailscale", "status"], timeout=2.5)
            if plain:
                is_running = "Logged out" not in plain and "Stopped" not in plain
                return {
                    "status": STATUS_HEALTHY if is_running else STATUS_DEGRADED,
                    "installed": True,
                    "backend_state": "Running" if is_running else "Stopped",
                    "message": "Tailscale active" if is_running else "Tailscale stopped or logged out"
                }
            return {
                "status": STATUS_DEGRADED,
                "installed": True,
                "message": "Unable to query tailscale daemon (daemon may be stopped)"
            }

        try:
            data = json.loads(raw)
            backend_state = data.get("BackendState", "Unknown")
            self_node = data.get("Self", {})
            self_ips = self_node.get("TailscaleIPs", [])
            online = self_node.get("Online", backend_state.lower() == "running")
            peers = data.get("Peer", {})

            peer_summary = []
            peers_online = 0
            for _, p in peers.items():
                is_p_online = p.get("Online", False)
                if is_p_online:
                    peers_online += 1
                peer_summary.append({
                    "hostname": p.get("HostName", "unknown"),
                    "online": is_p_online,
                    "active": p.get("Active", False),
                })

            is_healthy = backend_state.lower() == "running" and online
            st = STATUS_HEALTHY if is_healthy else STATUS_DEGRADED

            if not is_healthy:
                self.manual_actions.append(f"Tailscale is {backend_state}. Verify connection via: tailscale up")

            return {
                "status": st,
                "installed": True,
                "backend_state": backend_state,
                "tailscale_ip": self_ips[0] if self_ips else None,
                "online": online,
                "peers_count": len(peers),
                "peers_online": peers_online,
                "peers": peer_summary[:5]  # Limit to 5 peers in report
            }
        except Exception:
            return {
                "status": STATUS_DEGRADED,
                "installed": True,
                "message": "Failed to parse tailscale status"
            }

    def check_node_api(self) -> Dict[str, Any]:
        """Checks Node API process, PID consistency, port, and /health endpoint."""
        pattern = "services/node-api/app.py"
        pid_info = self.check_service_process(NODE_API_PID_FILE, pattern)
        port_open = check_tcp_port("127.0.0.1", 8080, timeout=1.5)
        is_running = pid_info["status"] in ("RUNNING", "RUNNING_UNTRACKED")
        effective_pid = pid_info.get("pid")

        data: Dict[str, Any] = {
            "status": STATUS_HEALTHY,
            "port": 8080,
            "port_open": port_open,
            "process": pid_info,
            "endpoint": "http://127.0.0.1:8080/health"
        }

        if not is_running:
            data["status"] = STATUS_ACTION
            start_cmd = "python services/node-api/app.py" if sys.platform == "win32" else "bash scripts/start.sh"
            if pid_info["status"] == "STALE_PID_FILE":
                self.manual_actions.append(f"Node API has a stale PID file (PID {pid_info.get('pid')} is dead). Service stopped.")
            else:
                self.manual_actions.append(f"Node API is stopped. Start with: {start_cmd}")
            return data

        if pid_info["status"] == "RUNNING_UNTRACKED":
            self.warnings.append(f"Node API process running (PID: {effective_pid}) but untracked by runtime/node-api.pid.")

        # Check Health endpoint
        code, body_json, raw = http_get_json("http://127.0.0.1:8080/health", timeout=2.5)
        is_online = bool(code == 200 and body_json and body_json.get("status") in ("online", True, "ok", "healthy"))
        data["health_response"] = {
            "http_status": code,
            "is_online": is_online
        }

        if is_online:
            data["status"] = STATUS_DEGRADED if pid_info["status"] == "RUNNING_UNTRACKED" else STATUS_HEALTHY
        else:
            data["status"] = STATUS_DEGRADED
            self.warnings.append(f"Node API running on PID {effective_pid} but /health returned HTTP {code}: {raw[:80]}")

        return data

    def check_cloudflare_tunnel(self) -> Dict[str, Any]:
        """Checks cloudflared process, log error patterns, and connection status."""
        pattern = "cloudflared"
        pid_info = self.check_service_process(CLOUDFLARED_PID_FILE, pattern)
        is_running = pid_info["status"] in ("RUNNING", "RUNNING_UNTRACKED")
        effective_pid = pid_info.get("pid")

        data: Dict[str, Any] = {
            "status": STATUS_HEALTHY,
            "process": pid_info,
            "log_file": str(CLOUDFLARED_LOG.relative_to(BASE_DIR)) if CLOUDFLARED_LOG.exists() else None,
            "connection_state": "UNKNOWN"
        }

        if not is_running:
            data["status"] = STATUS_ACTION
            data["connection_state"] = "DISCONNECTED"
            start_cmd = "cloudflared tunnel run" if sys.platform == "win32" else "bash scripts/start.sh"
            if pid_info["status"] == "STALE_PID_FILE":
                self.manual_actions.append(f"Cloudflare Tunnel has a stale PID file (PID {pid_info.get('pid')} is dead). Service stopped.")
            else:
                self.manual_actions.append(f"Cloudflare Tunnel is stopped. Start with: {start_cmd}")
            return data

        if pid_info["status"] == "RUNNING_UNTRACKED":
            self.warnings.append(f"Cloudflare Tunnel running (PID: {effective_pid}) but untracked by runtime/cloudflared.pid.")

        # Inspect log for recent connection / error patterns
        if CLOUDFLARED_LOG.exists():
            try:
                lines = CLOUDFLARED_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                log_text = "\n".join(lines)

                has_connected = any(
                    marker in log_text for marker in [
                        "Registered tunnel connection",
                        "QUIC connection successful",
                        "Connection registered",
                        "PASS",
                    ]
                )

                has_quic_err = "Failed to create new quic connection" in log_text or ("QUIC" in log_text and "error" in log_text.lower())
                has_retry_err = "retrying in" in log_text.lower() or "failed to dial to edge" in log_text.lower()

                if has_retry_err or has_quic_err:
                    data["status"] = STATUS_DEGRADED
                    data["connection_state"] = "RETRYING_OR_DEGRADED"
                    if has_quic_err:
                        self.warnings.append("Cloudflare Tunnel experiencing QUIC connectivity issues. (Carrier may block UDP 7844).")
                    else:
                        self.warnings.append("Cloudflare Tunnel logs indicate reconnect attempts.")
                elif has_connected:
                    data["connection_state"] = "CONNECTED"
                else:
                    data["connection_state"] = "ACTIVE"

                # Provide 3 sanitized log excerpts
                sanitized_excerpts = [redact_sensitive_text(l) for l in lines[-3:] if l.strip()]
                data["recent_log_excerpts"] = sanitized_excerpts
            except Exception as e:
                data["log_error"] = str(e)

        return data

    def check_node_worker(self) -> Dict[str, Any]:
        """Checks Node Worker daemon process and registration state."""
        pattern = "agent/node-agent.py work"
        pid_info = self.check_service_process(NODE_WORKER_PID_FILE, pattern)
        is_running = pid_info["status"] in ("RUNNING", "RUNNING_UNTRACKED")
        effective_pid = pid_info.get("pid")

        reg_file = RUNTIME_DIR / "registration.json"
        is_registered = reg_file.exists()

        data: Dict[str, Any] = {
            "status": STATUS_HEALTHY,
            "process": pid_info,
            "registered": is_registered,
        }

        if is_registered:
            try:
                with open(reg_file, "r", encoding="utf-8") as f:
                    reg_data = json.load(f)
                    data["controller_url"] = reg_data.get("controller_url")
                    data["node_id"] = reg_data.get("node_id")
            except Exception:
                pass

        if not is_running:
            data["status"] = STATUS_DEGRADED
            start_cmd = "python agent/node-agent.py work --daemon"
            self.warnings.append(f"Node Worker daemon is not running. Start with: {start_cmd}")
        else:
            if pid_info["status"] == "RUNNING_UNTRACKED":
                self.warnings.append(f"Node Worker process running (PID: {effective_pid}) but untracked by runtime/node-worker.pid.")
            if not is_registered:
                data["status"] = STATUS_DEGRADED
                self.warnings.append("Node Worker process running but registration state missing at runtime/registration.json.")

        return data

    def check_controller_and_router(self) -> Dict[str, Any]:
        """Checks Controller HTTP service (:8000) and App Router (:8088) if present."""
        data: Dict[str, Any] = {"status": STATUS_HEALTHY}

        # Check Controller port 8000
        ctrl_listening = check_tcp_port("127.0.0.1", 8000, timeout=1.5)
        ctrl_code, ctrl_data, _ = http_get_json("http://127.0.0.1:8000/health", timeout=2.0)

        data["controller"] = {
            "port": 8000,
            "listening": ctrl_listening,
            "http_status": ctrl_code,
            "healthy": ctrl_code in (200, 404, 401)  # If listening and returns HTTP, it is active
        }

        # Check App Router port 8088
        router_listening = check_tcp_port("127.0.0.1", 8088, timeout=1.5)
        data["app_router"] = {
            "port": 8088,
            "listening": router_listening
        }

        if not ctrl_listening:
            data["status"] = STATUS_DEGRADED
            self.warnings.append("Cluster Controller is not listening on 127.0.0.1:8000.")

        return data

    def run_all(self) -> Dict[str, Any]:
        """Executes full diagnostic audit based on local applicability."""
        env = self.detect_platform_and_role()
        is_termux = env["is_termux"]

        self.results["environment"] = env
        self.results["timestamp"] = self.timestamp
        self.results["services"] = {}

        # 1. System resources
        self.results["resources"] = self.check_system_resources(is_termux)

        # 2. Configuration files
        self.results["config_files"] = self.check_configuration_files(env)

        # 3. SSH Service (for Termux or Linux)
        if is_termux or sys.platform.startswith("linux"):
            self.results["services"]["ssh"] = self.check_ssh_service(is_termux)

        # 4. Tailscale status
        self.results["services"]["tailscale"] = self.check_tailscale()

        # 5. Node API (if applicable)
        if env["has_node_api_files"] or env["configured_role"] in ("compute", "storage", "ingress"):
            self.results["services"]["node_api"] = self.check_node_api()

        # 6. Cloudflare Tunnel (if applicable)
        if env["has_cloudflared"] or (SECRETS_DIR / "cloudflared.token").exists():
            self.results["services"]["cloudflare_tunnel"] = self.check_cloudflare_tunnel()

        # 7. Node Worker Daemon (if applicable)
        if (BASE_DIR / "agent" / "node-agent.py").exists():
            self.results["services"]["node_worker"] = self.check_node_worker()

        # 8. Controller & Router (if controller files exist on host)
        if env["has_controller_files"]:
            self.results["services"]["controller_router"] = self.check_controller_and_router()

        # Compute overall summary status
        has_action = bool(self.manual_actions)
        for _, check in self.results.get("services", {}).items():
            if check.get("status") == STATUS_ACTION:
                has_action = True
                break

        has_degraded = bool(self.warnings) or (self.results.get("resources", {}).get("status") == STATUS_DEGRADED)
        for _, check in self.results.get("services", {}).items():
            if check.get("status") == STATUS_DEGRADED:
                has_degraded = True
                break

        if has_action:
            self.summary_status = STATUS_ACTION
        elif has_degraded:
            self.summary_status = STATUS_DEGRADED
        else:
            self.summary_status = STATUS_HEALTHY

        self.results["summary"] = {
            "status": self.summary_status,
            "manual_actions": self.manual_actions,
            "warnings": self.warnings
        }

        return self.results

    def save_report(self) -> Path:
        """Saves a timestamped, sanitized report (JSON and text) to runtime/reports/."""
        if not self.results:
            self.run_all()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        t_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_file = REPORTS_DIR / f"doctor_{t_slug}.json"
        txt_file = REPORTS_DIR / f"doctor_{t_slug}.txt"

        # Sanitize data before persistence
        sanitized = redact_sensitive_data(self.results)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(sanitized, f, indent=2)

        # Write text report
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(format_text_report(sanitized))

        return json_file

    def repair(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Executes targeted, safe self-healing recovery on unhealthy components.
        Enforces locking, circuit breakers, process verification, and post-restart validation.
        """
        diag = self.run_all()
        summary = diag.get("summary", {})
        initial_status = summary.get("status", STATUS_HEALTHY)

        if initial_status == STATUS_HEALTHY:
            recovery_info = {
                "initial_status": STATUS_HEALTHY,
                "final_status": STATUS_HEALTHY,
                "dry_run": dry_run,
                "actions": [],
                "message": "All services healthy. No recovery action needed.",
                "verification": {}
            }
            self.recovery_results = recovery_info
            self.results["recovery"] = recovery_info
            return recovery_info

        actions_taken = []
        lock = RecoveryLock(RECOVERY_LOCK_FILE)

        if not dry_run:
            ok, msg = lock.acquire()
            if not ok:
                recovery_info = {
                    "initial_status": initial_status,
                    "final_status": STATUS_DEGRADED,
                    "dry_run": dry_run,
                    "error": msg,
                    "actions": []
                }
                self.recovery_results = recovery_info
                self.results["recovery"] = recovery_info
                return recovery_info

        try:
            cb = CircuitBreaker(RECOVERY_STATE_FILE)
            env = diag.get("environment", {})
            is_termux = env.get("is_termux", False)
            services = diag.get("services", {})

            # 1. Termux wake-lock
            if is_termux:
                act = self.repair_termux_wake_lock(dry_run=dry_run)
                if act:
                    actions_taken.append(act)

            # 2. Repair each applicable unhealthy service
            for s_name, s_data in services.items():
                s_status = s_data.get("status", STATUS_HEALTHY)
                if s_status in (STATUS_DEGRADED, STATUS_ACTION):
                    can_try, count, cb_msg = cb.can_attempt(s_name)
                    if not can_try:
                        actions_taken.append({
                            "service": s_name,
                            "action": "SKIPPED_CIRCUIT_BREAKER",
                            "message": f"Circuit breaker tripped ({count} recent failures). Manual repair required.",
                            "success": False
                        })
                        continue

                    act = self.repair_service(s_name, dry_run=dry_run)
                    if act:
                        actions_taken.append(act)
                        if not dry_run:
                            cb.record_attempt(s_name, success=act.get("success", False))

            # 3. Post-recovery verification
            verification = {}
            if not dry_run:
                time.sleep(1.0)
                re_diag = self.run_all()
                re_summary = re_diag.get("summary", {})
                re_status = re_summary.get("status", STATUS_HEALTHY)

                final_status = STATUS_RECOVERED if re_status == STATUS_HEALTHY else re_status
                self.summary_status = final_status
                self.results["summary"]["status"] = final_status

                verification = {
                    "status_after_repair": re_status,
                    "services": re_diag.get("services", {}),
                    "warnings": re_summary.get("warnings", []),
                    "manual_actions": re_summary.get("manual_actions", [])
                }
            else:
                final_status = initial_status

            recovery_info = {
                "initial_status": initial_status,
                "final_status": final_status,
                "dry_run": dry_run,
                "actions": actions_taken,
                "verification": verification
            }
            self.recovery_results = recovery_info
            self.results["recovery"] = recovery_info
            return recovery_info
        finally:
            if not dry_run:
                lock.release()

    def repair_service(self, service_name: str, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """Dispatches targeted repair for a named service."""
        if service_name == "node_api":
            return self.repair_node_api(dry_run=dry_run)
        elif service_name == "node_worker":
            return self.repair_node_worker(dry_run=dry_run)
        elif service_name == "cloudflare_tunnel":
            return self.repair_cloudflare_tunnel(dry_run=dry_run)
        elif service_name == "ssh":
            return self.repair_ssh(dry_run=dry_run)
        elif service_name == "controller_router":
            return self.repair_controller_and_router(dry_run=dry_run)
        elif service_name == "tailscale":
            return self.repair_tailscale(dry_run=dry_run)
        return None

    def repair_node_api(self, dry_run: bool = False) -> Dict[str, Any]:
        """Safely recovers Node API service."""
        node_api_info = self.check_node_api()
        proc_info = node_api_info.get("process", {})
        proc_st = proc_info.get("status")
        pid = proc_info.get("pid")
        is_online = node_api_info.get("health_response", {}).get("is_online", False)

        # 1. Untracked process that is healthy: adopt it
        if proc_st == "RUNNING_UNTRACKED" and is_online:
            if dry_run:
                return {
                    "service": "node_api",
                    "action": "WOULD_ADOPT_PID",
                    "message": f"Would adopt running Node API PID {pid} into runtime/node-api.pid",
                    "success": True
                }
            NODE_API_PID_FILE.write_text(f"{pid}\n", encoding="utf-8")
            return {
                "service": "node_api",
                "action": "ADOPTED_PID",
                "message": f"Adopted untracked Node API process (PID {pid})",
                "success": True
            }

        # 2. Stale PID file: remove it
        if proc_st == "STALE_PID_FILE":
            if not dry_run and NODE_API_PID_FILE.exists():
                NODE_API_PID_FILE.unlink(missing_ok=True)

        # 3. Running but unresponsive / unhealthy: terminate verified PID
        if proc_st in ("RUNNING", "RUNNING_UNTRACKED") and not is_online:
            if dry_run:
                return {
                    "service": "node_api",
                    "action": "WOULD_RESTART_UNHEALTHY",
                    "message": f"Would terminate unresponsive Node API PID {pid} and restart",
                    "success": True
                }
            safe_terminate_process(pid, "services/node-api/app.py")
            NODE_API_PID_FILE.unlink(missing_ok=True)

        # 4. Start Node API
        script_path = self.base_dir / "services" / "node-api" / "app.py"
        if not script_path.exists():
            return {
                "service": "node_api",
                "action": "FAILED",
                "message": f"Missing script at {script_path.relative_to(self.base_dir)}",
                "success": False
            }

        if dry_run:
            return {
                "service": "node_api",
                "action": "WOULD_START",
                "message": "Would launch Node API on 127.0.0.1:8080",
                "success": True
            }

        cmd = [sys.executable, str(script_path)]
        new_pid = spawn_background_process(cmd, NODE_API_LOG, self.base_dir)
        if not new_pid:
            return {
                "service": "node_api",
                "action": "FAILED",
                "message": "Failed to spawn Node API process",
                "success": False
            }

        NODE_API_PID_FILE.write_text(f"{new_pid}\n", encoding="utf-8")

        # 5. Post-restart health check with retries
        verified = False
        for _ in range(10):
            time.sleep(0.5)
            code, body, _ = http_get_json("http://127.0.0.1:8080/health", timeout=1.5)
            if code == 200 and body and body.get("status") in ("online", True, "ok", "healthy"):
                verified = True
                break

        return {
            "service": "node_api",
            "action": "STARTED" if verified else "STARTED_UNVERIFIED",
            "message": f"Node API started (PID {new_pid})" + (" and verified healthy" if verified else " but health probe failed"),
            "pid": new_pid,
            "success": verified
        }

    def repair_node_worker(self, dry_run: bool = False) -> Dict[str, Any]:
        """Safely recovers Node Worker daemon."""
        worker_info = self.check_node_worker()
        proc_info = worker_info.get("process", {})
        proc_st = proc_info.get("status")
        pid = proc_info.get("pid")

        # 1. Untracked process: adopt it
        if proc_st == "RUNNING_UNTRACKED":
            if dry_run:
                return {
                    "service": "node_worker",
                    "action": "WOULD_ADOPT_PID",
                    "message": f"Would adopt running Node Worker PID {pid} into runtime/node-worker.pid",
                    "success": True
                }
            NODE_WORKER_PID_FILE.write_text(f"{pid}\n", encoding="utf-8")
            return {
                "service": "node_worker",
                "action": "ADOPTED_PID",
                "message": f"Adopted untracked Node Worker process (PID {pid})",
                "success": True
            }

        # 2. Stale PID file: remove it
        if proc_st == "STALE_PID_FILE":
            if not dry_run and NODE_WORKER_PID_FILE.exists():
                NODE_WORKER_PID_FILE.unlink(missing_ok=True)

        # 3. Check registration state
        reg_file = RUNTIME_DIR / "registration.json"
        if not reg_file.exists():
            return {
                "service": "node_worker",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "Node registration missing. Run: python agent/node-agent.py register",
                "success": False
            }

        # 4. Start Worker
        script_path = self.base_dir / "agent" / "node-agent.py"
        if not script_path.exists():
            return {
                "service": "node_worker",
                "action": "FAILED",
                "message": "Missing node-agent.py script",
                "success": False
            }

        if dry_run:
            return {
                "service": "node_worker",
                "action": "WOULD_START",
                "message": "Would launch Node Worker daemon (agent/node-agent.py work --daemon)",
                "success": True
            }

        cmd = [sys.executable, str(script_path), "work", "--daemon"]
        new_pid = spawn_background_process(cmd, NODE_WORKER_LOG, self.base_dir)
        if not new_pid:
            return {
                "service": "node_worker",
                "action": "FAILED",
                "message": "Failed to spawn Node Worker process",
                "success": False
            }

        NODE_WORKER_PID_FILE.write_text(f"{new_pid}\n", encoding="utf-8")
        time.sleep(1.0)
        verified = check_process_alive(new_pid, "agent/node-agent.py work")

        return {
            "service": "node_worker",
            "action": "STARTED" if verified else "FAILED",
            "message": f"Node Worker daemon started (PID {new_pid})" if verified else "Worker process failed to stay alive",
            "pid": new_pid,
            "success": verified
        }

    def repair_cloudflare_tunnel(self, dry_run: bool = False) -> Dict[str, Any]:
        """Safely recovers Cloudflare Tunnel with fallback detection."""
        cf_bin = shutil.which("cloudflared")
        if not cf_bin:
            return {
                "service": "cloudflare_tunnel",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "cloudflared binary not found in PATH",
                "success": False
            }

        token_file = SECRETS_DIR / "cloudflared.token"
        if not token_file.exists() or token_file.stat().st_size == 0:
            return {
                "service": "cloudflare_tunnel",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "Token missing at config/secrets/cloudflared.token",
                "success": False
            }

        cf_info = self.check_cloudflare_tunnel()
        proc_info = cf_info.get("process", {})
        proc_st = proc_info.get("status")
        pid = proc_info.get("pid")

        # 1. Adopt untracked PID
        if proc_st == "RUNNING_UNTRACKED" and cf_info.get("connection_state") in ("CONNECTED", "ACTIVE"):
            if dry_run:
                return {
                    "service": "cloudflare_tunnel",
                    "action": "WOULD_ADOPT_PID",
                    "message": f"Would adopt running cloudflared PID {pid} into runtime/cloudflared.pid",
                    "success": True
                }
            CLOUDFLARED_PID_FILE.write_text(f"{pid}\n", encoding="utf-8")
            return {
                "service": "cloudflare_tunnel",
                "action": "ADOPTED_PID",
                "message": f"Adopted untracked cloudflared process (PID {pid})",
                "success": True
            }

        # 2. Terminate hung/retrying process
        if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
            if dry_run:
                return {
                    "service": "cloudflare_tunnel",
                    "action": "WOULD_RESTART_DEGRADED",
                    "message": f"Would restart degraded cloudflared PID {pid}",
                    "success": True
                }
            safe_terminate_process(pid, "cloudflared")
            CLOUDFLARED_PID_FILE.unlink(missing_ok=True)

        if proc_st == "STALE_PID_FILE":
            if not dry_run and CLOUDFLARED_PID_FILE.exists():
                CLOUDFLARED_PID_FILE.unlink(missing_ok=True)

        # 3. Check logs for QUIC protocol errors
        quic_fallback = False
        if CLOUDFLARED_LOG.exists():
            try:
                log_txt = CLOUDFLARED_LOG.read_text(encoding="utf-8", errors="replace")[-2000:]
                if "Failed to create new quic connection" in log_txt or "QUIC" in log_txt:
                    quic_fallback = True
            except Exception:
                pass

        cmd = [cf_bin, "tunnel", "--no-prechecks", "run", "--token-file", str(token_file)]
        if quic_fallback:
            cmd.extend(["--protocol", "http2"])

        if dry_run:
            return {
                "service": "cloudflare_tunnel",
                "action": "WOULD_START",
                "message": f"Would launch cloudflared tunnel {'with HTTP/2 fallback' if quic_fallback else ''}",
                "success": True
            }

        new_pid = spawn_background_process(cmd, CLOUDFLARED_LOG, self.base_dir)
        if not new_pid:
            return {
                "service": "cloudflare_tunnel",
                "action": "FAILED",
                "message": "Failed to spawn cloudflared process",
                "success": False
            }

        CLOUDFLARED_PID_FILE.write_text(f"{new_pid}\n", encoding="utf-8")
        time.sleep(2.0)
        verified = check_process_alive(new_pid, "cloudflared")

        return {
            "service": "cloudflare_tunnel",
            "action": "STARTED" if verified else "FAILED",
            "message": f"Cloudflare Tunnel started (PID {new_pid})" + (" with HTTP/2 fallback" if quic_fallback else ""),
            "pid": new_pid,
            "success": verified
        }

    def repair_ssh(self, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """Safely recovers SSH on Termux."""
        if not is_termux_environment():
            return None

        ssh_info = self.check_ssh_service(is_termux=True)
        if ssh_info.get("listening"):
            return None

        sshd_bin = shutil.which("sshd")
        if not sshd_bin:
            return {
                "service": "ssh",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "OpenSSH not installed. Run: pkg install openssh && sshd",
                "success": False
            }

        if dry_run:
            return {
                "service": "ssh",
                "action": "WOULD_START",
                "message": "Would start Termux OpenSSH daemon (sshd on port 8022)",
                "success": True
            }

        subprocess.run(["sshd"], check=False)
        time.sleep(1.0)
        verified = check_tcp_port("127.0.0.1", 8022, timeout=1.5)

        return {
            "service": "ssh",
            "action": "STARTED" if verified else "FAILED",
            "message": "Termux OpenSSH started on port 8022" if verified else "sshd invoked but port 8022 not listening",
            "success": verified
        }

    def repair_termux_wake_lock(self, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """Idempotently acquires Termux wake-lock."""
        if not is_termux_environment():
            return None

        tool = shutil.which("termux-wake-lock")
        if not tool:
            return None

        if dry_run:
            return {
                "service": "termux_wake_lock",
                "action": "WOULD_ACQUIRE",
                "message": "Would acquire termux-wake-lock",
                "success": True
            }

        subprocess.run(["termux-wake-lock"], check=False)
        return {
            "service": "termux_wake_lock",
            "action": "ACQUIRED",
            "message": "Acquired Termux wake-lock to prevent background sleep",
            "success": True
        }

    def repair_controller_and_router(self, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """Safely recovers Controller and App Router on controller host."""
        if not self.is_controller_host():
            return None

        actions = []
        # 1. Controller (:8000)
        ctrl_listening = check_tcp_port("127.0.0.1", 8000, timeout=1.5)
        if not ctrl_listening:
            ctrl_script = self.base_dir / "controller" / "controller.py"
            if ctrl_script.exists():
                if dry_run:
                    actions.append("Would start Controller on port 8000")
                else:
                    cmd = [sys.executable, str(ctrl_script), "start", "--host", "0.0.0.0", "--port", "8000"]
                    c_pid = spawn_background_process(cmd, CONTROLLER_LOG, self.base_dir)
                    if c_pid:
                        CONTROLLER_PID_FILE.write_text(f"{c_pid}\n", encoding="utf-8")
                        time.sleep(1.5)
                        if check_tcp_port("127.0.0.1", 8000, timeout=2.0):
                            actions.append(f"Controller started (PID {c_pid})")
                        else:
                            actions.append(f"Controller launched (PID {c_pid}) but port 8000 not responding")

        # 2. Router (:8088)
        router_listening = check_tcp_port("127.0.0.1", 8088, timeout=1.5)
        if not router_listening:
            router_script = self.base_dir / "router" / "app_router.py"
            if router_script.exists():
                if dry_run:
                    actions.append("Would start App Router on port 8088")
                else:
                    cmd = [sys.executable, str(router_script)]
                    r_pid = spawn_background_process(cmd, ROUTER_LOG, self.base_dir)
                    if r_pid:
                        ROUTER_PID_FILE.write_text(f"{r_pid}\n", encoding="utf-8")
                        time.sleep(1.0)
                        if check_tcp_port("127.0.0.1", 8088, timeout=2.0):
                            actions.append(f"App Router started (PID {r_pid})")
                        else:
                            actions.append(f"App Router launched (PID {r_pid}) but port 8088 not responding")

        if actions:
            return {
                "service": "controller_router",
                "action": "REPAIRED" if not dry_run else "WOULD_REPAIR",
                "message": "; ".join(actions),
                "success": not dry_run
            }
        return None

    def repair_tailscale(self, dry_run: bool = False) -> Dict[str, Any]:
        """Provides non-destructive, guarded Tailscale instructions."""
        if is_termux_environment():
            msg = "Tailscale on Android requires OS VPN permission. Open the Tailscale app on your phone and tap Connect."
            return {
                "service": "tailscale",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": msg,
                "success": False
            }

        ts_bin = shutil.which("tailscale")
        if not ts_bin:
            return {
                "service": "tailscale",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "tailscale CLI not found on PATH. Install Tailscale from https://tailscale.com/download",
                "success": False
            }

        # Check status
        st = run_cmd_safe(["tailscale", "status"], timeout=2.5)
        if "Logged out" in st:
            return {
                "service": "tailscale",
                "action": "MANUAL_ACTION_REQUIRED",
                "message": "Tailscale requires authentication. Run: tailscale up",
                "success": False
            }
        elif "Stopped" in st:
            if dry_run:
                return {
                    "service": "tailscale",
                    "action": "WOULD_START",
                    "message": "Would start Tailscale connection (tailscale up)",
                    "success": True
                }
            run_cmd_safe(["tailscale", "up"], timeout=5.0)
            time.sleep(1.0)
            re_st = run_cmd_safe(["tailscale", "status"], timeout=2.0)
            verified = "Stopped" not in re_st and "Logged out" not in re_st
            return {
                "service": "tailscale",
                "action": "STARTED" if verified else "FAILED",
                "message": "Tailscale started" if verified else "Failed to bring up Tailscale",
                "success": verified
            }

        return {
            "service": "tailscale",
            "action": "INSPECTED",
            "message": "Tailscale is in a non-running state. Check 'tailscale status'.",
            "success": False
        }

    def run_watchdog(self, interval: int = 60, iterations: Optional[int] = None) -> int:
        """
        Runs bounded local monitoring. Periodically verifies health and triggers
        targeted self-healing when services become degraded or failed.
        """
        import signal

        print("=================================================================")
        print(" PersonalServer Self-Healing Watchdog")
        print("=================================================================")
        print(f"Monitoring Interval: {interval}s")
        print(f"Watchdog PID:        {os.getpid()}")
        print(f"PID File:            {WATCHDOG_PID_FILE.relative_to(self.base_dir)}")
        print("Press Ctrl+C to stop watchdog monitoring.")
        print("=================================================================\n")

        WATCHDOG_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATCHDOG_PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")

        running = [True]

        def handle_signal(sig, frame):
            running[0] = False
            print("\n[+] Received termination signal. Stopping watchdog...")

        signal.signal(signal.SIGINT, handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handle_signal)

        cycle = 0
        try:
            while running[0]:
                cycle += 1
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now_str}] Watchdog Cycle #{cycle} checking...")

                diag = self.run_all()
                st = diag.get("summary", {}).get("status", STATUS_HEALTHY)

                if st == STATUS_HEALTHY:
                    print(f"[{now_str}] Cycle #{cycle}: All services HEALTHY.")
                else:
                    print(f"[{now_str}] Cycle #{cycle}: Status is {st}. Initiating targeted recovery...")
                    rep = self.repair(dry_run=False)
                    final_st = rep.get("final_status", st)
                    print(f"[{now_str}] Cycle #{cycle}: Recovery completed with status {final_st}.")
                    for act in rep.get("actions", []):
                        print(f"  * {act.get('service')}: {act.get('message')}")

                if iterations and cycle >= iterations:
                    print(f"[+] Reached maximum requested cycles ({iterations}). Exiting watchdog.")
                    break

                for _ in range(interval):
                    if not running[0]:
                        break
                    time.sleep(1)
        finally:
            if WATCHDOG_PID_FILE.exists():
                try:
                    WATCHDOG_PID_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

        return 0

    def stop_watchdog(self) -> Tuple[bool, str]:
        """Safely stops any running background watchdog instance."""
        if not WATCHDOG_PID_FILE.exists():
            return False, "No watchdog process currently active (runtime/watchdog.pid missing)"

        try:
            pid = int(WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            WATCHDOG_PID_FILE.unlink(missing_ok=True)
            return False, "Corrupt watchdog PID file removed."

        if not check_process_alive(pid, pattern="doctor.py"):
            WATCHDOG_PID_FILE.unlink(missing_ok=True)
            return True, f"Cleaned up stale watchdog PID file (PID {pid} was not running)."

        stopped = safe_terminate_process(pid, pattern="doctor.py", timeout=5.0)
        WATCHDOG_PID_FILE.unlink(missing_ok=True)
        if stopped:
            return True, f"Watchdog process (PID {pid}) stopped successfully."
        else:
            return False, f"Failed to stop watchdog process (PID {pid})."


# ==============================================================================
# Terminal Presentation & Text Report Formatting
# ==============================================================================

def format_text_report(res: Dict[str, Any]) -> str:
    """Formats a human-readable text diagnostic report."""
    env = res.get("environment", {})
    summary = res.get("summary", {})
    status = summary.get("status", STATUS_HEALTHY)
    services = res.get("services", {})
    resources = res.get("resources", {}).get("details", {})

    lines = []
    lines.append("=" * 65)
    lines.append(" PersonalServer Diagnostic Report")
    lines.append("=" * 65)
    lines.append(f"Timestamp:     {res.get('timestamp')}")
    lines.append(f"Host / Node:   {env.get('hostname')} ({env.get('node_id', 'unknown')})")
    lines.append(f"Platform:      {env.get('platform')} ({env.get('architecture')})")
    lines.append(f"Overall State: {status}")
    lines.append("-" * 65)
    lines.append(f"{'SERVICE':<22} | {'STATE':<14} | {'DETAILS'}")
    lines.append("-" * 65)

    for s_name, s_data in services.items():
        st = s_data.get("status", STATUS_HEALTHY)
        label = s_name.replace("_", " ").title()
        proc = s_data.get("process", {})
        proc_st = proc.get("status", "STOPPED")
        pid = proc.get("pid")

        if s_name == "node_api":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                h_code = s_data.get("health_response", {}).get("http_status")
                h_str = "OK" if h_code == 200 else f"HTTP {h_code}"
                detail = f"Running (PID: {pid}), Health: {h_str}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped (Port 8080)"
        elif s_name == "cloudflare_tunnel":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                detail = f"Running (PID: {pid}), State: {s_data.get('connection_state')}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped"
        elif s_name == "node_worker":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                detail = f"Running (PID: {pid}), Registered: {'Yes' if s_data.get('registered') else 'No'}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped"
        elif s_name == "ssh":
            detail = f"Port: {s_data.get('port')}, Listening: {s_data.get('listening')}"
        elif s_name == "tailscale":
            ip = s_data.get("tailscale_ip") or "No IP"
            detail = f"{s_data.get('backend_state')}, IP: {ip}, Peers: {s_data.get('peers_count', 0)}"
        elif s_name == "controller_router":
            ctrl = s_data.get("controller", {})
            router = s_data.get("app_router", {})
            detail = f"Ctrl: {ctrl.get('http_status') or 'Offline'}, Router: {'Active' if router.get('listening') else 'Offline'}"
        else:
            detail = s_data.get("message", "")

        lines.append(f"{label:<22} | {st:<14} | {detail}")

    lines.append("-" * 65)
    disk = resources.get("disk", {})
    if disk:
        lines.append(f"Storage:       {disk.get('free_gb')} GB free ({disk.get('used_pct')}% used of {disk.get('total_gb')} GB)")
    mem = resources.get("memory", {})
    if mem:
        lines.append(f"Memory:        {mem.get('available_mb')} MB available ({mem.get('used_pct')}% used)")

    recovery = res.get("recovery")
    if recovery:
        lines.append("\n" + "=" * 65)
        mode_label = "DRY RUN RECOVERY PLAN" if recovery.get("dry_run") else "RECOVERY ACTIONS EXECUTED"
        lines.append(f" {mode_label}")
        lines.append("=" * 65)
        for act in recovery.get("actions", []):
            st_marker = "[OK]" if act.get("success") else ("[DRY RUN]" if recovery.get("dry_run") else "[FAILED]")
            lines.append(f"{st_marker:<10} | {act.get('service'):<20} | {act.get('message')}")
        if recovery.get("error"):
            lines.append(f"Lock/State Error: {recovery.get('error')}")

    warnings = summary.get("warnings", [])
    actions = summary.get("manual_actions", [])

    if warnings:
        lines.append("\nWarnings:")
        for w in warnings:
            lines.append(f"  * {w}")

    if actions:
        lines.append("\nRequired Actions:")
        for idx, act in enumerate(actions, 1):
            lines.append(f"  {idx}. {act}")

    lines.append("=" * 65)
    return "\n".join(lines)


def print_terminal_summary(res: Dict[str, Any], verbose: bool = False) -> None:
    """Prints a clean, concise, human-readable terminal status summary."""
    env = res.get("environment", {})
    summary = res.get("summary", {})
    status = summary.get("status", STATUS_HEALTHY)
    services = res.get("services", {})
    resources = res.get("resources", {}).get("details", {})

    status_color = {
        STATUS_HEALTHY: "\033[92m[HEALTHY]\033[0m",
        STATUS_RECOVERED: "\033[92m[RECOVERED]\033[0m",
        STATUS_DEGRADED: "\033[93m[DEGRADED]\033[0m",
        STATUS_ACTION: "\033[91m[NEEDS MANUAL ACTION]\033[0m",
    }.get(status, f"[{status}]")

    print("\n" + "=" * 65)
    print(" PersonalServer Diagnostic Engine (Doctor)")
    print("=" * 65)
    print(f"Host / Node:   {env.get('hostname')} ({env.get('node_id', 'unknown')})")
    print(f"Platform:      {env.get('platform')} ({env.get('architecture')})")
    print(f"Overall State: {status_color}")
    print("-" * 65)

    print(f"{'SERVICE':<22} | {'STATE':<14} | {'DETAILS'}")
    print("-" * 65)

    for s_name, s_data in services.items():
        st = s_data.get("status", STATUS_HEALTHY)
        label = s_name.replace("_", " ").title()

        st_fmt = {
            STATUS_HEALTHY: "\033[92mOK\033[0m",
            STATUS_RECOVERED: "\033[92mRECOVERED\033[0m",
            STATUS_DEGRADED: "\033[93mDEGRADED\033[0m",
            STATUS_ACTION: "\033[91mACTION\033[0m",
        }.get(st, st)

        proc = s_data.get("process", {})
        proc_st = proc.get("status", "STOPPED")
        pid = proc.get("pid")

        if s_name == "node_api":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                h_code = s_data.get("health_response", {}).get("http_status")
                h_str = "OK" if h_code == 200 else f"HTTP {h_code}"
                detail = f"Running (PID: {pid}), Health: {h_str}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped (Port 8080)"
        elif s_name == "cloudflare_tunnel":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                detail = f"Running (PID: {pid}), State: {s_data.get('connection_state')}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped"
        elif s_name == "node_worker":
            if proc_st in ("RUNNING", "RUNNING_UNTRACKED"):
                detail = f"Running (PID: {pid}), Reg: {'Yes' if s_data.get('registered') else 'No'}"
            elif proc_st == "STALE_PID_FILE":
                detail = f"Stale PID ({pid}), Stopped"
            else:
                detail = "Stopped"
        elif s_name == "ssh":
            detail = f"Port: {s_data.get('port')}, Listening: {s_data.get('listening')}"
        elif s_name == "tailscale":
            ip = s_data.get("tailscale_ip") or "No IP"
            detail = f"{s_data.get('backend_state')}, IP: {ip}, Peers: {s_data.get('peers_count', 0)}"
        elif s_name == "controller_router":
            ctrl = s_data.get("controller", {})
            router = s_data.get("app_router", {})
            detail = f"Ctrl: {ctrl.get('http_status') or 'Offline'}, Router: {'Active' if router.get('listening') else 'Offline'}"
        else:
            detail = s_data.get("message", "")

        print(f"{label:<22} | {st_fmt:<23} | {detail}")

    # Resources
    print("-" * 65)
    disk = resources.get("disk", {})
    if disk:
        print(f"Storage:       {disk.get('free_gb')} GB free ({disk.get('used_pct')}% used of {disk.get('total_gb')} GB)")
    mem = resources.get("memory", {})
    if mem:
        print(f"Memory:        {mem.get('available_mb')} MB available ({mem.get('used_pct')}% used)")

    # Recovery Details (if present)
    recovery = res.get("recovery")
    if recovery:
        print("\n" + "=" * 65)
        mode_label = "DRY RUN RECOVERY PLAN" if recovery.get("dry_run") else "RECOVERY ACTIONS EXECUTED"
        print(f" \033[96m{mode_label}\033[0m")
        print("=" * 65)
        for act in recovery.get("actions", []):
            if recovery.get("dry_run"):
                st_color = "\033[93m[DRY RUN]\033[0m"
            elif act.get("success"):
                st_color = "\033[92m[OK]\033[0m"
            else:
                st_color = "\033[91m[FAILED]\033[0m"
            print(f"{st_color:<18} | {act.get('service'):<20} | {act.get('message')}")
        if recovery.get("error"):
            print(f"\033[91mLock/State Error: {recovery.get('error')}\033[0m")
        print("-" * 65)

    # Warnings & Actions
    warnings = summary.get("warnings", [])
    actions = summary.get("manual_actions", [])

    if warnings:
        print("\n\033[93mWarnings:\033[0m")
        for w in warnings:
            print(f"  * {w}")

    if actions:
        print("\n\033[91mRequired Actions:\033[0m")
        for idx, act in enumerate(actions, 1):
            print(f"  {idx}. {act}")

    print("=" * 65 + "\n")


# ==============================================================================
# CLI Main
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="PersonalServer Unified Diagnostic & Recovery Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON report")
    parser.add_argument("--save-report", action="store_true", help="Save sanitized report to runtime/reports/")
    parser.add_argument("-v", "--verbose", action="store_true", help="Display verbose diagnostic logging")
    parser.add_argument("--repair", action="store_true", help="Perform safe, targeted self-healing recovery")
    parser.add_argument("--dry-run", action="store_true", help="Simulate recovery actions with zero side-effects")
    parser.add_argument("--watchdog", action="store_true", help="Run bounded local monitoring with circuit-breaker self-healing")
    parser.add_argument("--interval", type=int, default=60, help="Watchdog interval in seconds (default: 60)")
    parser.add_argument("--iterations", type=int, default=None, help="Limit watchdog to N cycles (default: run until stopped)")
    parser.add_argument("--stop-watchdog", action="store_true", help="Safely stop running watchdog instance")

    args = parser.parse_args()

    engine = DiagnosticEngine(base_dir=BASE_DIR)

    # 1. Stop Watchdog requested
    if args.stop_watchdog:
        ok, msg = engine.stop_watchdog()
        if args.json:
            print(json.dumps({"success": ok, "message": msg}, indent=2))
        else:
            print(f"[+] {msg}")
        return 0 if ok else 1

    # 2. Watchdog mode requested
    if args.watchdog:
        return engine.run_watchdog(interval=args.interval, iterations=args.iterations)

    # 3. Repair mode requested
    if args.repair:
        engine.repair(dry_run=args.dry_run)
        results = engine.results

        if args.save_report:
            report_path = engine.save_report()
            if not args.json:
                print(f"[+] Detailed recovery report saved to: {report_path.relative_to(BASE_DIR)}")

        if args.json:
            sanitized = redact_sensitive_data(results)
            print(json.dumps(sanitized, indent=2))
        else:
            print_terminal_summary(results, verbose=args.verbose)

        status = results.get("summary", {}).get("status", STATUS_HEALTHY)
        if status in (STATUS_HEALTHY, STATUS_RECOVERED):
            return 0
        elif status == STATUS_DEGRADED:
            return 1
        else:
            return 2

    # 4. Standard diagnostic mode
    results = engine.run_all()

    if args.save_report:
        report_path = engine.save_report()
        if not args.json:
            print(f"[+] Detailed report saved to: {report_path.relative_to(BASE_DIR)}")

    if args.json:
        sanitized = redact_sensitive_data(results)
        print(json.dumps(sanitized, indent=2))
    else:
        print_terminal_summary(results, verbose=args.verbose)

    status = results.get("summary", {}).get("status", STATUS_HEALTHY)
    if status in (STATUS_HEALTHY, STATUS_RECOVERED):
        return 0
    elif status == STATUS_DEGRADED:
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())

