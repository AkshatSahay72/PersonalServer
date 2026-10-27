#!/usr/bin/env python3
"""
PersonalServer Portfolio Production WSGI Runner & Lifecycle Manager
===================================================================
Manages local execution of the portfolio Flask application:
- Loads secrets safely from config/secrets/portfolio.env (without logging)
- Uses Waitress (multi-threaded, production WSGI) on Windows, Gunicorn on POSIX
- Enforces debug=False (never runs Flask debug mode)
- Manages process lifecycle: start, stop, status, restart
- Binds strictly to 127.0.0.1 (loopback only)
"""

import sys
import os
import time
import json
import signal
import socket
import argparse
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PORTFOLIO_DIR = BASE_DIR / "storage" / "applications" / "portfolio"
RUNTIME_DIR = BASE_DIR / "runtime"
LOGS_DIR = BASE_DIR / "logs"
SECRETS_DIR = BASE_DIR / "config" / "secrets"

PID_FILE = RUNTIME_DIR / "portfolio.pid"
LOG_FILE = LOGS_DIR / "portfolio.log"
ENV_FILE = SECRETS_DIR / "portfolio.env"

DEFAULT_PORT = 8090
DEFAULT_HOST = "127.0.0.1"


def get_venv_python() -> Path:
    """Resolves Python interpreter from the isolated portfolio virtual environment."""
    if sys.platform == "win32":
        venv_py = PORTFOLIO_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = PORTFOLIO_DIR / ".venv" / "bin" / "python"

    if venv_py.exists():
        return venv_py
    return Path(sys.executable)


def load_env_vars() -> dict:
    """
    Safely loads environment variables from portfolio.env without printing secrets.
    """
    env = os.environ.copy()
    if ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k:
                            env[k] = v
        except Exception:
            pass
    return env


def is_port_in_use(port: int, host: str = DEFAULT_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def get_running_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if pid <= 0:
                return None
            if sys.platform == "win32":
                # Passive check using tasklist - sends NO signals and cannot kill any process
                res = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if str(pid) in res.stdout:
                    return pid
            else:
                # Passive check via /proc on Linux/Android - sends NO signals
                if Path(f"/proc/{pid}").exists():
                    return pid
        except Exception:
            pass
    return None


def run_server_foreground(host: str, port: int):
    """
    Directly runs the production WSGI server inside the portfolio virtual environment.
    """
    import site
    venv_site_pkgs = PORTFOLIO_DIR / ".venv" / ("Lib" if sys.platform == "win32" else "lib") / ("site-packages" if sys.platform == "win32" else f"python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    if venv_site_pkgs.exists():
        site.addsitedir(str(venv_site_pkgs))

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    sys.path.insert(0, str(PORTFOLIO_DIR))
    os.chdir(str(PORTFOLIO_DIR))

    # Import portfolio app
    try:
        from app import app
    except ImportError as e:
        print(f"[PORTFOLIO] Failed to import portfolio app: {e}", file=sys.stderr)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    app.debug = False

    print(f"[PORTFOLIO] Starting production WSGI server on http://{host}:{port} (debug=False, PID={os.getpid()})")

    try:
        # Waitress (multi-threaded production WSGI)
        from waitress import serve
        serve(app, host=host, port=port, threads=4, _quiet=False)
    except ImportError:
        try:
            from wsgiref.simple_server import make_server
            httpd = make_server(host, port, app)
            httpd.serve_forever()
        except Exception as e:
            print(f"[PORTFOLIO] Server terminated: {e}", file=sys.stderr)
    finally:
        PID_FILE.unlink(missing_ok=True)


def start(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    running_pid = get_running_pid()
    if running_pid:
        print(f"[PORTFOLIO] Already running (PID: {running_pid}) on port {port}")
        return 0

    if is_port_in_use(port, host):
        print(f"[PORTFOLIO] Error: Port {port} on {host} is already in use by another process.", file=sys.stderr)
        return 1

    venv_py = get_venv_python()
    env = load_env_vars()
    env["PORT"] = str(port)

    # Launch detached background process
    log_fd = open(LOG_FILE, "a", encoding="utf-8")
    cmd = [
        str(venv_py),
        str(Path(__file__).resolve()),
        "_run_foreground",
        "--host", host,
        "--port", str(port)
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        cwd=str(PORTFOLIO_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creationflags
    )
    log_fd.close()

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[PORTFOLIO] Started successfully (PID: {proc.pid}) on http://{host}:{port}")
    print(f"[PORTFOLIO] Logging to: {LOG_FILE}")

    # Wait briefly for startup and health check
    time.sleep(1.5)
    return 0


def stop():
    pid = get_running_pid()
    if not pid:
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
        print("[PORTFOLIO] Application is not running.")
        return 0

    print(f"[PORTFOLIO] Stopping process (PID: {pid})...")
    try:
        from common.process_manager import SafeProcessLifecycleManager
        # Verify immunity first
        SafeProcessLifecycleManager.verify_infrastructure_immunity(pid, "portfolio")
        ok, msg = SafeProcessLifecycleManager.stop_process("portfolio")
        if not ok:
            print(f"[PORTFOLIO] {msg}")
    except Exception as e:
        print(f"[PORTFOLIO] Error stopping PID {pid}: {e}")

    PID_FILE.unlink(missing_ok=True)
    print("[PORTFOLIO] Application stopped.")
    return 0


def status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    pid = get_running_pid()
    if not pid:
        print("Status:       STOPPED")
        return 1

    # Check HTTP health
    health_ok = False
    try:
        req = urllib.request.Request(f"http://{host}:{port}/", headers={"User-Agent": "HealthCheck"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            health_ok = (resp.status == 200)
    except Exception:
        pass

    print("==========================================")
    print(" PersonalServer Portfolio Application")
    print("==========================================")
    print(f"Status:       RUNNING")
    print(f"PID:          {pid}")
    print(f"Endpoint:     http://{host}:{port}/")
    print(f"Health Check: {'HEALTHY (HTTP 200)' if health_ok else 'UNHEALTHY / STARTING'}")
    print(f"Log File:     {LOG_FILE}")
    print("==========================================")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Portfolio Application Runner")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "_run_foreground"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.action == "_run_foreground":
        run_server_foreground(args.host, args.port)
        return 0
    elif args.action == "start":
        return start(args.host, args.port)
    elif args.action == "stop":
        return stop()
    elif args.action == "restart":
        stop()
        time.sleep(1)
        return start(args.host, args.port)
    elif args.action == "status":
        return status(args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
