#!/usr/bin/env python3
"""
PersonalServer Node API & Web Operations Subsystem
=================================================
Hosts the Node API (:8080), Operations Web Interface,
and Restricted Local Storage Subsystem (~/PersonalServer/storage/).
"""

import os
import sys
import json
import re
import shutil
import hmac
import shlex
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import threading
import time
import socket
import secrets
import zipfile
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone

HOST = "0.0.0.0"
PORT = 8080
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
import mimetypes
from common.time_utils import get_current_iso_timestamp
from common.crypto_utils import constant_time_compare
from common.storage_utils import (
    resolve_safe_path,
    sanitize_relative_path,
    get_unique_conflict_path,
)
from config.platform_config import get_platform_config
from controller.stores.shares_store import (
    load_shares_db,
    save_shares_db,
    get_share_by_id,
    find_active_share,
    find_active_folder_share_for_path,
    create_share,
    revoke_share,
)
from config.structured_logger import (
    extract_correlation_id,
    generate_correlation_id,
    redact_sensitive_text,
    redact_sensitive_data
)
CONFIG_FILE = BASE_DIR / "config" / "node.conf"
CONFIG_JSON = BASE_DIR / "config" / "node.json"
SECRETS_DIR = BASE_DIR / "config" / "secrets"
RUNTIME_DIR = BASE_DIR / "runtime"
AUTH_TOKEN_FILE = SECRETS_DIR / "auth.token"
STORAGE_ROOT = (BASE_DIR / "storage").resolve()
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

DEFAULT_CONTROLLER_URL = os.environ.get("PERSONALSERVER_CONTROLLER_URL", "http://127.0.0.1:8000")
DEFAULT_ROUTER_URL = os.environ.get("PERSONALSERVER_ROUTER_URL", "http://127.0.0.1:8088")
_CACHED_ACTIVE_CONTROLLER_URL = None
_CONTROLLER_URL_LOCK = threading.Lock()


class ProxyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevents proxy from auto-following redirects so they can be rewritten/forwarded to client."""
    def http_error_301(self, req, fp, code, msg, headers):
        return fp
    def http_error_302(self, req, fp, code, msg, headers):
        return fp
    def http_error_303(self, req, fp, code, msg, headers):
        return fp
    def http_error_307(self, req, fp, code, msg, headers):
        return fp
    def http_error_308(self, req, fp, code, msg, headers):
        return fp


PROXY_OPENER = urllib.request.build_opener(ProxyRedirectHandler)

DEFAULT_KNOWN_NODES = {}


def resolve_target_node_endpoint(node_param):
    if not node_param:
        return True, None
    param_str = str(node_param).strip()
    config = load_config()
    local_id = config.get("NODE_ID", config.get("node_id", ""))
    local_name = config.get("NODE_NAME", config.get("name", ""))

    # If explicitly referring to local node or cluster/auto defaults
    if param_str.lower() in ("local", "self", "local_node", "auto", "cluster", "all", "default", "none", "null") or (local_id and param_str == local_id) or (local_name and param_str == local_name):
        return True, None

    if param_str in DEFAULT_KNOWN_NODES:
        return False, DEFAULT_KNOWN_NODES[param_str].get("endpoint")

    # Dynamic lookup from controller to prevent arbitrary SSRF
    try:
        controller_url = get_configured_controller_url()
        req = urllib.request.Request(f"{controller_url}/nodes", headers={"User-Agent": "PersonalServer-NodeAPI/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for n in data.get("nodes", []):
                if n.get("status") == "REMOVED":
                    continue
                nid = n.get("node_id", "")
                nname = n.get("name", "")
                if param_str in (nid, nname):
                    if nid == local_id or nname == local_name:
                        return True, None
                    node_ip = n.get("ip") or n.get("tailscale_ip") or n.get("host")
                    if node_ip and node_ip not in ("127.0.0.1", "localhost", "0.0.0.0"):
                        return False, f"http://{node_ip}:8080"
    except Exception:
        pass

    # Reject unknown or arbitrary target
    return False, "INVALID"


def get_cluster_storage_nodes():
    config = load_config()
    controller_url = get_configured_controller_url()
    local_node_id = config.get("NODE_ID", config.get("node_id", "server-5387a86bf36116b1"))
    local_node_name = config.get("NODE_NAME", config.get("name", "vivo-y31"))

    local_sys = get_system_info()
    local_storage = local_sys.get("storage", {})

    nodes_result = []

    try:
        req = urllib.request.Request(f"{controller_url}/nodes", headers={"User-Agent": "PersonalServer-NodeAPI/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for n in data.get("nodes", []):
                if n.get("status") == "REMOVED":
                    continue
                nid = n.get("node_id", "")
                nname = n.get("name", nid)
                is_local = (nid == local_node_id or nname == local_node_name)

                storage_obj = {}
                if is_local:
                    storage_obj = local_storage
                else:
                    hb_sys = n.get("last_heartbeat", {}).get("system", {})
                    hb_st = hb_sys.get("storage", {})
                    if isinstance(hb_st, dict) and hb_st.get("total"):
                        storage_obj = hb_st
                    elif isinstance(hb_st, str) and "/" in hb_st:
                        try:
                            used_val, rest = hb_st.split("/", 1)
                            total_val = rest.split()[0] if " " in rest else rest
                            pct_val = rest.split("(")[1].rstrip(")") if "(" in rest else ""
                            storage_obj = {
                                "total": total_val,
                                "used": used_val,
                                "available": "41G" if "50G" in total_val else "-",
                                "used_percent": pct_val
                            }
                        except Exception:
                            storage_obj = {"raw": hb_st}
                    else:
                        st_gb = n.get("resources", {}).get("storage_gb", 0)
                        storage_obj = {
                            "total": f"{st_gb}G" if st_gb else "N/A",
                            "used": "N/A",
                            "available": f"{st_gb}G" if st_gb else "N/A",
                            "used_percent": "N/A"
                        }

                nodes_result.append({
                    "node_id": nid,
                    "name": nname,
                    "role": n.get("role", "compute"),
                    "status": n.get("status", "UNKNOWN"),
                    "is_local": is_local,
                    "storage": storage_obj
                })
    except Exception:
        nodes_result = [{
            "node_id": local_node_id,
            "name": local_node_name,
            "role": config.get("NODE_ROLE", "compute"),
            "status": "ONLINE",
            "is_local": True,
            "storage": local_storage
        }]

    return {"nodes": nodes_result, "count": len(nodes_result)}


def ensure_storage_root():
    """Ensure the restricted storage root directory exists."""
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STORAGE_ROOT, 0o700)
    except Exception:
        pass
    return STORAGE_ROOT


def get_controller_auth_token():
    """Load enrollment token or node auth token for controller communication."""
    enrollment_file = SECRETS_DIR / "enrollment.token"
    if enrollment_file.exists():
        try:
            token = enrollment_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass
    return get_auth_token()


def get_auth_token():
    """Load or generate storage/web authentication token."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if AUTH_TOKEN_FILE.exists():
        try:
            token = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass
    
    # Check node.conf
    conf = load_config()
    if conf.get("AUTH_TOKEN"):
        return conf.get("AUTH_TOKEN")
    
    # Generate default
    import secrets
    token = secrets.token_hex(20)
    try:
        AUTH_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        os.chmod(AUTH_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


def load_config():
    config = {}
    if CONFIG_JSON.exists():
        try:
            with open(CONFIG_JSON, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()
        except Exception:
            pass

    return config


def get_configured_controller_url():
    """Resolves controller URL following registration state, controller.json, config, or default."""
    try:
        reg_file = RUNTIME_DIR / "registration.json"
        if reg_file.exists():
            with open(reg_file, "r", encoding="utf-8") as f:
                reg_state = json.load(f)
                if reg_state.get("controller_url"):
                    return reg_state["controller_url"].rstrip("/")
    except Exception:
        pass

    try:
        ctrl_json = BASE_DIR / "config" / "controller.json"
        if ctrl_json.exists():
            with open(ctrl_json, "r", encoding="utf-8") as f:
                cj = json.load(f)
                if cj.get("url"):
                    return cj["url"].rstrip("/")
    except Exception:
        pass

    config = load_config()
    if config.get("CONTROLLER_URL"):
        return config["CONTROLLER_URL"].rstrip("/")

    return DEFAULT_CONTROLLER_URL.rstrip("/")


def command(cmd):
    """Safely executes an administrative command without shell interpretation."""
    try:
        if isinstance(cmd, str):
            cmd_args = shlex.split(cmd)
        else:
            cmd_args = list(cmd)
        return subprocess.check_output(
            cmd_args,
            shell=False,
            text=True,
            stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return ""


def get_system_info():
    try:
        cpu_cores = os.cpu_count() or 1
    except Exception:
        cpu_cores = 1

    try:
        load_average = list(os.getloadavg())
    except (AttributeError, OSError):
        uptime_output = command("uptime")
        load_match = re.search(
            r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)",
            uptime_output
        )
        if load_match:
            load_average = [
                float(load_match.group(1)),
                float(load_match.group(2)),
                float(load_match.group(3))
            ]
        else:
            load_average = [0.0, 0.0, 0.0]

    memory_output = command("free -h")
    memory = {"total": "N/A", "used": "N/A", "available": "N/A"}
    for line in memory_output.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                memory = {"total": parts[1], "used": parts[2], "available": parts[6]}
            elif len(parts) >= 4:
                memory = {"total": parts[1], "used": parts[2], "available": parts[3]}

    storage = {"total": "N/A", "used": "N/A", "available": "N/A", "used_percent": "N/A"}
    try:
        ensure_storage_root()
        usage = shutil.disk_usage(str(STORAGE_ROOT))
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        pct = (usage.used / usage.total * 100) if usage.total > 0 else 0
        storage = {
            "total": f"{total_gb:.1f}G",
            "used": f"{used_gb:.1f}G",
            "available": f"{free_gb:.1f}G",
            "used_percent": f"{pct:.0f}%"
        }
    except Exception:
        pass

    try:
        from config.node_telemetry import collect_comprehensive_telemetry
        telem = collect_comprehensive_telemetry()
    except Exception:
        telem = {}

    return {
        "cpu_cores": cpu_cores,
        "load_average": load_average,
        "memory": memory,
        "storage": storage,
        "battery": telem.get("battery", {}),
        "network": telem.get("network", {}),
        "cpu_details": telem.get("cpu_details", {}),
        "thermals": telem.get("thermals", {})
    }


def get_health(correlation_id=None):
    config = load_config()
    return {
        "status": "online",
        "node": {
            "id": config.get("NODE_ID", config.get("node_id", "unknown")),
            "name": config.get("NODE_NAME", config.get("name", "unknown")),
            "role": config.get("NODE_ROLE", config.get("role", "compute"))
        },
        "system": get_system_info(),
        "correlation_id": correlation_id or generate_correlation_id(),
        "timestamp": get_current_iso_timestamp()
    }


def resolve_safe_storage_path(relative_path_str: str) -> Path:
    """
    Strictly resolves and validates path within STORAGE_ROOT.
    Rejects path traversal, absolute path escapes, null bytes, symlink escape,
    and outside directory access.
    """
    ensure_storage_root()
    return resolve_safe_path(STORAGE_ROOT, relative_path_str)


RESERVED_STORAGE_ROUTES = {
    "nodes", "list", "download", "usage", "share", "shares",
    "s", "mkdir", "upload", "rename", "preview"
}


class NodeAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15  # Bounded socket timeout prevents hanging on idle or aborted connections

    def send_json(self, status_code, data):
        corr_id = getattr(self, "current_correlation_id", None) or extract_correlation_id(self.headers)
        if corr_id and isinstance(data, dict) and "correlation_id" not in data:
            data["correlation_id"] = corr_id
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if corr_id:
            self.send_header("X-Correlation-ID", corr_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Correlation-ID, X-Request-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
            self.close_connection = True

    def serve_storage_file_inline(self, target_file: Path, filename: str = None, is_preview: bool = False):
        """
        Safely streams a storage file with inline Content-Disposition, strict MIME typing,
        and sandbox Content-Security-Policy for untrusted markup (HTML, SVG, XML).
        """
        if not target_file.exists() or not target_file.is_file():
            self.send_json(404, {"error": "File not found", "status": 404})
            return

        file_size = target_file.stat().st_size
        fname = filename or target_file.name
        ext = target_file.suffix.lower()

        # Security: Untrusted HTML/SVG must NEVER run as active same-origin scripts
        if ext in (".html", ".htm", ".svg", ".xhtml", ".xml"):
            content_type = "text/plain; charset=utf-8"
            disposition = f'inline; filename="{fname}"'
            csp = "default-src 'none'; sandbox"
        else:
            guessed_type = mimetypes.guess_type(fname)[0]
            content_type = guessed_type or "application/octet-stream"
            disposition = f'inline; filename="{fname}"'
            csp = None

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Content-Disposition", disposition)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()

        try:
            with open(target_file, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
            self.close_connection = True

    def serve_storage_file_download(self, target_file: Path, filename: str = None):
        """
        Streams a storage file with attachment Content-Disposition to trigger browser download.
        """
        if not target_file.exists() or not target_file.is_file():
            self.send_json(404, {"error": "File not found", "status": 404})
            return

        file_size = target_file.stat().st_size
        fname = filename or target_file.name
        guessed_type = mimetypes.guess_type(fname)[0]
        content_type = guessed_type or "application/octet-stream"
        safe_fname = fname.replace('"', '\\"')

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_fname}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

        try:
            with open(target_file, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
            self.close_connection = True


    def get_public_base_url(self):
        try:
            pcfg = get_platform_config()
            if pcfg and pcfg.admin_url:
                return pcfg.admin_url.rstrip("/")
        except Exception:
            pass
        host_hdr = self.headers.get("Host", "")
        if host_hdr:
            proto = "https" if (self.headers.get("X-Forwarded-Proto") == "https" or "server." in host_hdr) else "http"
            return f"{proto}://{host_hdr}"
        return "https://server.akshatsahay.space"

    def do_OPTIONS(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/demo-app/") or path.startswith("/recallflow/") or path.startswith("/apps/"):
            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else None
            self.proxy_to_router("OPTIONS", self.path, body_bytes=body_bytes)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Correlation-ID, X-Request-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def check_auth(self, require_explicit: bool = False):
        """Token and origin check for administrative storage endpoints."""
        expected_token = get_auth_token()
        controller_token = get_controller_auth_token()
        auth_header = self.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = self.headers.get("X-Auth-Token", "").strip()

        # Cloudflare Access authentication verification
        cf_email = self.headers.get("Cf-Access-Authenticated-User-Email")
        is_proxied_public = bool(self.headers.get("Cf-Ray") or self.headers.get("Cf-Connecting-Ip"))
        if cf_email and is_proxied_public:
            return True

        # If token matches configured auth token or controller enrollment token
        if token and (constant_time_compare(token, expected_token) or constant_time_compare(token, controller_token)):
            return True

        # If proxied through public tunnel, deny without valid CF auth or token
        if is_proxied_public:
            return False

        # If an explicit auth token was supplied but failed validation, deny
        if auth_header or self.headers.get("X-Auth-Token"):
            return False

        # If explicit authentication is required (e.g. unshared files on public file routes)
        if require_explicit:
            return False

        # Allow direct local loopback or Tailscale private mesh access (when not public proxied)
        client_ip = self.client_address[0]
        if not is_proxied_public and (client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("100.")):
            return True

        # Deny unauthenticated external requests
        return False

    def read_json_body(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len == 0:
                return {}
            raw = self.rfile.read(content_len).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def proxy_to_controller(self, method, subpath, query_str="", body_bytes=None):
        global _CACHED_ACTIVE_CONTROLLER_URL
        configured_url = get_configured_controller_url()
        with _CONTROLLER_URL_LOCK:
            cached_url = _CACHED_ACTIVE_CONTROLLER_URL
        controller_url = cached_url or configured_url
        target_url = f"{controller_url}{subpath}"
        if query_str:
            target_url += f"?{query_str}"

        headers = {
            "User-Agent": "PersonalServer-WebGateway/1.0"
        }
        corr_id = getattr(self, "current_correlation_id", None) or extract_correlation_id(self.headers)
        headers["X-Correlation-ID"] = corr_id
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        if body_bytes is not None:
            headers["Content-Length"] = str(len(body_bytes))

        auth_hdr = self.headers.get("Authorization") or self.headers.get("X-Auth-Token")
        if not auth_hdr:
            auth_token = get_controller_auth_token()
            if auth_token:
                auth_hdr = f"Bearer {auth_token}"
        if auth_hdr:
            headers["Authorization"] = auth_hdr if auth_hdr.startswith("Bearer ") else f"Bearer {auth_hdr}"

        timeout_sec = 35 if ("/apps" in subpath or "/jobs" in subpath) else 10
        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as response:
                resp_data = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(resp_data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(resp_data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
                return
        except urllib.error.HTTPError as e:
            err_body = e.read()
            # If controller rejected with 503 because it is in STANDBY mode, follow leader redirect
            if e.code == 503:
                try:
                    err_json = json.loads(err_body.decode("utf-8"))
                    new_leader_url = err_json.get("leader_url")
                    if new_leader_url and new_leader_url.rstrip("/") != controller_url:
                        with _CONTROLLER_URL_LOCK:
                            _CACHED_ACTIVE_CONTROLLER_URL = new_leader_url.rstrip("/")
                            active_target = _CACHED_ACTIVE_CONTROLLER_URL
                        retry_target = f"{active_target}{subpath}"
                        if query_str:
                            retry_target += f"?{query_str}"
                        retry_req = urllib.request.Request(retry_target, data=body_bytes, headers=headers, method=method)
                        with urllib.request.urlopen(retry_req, timeout=timeout_sec) as retry_resp:
                            retry_data = retry_resp.read()
                            self.send_response(retry_resp.status)
                            self.send_header("Content-Type", retry_resp.headers.get("Content-Type", "application/json"))
                            self.send_header("Content-Length", str(len(retry_data)))
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            try:
                                self.wfile.write(retry_data)
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                                self.close_connection = True
                            return
                except Exception:
                    pass

            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                self.close_connection = True
            return
        except Exception as initial_err:
            # Trigger HA master auto-discovery across candidate controller URLs
            try:
                from config.ha_manager import find_active_leader
                env_candidates = os.environ.get("PERSONALSERVER_CONTROLLER_URLS", "")
                candidates = [u.strip().rstrip("/") for u in env_candidates.split(",") if u.strip()]
                if controller_url not in candidates:
                    candidates.append(controller_url)
                if configured_url not in candidates:
                    candidates.append(configured_url)
                if DEFAULT_CONTROLLER_URL not in candidates:
                    candidates.append(DEFAULT_CONTROLLER_URL)

                active_url, leader_info = find_active_leader(candidates, exclude_url=controller_url)
                if active_url:
                    with _CONTROLLER_URL_LOCK:
                        _CACHED_ACTIVE_CONTROLLER_URL = active_url
                        active_target = _CACHED_ACTIVE_CONTROLLER_URL
                    retry_target = f"{active_target}{subpath}"
                    if query_str:
                        retry_target += f"?{query_str}"
                    retry_req = urllib.request.Request(retry_target, data=body_bytes, headers=headers, method=method)
                    with urllib.request.urlopen(retry_req, timeout=timeout_sec) as response:
                        resp_data = response.read()
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                        self.send_header("Content-Length", str(len(resp_data)))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        try:
                            self.wfile.write(resp_data)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                            self.close_connection = True
                        return
            except Exception:
                pass
            self.send_json(502, {"error": f"Controller unavailable at {controller_url} (HA failover attempted): {initial_err}"})

    def proxy_to_router(self, method, full_path, body_bytes=None):
        config = load_config()
        router_url = (config.get("ROUTER_URL") or DEFAULT_ROUTER_URL).rstrip("/")
        target_url = f"{router_url}{full_path}"

        headers = {}
        for hdr, val in self.headers.items():
            hdr_lower = hdr.lower()
            if hdr_lower in ("host", "authorization", "x-auth-token", "x-onboarding-code", "cf-access-authenticated-user-email", "cf-access-jwt-assertion", "cf-access-user", "cookie-secret"):
                continue
            headers[hdr] = val

        client_ip = self.client_address[0]
        cf_ip = self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For")
        remote_ip = cf_ip.split(",")[0].strip() if cf_ip else client_ip
        headers["X-Forwarded-For"] = remote_ip
        headers["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "https" if (self.headers.get("Cf-Ray") or self.headers.get("X-Forwarded-Proto") == "https") else "http")
        headers["X-Forwarded-Host"] = self.headers.get("Host", get_platform_config().primary_domain)

        if body_bytes is not None:
            headers["Content-Length"] = str(len(body_bytes))

        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with PROXY_OPENER.open(req, timeout=15) as response:
                resp_data = response.read()
                self.send_response(response.status)
                has_content_length = False
                for k, v in response.headers.items():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    if k.lower() == "content-length":
                        has_content_length = True
                    self.send_header(k, v)
                if not has_content_length:
                    self.send_header("Content-Length", str(len(resp_data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(resp_data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            has_content_length = False
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                if k.lower() == "content-length":
                    has_content_length = True
                self.send_header(k, v)
            if not has_content_length:
                self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                self.close_connection = True
        except urllib.error.URLError as e:
            self.send_json(504, {"error": "Application host node is temporarily unreachable", "status": 504})
        except Exception as e:
            self.send_json(502, {"error": "Application gateway error", "status": 502})

    def proxy_to_remote_storage(self, remote_endpoint, method, subpath, query_dict=None, body_bytes=None):
        target_url = f"{remote_endpoint.rstrip('/')}{subpath}"
        if query_dict:
            filtered_qs = {k: v for k, v in query_dict.items() if k != "node"}
            if filtered_qs:
                target_url += f"?{urllib.parse.urlencode(filtered_qs, doseq=True)}"

        headers = {
            "User-Agent": "PersonalServer-StorageProxy/1.0"
        }
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type

        if body_bytes is not None:
            headers["Content-Length"] = str(len(body_bytes))

        # Forward authorization headers or use local node auth token
        auth_hdr = self.headers.get("Authorization") or self.headers.get("X-Auth-Token")
        if not auth_hdr:
            auth_token = get_auth_token()
            if auth_token:
                auth_hdr = f"Bearer {auth_token}"
        if auth_hdr:
            headers["Authorization"] = auth_hdr if auth_hdr.startswith("Bearer ") else f"Bearer {auth_hdr}"

        timeout_sec = 60 if subpath in ("/storage/download", "/storage/upload") else 20
        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as response:
                self.send_response(response.status)
                for hdr in ("Content-Type", "Content-Disposition", "Content-Length"):
                    hdr_val = response.headers.get(hdr)
                    if hdr_val:
                        self.send_header(hdr, hdr_val)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                self.close_connection = True
        except urllib.error.URLError as e:
            err_msg = str(e.reason) if hasattr(e, "reason") else str(e)
            if "timed out" in err_msg.lower():
                self.send_json(504, {"error": f"Remote storage node timed out at {remote_endpoint}: {err_msg}"})
            else:
                self.send_json(502, {"error": f"Remote storage node unavailable at {remote_endpoint}: {err_msg}"})
        except Exception as e:
            self.send_json(502, {"error": f"Remote storage proxy error at {remote_endpoint}: {e}"})

    def do_GET(self):
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. UI Root
        if path == "/":
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                content = index_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
                return
            else:
                self.send_json(200, get_health())
                return

        # 2. Static Files (/static/style.css, /static/app.js)
        if path.startswith("/static/"):
            rel_name = os.path.basename(path)
            static_file = (STATIC_DIR / rel_name).resolve()
            if static_file.exists() and static_file.is_file():
                content = static_file.read_bytes()
                content_type = "text/plain"
                if rel_name.endswith(".css"):
                    content_type = "text/css"
                elif rel_name.endswith(".js"):
                    content_type = "application/javascript"
                elif rel_name.endswith(".html"):
                    content_type = "text/html"
                elif rel_name.endswith(".json"):
                    content_type = "application/json"

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
                return
            else:
                self.send_json(404, {"error": "Static file not found"})
                return

        # 3. Node Health / Status
        if path in ("/health", "/status"):
            self.send_json(200, get_health(self.current_correlation_id))
            return

        # 3a. Dedicated Public Share Namespace: GET /share/<path_or_share_id>...
        if path.startswith("/share/"):
            raw_share_sub = path[len("/share/"):].strip("/")
            if not raw_share_sub:
                self.send_json(404, {"error": "Share link not found", "status": 404})
                return

            parts = raw_share_sub.split("/")
            possible_token = parts[0]
            token_action = parts[1] if len(parts) > 1 else ""

            # Check if the first segment is an existing share token (backward compatibility)
            token_share = None
            if re.match(r'^[a-zA-Z0-9_-]{16,32}$', possible_token):
                token_share = get_share_by_id(possible_token)

            if token_share is not None:
                share = token_share
                action = token_action
                share_id = possible_token
                if share.get("revoked", False):
                    accept = self.headers.get("Accept", "")
                    if "text/html" in accept and not action:
                        self.send_response(404)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        html_err = (
                            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Share Inactive</title>"
                            "<style>body{font-family:sans-serif;background:#090d16;color:#f1f5f9;display:flex;"
                            "align-items:center;justify-content:center;height:100vh;margin:0;}"
                            ".card{background:#111827;border:1px solid #243046;padding:32px 40px;border-radius:8px;"
                            "text-align:center;max-width:440px;box-shadow:0 4px 20px rgba(0,0,0,0.4);}"
                            "h2{margin-top:0;color:#ef4444;}p{color:#94a3b8;font-size:14px;line-height:1.5;}</style></head>"
                            "<body><div class='card'><h2>⚠️ Share Link Unavailable</h2>"
                            "<p>This share link has been revoked, expired, or does not exist.</p></div></body></html>"
                        )
                        body = html_err.encode("utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        try:
                            self.wfile.write(body)
                        except Exception:
                            pass
                        return
                    self.send_json(404, {"error": "This share link has been revoked or does not exist", "status": 404})
                    return

                target_node = share.get("node_id", "")
                share_rel_path = share.get("path", "").replace("\\", "/").strip("/")
                is_dir_share = bool(share.get("is_dir") or share.get("item_type") == "folder")

                is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
                if remote_endpoint == "INVALID":
                    self.send_json(404, {"error": "Storage node for this share is not available", "status": 404})
                    return

                # Action 1: Directory listing: GET /share/<share_id>/list
                if action == "list":
                    if not is_dir_share:
                        self.send_json(400, {"error": "Target share is a file, not a directory", "status": 400})
                        return

                    if not is_local and remote_endpoint:
                        self.proxy_to_remote_storage(remote_endpoint, "GET", f"/share/{share_id}/list", query)
                        return

                    subpath = query.get("subpath", [""])[0].replace("\\", "/").strip("/")
                    if subpath:
                        sub_segs = [s for s in subpath.split("/") if s]
                        if any(s == ".." for s in sub_segs) or "\0" in subpath or subpath.startswith("/"):
                            self.send_json(403, {"error": "Access denied: Path traversal detected", "status": 403})
                            return
                        full_rel = f"{share_rel_path}/{'/'.join(sub_segs)}"
                    else:
                        full_rel = share_rel_path

                    try:
                        folder_base = resolve_safe_storage_path(share_rel_path)
                        target_dir = resolve_safe_storage_path(full_rel)
                        target_dir.resolve().relative_to(folder_base.resolve())
                    except ValueError:
                        self.send_json(403, {"error": "Access denied: Folder boundary violation", "status": 403})
                        return

                    if not target_dir.exists() or not target_dir.is_dir():
                        self.send_json(404, {"error": "Directory not found", "status": 404})
                        return

                    items = []
                    try:
                        for entry in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                            try:
                                entry.resolve().relative_to(folder_base.resolve())
                            except ValueError:
                                continue

                            try:
                                st = entry.stat()
                                is_dir_entry = entry.is_dir()
                                size = 0 if is_dir_entry else st.st_size
                                mtime_iso = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
                                mime = None
                                if not is_dir_entry:
                                    mime, _ = mimetypes.guess_type(entry.name)
                                items.append({
                                    "name": entry.name,
                                    "is_dir": is_dir_entry,
                                    "size": size,
                                    "modified": mtime_iso,
                                    "mime": mime or "application/octet-stream"
                                })
                            except Exception:
                                continue
                    except Exception as e:
                        self.send_json(500, {"error": f"Failed to list directory: {e}", "status": 500})
                        return

                    self.send_json(200, {
                        "share_id": share_id,
                        "folder_name": share.get("filename") or folder_base.name,
                        "current_subpath": subpath,
                        "items": items
                    })
                    return

                # Action 2: File access / download / preview: /share/<share_id>/file, /download, /preview
                if action in ("file", "download", "preview"):
                    if not is_local and remote_endpoint:
                        self.proxy_to_remote_storage(remote_endpoint, "GET", f"/share/{share_id}/{action}", query)
                        return

                    if is_dir_share:
                        subpath = query.get("subpath", [""])[0].replace("\\", "/").strip("/")
                        if not subpath:
                            self.send_json(400, {"error": "Missing 'subpath' parameter", "status": 400})
                            return
                        sub_segs = [s for s in subpath.split("/") if s]
                        if any(s == ".." for s in sub_segs) or "\0" in subpath or subpath.startswith("/"):
                            self.send_json(403, {"error": "Access denied: Path traversal detected", "status": 403})
                            return
                        full_rel = f"{share_rel_path}/{'/'.join(sub_segs)}"
                        try:
                            folder_base = resolve_safe_storage_path(share_rel_path)
                            target_file = resolve_safe_storage_path(full_rel)
                            target_file.resolve().relative_to(folder_base.resolve())
                        except ValueError:
                            self.send_json(403, {"error": "Access denied: Folder boundary violation", "status": 403})
                            return
                    else:
                        try:
                            target_file = resolve_safe_storage_path(share_rel_path)
                        except ValueError:
                            self.send_json(403, {"error": "Access denied", "status": 403})
                            return

                    if not target_file.exists() or not target_file.is_file():
                        self.send_json(404, {"error": "File not found", "status": 404})
                        return

                    as_download = (action == "download") or (query.get("download", ["0"])[0] == "1")
                    if as_download:
                        self.serve_storage_file_download(target_file)
                    else:
                        self.serve_storage_file_inline(target_file)
                    return

                # Action 3: Root share URL: GET /share/<share_id>
                if not action:
                    if is_dir_share:
                        viewer_path = STATIC_DIR / "shared_folder.html"
                        if viewer_path.exists():
                            try:
                                html_text = viewer_path.read_text(encoding="utf-8")
                                injection = (
                                    f"<script>"
                                    f"window.__SHARE_RECORD__ = {json.dumps({'share_id': share['share_id'], 'path': share_rel_path, 'folder_name': share.get('filename') or Path(share_rel_path).name})};"
                                    f"</script>"
                                )
                                if "<head>" in html_text:
                                    html_text = html_text.replace("<head>", f"<head>\n  {injection}", 1)
                                else:
                                    html_text = injection + "\n" + html_text
                                body = html_text.encode("utf-8")
                                self.send_response(200)
                                self.send_header("Content-Type", "text/html; charset=utf-8")
                                self.send_header("Content-Length", str(len(body)))
                                self.send_header("Cache-Control", "no-cache")
                                self.end_headers()
                                self.wfile.write(body)
                                return
                            except Exception as e:
                                self.send_json(500, {"error": f"Failed to serve shared folder viewer: {e}"})
                                return
                        else:
                            self.send_json(500, {"error": "Shared folder viewer not installed"})
                            return
                    else:
                        # Shared file: serve inline
                        if not is_local and remote_endpoint:
                            self.proxy_to_remote_storage(remote_endpoint, "GET", f"/share/{share_id}", query)
                            return
                        try:
                            target_file = resolve_safe_storage_path(share_rel_path)
                        except ValueError:
                            self.send_json(403, {"error": "Access denied"})
                            return
                        if not target_file.exists() or not target_file.is_file():
                            self.send_json(404, {"error": "Shared file not found", "status": 404})
                            return
                        self.serve_storage_file_inline(target_file)
                        return

                self.send_json(404, {"error": "Invalid share action", "status": 404})
                return

            # PATH-BASED PUBLIC SHARING: /share/<logical-path>
            try:
                decoded_rel_path = urllib.parse.unquote(raw_share_sub)
            except Exception:
                self.send_json(400, {"error": "Malformed URL path encoding", "status": 400})
                return

            if not decoded_rel_path or "\0" in decoded_rel_path:
                self.send_json(404, {"error": "File not found", "status": 404})
                return

            clean_norm = decoded_rel_path.replace("\\", "/")
            segs = [s for s in clean_norm.split("/") if s]
            if not segs or any(s == ".." for s in segs) or clean_norm.startswith("/"):
                self.send_json(404, {"error": "File not found", "status": 404})
                return

            clean_rel_path = "/".join(segs)
            target_node = query.get("node", [""])[0]

            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": "Storage node not found", "status": 404})
                return

            if not is_local and remote_endpoint:
                encoded_sub = "/".join(urllib.parse.quote(s) for s in clean_rel_path.split("/"))
                self.proxy_to_remote_storage(remote_endpoint, "GET", f"/share/{encoded_sub}", query)
                return

            try:
                target_item = resolve_safe_storage_path(clean_rel_path)
            except ValueError:
                self.send_json(404, {"error": "File not found", "status": 404})
                return

            canonical_rel = str(target_item.relative_to(STORAGE_ROOT)).replace("\\", "/")

            # Resolve against active share records
            active_share = find_active_share(target_node, canonical_rel)
            if not active_share:
                active_share = find_active_folder_share_for_path(target_node, canonical_rel)

            if not active_share or active_share.get("revoked", False):
                accept = self.headers.get("Accept", "")
                if "text/html" in accept and not query.get("download"):
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    html_err = (
                        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Share Inactive</title>"
                        "<style>body{font-family:sans-serif;background:#090d16;color:#f1f5f9;display:flex;"
                        "align-items:center;justify-content:center;height:100vh;margin:0;}"
                        ".card{background:#111827;border:1px solid #243046;padding:32px 40px;border-radius:8px;"
                        "text-align:center;max-width:440px;box-shadow:0 4px 20px rgba(0,0,0,0.4);}"
                        "h2{margin-top:0;color:#ef4444;}p{color:#94a3b8;font-size:14px;line-height:1.5;}</style></head>"
                        "<body><div class='card'><h2>⚠️ Share Link Unavailable</h2>"
                        "<p>This share link has been revoked, expired, or does not exist.</p></div></body></html>"
                    )
                    body = html_err.encode("utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass
                    return
                self.send_json(404, {"error": "This share link has been revoked or does not exist", "status": 404})
                return

            if not target_item.exists():
                self.send_json(404, {"error": "File not found", "status": 404})
                return

            # If folder share: serve shared folder viewer
            if target_item.is_dir():
                viewer_path = STATIC_DIR / "shared_folder.html"
                if viewer_path.exists():
                    try:
                        html_text = viewer_path.read_text(encoding="utf-8")
                        injection = (
                            f"<script>"
                            f"window.__SHARE_RECORD__ = {json.dumps({'share_id': active_share['share_id'], 'path': canonical_rel, 'folder_name': active_share.get('filename') or target_item.name})};"
                            f"</script>"
                        )
                        if "<head>" in html_text:
                            html_text = html_text.replace("<head>", f"<head>\n  {injection}", 1)
                        else:
                            html_text = injection + "\n" + html_text
                        body = html_text.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception as e:
                        self.send_json(500, {"error": f"Failed to serve shared folder viewer: {e}"})
                        return
                else:
                    self.send_json(500, {"error": "Shared folder viewer not installed"})
                    return

            # If file: serve inline or as download
            as_download = (query.get("download", ["0"])[0] == "1")
            if as_download:
                self.serve_storage_file_download(target_item)
            else:
                self.serve_storage_file_inline(target_item)
            return

        # 3b. Public Storage Share: GET /storage/s/<share_id>
        if path.startswith("/storage/s/"):
            share_id = path[len("/storage/s/"):].strip("/")
            if not share_id or re.search(r'[^a-zA-Z0-9_-]', share_id):
                self.send_json(404, {"error": "Share link not found", "status": 404})
                return

            share = get_share_by_id(share_id)
            if not share or share.get("revoked", False):
                self.send_json(404, {"error": "This share link has been revoked or does not exist", "status": 404})
                return

            target_node = share.get("node_id", "")
            rel_path = share.get("path", "")
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": "Storage node for this share is not available", "status": 404})
                return

            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "GET", "/storage/download", {"path": rel_path})
                return

            try:
                target_file = resolve_safe_storage_path(rel_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e), "status": 403})
                return

            if not target_file.exists() or not target_file.is_file():
                self.send_json(404, {"error": "Shared file not found", "status": 404})
                return

            self.serve_storage_file_inline(target_file)
            return

        # 3c. Storage Subsystem: GET /storage/share (or /storage/shares)
        if path in ("/storage/share", "/storage/shares"):
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            req_path = query.get("path", [""])[0]

            if not req_path:
                db = load_shares_db()
                active = [s for s in db.get("shares", {}).values() if not s.get("revoked")]
                self.send_json(200, {"shares": active})
                return

            active_share = find_active_share(target_node, req_path)
            if active_share:
                pub_base = self.get_public_base_url()
                s_path = (active_share.get("path") or req_path).replace("\\", "/").strip("/")
                encoded_path = "/".join(urllib.parse.quote(seg) for seg in s_path.split("/"))
                is_dir = bool(active_share.get("is_dir") or active_share.get("item_type") == "folder")
                share_url = f"{pub_base}/share/{encoded_path}"
                self.send_json(200, {
                    "shared": True,
                    "share_id": active_share["share_id"],
                    "is_dir": is_dir,
                    "url": share_url,
                    "share_url": share_url,
                    "path_url": share_url,
                    "token_url": f"{pub_base}/share/{active_share['share_id']}",
                    "stable_url": share_url,
                    "path": s_path,
                    "created_at": active_share.get("created_at")
                })
            else:
                self.send_json(200, {"shared": False})
            return

        # 4. Storage Subsystem: GET /storage/nodes
        if path == "/storage/nodes":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return
            self.send_json(200, get_cluster_storage_nodes())
            return

        # 5. Storage Subsystem: GET /storage/list or GET /storage
        if path in ("/storage", "/storage/", "/storage/list"):
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "GET", "/storage/list", query)
                return

            req_path = query.get("path", [""])[0]
            try:
                target_dir = resolve_safe_storage_path(req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_dir.exists():
                self.send_json(404, {"error": f"Directory not found: {req_path}"})
                return

            if not target_dir.is_dir():
                self.send_json(400, {"error": f"Path is a file, not a directory: {req_path}"})
                return

            items = []
            try:
                with os.scandir(target_dir) as entries:
                    for entry in entries:
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            is_dir = entry.is_dir(follow_symlinks=False)
                            items.append({
                                "name": entry.name,
                                "is_dir": is_dir,
                                "size_bytes": stat.st_size if not is_dir else 0,
                                "modified": stat.st_mtime,
                                "extension": Path(entry.name).suffix.lstrip(".") if not is_dir else None
                            })
                        except Exception:
                            continue
            except Exception as e:
                self.send_json(500, {"error": f"Failed to list directory: {e}"})
                return

            # Sort: directories first, then alphabetically
            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

            rel_display = str(target_dir.relative_to(STORAGE_ROOT)).replace("\\", "/")
            if rel_display == ".":
                rel_display = ""

            self.send_json(200, {
                "path": rel_display,
                "items": items,
                "count": len(items)
            })
            return

        # 6. Storage Subsystem: GET /storage/download
        if path == "/storage/download":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "GET", "/storage/download", query)
                return

            req_path = query.get("path", [""])[0]
            try:
                target_file = resolve_safe_storage_path(req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_file.exists():
                self.send_json(404, {"error": "File or folder not found"})
                return

            # Directory ZIP archive download
            if target_file.is_dir():
                folder_name = target_file.name if target_file != STORAGE_ROOT else "storage_archive"
                if not folder_name:
                    folder_name = "storage_archive"
                zip_filename = f"{folder_name}.zip"

                tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                tmp_zip_path = Path(tmp_zip.name)
                tmp_zip.close()

                try:
                    with zipfile.ZipFile(tmp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                        for root_dir, dirs, files in os.walk(target_file, followlinks=False):
                            root_path = Path(root_dir)
                            # Safe symlink handling for directories: prevent symlink escapes
                            safe_dirs = []
                            for d in dirs:
                                d_path = root_path / d
                                if d_path.is_symlink():
                                    try:
                                        resolved = d_path.resolve()
                                        resolved.relative_to(target_file)
                                        safe_dirs.append(d)
                                    except Exception:
                                        continue
                                else:
                                    safe_dirs.append(d)
                            dirs[:] = safe_dirs

                            # Preserve empty directories
                            if not dirs and not files and root_path != target_file:
                                rel_dir = os.path.relpath(root_path, target_file).replace("\\", "/")
                                if rel_dir != "." and not rel_dir.startswith("/") and ".." not in rel_dir.split("/"):
                                    zinfo = zipfile.ZipInfo(f"{rel_dir}/")
                                    zinfo.external_attr = 0o755 << 16
                                    zf.writestr(zinfo, b"")

                            for f in files:
                                f_path = root_path / f
                                if f_path.is_symlink():
                                    try:
                                        resolved = f_path.resolve()
                                        resolved.relative_to(target_file)
                                    except Exception:
                                        continue
                                rel_file = os.path.relpath(f_path, target_file).replace("\\", "/")
                                if rel_file.startswith("/") or rel_file.startswith("\\") or ".." in rel_file.split("/"):
                                    continue
                                try:
                                    zf.write(f_path, arcname=rel_file)
                                except (OSError, IOError, PermissionError) as e:
                                    self.send_json(500, {"error": f"Failed to read file '{f}' for archive: {e}"})
                                    return

                    zip_size = tmp_zip_path.stat().st_size
                    ascii_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', zip_filename)
                    utf8_filename = urllib.parse.quote(zip_filename, safe="")

                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(zip_size))
                    self.send_header("Content-Disposition", f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}')
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    with open(tmp_zip_path, "rb") as zf_in:
                        while True:
                            chunk = zf_in.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    self.close_connection = True
                    return
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to generate ZIP archive: {e}"})
                    return
                finally:
                    if tmp_zip_path.exists():
                        try:
                            tmp_zip_path.unlink()
                        except Exception:
                            pass
                return

            # Single file download
            file_size = target_file.stat().st_size
            filename = target_file.name
            ascii_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
            utf8_filename = urllib.parse.quote(filename, safe="")

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition", f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Stream file in 64KB chunks
            try:
                with open(target_file, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                self.close_connection = True
                return
            return

        # 7. Storage Usage: GET /storage/usage
        if path == "/storage/usage":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return
            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "GET", "/storage/usage", query)
                return

            ensure_storage_root()
            total_size = 0
            file_count = 0
            dir_count = 0
            for root, dirs, files in os.walk(STORAGE_ROOT):
                dir_count += len(dirs)
                for f in files:
                    file_count += 1
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                    except Exception:
                        pass

            sys_info = get_system_info()
            self.send_json(200, {
                "storage_root": str(STORAGE_ROOT),
                "used_bytes": total_size,
                "files_count": file_count,
                "folders_count": dir_count,
                "disk": sys_info.get("storage", {})
            })
            return

        # 6b. Storage Subsystem: GET /storage/preview (Inline Document & Media Preview)
        if path == "/storage/preview":
            target_node = query.get("node", [""])[0]
            req_path = query.get("path", [""])[0]
            if not req_path:
                self.send_json(400, {"error": "Missing 'path' parameter", "status": 400})
                return

            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return

            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "GET", "/storage/preview", query)
                return

            try:
                target_file = resolve_safe_storage_path(req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e), "status": 403})
                return

            if not target_file.exists() or not target_file.is_file():
                self.send_json(404, {"error": "File not found", "status": 404})
                return

            canonical_rel = str(target_file.relative_to(STORAGE_ROOT)).replace("\\", "/")
            active_share = find_active_share(target_node, canonical_rel)
            is_shared = active_share is not None and not active_share.get("revoked", False)

            # If not publicly shared, preview requires administrative or authenticated access
            if not is_shared and not self.check_auth():
                self.send_json(401, {"error": "Unauthorized", "status": 401})
                return

            self.serve_storage_file_inline(target_file, is_preview=True)
            return

        # 6c. Stable Path-Based Storage File Route: GET /storage/<path>
        if path.startswith("/storage/") and not path.startswith("/storage/s/"):
            raw_subpath = path[len("/storage/"):].strip("/")
            first_seg = raw_subpath.split("/")[0] if raw_subpath else ""

            if first_seg not in RESERVED_STORAGE_ROUTES:
                try:
                    decoded_rel_path = urllib.parse.unquote(raw_subpath)
                except Exception:
                    self.send_json(400, {"error": "Malformed URL path encoding", "status": 400})
                    return

                if not decoded_rel_path or "\0" in decoded_rel_path:
                    self.send_json(404, {"error": "File not found", "status": 404})
                    return

                target_node = query.get("node", [""])[0]
                is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
                if remote_endpoint == "INVALID":
                    self.send_json(404, {"error": "Storage node not found", "status": 404})
                    return

                if not is_local and remote_endpoint:
                    encoded_sub = "/".join(urllib.parse.quote(s) for s in decoded_rel_path.split("/"))
                    self.proxy_to_remote_storage(remote_endpoint, "GET", f"/storage/{encoded_sub}", query)
                    return

                try:
                    target_file = resolve_safe_storage_path(decoded_rel_path)
                except ValueError:
                    # Path traversal or escape attempts receive 404 to avoid leaking filesystem layout
                    self.send_json(404, {"error": "File not found", "status": 404})
                    return

                # Directories cannot be accessed through public file URLs
                if not target_file.exists() or not target_file.is_file():
                    self.send_json(404, {"error": "File not found", "status": 404})
                    return

                # Canonicalize relative path against STORAGE_ROOT
                canonical_rel_path = str(target_file.relative_to(STORAGE_ROOT)).replace("\\", "/")
                active_share = find_active_share(target_node, canonical_rel_path)
                if not active_share:
                    active_share = find_active_folder_share_for_path(target_node, canonical_rel_path)
                is_shared = active_share is not None and not active_share.get("revoked", False)

                if is_shared:
                    # Public access permitted for explicitly shared file
                    self.serve_storage_file_inline(target_file)
                    return

                # If sharing is OFF, allow explicitly authenticated user/admin to access directly
                if self.check_auth(require_explicit=True):
                    self.serve_storage_file_inline(target_file)
                    return

                # Unauthenticated access to unshared/revoked private file: ALWAYS return 404
                # (Prevents enumeration of private file names)
                self.send_json(404, {"error": "File not found", "status": 404})
                return

        # 7. Session Info: GET /api/session
        if path == "/api/session":
            cf_email = self.headers.get("Cf-Access-Authenticated-User-Email") or self.headers.get("cf-access-authenticated-user-email")
            is_proxied_public = bool(self.headers.get("Cf-Ray") or self.headers.get("Cf-Connecting-Ip"))
            client_ip = self.client_address[0]
            is_direct_private = not is_proxied_public and (client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("100."))

            if cf_email:
                self.send_json(200, {
                    "authenticated": True,
                    "user": cf_email,
                    "auth_method": "cloudflare-access"
                })
            elif is_direct_private:
                self.send_json(200, {
                    "authenticated": True,
                    "user": "Local Admin",
                    "auth_method": "tailscale-private"
                })
            else:
                self.send_json(200, {
                    "authenticated": False,
                    "user": "Unauthenticated",
                    "auth_method": "none"
                })
            return

        # 8. Services Status: GET /api/services
        if path == "/api/services":
            controller_url = get_configured_controller_url()
            controller_connected = False
            try:
                with urllib.request.urlopen(f"{controller_url}/health", timeout=2) as r:
                    if r.status == 200:
                        controller_connected = True
            except Exception:
                pass

            self.send_json(200, {
                "node_api": "running",
                "cloudflare": "connected",
                "controller": "connected" if controller_connected else "disconnected"
            })
            return

        # 8a. Live Diagnostics / Hardware & Speed Test: GET /diagnostics or GET /api/diagnostics
        if path in ("/diagnostics", "/api/diagnostics"):
            try:
                from config.node_telemetry import run_live_diagnostics
                config = load_config()
                node_id = config.get("NODE_ID", config.get("node_id", "unknown"))
                diag_res = run_live_diagnostics()
                self.send_json(200, {"node_id": node_id, "diagnostics": diag_res})
            except Exception as e:
                self.send_json(500, {"error": f"Diagnostics error: {e}"})
            return

        # 8a2. Node Diagnostics: GET /api/nodes/<node_id>/diagnostics or /nodes/<node_id>/diagnostics
        norm_path = path.rstrip("/")
        if (norm_path.startswith("/api/nodes/") or norm_path.startswith("/nodes/")) and norm_path.endswith("/diagnostics"):
            parts = norm_path.strip("/").split("/")
            nid = parts[-2]
            config = load_config()
            local_id = config.get("NODE_ID", config.get("node_id", ""))
            local_name = config.get("NODE_NAME", config.get("name", ""))
            if nid in ("local", "self", local_id, local_name):
                try:
                    from config.node_telemetry import run_live_diagnostics
                    diag_res = run_live_diagnostics()
                    self.send_json(200, {"node_id": local_id or nid, "diagnostics": diag_res})
                    return
                except Exception as e:
                    self.send_json(500, {"error": f"Diagnostics error: {e}"})
                    return
            subpath = norm_path[4:] if norm_path.startswith("/api/") else norm_path
            self.proxy_to_controller("GET", subpath, parsed.query)
            return

        # 8b. Platform Configuration: GET /api/platform or GET /api/config/platform
        if path in ("/api/platform", "/api/config/platform"):
            self.send_json(200, get_platform_config().to_dict())
            return

        # 8c. Cluster Utilization and Cluster Info Proxy
        if path in ("/cluster/utilization", "/cluster", "/nodes") or path.startswith("/cluster/") or path.startswith("/nodes/"):
            self.proxy_to_controller("GET", path, parsed.query)
            return

        # 9. Proxy to Controller for UI: /api/cluster, /api/jobs, /api/jobs/<job_id>
        if path.startswith("/api/"):
            subpath = path[4:]  # /api/cluster -> /cluster, /api/jobs -> /jobs
            self.proxy_to_controller("GET", subpath, parsed.query)
            return

        # Fall through to PersonalServer Application Router (/demo-app/*, /recallflow/*, etc.)
        self.proxy_to_router("GET", self.path)

    def do_POST(self):
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 0b. Storage Subsystem: POST /storage/share (Create/Toggle Share)
        if path == "/storage/share":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            body = self.read_json_body()
            if not body or not body.get("path"):
                self.send_json(400, {"error": "Missing 'path' parameter"})
                return

            req_path = body.get("path", "").strip()
            target_node = body.get("node", "").strip()

            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return

            filename = Path(req_path).name
            is_dir = False
            if is_local:
                try:
                    target_file = resolve_safe_storage_path(req_path)
                except ValueError as e:
                    self.send_json(403, {"error": str(e)})
                    return

                if not target_file.exists():
                    self.send_json(404, {"error": "File not found"})
                    return

                is_dir = target_file.is_dir()
                filename = target_file.name
            else:
                is_dir = bool(body.get("is_dir", False))
                filename = Path(req_path).name

            share_record = create_share(target_node, req_path, filename, is_dir=is_dir)
            pub_base = self.get_public_base_url()
            clean_rel = str(target_file.relative_to(STORAGE_ROOT)).replace("\\", "/") if is_local else req_path.replace("\\", "/").strip("/")
            encoded_path = "/".join(urllib.parse.quote(seg) for seg in clean_rel.split("/"))
            share_url = f"{pub_base}/share/{encoded_path}"

            self.send_json(200, {
                "status": "enabled",
                "shared": True,
                "share_id": share_record["share_id"],
                "is_dir": is_dir,
                "url": share_url,
                "share_url": share_url,
                "path_url": share_url,
                "token_url": f"{pub_base}/share/{share_record['share_id']}",
                "stable_url": share_url,
                "path": clean_rel,
                "created_at": share_record["created_at"]
            })
            return

        # 1. Storage Subsystem: POST /storage/mkdir
        if path == "/storage/mkdir":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                content_len = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(content_len) if content_len > 0 else None
                self.proxy_to_remote_storage(remote_endpoint, "POST", "/storage/mkdir", query, body_bytes)
                return

            body = self.read_json_body()
            if not body or not body.get("name"):
                self.send_json(400, {"error": "Missing 'name' field for new folder"})
                return

            folder_name = body.get("name", "").strip()
            allow_subpaths = bool(body.get("parents") or body.get("recursive") or "/" in folder_name or "\\" in folder_name)
            if not allow_subpaths and (re.search(r'[\\/:\*\?"<>\|\x00]', folder_name) or ".." in folder_name):
                self.send_json(400, {"error": "Invalid directory name"})
                return
            if allow_subpaths:
                try:
                    folder_name = sanitize_relative_path(folder_name)
                except ValueError as e:
                    self.send_json(400, {"error": f"Invalid directory path: {e}"})
                    return

            parent_rel = query.get("path", [""])[0] or body.get("path", "")
            try:
                parent_dir = resolve_safe_storage_path(parent_rel)
                target_dir = resolve_safe_storage_path(os.path.join(parent_rel, folder_name))
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if target_dir.exists():
                self.send_json(409, {"error": "Directory already exists"})
                return

            try:
                target_dir.mkdir(parents=True, exist_ok=False)
                self.send_json(200, {
                    "status": "created",
                    "path": str(target_dir.relative_to(STORAGE_ROOT)).replace("\\", "/")
                })
            except Exception as e:
                self.send_json(500, {"error": f"Failed to create directory: {e}"})
            return

        # 2. Storage Subsystem: POST /storage/upload
        if path == "/storage/upload":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return

            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > MAX_UPLOAD_BYTES:
                self.send_json(413, {"error": f"Upload exceeds maximum allowed size ({MAX_UPLOAD_BYTES // (1024*1024)} MB)"})
                return

            if not is_local and remote_endpoint:
                body_bytes = self.rfile.read(content_len) if content_len > 0 else None
                self.proxy_to_remote_storage(remote_endpoint, "POST", "/storage/upload", query, body_bytes)
                return

            target_dir_rel = query.get("path", [""])[0]
            try:
                target_dir = resolve_safe_storage_path(target_dir_rel)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_dir.exists() or not target_dir.is_dir():
                self.send_json(400, {"error": "Upload target directory does not exist"})
                return

            # Conflict policy: 'fail' (default), 'overwrite', 'skip', 'rename'
            conflict = query.get("conflict", ["fail"])[0].lower()
            if query.get("overwrite", ["false"])[0].lower() in ("true", "1"):
                conflict = "overwrite"

            # Case: Empty directory creation via upload
            is_dir_upload = query.get("is_dir", ["false"])[0].lower() in ("true", "1")
            if is_dir_upload:
                dir_rel = query.get("relative_path", [""])[0] or query.get("filepath", [""])[0]
                if not dir_rel:
                    self.send_json(400, {"error": "Missing directory path for is_dir upload"})
                    return
                try:
                    clean_rel = sanitize_relative_path(dir_rel)
                    created_dir = (target_dir / clean_rel).resolve()
                    created_dir.relative_to(STORAGE_ROOT)
                    created_dir.relative_to(target_dir)
                    created_dir.mkdir(parents=True, exist_ok=True)
                    self.send_json(200, {
                        "status": "created",
                        "path": str(created_dir.relative_to(STORAGE_ROOT)).replace("\\", "/")
                    })
                except Exception as e:
                    self.send_json(400, {"error": str(e)})
                return

            content_type = self.headers.get("Content-Type", "")

            # Case A: Multipart form upload
            if "multipart/form-data" in content_type:
                boundary = ""
                for part in content_type.split(";"):
                    part = part.strip()
                    if part.startswith("boundary="):
                        boundary = part[9:].strip('"')
                        break
                if not boundary:
                    self.send_json(400, {"error": "Missing boundary in multipart form data"})
                    return

                raw_data = self.rfile.read(content_len)
                boundary_bytes = boundary.encode("utf-8")

                parts = raw_data.split(b"--" + boundary_bytes)
                saved_files = []

                for part in parts:
                    if b"Content-Disposition" in part and b"filename=" in part:
                        header_part, file_data = part.split(b"\r\n\r\n", 1)
                        if file_data.endswith(b"\r\n"):
                            file_data = file_data[:-2]
                        elif file_data.endswith(b"\n"):
                            file_data = file_data[:-1]

                        match = re.search(rb'filename="?([^";\r\n]+)"?', header_part)
                        if match:
                            raw_filename = match.group(1).decode("utf-8", errors="ignore").strip()

                            # Check for relative_path in query or multipart filename
                            rel_candidate = query.get("relative_path", [""])[0] or query.get("filepath", [""])[0]
                            if not rel_candidate and ("/" in raw_filename or "\\" in raw_filename):
                                rel_candidate = raw_filename

                            if rel_candidate:
                                try:
                                    clean_rel = sanitize_relative_path(rel_candidate)
                                except ValueError as e:
                                    self.send_json(400, {"error": f"Invalid relative path: {e}"})
                                    return
                                dest_path = (target_dir / clean_rel).resolve()
                            else:
                                safe_name = os.path.basename(raw_filename)
                                safe_name = re.sub(r'[\\/:\*\?"<>\|\x00-\x1f]', '_', safe_name).strip()
                                if not safe_name or safe_name in (".", ".."):
                                    safe_name = "uploaded_file"
                                dest_path = (target_dir / safe_name).resolve()

                            # Security: Strict containment check
                            try:
                                dest_path.relative_to(STORAGE_ROOT)
                                dest_path.relative_to(target_dir)
                            except Exception:
                                self.send_json(403, {"error": "Access Denied: Path escapes upload root"})
                                return

                            # Conflict resolution
                            if dest_path.exists():
                                rel_display = str(dest_path.relative_to(target_dir)).replace("\\", "/")
                                if conflict == "fail":
                                    self.send_json(409, {
                                        "error": f"File already exists: {rel_display}",
                                        "code": "CONFLICT",
                                        "file": rel_display
                                    })
                                    return
                                elif conflict == "skip":
                                    saved_files.append({
                                        "name": dest_path.name,
                                        "file": rel_display,
                                        "status": "skipped",
                                        "reason": "already_exists"
                                    })
                                    continue
                                elif conflict == "rename":
                                    dest_path = get_unique_conflict_path(dest_path)

                            dest_parent = dest_path.parent
                            dest_parent.mkdir(parents=True, exist_ok=True)
                            tmp_path = dest_parent / f".tmp_{dest_path.name}_{secrets.token_hex(4)}"

                            try:
                                with open(tmp_path, "wb") as f:
                                    f.write(file_data)
                                os.replace(tmp_path, dest_path)
                            except Exception:
                                if tmp_path.exists():
                                    try:
                                        tmp_path.unlink()
                                    except Exception:
                                        pass
                                raise

                            rel_display = str(dest_path.relative_to(target_dir)).replace("\\", "/")
                            saved_files.append({
                                "name": dest_path.name,
                                "file": rel_display,
                                "path": str(dest_path.relative_to(STORAGE_ROOT)).replace("\\", "/"),
                                "status": "uploaded",
                                "size_bytes": len(file_data)
                            })

                if not saved_files:
                    self.send_json(400, {"error": "No valid file data found in upload request"})
                    return

                self.send_json(200, {
                    "status": "uploaded",
                    "files": [f["name"] for f in saved_files],
                    "items": saved_files,
                    "target_dir": str(target_dir.relative_to(STORAGE_ROOT)).replace("\\", "/")
                })
                return

            # Case B: Raw body upload
            else:
                rel_candidate = query.get("relative_path", [""])[0] or query.get("filepath", [""])[0]
                if rel_candidate:
                    try:
                        clean_rel = sanitize_relative_path(rel_candidate)
                    except ValueError as e:
                        self.send_json(400, {"error": f"Invalid relative path: {e}"})
                        return
                    dest_path = (target_dir / clean_rel).resolve()
                else:
                    filename = query.get("filename", ["upload.dat"])[0]
                    safe_name = os.path.basename(filename)
                    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
                    dest_path = (target_dir / safe_name).resolve()

                # Security: Strict containment check
                try:
                    dest_path.relative_to(STORAGE_ROOT)
                    dest_path.relative_to(target_dir)
                except Exception:
                    self.send_json(403, {"error": "Access Denied: Path escapes upload root"})
                    return

                # Conflict resolution
                if dest_path.exists():
                    rel_display = str(dest_path.relative_to(target_dir)).replace("\\", "/")
                    if conflict == "fail":
                        self.send_json(409, {
                            "error": f"File already exists: {rel_display}",
                            "code": "CONFLICT",
                            "file": rel_display
                        })
                        return
                    elif conflict == "skip":
                        self.send_json(200, {
                            "status": "skipped",
                            "file": rel_display,
                            "reason": "already_exists"
                        })
                        return
                    elif conflict == "rename":
                        dest_path = get_unique_conflict_path(dest_path)

                dest_parent = dest_path.parent
                dest_parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_parent / f".tmp_{dest_path.name}_{secrets.token_hex(4)}"

                try:
                    with open(tmp_path, "wb") as f:
                        remaining = content_len
                        while remaining > 0:
                            chunk_size = min(remaining, 65536)
                            chunk = self.rfile.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            remaining -= len(chunk)
                    if remaining == 0:
                        os.replace(tmp_path, dest_path)
                    else:
                        if tmp_path.exists():
                            try:
                                tmp_path.unlink()
                            except Exception:
                                pass
                        self.send_json(400, {"error": "Upload incomplete, connection ended early"})
                        return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass
                    self.close_connection = True
                    return
                except Exception:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass
                    raise

                rel_display = str(dest_path.relative_to(target_dir)).replace("\\", "/")
                self.send_json(200, {
                    "status": "uploaded",
                    "file": rel_display,
                    "name": dest_path.name,
                    "path": str(dest_path.relative_to(STORAGE_ROOT)).replace("\\", "/"),
                    "bytes": content_len
                })
                return

        # 3. Storage Subsystem: POST /storage/rename
        if path == "/storage/rename":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                content_len = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(content_len) if content_len > 0 else None
                self.proxy_to_remote_storage(remote_endpoint, "POST", "/storage/rename", query, body_bytes)
                return

            body = self.read_json_body()
            if not body or not body.get("path") or not body.get("new_name"):
                self.send_json(400, {"error": "Missing 'path' or 'new_name' field"})
                return

            item_path = body.get("path")
            new_name = body.get("new_name", "").strip()

            if re.search(r'[\\/:\*\?"<>\|\x00]', new_name) or ".." in new_name:
                self.send_json(400, {"error": "Invalid new name"})
                return

            try:
                src_item = resolve_safe_storage_path(item_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not src_item.exists():
                self.send_json(404, {"error": "Source file or directory does not exist"})
                return

            if src_item == STORAGE_ROOT:
                self.send_json(403, {"error": "Cannot rename storage root"})
                return

            dest_item = src_item.parent / new_name
            try:
                resolve_safe_storage_path(str(dest_item.relative_to(STORAGE_ROOT)))
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if dest_item.exists():
                self.send_json(409, {"error": "Destination already exists"})
                return

            try:
                os.rename(src_item, dest_item)
                self.send_json(200, {
                    "status": "renamed",
                    "from": str(src_item.relative_to(STORAGE_ROOT)).replace("\\", "/"),
                    "to": str(dest_item.relative_to(STORAGE_ROOT)).replace("\\", "/")
                })
            except Exception as e:
                self.send_json(500, {"error": f"Rename failed: {e}"})
            return

        # 4. Proxy Job Submission and Node Lifecycle to Controller: POST /api/* or POST /nodes/*
        if path.startswith("/api/") or path.startswith("/nodes/"):
            subpath = path[4:] if path.startswith("/api/") else path
            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else None
            self.proxy_to_controller("POST", subpath, parsed.query, body_bytes)
            return

        # Fall through to PersonalServer Application Router
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None
        self.proxy_to_router("POST", self.path, body_bytes=body_bytes)

    def do_DELETE(self):
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Storage Subsystem: DELETE /storage/share (Revoke Share)
        if path == "/storage/share":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            share_id = query.get("share_id", [""])[0]
            req_path = query.get("path", [""])[0]
            target_node = query.get("node", [""])[0]

            if not share_id and not req_path:
                body = self.read_json_body()
                if body:
                    share_id = body.get("share_id", "")
                    req_path = body.get("path", "")
                    target_node = body.get("node", target_node)

            identifier = share_id or req_path
            if not identifier:
                self.send_json(400, {"error": "Missing 'share_id' or 'path' parameter"})
                return

            revoke_share(identifier, target_node)
            self.send_json(200, {
                "status": "revoked",
                "shared": False
            })
            return

        # Storage Subsystem: DELETE /storage
        if path == "/storage":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            target_node = query.get("node", [""])[0]
            is_local, remote_endpoint = resolve_target_node_endpoint(target_node)
            if remote_endpoint == "INVALID":
                self.send_json(404, {"error": f"Target storage node '{target_node}' not found in cluster"})
                return
            if not is_local and remote_endpoint:
                self.proxy_to_remote_storage(remote_endpoint, "DELETE", "/storage", query)
                return

            req_path = query.get("path", [""])[0]
            if not req_path:
                self.send_json(400, {"error": "Missing 'path' parameter"})
                return

            try:
                target_item = resolve_safe_storage_path(req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if target_item == STORAGE_ROOT:
                self.send_json(403, {"error": "Cannot delete storage root"})
                return

            if not target_item.exists():
                self.send_json(404, {"error": "Item not found"})
                return

            try:
                if target_item.is_dir():
                    shutil.rmtree(target_item)
                else:
                    target_item.unlink()
                self.send_json(200, {
                    "status": "deleted",
                    "path": req_path
                })
            except Exception as e:
                self.send_json(500, {"error": f"Failed to delete item: {e}"})
            return

        # Proxy /api/ DELETE requests (e.g. DELETE /api/apps/<app_id>) and /nodes/ to controller
        if path.startswith("/api/") or path.startswith("/nodes/"):
            subpath = path[4:] if path.startswith("/api/") else path
            self.proxy_to_controller("DELETE", subpath, parsed.query)
            return

        # Fall through to PersonalServer Application Router
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None
        self.proxy_to_router("DELETE", self.path, body_bytes=body_bytes)

    def do_PUT(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None
        self.proxy_to_router("PUT", self.path, body_bytes=body_bytes)

    def do_PATCH(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None
        self.proxy_to_router("PATCH", self.path, body_bytes=body_bytes)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/health", "/status") or path.startswith("/static/") or path.startswith("/storage/s/") or path.startswith("/share/"):
            self.do_GET()
            return
        self.proxy_to_router("HEAD", self.path)

    def log_message(self, format, *args):
        print(f"[NODE-API] {args[0]}")


def is_service_running(pattern):
    try:
        out = subprocess.check_output(["pgrep", "-f", str(pattern)], shell=False, text=True, stderr=subprocess.DEVNULL)
        return bool(out.strip())
    except Exception:
        return False


def start_heartbeat_reporter(interval_seconds=15):
    """Background daemon thread to report authenticated node heartbeats to the controller periodically."""
    def _reporter():
        while True:
            try:
                reg_file = RUNTIME_DIR / "registration.json"
                if reg_file.exists():
                    with open(reg_file, "r", encoding="utf-8") as f:
                        reg_state = json.load(f)
                    if reg_state.get("registered") and reg_state.get("auth_token"):
                        controller_url = get_configured_controller_url()
                        auth_token = reg_state.get("auth_token")
                        node_id = reg_state.get("node_id")

                        now = datetime.now(timezone.utc).isoformat()
                        sys_info = get_system_info()
                        services_info = {
                            "node_api": "running",
                            "node_worker": "running" if (is_service_running("agent/node-agent.py work") or is_service_running("node-agent.py work") or is_service_running("local_worker")) else "stopped",
                            "cloudflare": "running" if is_service_running("cloudflared") else "stopped"
                        }

                        payload = {
                            "node_id": node_id,
                            "status": "online",
                            "timestamp": now,
                            "services": services_info,
                            "battery": sys_info.get("battery", {}),
                            "network": sys_info.get("network", {}),
                            "cpu_details": sys_info.get("cpu_details", {}),
                            "thermals": sys_info.get("thermals", {}),
                            "system": {
                                "cpu_cores": sys_info.get("cpu_cores", 8),
                                "load_average": sys_info.get("load_average", []),
                                "memory": f"{sys_info.get('memory', {}).get('used', '0')}/{sys_info.get('memory', {}).get('total', '0')}",
                                "storage": f"{sys_info.get('storage', {}).get('used', '0')}/{sys_info.get('storage', {}).get('total', '0')} ({sys_info.get('storage', {}).get('used_percent', '0%')})",
                                "battery": sys_info.get("battery", {}),
                                "network": sys_info.get("network", {}),
                                "cpu_details": sys_info.get("cpu_details", {}),
                                "thermals": sys_info.get("thermals", {})
                            }
                        }

                        req = urllib.request.Request(
                            f"{controller_url}/heartbeat",
                            data=json.dumps(payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {auth_token}",
                                "User-Agent": "PersonalServer-NodeAgent/1.0"
                            },
                            method="POST"
                        )
                        try:
                            with urllib.request.urlopen(req, timeout=5) as resp:
                                if resp.status == 200:
                                    reg_state["last_heartbeat"] = now
                                    with open(reg_file, "w", encoding="utf-8") as f_out:
                                        json.dump(reg_state, f_out, indent=2)
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(interval_seconds)

    t = threading.Thread(target=_reporter, daemon=True, name="HeartbeatReporterDaemon")
    t.start()


def create_node_api_server(host=HOST, port=PORT):
    server = ThreadingHTTPServer((host, port), NodeAPIHandler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    ensure_storage_root()
    start_heartbeat_reporter(interval_seconds=15)
    server = create_node_api_server(HOST, PORT)
    print(f"PersonalServer Node API & Web Operations running on {HOST}:{PORT}")
    print(f"Storage Root initialized at: {STORAGE_ROOT}")
    server.serve_forever()

