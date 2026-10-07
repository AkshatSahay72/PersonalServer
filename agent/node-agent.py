#!/usr/bin/env python3
"""
PersonalServer Node Agent
=========================
Local control layer for node identity, health, status, service orchestration,
controller registration, heartbeat reporting, and workload job execution.
"""

import sys
import os
import json
import re
import secrets
import socket
import platform
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Import local JobExecutor
from job_executor import JobExecutor, ALLOWLISTED_WORKLOADS

# Paths relative to agent directory
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
SCRIPTS_DIR = BASE_DIR / "scripts"
RUNTIME_DIR = BASE_DIR / "runtime"
LOGS_DIR = BASE_DIR / "logs"
JOBS_DIR = RUNTIME_DIR / "jobs"

NODE_JSON = CONFIG_DIR / "node.json"
NODE_CONF = CONFIG_DIR / "node.conf"
CONTROLLER_JSON = CONFIG_DIR / "controller.json"
ENROLLMENT_TOKEN_FILE = SECRETS_DIR / "enrollment.token"
REGISTRATION_FILE = RUNTIME_DIR / "registration.json"

START_SCRIPT = SCRIPTS_DIR / "start.sh"
STOP_SCRIPT = SCRIPTS_DIR / "stop.sh"
RESTART_SCRIPT = SCRIPTS_DIR / "restart.sh"
STATUS_SCRIPT = SCRIPTS_DIR / "status.sh"
HEALTH_SCRIPT = SCRIPTS_DIR / "health.sh"

DEFAULT_CONTROLLER_URL = "http://100.120.251.42:8000"


def get_current_iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


