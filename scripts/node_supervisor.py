#!/usr/bin/env python3
"""
PersonalServer Node Process Supervisor & Autonomous Self-Healing Daemon (NODE-006).
===================================================================================
Continuously monitors core local node daemons:
- Node API (:8080)
- Node Agent Worker
- Node Agent Heartbeat
- Controller / Router (when running on leader/controller nodes)

Automatically detects crashed or unresponsive services, resurrects them in detached
background sessions, enforces exponential backoff / anti-churn rate limits, and persists
operational health telemetry.
"""

import os
import sys
import time
import json
import signal
import socket
import logging
import argparse
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_DIR = BASE_DIR / "config"

SUPERVISOR_PID_FILE = RUNTIME_DIR / "node-supervisor.pid"
SUPERVISOR_STATE_FILE = RUNTIME_DIR / "node-supervisor.json"
SUPERVISOR_LOG_FILE = LOGS_DIR / "node-supervisor.log"

MAX_RESTARTS_PER_WINDOW = 5
WINDOW_SECONDS = 60.0
COOLDOWN_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 10.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Supervisor] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("node_supervisor")


def is_pid_alive(pid: int) -> bool:
    """Checks if a given process ID exists and is actively running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def is_port_responding(host: str, port: int, timeout: float = 1.0) -> bool:
    """Checks if a TCP socket connection can be established to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class NodeProcessSupervisor:
    """Autonomous self-healing supervisor managing local node daemons."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or BASE_DIR
        self.runtime_dir = self.base_dir / "runtime"
        self.logs_dir = self.base_dir / "logs"
        self.config_dir = self.base_dir / "config"

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.restart_history: Dict[str, List[float]] = {}
        self.cooldown_until: Dict[str, float] = {}
        self.resurrection_counts: Dict[str, int] = {}
        self.running = False

    def get_service_definitions(self) -> Dict[str, Dict[str, Any]]:
        """Returns the definitions of services monitored on this node."""
        py = sys.executable

        services: Dict[str, Dict[str, Any]] = {
            "node-api": {
                "name": "Node API Server",
                "cmd": [py, str(self.base_dir / "services" / "node-api" / "app.py")],
                "pid_file": self.runtime_dir / "node-api.pid",
                "port": 8080,
                "log_file": self.logs_dir / "node-api.log",
                "match_pattern": "services/node-api/app.py",
                "enabled": True
            },
            "node-worker": {
                "name": "Node Agent Worker",
                "cmd": [py, str(self.base_dir / "agent" / "node-agent.py"), "work", "--daemon"],
                "pid_file": self.runtime_dir / "node-worker.pid",
                "port": None,
                "log_file": self.logs_dir / "node-worker.log",
                "match_pattern": "agent/node-agent.py work",
                "enabled": True
            },
            "node-heartbeat": {
                "name": "Node Agent Heartbeat",
                "cmd": [py, str(self.base_dir / "agent" / "node-agent.py"), "heartbeat", "--daemon"],
                "pid_file": self.runtime_dir / "node-heartbeat.pid",
                "port": None,
                "log_file": self.logs_dir / "node-heartbeat.log",
                "match_pattern": "agent/node-agent.py heartbeat",
                "enabled": True
            }
        }

        # Check if this node also hosts the Controller or Router
        controller_json = self.config_dir / "controller.json"
        if controller_json.exists():
            services["controller"] = {
                "name": "Master Controller",
                "cmd": [py, str(self.base_dir / "controller" / "controller.py"), "start", "--host", "0.0.0.0", "--port", "8000"],
                "pid_file": self.runtime_dir / "controller.pid",
                "port": 8000,
                "log_file": self.logs_dir / "controller.log",
                "match_pattern": "controller/controller.py",
                "enabled": True
            }
            services["app-router"] = {
                "name": "Reverse Proxy Router",
                "cmd": [py, str(self.base_dir / "router" / "app_router.py")],
                "pid_file": self.runtime_dir / "app-router.pid",
                "port": 8088,
                "log_file": self.logs_dir / "app-router.log",
                "match_pattern": "router/app_router.py",
                "enabled": True
            }

        # Check Cloudflare Tunnel
        cf_token = self.config_dir / "secrets" / "cloudflared.token"
        cf_bin = shutil.which("cloudflared") or ("/data/data/com.termux/files/usr/bin/cloudflared" if Path("/data/data/com.termux/files/usr/bin/cloudflared").exists() else None)
        if cf_token.exists() and cf_bin:
            services["cloudflared"] = {
                "name": "Cloudflare Tunnel",
                "cmd": [str(cf_bin), "tunnel", "--no-prechecks", "run", "--token-file", str(cf_token)],
                "pid_file": self.runtime_dir / "cloudflared.pid",
                "port": None,
                "log_file": self.logs_dir / "cloudflared.log",
                "match_pattern": "cloudflared",
                "enabled": True
            }

        return services

    def read_pid(self, pid_file: Path) -> Optional[int]:
        """Reads a recorded PID from file if present and valid."""
        if not pid_file.exists():
            return None
        try:
            val = int(pid_file.read_text().strip())
            return val if val > 0 else None
        except Exception:
            return None

    def write_pid(self, pid_file: Path, pid: int):
        """Atomically persists a service PID."""
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid) + "\n")

    def check_service(self, name: str, svc: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
        """
        Inspects service health.
        Returns: (is_healthy, pid, details)
        """
        pid = self.read_pid(svc["pid_file"])
        port = svc.get("port")

        if pid is not None and is_pid_alive(pid):
            if port is not None:
                if is_port_responding("127.0.0.1", port):
                    return True, pid, f"Running (PID: {pid}, Port :{port} OK)"
                else:
                    return False, pid, f"Unresponsive (PID {pid} alive but port :{port} not responding)"
            return True, pid, f"Running (PID: {pid})"

        # Fallback check if port is responding despite stale PID
        if port is not None and is_port_responding("127.0.0.1", port):
            return True, None, f"Running (Port :{port} active, PID unrecorded)"

        return False, None, "Process is dead / stopped"

    def can_restart(self, name: str) -> Tuple[bool, str]:
        """Enforces restart rate limiting and anti-churn cooldown."""
        now = time.time()
        cd = self.cooldown_until.get(name, 0.0)
        if now < cd:
            remaining = round(cd - now, 1)
            return False, f"In cooldown period ({remaining}s remaining due to repeated crashes)"

        # Prune old restart timestamps outside sliding window
        history = [t for t in self.restart_history.get(name, []) if now - t < WINDOW_SECONDS]
        self.restart_history[name] = history

        if len(history) >= MAX_RESTARTS_PER_WINDOW:
            self.cooldown_until[name] = now + COOLDOWN_SECONDS
            return False, f"Crash rate limit reached ({len(history)} restarts in {WINDOW_SECONDS}s). Cooling down {COOLDOWN_SECONDS}s"

        return True, "OK"

    def resurrect_service(self, name: str, svc: Dict[str, Any]) -> Tuple[bool, str]:
        """Spawns a crashed service in detached background mode."""
        allowed, reason = self.can_restart(name)
        if not allowed:
            logger.warning("Resurrection blocked for %s: %s", name, reason)
            return False, reason

        log_path = svc.get("log_file", self.logs_dir / f"{name}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = svc["cmd"]

        try:
            logger.info("Resurrecting service %s: %s", name, " ".join(cmd))
            env = dict(os.environ)
            if "SSL_CERT_FILE" not in env:
                for cert_candidate in [
                    Path("/data/data/com.termux/files/usr/etc/tls/cert.pem"),
                    Path("/etc/ssl/certs/ca-certificates.crt"),
                ]:
                    if cert_candidate.exists():
                        env["SSL_CERT_FILE"] = str(cert_candidate)
                        env["SSL_CERT_DIR"] = str(cert_candidate.parent)
                        break

            with open(log_path, "a") as log_out:
                log_out.write(f"\n--- [Supervisor Resurrected at {datetime.now(timezone.utc).isoformat()}] ---\n")
                log_out.flush()

                if sys.platform == "win32":
                    proc = subprocess.Popen(
                        cmd,
                        stdout=log_out,
                        stderr=log_out,
                        cwd=str(self.base_dir),
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                        env=env
                    )
                else:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=log_out,
                        stderr=log_out,
                        cwd=str(self.base_dir),
                        start_new_session=True,
                        env=env
                    )

            self.write_pid(svc["pid_file"], proc.pid)
            self.restart_history.setdefault(name, []).append(time.time())
            self.resurrection_counts[name] = self.resurrection_counts.get(name, 0) + 1

            logger.info("Service %s successfully resurrected with PID %d", name, proc.pid)
            return True, f"Resurrected (new PID: {proc.pid})"
        except Exception as e:
            logger.error("Failed to resurrect service %s: %s", name, e)
            return False, f"Spawn failed: {e}"

    def inspect_and_heal(self, auto_resurrect: bool = True) -> Dict[str, Any]:
        """Performs a full audit of all services, auto-resurrecting any dead daemons."""
        services = self.get_service_definitions()
        status_report: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "all_healthy": True,
            "services": {},
            "resurrections_total": sum(self.resurrection_counts.values())
        }

        for name, svc in services.items():
            if not svc.get("enabled", True):
                continue

            healthy, pid, details = self.check_service(name, svc)
            action_taken = "NONE"

            if not healthy:
                status_report["all_healthy"] = False
                if auto_resurrect:
                    resurrected, res_msg = self.resurrect_service(name, svc)
                    action_taken = "RESURRECTED" if resurrected else "FAILED"
                    details += f" -> {res_msg}"

            status_report["services"][name] = {
                "name": svc["name"],
                "healthy": healthy or (action_taken == "RESURRECTED"),
                "pid": pid,
                "port": svc.get("port"),
                "details": details,
                "action_taken": action_taken,
                "resurrections": self.resurrection_counts.get(name, 0)
            }

        # Write state to disk
        try:
            SUPERVISOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SUPERVISOR_STATE_FILE.write_text(json.dumps(status_report, indent=2) + "\n")
        except Exception:
            pass

        return status_report

    def run_loop(self, poll_interval: float = DEFAULT_POLL_INTERVAL):
        """Runs the continuous supervisor loop."""
        self.running = True
        self.write_pid(SUPERVISOR_PID_FILE, os.getpid())
        logger.info("Supervisor loop started (PID: %d, poll interval: %.1fs)", os.getpid(), poll_interval)

        try:
            while self.running:
                try:
                    self.inspect_and_heal(auto_resurrect=True)
                except Exception as loop_err:
                    logger.error("Error in supervisor iteration: %s", loop_err)
                time.sleep(poll_interval)
        finally:
            if SUPERVISOR_PID_FILE.exists():
                try:
                    SUPERVISOR_PID_FILE.unlink()
                except Exception:
                    pass
            logger.info("Supervisor loop terminated")


def main():
    parser = argparse.ArgumentParser(description="PersonalServer Autonomous Node Process Supervisor (NODE-006)")
    parser.add_argument("command", choices=["check", "run", "status", "stop"], default="check", nargs="?")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Poll interval in seconds")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    args = parser.parse_args()

    supervisor = NodeProcessSupervisor()

    if args.command == "check":
        report = supervisor.inspect_and_heal(auto_resurrect=True)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"PersonalServer Node Process Supervisor: {'HEALTHY' if report['all_healthy'] else 'DEGRADED'}")
            for name, s in report["services"].items():
                mark = "✓" if s["healthy"] else "✗"
                act = f" [{s['action_taken']}]" if s["action_taken"] != "NONE" else ""
                print(f"  {mark} {s['name']} ({name}): {s['details']}{act}")
        sys.exit(0 if report["all_healthy"] else 1)

    elif args.command == "status":
        report = supervisor.inspect_and_heal(auto_resurrect=False)
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["all_healthy"] else 1)

    elif args.command == "run":
        supervisor.run_loop(poll_interval=args.interval)

    elif args.command == "stop":
        if SUPERVISOR_PID_FILE.exists():
            try:
                pid = int(SUPERVISOR_PID_FILE.read_text().strip())
                if is_pid_alive(pid):
                    os.kill(pid, signal.SIGTERM)
                    print(f"Sent SIGTERM to supervisor (PID {pid})")
                SUPERVISOR_PID_FILE.unlink(missing_ok=True)
            except Exception as e:
                print(f"Failed to stop supervisor: {e}")
        else:
            print("No supervisor PID file found")


if __name__ == "__main__":
    main()