def run_cmd(cmd):
    """Run a shell command and return trimmed stdout."""
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def discover_hardware(base_dir=BASE_DIR):
    """
    Auto-discovers hardware specifications (CPU, RAM, storage, platform, arch, OS, hostname).
    """
    # CPU Cores
    try:
        cpu_cores = os.cpu_count() or int(run_cmd("nproc") or 1)
    except Exception:
        cpu_cores = 1

    # RAM in MB
    ram_mb = 0
    if Path("/proc/meminfo").exists():
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
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            ram_mb = int(round((pages * page_size) / (1024 * 1024)))
        except Exception:
            pass

    if ram_mb <= 0:
        try:
            mem_raw = run_cmd("free -m")
            for line in mem_raw.splitlines():
                if line.startswith("Mem:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        ram_mb = int(parts[1])
                        break
        except Exception:
            pass

    if ram_mb <= 0:
        ram_mb = 4096  # safe fallback

    # Total Storage in GB
    storage_gb = 0
    try:
        total_bytes, _, _ = shutil.disk_usage(str(base_dir))
        storage_gb = int(round(total_bytes / (1024 ** 3)))
    except Exception:
        pass

    if storage_gb <= 0:
        storage_gb = 32  # safe fallback

    # Platform & OS & Architecture & Hostname
    is_termux = "com.termux" in os.environ.get("PREFIX", "")
    platform_name = "termux" if is_termux else sys.platform
    os_name = "Android" if is_termux else (platform.system() or "Linux")
    arch = platform.machine() or "unknown"
    hostname = socket.gethostname() or "node"

    # Capability probing: Docker is true ONLY if daemon is reachable
    capabilities = {
        "compute": True,
        "storage": True,
        "network": True
    }
    if is_docker_available():
        capabilities["container_runtime:docker"] = True

    return {
        "name": hostname,
        "role": "compute",
        "platform": platform_name,
        "os": os_name,
        "architecture": arch,
        "cpu_cores": cpu_cores,
        "ram_mb": ram_mb,
        "storage_gb": storage_gb,
        "capabilities": capabilities
    }


def is_docker_available():
    """
    Checks if Docker binary exists AND Docker daemon is actively responding.
    Returns True only if docker info connects to a running engine.
    """
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False


def generate_node_id():
    """Generates a secure random node ID in format server-<16 hex chars>."""
    return f"server-{secrets.token_hex(8)}"


def load_node_config():
    """Load node identity from node.json or fallback to node.conf."""
    config = {}
    if NODE_JSON.exists():
        try:
            with open(NODE_JSON, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to parse {NODE_JSON}: {e}", file=sys.stderr)

    if not config and NODE_CONF.exists():
        try:
            with open(NODE_CONF, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        config[k.strip().lower()] = v.strip()
        except Exception as e:
            print(f"Warning: Failed to read {NODE_CONF}: {e}", file=sys.stderr)

    hw = discover_hardware()
    return {
        "node_id": config.get("node_id", "unknown"),
        "name": config.get("name", config.get("node_name", hw["name"])),
        "role": config.get("role", config.get("node_role", "compute")),
        "platform": config.get("platform", hw["platform"]),
        "os": config.get("os", hw["os"]),
        "architecture": config.get("architecture", hw["architecture"]),
        "cpu_cores": int(config.get("cpu_cores", hw["cpu_cores"])),
        "ram_mb": int(config.get("ram_mb", hw["ram_mb"])),
        "storage_gb": int(config.get("storage_gb", hw["storage_gb"])),
        "capabilities": config.get("capabilities", hw["capabilities"])
    }


def get_system_metrics():
    """Collect lightweight system metrics."""
    try:
        cpu_cores = int(run_cmd("nproc") or 1)
    except Exception:
        cpu_cores = 1

    uptime_raw = run_cmd("uptime")
    load_match = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", uptime_raw)
    load_average = [float(load_match.group(i)) for i in range(1, 4)] if load_match else []

    memory = {"total": "N/A", "used": "N/A", "available": "N/A"}
    mem_raw = run_cmd("free -h")
    for line in mem_raw.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                memory = {"total": parts[1], "used": parts[2], "available": parts[6]}
            elif len(parts) >= 4:
                memory = {"total": parts[1], "used": parts[2], "available": parts[3]}

    storage = {"total": "N/A", "used": "N/A", "available": "N/A", "used_percent": "N/A"}
    df_raw = run_cmd("df -h ~")
    df_lines = df_raw.splitlines()
    if len(df_lines) >= 2:
        parts = df_lines[-1].split()
        if len(parts) >= 5:
            storage = {
                "total": parts[1],
                "used": parts[2],
                "available": parts[3],
                "used_percent": parts[4]
            }

    return {
        "cpu_cores": cpu_cores,
        "load_average": load_average,
        "memory": memory,
        "storage": storage,
        "uptime": uptime_raw.strip()
    }


def is_pid_alive(pid_str, pattern=None):
    if not pid_str or not str(pid_str).strip().isdigit():
        return False
    pid = int(pid_str)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if pattern:
        args = run_cmd(f"ps -p {pid} -o args=")
        return pattern in args
    return True


def get_service_states():
    services = {}

    if is_docker_available():
        services["docker"] = "running"

    if (BASE_DIR / "services" / "node-api" / "app.py").exists():
        api_pid = ""
        api_pid_file = RUNTIME_DIR / "node-api.pid"
        if api_pid_file.exists():
            try:
                api_pid = api_pid_file.read_text().strip()
            except Exception:
                pass

        if not is_pid_alive(api_pid, "services/node-api/app.py"):
            api_pid = run_cmd("pgrep -f 'services/node-api/app.py' | head -n 1")

        if is_pid_alive(api_pid, "services/node-api/app.py"):
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/health", headers={"User-Agent": "NodeAgent/1.0"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        services["node_api"] = "running"
                    else:
                        services["node_api"] = "degraded"
            except Exception:
                services["node_api"] = "unresponsive"
        elif (BASE_DIR / "start.sh").exists():
            services["node_api"] = "stopped"

    if (BASE_DIR / "start.sh").exists():
        cf_pid = ""
        cf_pid_file = RUNTIME_DIR / "cloudflared.pid"
        if cf_pid_file.exists():
            try:
                cf_pid = cf_pid_file.read_text().strip()
            except Exception:
                pass

        if not is_pid_alive(cf_pid, "cloudflared"):
            cf_pid = run_cmd("pgrep -f 'cloudflared tunnel run' | head -n 1")

        if is_pid_alive(cf_pid, "cloudflared"):
            services["cloudflare"] = "running"
        else:
            services["cloudflare"] = "stopped"

    return services


# ------------------------------------------------------------------------------
# Registration & Controller Communication
# ------------------------------------------------------------------------------

def get_default_controller_url():
    if CONTROLLER_JSON.exists():
        try:
            with open(CONTROLLER_JSON, "r", encoding="utf-8") as f:
                d = json.load(f)
                if "url" in d:
                    return d["url"]
        except Exception:
            pass
    return DEFAULT_CONTROLLER_URL


def load_registration_state():
    if REGISTRATION_FILE.exists():
        try:
            with open(REGISTRATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_registration_state(state):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = REGISTRATION_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    temp_file.replace(REGISTRATION_FILE)


def get_enrollment_token(arg_token=None):
    if arg_token:
        return arg_token.strip()
    if ENROLLMENT_TOKEN_FILE.exists():
        try:
            token = ENROLLMENT_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass
    return None


# ------------------------------------------------------------------------------
# CLI Commands
# ------------------------------------------------------------------------------

def cmd_info(args):
    node_info = load_node_config()
    if getattr(args, "json", False):
        print(json.dumps(node_info, indent=2))
        return 0

    print("==========================================")
    print(" PersonalServer Node Identity")
    print("==========================================")
    for k, v in node_info.items():
        print(f"{k.replace('_', ' ').title():<16}: {v}")
    print("==========================================")
    return 0


def cmd_health(args):
    node_info = load_node_config()
    system_metrics = get_system_metrics()
    services = get_service_states()

    report = {
        "node": {
            "id": node_info.get("node_id"),
            "name": node_info.get("name"),
            "role": node_info.get("role")
        },
        "system": system_metrics,
        "services": services
    }

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return 0

    print("==========================================")
    print(" PersonalServer Health Report")
    print("==========================================")
    print("=== NODE IDENTITY ===")
    print(f"Node: {node_info.get('name')}")
    print(f"ID:   {node_info.get('node_id')}")
    print(f"Role: {node_info.get('role')}")
    print(f"OS:   {node_info.get('os')} ({node_info.get('architecture')})")
    print()
    print("=== SYSTEM METRICS ===")
    print(f"Uptime:    {system_metrics.get('uptime')}")
    print(f"CPU Cores: {system_metrics.get('cpu_cores')}")
    print(f"Memory:    Total: {system_metrics['memory']['total']} | Used: {system_metrics['memory']['used']} | Available: {system_metrics['memory']['available']}")
    print(f"Storage:   Total: {system_metrics['storage']['total']} | Used: {system_metrics['storage']['used']} | Usage: {system_metrics['storage']['used_percent']}")
    print()
    print("=== SERVICE STATUS ===")
    print(f"Node API:   {services['node_api'].upper()}")
    print(f"Cloudflare: {services['cloudflare'].upper()}")
    print("==========================================")
    return 0


def cmd_status(args):
    node_info = load_node_config()
    system_metrics = get_system_metrics()
    services = get_service_states()

    unified_status = {
        "node": {
            "id": node_info.get("node_id"),
            "name": node_info.get("name"),
            "role": node_info.get("role"),
            "platform": node_info.get("platform")
        },
        "system": {
            "cpu_cores": system_metrics.get("cpu_cores"),
            "memory": f"{system_metrics['memory']['used']}/{system_metrics['memory']['total']}",
            "storage": f"{system_metrics['storage']['used']}/{system_metrics['storage']['total']} ({system_metrics['storage']['used_percent']})",
            "uptime": system_metrics.get("uptime")
        },
        "services": services
    }

    if getattr(args, "json", False):
        print(json.dumps(unified_status, indent=2))
        return 0

    print("==========================================")
    print(" PersonalServer Node Status")
    print("==========================================")
    print("Node:")
    print(f"  ID:           {node_info.get('node_id')}")
    print(f"  Name:         {node_info.get('name')}")
    print(f"  Role:         {node_info.get('role')}")
    print(f"  Platform:     {node_info.get('platform')}")
    print(f"  Architecture: {node_info.get('architecture')}")
    print()
    print("Services:")
    for srv, state in services.items():
        print(f"  {srv.replace('_', ' ').title():<14}: {state}")
    print("==========================================")
    return 0


def execute_script(script_path):
    if not script_path.exists():
        print(f"Error: Script not found: {script_path}", file=sys.stderr)
        return 1
    res = subprocess.run(["bash", str(script_path)])
    return res.returncode


def cmd_start(args):
    return execute_script(START_SCRIPT)


def cmd_stop(args):
    return execute_script(STOP_SCRIPT)


def cmd_restart(args):
    return execute_script(RESTART_SCRIPT)


def cmd_register(args):
    controller_url = (args.controller or get_default_controller_url()).rstrip("/")
    enrollment_token = get_enrollment_token(args.token)

    if not enrollment_token:
        print("Error: Enrollment token required. Provide via --token or place in config/secrets/enrollment.token", file=sys.stderr)
        return 1

    node_info = load_node_config()
    if not node_info.get("node_id") or node_info.get("node_id") == "unknown":
        node_info["node_id"] = generate_node_id()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(NODE_JSON, "w", encoding="utf-8") as f:
            json.dump(node_info, f, indent=2)

    register_endpoint = f"{controller_url}/register"

    payload = json.dumps(node_info).encode("utf-8")
    req = urllib.request.Request(
        register_endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {enrollment_token}",
            "User-Agent": "PersonalServer-NodeAgent/1.0"
        },
        method="POST"
    )

    print("==========================================")
    print(" PersonalServer Node Registration")
    print("==========================================")
    print(f"Controller: {controller_url}")
    print(f"Node ID:    {node_info.get('node_id')}")
    print(f"Node Name:  {node_info.get('name')}")
    print("Connecting to controller... ", end="", flush=True)

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            auth_token = res_data.get("auth_token", "")
            reg_time = res_data.get("registered_at", get_current_iso_timestamp())

            reg_state = {
                "registered": True,
                "controller_url": controller_url,
                "node_id": node_info.get("node_id"),
                "node_name": node_info.get("name"),
                "auth_token": auth_token,
                "registered_at": reg_time,
                "last_heartbeat": None
            }
            save_registration_state(reg_state)

            print("SUCCESS")
            print(f"Status:     {res_data.get('status', 'registered')}")
            print(f"Message:    {res_data.get('message', 'Node successfully registered.')}")
            print("==========================================")
            return 0

    except urllib.error.HTTPError as e:
        print("FAILED")
        err_msg = ""
        try:
            err_data = json.loads(e.read().decode("utf-8"))
            err_msg = err_data.get("error", "")
        except Exception:
            pass
        print(f"Registration Error (HTTP {e.code}): {err_msg or e.reason}", file=sys.stderr)
        print("==========================================")
        return 1

    except Exception as e:
        print("FAILED")
        print(f"Connection Error: {e}", file=sys.stderr)
        print("==========================================")
        return 1


def cmd_heartbeat(args=None):
    reg_state = load_registration_state()
    if not reg_state or not reg_state.get("registered"):
        print("Error: Node is not registered. Run 'python agent/node-agent.py register' first.", file=sys.stderr)
        return 1

    override_url = getattr(args, "controller", None) if args else None
    controller_url = (override_url or reg_state.get("controller_url") or get_default_controller_url()).rstrip("/")
    auth_token = reg_state.get("auth_token", "")
    node_id = reg_state.get("node_id", "")

    heartbeat_endpoint = f"{controller_url}/heartbeat"

    system_metrics = get_system_metrics()
    services_state = get_service_states()
    now = get_current_iso_timestamp()

    payload_data = {
        "node_id": node_id,
        "status": "online",
        "timestamp": now,
        "services": services_state,
        "system": {
            "cpu_cores": system_metrics.get("cpu_cores"),
            "load_average": system_metrics.get("load_average"),
            "memory": f"{system_metrics['memory']['used']}/{system_metrics['memory']['total']}",
            "storage": f"{system_metrics['storage']['used']}/{system_metrics['storage']['total']} ({system_metrics['storage']['used_percent']})",
            "uptime": system_metrics.get("uptime")
        }
    }

    payload = json.dumps(payload_data).encode("utf-8")
    req = urllib.request.Request(
        heartbeat_endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "User-Agent": "PersonalServer-NodeAgent/1.0"
        },
        method="POST"
    )

    print(f"Sending heartbeat to {controller_url}... ", end="", flush=True)

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            reg_state["last_heartbeat"] = now
            save_registration_state(reg_state)
            print("OK")
            if args and getattr(args, "json", False):
                print(json.dumps(res_data, indent=2))
            else:
                print(f"Heartbeat acknowledged at {res_data.get('timestamp', now)}.")
            return 0

    except urllib.error.HTTPError as e:
        print("FAILED")
        err_msg = ""
        try:
            err_data = json.loads(e.read().decode("utf-8"))
            err_msg = err_data.get("error", "")
        except Exception:
            pass
        print(f"Heartbeat Error (HTTP {e.code}): {err_msg or e.reason}", file=sys.stderr)
        return 1

    except Exception as e:
        print("FAILED")
        print(f"Connection Error: {e}", file=sys.stderr)
        return 1


def cmd_onboard(args):
    controller_url = (getattr(args, "controller", None) or get_default_controller_url()).rstrip("/")
    onboarding_code = (getattr(args, "code", "") or "").strip()

    if not onboarding_code:
        print("Error: --code is required for onboarding.", file=sys.stderr)
        return 1

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Hardware discovery & Node config creation
    node_config = {}
    if NODE_JSON.exists():
        try:
            with open(NODE_JSON, "r", encoding="utf-8") as f:
                node_config = json.load(f)
        except Exception:
            pass

    if not node_config and NODE_CONF.exists():
        node_config = load_node_config()

    if not node_config:
        discovered = discover_hardware()
        node_id = generate_node_id()
        node_config = {
            "node_id": node_id,
            **discovered
        }
        try:
            with open(NODE_JSON, "w", encoding="utf-8") as f:
                json.dump(node_config, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to write {NODE_JSON}: {e}", file=sys.stderr)
    else:
        # If node.json existed but lacked node_id
        if not node_config.get("node_id") or node_config.get("node_id") == "unknown":
            node_config["node_id"] = generate_node_id()
            if not NODE_JSON.exists():
                try:
                    with open(NODE_JSON, "w", encoding="utf-8") as f:
                        json.dump(node_config, f, indent=2)
                except Exception:
                    pass

    # 2. Write controller.json if not exists
    if not CONTROLLER_JSON.exists():
        try:
            with open(CONTROLLER_JSON, "w", encoding="utf-8") as f:
                json.dump({"url": controller_url}, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to write {CONTROLLER_JSON}: {e}", file=sys.stderr)

    node_info = load_node_config()
    register_endpoint = f"{controller_url}/register"
    payload = json.dumps(node_info).encode("utf-8")

    req = urllib.request.Request(
        register_endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Onboarding-Code": onboarding_code,
            "User-Agent": "PersonalServer-NodeAgent/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            auth_token = res_data.get("auth_token", "")
            reg_time = res_data.get("registered_at", get_current_iso_timestamp())

            reg_state = {
                "registered": True,
                "controller_url": controller_url,
                "node_id": node_info.get("node_id"),
                "node_name": node_info.get("name"),
                "auth_token": auth_token,
                "registered_at": reg_time,
                "last_heartbeat": None
            }
            save_registration_state(reg_state)

    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_data = json.loads(e.read().decode("utf-8"))
            err_msg = err_data.get("error", "")
        except Exception:
            pass
        print(f"Onboarding failed (HTTP {e.code}): {err_msg or e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Onboarding failed (Connection error): {e}", file=sys.stderr)
        return 1

    # 3. Startup Safety: Start managed services through existing start.sh
    if START_SCRIPT.exists():
        res = execute_script(START_SCRIPT)
        if res != 0:
            print("Warning: Service startup returned non-zero exit code. Check logs.", file=sys.stderr)

    # 4. Initial heartbeat
    try:
        cmd_heartbeat(None)
    except Exception:
        pass

    # 5. Output concise success information
    print("==========================================")
    print("Onboarding successful")
    print(f"Node ID:    {node_info.get('node_id')}")
    print(f"Controller: {controller_url}")
    print("Status:     registered")
    print("==========================================")
    return 0


def cmd_registration_status(args):
    reg_state = load_registration_state()

    if getattr(args, "json", False):
        if reg_state:
            safe_state = dict(reg_state)
            if "auth_token" in safe_state:
                safe_state["auth_token"] = safe_state["auth_token"][:6] + "..." if safe_state["auth_token"] else ""
            print(json.dumps(safe_state, indent=2))
        else:
            print(json.dumps({"registered": False, "status": "unregistered"}, indent=2))
        return 0

    print("==========================================")
    print(" PersonalServer Registration Status")
    print("==========================================")
    if reg_state and reg_state.get("registered"):
        print("Status:         REGISTERED")
        print(f"Controller:     {reg_state.get('controller_url')}")
        print(f"Node ID:        {reg_state.get('node_id')}")
        print(f"Node Name:      {reg_state.get('node_name')}")
        print(f"Registered At:  {reg_state.get('registered_at')}")
        print(f"Last Heartbeat: {reg_state.get('last_heartbeat') or 'None'}")
    else:
        print("Status:         NOT REGISTERED")
        print("Use 'python agent/node-agent.py register' to enroll with a controller.")
    print("==========================================")
    return 0


def cmd_work(args):
    """Fetch and execute pending jobs assigned to this node by the controller."""
    reg_state = load_registration_state()
    if not reg_state or not reg_state.get("registered"):
        print("Error: Node is not registered. Run 'python agent/node-agent.py register' first.", file=sys.stderr)
        return 1

    controller_url = (args.controller or reg_state.get("controller_url") or get_default_controller_url()).rstrip("/")
    auth_token = reg_state.get("auth_token", "")
    node_id = reg_state.get("node_id", "")

    fetch_url = f"{controller_url}/nodes/{node_id}/jobs/next"
    req = urllib.request.Request(
        fetch_url,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "User-Agent": "PersonalServer-NodeAgent/1.0"
        }
    )

    print(f"Checking for pending jobs at {controller_url}...")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            job = data.get("job")

            if not job:
                print("No pending jobs assigned to this node.")
                return 0

            print(f"-> Assigned Job: {job.get('job_id')} ({job.get('type')})")
            print(f"-> Executing workload under user permissions...")

            # Execute via JobExecutor
            result = JobExecutor.execute(job)

            print(f"-> Execution completed with status: {result.get('status')} (Exit: {result.get('exit_code')})")

            # Post result back to controller
            result_url = f"{controller_url}/jobs/{job.get('job_id')}/result"
            res_req = urllib.request.Request(
                result_url,
                data=json.dumps(result).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {auth_token}",
                    "User-Agent": "PersonalServer-NodeAgent/1.0"
                },
                method="POST"
            )

            with urllib.request.urlopen(res_req, timeout=5) as res_resp:
                ack_data = json.loads(res_resp.read().decode("utf-8"))
                print(f"-> Result reported to controller: {ack_data.get('job_status')}")

            return 0

    except urllib.error.HTTPError as e:
        print(f"HTTP Error (HTTP {e.code}): {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Work Execution Error: {e}", file=sys.stderr)
        return 1


def cmd_exec_local(args):
    """Directly test executing a safe workload locally."""
    job_payload = {
        "job_id": f"local-{int(datetime.now().timestamp())}",
        "type": args.type,
        "timeout": args.timeout,
        "parameters": json.loads(args.params) if args.params else {}
    }
    print(f"Executing local workload '{args.type}'...")
    res = JobExecutor.execute(job_payload)
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") == "SUCCEEDED" else 1


def main():
    parser = argparse.ArgumentParser(
        prog="node-agent",
        description="PersonalServer Node Agent - Local Node Control & Workload Execution"
    )
    subparsers = parser.add_subparsers(dest="command", help="Agent commands")

    # info command
    p_info = subparsers.add_parser("info", help="Display node identity")
    p_info.add_argument("--json", action="store_true", help="Output in JSON format")

    # health command
    p_health = subparsers.add_parser("health", help="Display node health and system metrics")
    p_health.add_argument("--json", action="store_true", help="Output in JSON format")

    # status command
    p_status = subparsers.add_parser("status", help="Display unified node and services status")
    p_status.add_argument("--json", action="store_true", help="Output in JSON format")

    # Service orchestration commands
    subparsers.add_parser("start", help="Start managed node services")
    subparsers.add_parser("stop", help="Stop managed node services")
    subparsers.add_parser("restart", help="Restart managed node services")

    # Registration and Heartbeat commands
    p_reg = subparsers.add_parser("register", help="Register node with a PersonalServer controller")
    p_reg.add_argument("--controller", help="Controller URL (e.g. http://100.120.251.42:8000)")
    p_reg.add_argument("--token", help="Enrollment secret token")

    p_onboard = subparsers.add_parser("onboard", help="Automatically onboard and register this node with a controller")
    p_onboard.add_argument("--controller", help="Controller URL (e.g. http://100.120.251.42:8000)")
    p_onboard.add_argument("--code", required=True, help="One-time onboarding code (PS-XXXX-XXXX)")

    p_hb = subparsers.add_parser("heartbeat", help="Send heartbeat to registered controller")
    p_hb.add_argument("--controller", help="Override controller URL")
    p_hb.add_argument("--json", action="store_true", help="Output response in JSON")

    p_reg_stat = subparsers.add_parser("registration-status", help="Display node registration status")
    p_reg_stat.add_argument("--json", action="store_true", help="Output in JSON format")

    # Workload execution commands
    p_work = subparsers.add_parser("work", help="Poll and execute pending assigned jobs from controller")
    p_work.add_argument("--controller", help="Override controller URL")

    p_exec_local = subparsers.add_parser("exec-local", help="Directly test safe workload execution locally")
    p_exec_local.add_argument("--type", required=True, choices=list(ALLOWLISTED_WORKLOADS.keys()), help="Workload type")
    p_exec_local.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    p_exec_local.add_argument("--params", help="JSON parameters")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    command_handlers = {
        "info": cmd_info,
        "health": cmd_health,
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "register": cmd_register,
        "onboard": cmd_onboard,
        "heartbeat": cmd_heartbeat,
        "registration-status": cmd_registration_status,
        "work": cmd_work,
        "exec-local": cmd_exec_local,
    }

    handler = command_handlers.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
