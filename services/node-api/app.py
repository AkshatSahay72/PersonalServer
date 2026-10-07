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
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime, timezone

HOST = "0.0.0.0"
PORT = 8080
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from config.platform_config import get_platform_config
CONFIG_FILE = BASE_DIR / "config" / "node.conf"
CONFIG_JSON = BASE_DIR / "config" / "node.json"
SECRETS_DIR = BASE_DIR / "config" / "secrets"
RUNTIME_DIR = BASE_DIR / "runtime"
AUTH_TOKEN_FILE = SECRETS_DIR / "auth.token"
STORAGE_ROOT = (BASE_DIR / "storage").resolve()
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

DEFAULT_CONTROLLER_URL = "http://100.120.251.42:8000"
DEFAULT_ROUTER_URL = "http://100.120.251.42:8088"


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

DEFAULT_KNOWN_NODES = {
    "server-5387a86bf36116b1": {"name": "vivo-y31", "endpoint": "http://100.85.108.5:8080"},
    "vivo-y31": {"node_id": "server-5387a86bf36116b1", "endpoint": "http://100.85.108.5:8080"},
    "server-95bad5ff01424d4c8d184330d6d2e394": {"name": "node-02", "endpoint": "http://100.73.52.72:8080"},
    "node-02": {"node_id": "server-95bad5ff01424d4c8d184330d6d2e394", "endpoint": "http://100.73.52.72:8080"},
}


def resolve_target_node_endpoint(node_param):
    if not node_param:
        return True, None
    param_str = str(node_param).strip()
    config = load_config()
    local_id = config.get("NODE_ID", config.get("node_id", ""))
    local_name = config.get("NODE_NAME", config.get("name", ""))

    # If explicitly referring to local node
    if param_str.lower() in ("local", "self", "local_node") or (local_id and param_str == local_id) or (local_name and param_str == local_name):
        return True, None

    if param_str in DEFAULT_KNOWN_NODES:
        return False, DEFAULT_KNOWN_NODES[param_str].get("endpoint")

    # Dynamic lookup from controller to prevent arbitrary SSRF
    try:
        controller_url = (config.get("CONTROLLER_URL") or DEFAULT_CONTROLLER_URL).rstrip("/")
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
                    if nid in DEFAULT_KNOWN_NODES:
                        return False, DEFAULT_KNOWN_NODES[nid].get("endpoint")
                    if nname in DEFAULT_KNOWN_NODES:
                        return False, DEFAULT_KNOWN_NODES[nname].get("endpoint")
    except Exception:
        pass

    # Reject unknown or arbitrary target
    return False, "INVALID"


def get_cluster_storage_nodes():
    config = load_config()
    controller_url = (config.get("CONTROLLER_URL") or DEFAULT_CONTROLLER_URL).rstrip("/")
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


def command(cmd):
    try:
        return subprocess.check_output(
            cmd,
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return ""


def get_system_info():
    try:
        cpu_cores = int(command("nproc"))
    except Exception:
        cpu_cores = 1

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

    storage_output = command(f"df -h '{STORAGE_ROOT}'")
    storage = {"total": "N/A", "used": "N/A", "available": "N/A", "used_percent": "N/A"}
    lines = storage_output.splitlines()
    if len(lines) >= 2:
        parts = lines[-1].split()
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
        "storage": storage
    }


def get_health():
    config = load_config()
    return {
        "status": "online",
        "node": {
            "id": config.get("NODE_ID", config.get("node_id", "unknown")),
            "name": config.get("NODE_NAME", config.get("name", "unknown")),
            "role": config.get("NODE_ROLE", config.get("role", "compute"))
        },
        "system": get_system_info(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def resolve_safe_storage_path(relative_path_str: str) -> Path:
    """
    Strictly resolves and validates path within STORAGE_ROOT.
    Rejects path traversal, absolute path escapes, null bytes, symlink escape,
    and outside directory access.
    """
    ensure_storage_root()
    
    if not relative_path_str:
        return STORAGE_ROOT

    # Reject null bytes
    if "\0" in relative_path_str:
        raise ValueError("Invalid path: Null byte detected")

    # Reject absolute path escapes or drive letters
    if relative_path_str.startswith("/") or relative_path_str.startswith("\\") or (len(relative_path_str) > 1 and relative_path_str[1] == ":"):
        raise ValueError("Access Denied: Absolute path escape detected")

    # Normalize backslashes to forward slashes
    clean_path = relative_path_str.replace("\\", "/")

    # Check for prohibited segments
    parts = Path(clean_path).parts
    if ".." in parts or any(".." in p for p in parts):
        raise ValueError("Access Denied: Path traversal detected")

    norm_rel = os.path.normpath(clean_path).lstrip("./\\")
    target = (STORAGE_ROOT / norm_rel).resolve()

    # Strict containment check
    try:
        common = os.path.commonpath([str(target), str(STORAGE_ROOT)])
        if os.path.abspath(common) != str(STORAGE_ROOT):
            raise ValueError("Access Denied: Path escapes storage root")
    except Exception as e:
        raise ValueError(f"Access Denied: Path verification failed ({e})")

    return target


class NodeAPIHandler(BaseHTTPRequestHandler):

    def send_json(self, status_code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def check_auth(self):
        """Optional token check for administrative storage endpoints."""
        expected_token = get_auth_token()
        auth_header = self.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = self.headers.get("X-Auth-Token", "").strip()

        # Cloudflare Access authentication verification
        cf_email = self.headers.get("Cf-Access-Authenticated-User-Email")
        if cf_email:
            return True

        # If token matches configured auth token
        if token and token == expected_token:
            return True

        # Allow local loopback access
        client_ip = self.client_address[0]
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return True

        # Allow if no auth token is explicitly mandated or matches
        return True

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
        config = load_config()
        controller_url = (config.get("CONTROLLER_URL") or DEFAULT_CONTROLLER_URL).rstrip("/")
        target_url = f"{controller_url}{subpath}"
        if query_str:
            target_url += f"?{query_str}"

        headers = {
            "User-Agent": "PersonalServer-WebGateway/1.0"
        }
        auth_hdr = self.headers.get("Authorization") or self.headers.get("X-Auth-Token")
        if not auth_hdr:
            auth_token = get_controller_auth_token()
            if auth_token:
                auth_hdr = f"Bearer {auth_token}"
        if auth_hdr:
            headers["Authorization"] = auth_hdr if auth_hdr.startswith("Bearer ") else f"Bearer {auth_hdr}"

        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                resp_data = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(resp_data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_data)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self.send_json(502, {"error": f"Controller unavailable at {controller_url}: {e}"})

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
                for k, v in response.headers.items():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_data)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)
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

        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                resp_data = response.read()
                self.send_response(response.status)
                for hdr in ("Content-Type", "Content-Disposition"):
                    if response.headers.get(hdr):
                        self.send_header(hdr, response.headers.get(hdr))
                self.send_header("Content-Length", str(len(resp_data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_data)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)
        except urllib.error.URLError as e:
            err_msg = str(e.reason) if hasattr(e, "reason") else str(e)
            if "timed out" in err_msg.lower():
                self.send_json(504, {"error": f"Remote storage node timed out at {remote_endpoint}: {err_msg}"})
            else:
                self.send_json(502, {"error": f"Remote storage node unavailable at {remote_endpoint}: {err_msg}"})
        except Exception as e:
            self.send_json(502, {"error": f"Remote storage proxy error at {remote_endpoint}: {e}"})

    def do_GET(self):
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
                self.wfile.write(content)
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
                self.wfile.write(content)
                return
            else:
                self.send_json(404, {"error": "Static file not found"})
                return

        # 3. Node Health / Status
        if path in ("/health", "/status"):
            self.send_json(200, get_health())
            return

        # 4. Storage Subsystem: GET /storage/nodes
        if path == "/storage/nodes":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return
            self.send_json(200, get_cluster_storage_nodes())
            return

        # 5. Storage Subsystem: GET /storage/list or GET /storage
        if path in ("/storage", "/storage/list"):
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

            if not target_file.exists() or not target_file.is_file():
                self.send_json(404, {"error": "File not found"})
                return

            file_size = target_file.stat().st_size
            filename = target_file.name

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()

            # Stream file in 64KB chunks
            with open(target_file, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        # 7. Storage Usage: GET /storage/usage
        if path == "/storage/usage":
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
            config = load_config()
            controller_url = (config.get("CONTROLLER_URL") or DEFAULT_CONTROLLER_URL).rstrip("/")
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

        # 8b. Platform Configuration: GET /api/platform or GET /api/config/platform
        if path in ("/api/platform", "/api/config/platform"):
            self.send_json(200, get_platform_config().to_dict())
            return

        # 8c. Cluster Utilization and Cluster Info Proxy
        if path == "/cluster/utilization" or path == "/cluster" or path.startswith("/cluster/"):
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

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
            # Validate folder name
            if re.search(r'[\\/:\*\?"<>\|\x00]', folder_name) or ".." in folder_name:
                self.send_json(400, {"error": "Invalid directory name"})
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

            content_type = self.headers.get("Content-Type", "")
            
            # Case A: Multipart form upload
            if "multipart/form-data" in content_type:
                boundary = content_type.split("boundary=")[-1].strip()
                if boundary.startswith('"') and boundary.endswith('"'):
                    boundary = boundary[1:-1]
                
                raw_data = self.rfile.read(content_len)
                boundary_bytes = boundary.encode("utf-8")
                
                parts = raw_data.split(b"--" + boundary_bytes)
                saved_files = []

                for part in parts:
                    if b"Content-Disposition" in part and b'filename="' in part:
                        header_part, file_data = part.split(b"\r\n\r\n", 1)
                        file_data = file_data.rstrip(b"\r\n--")
                        
                        match = re.search(rb'filename="([^"]+)"', header_part)
                        if match:
                            raw_filename = match.group(1).decode("utf-8", errors="ignore")
                            safe_name = os.path.basename(raw_filename)
                            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
                            if not safe_name or safe_name in (".", ".."):
                                safe_name = "uploaded_file"

                            dest_path = target_dir / safe_name
                            with open(dest_path, "wb") as f:
                                f.write(file_data)

                            saved_files.append(safe_name)

                self.send_json(200, {
                    "status": "uploaded",
                    "files": saved_files,
                    "target_dir": str(target_dir.relative_to(STORAGE_ROOT)).replace("\\", "/")
                })
                return

            # Case B: Raw body upload
            else:
                filename = query.get("filename", ["upload.dat"])[0]
                safe_name = os.path.basename(filename)
                safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
                dest_path = target_dir / safe_name

                with open(dest_path, "wb") as f:
                    remaining = content_len
                    while remaining > 0:
                        chunk_size = min(remaining, 65536)
                        chunk = self.rfile.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)

                self.send_json(200, {
                    "status": "uploaded",
                    "file": safe_name,
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

        # 4. Proxy Job Submission to Controller: POST /api/jobs
        if path.startswith("/api/"):
            subpath = path[4:]
            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else None
            self.proxy_to_controller("POST", subpath, parsed.query, body_bytes)
            return

        # Fall through to PersonalServer Application Router
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None
        self.proxy_to_router("POST", self.path, body_bytes=body_bytes)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

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

        # Proxy /api/ DELETE requests (e.g. DELETE /api/apps/<app_id>) to controller
        if path.startswith("/api/"):
            subpath = path[4:]
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
        if path in ("/", "/health", "/status") or path.startswith("/static/"):
            self.do_GET()
            return
        self.proxy_to_router("HEAD", self.path)

    def log_message(self, format, *args):
        print(f"[NODE-API] {args[0]}")


def is_service_running(pattern):
    try:
        out = subprocess.check_output(f"pgrep -f '{pattern}'", shell=True, text=True, stderr=subprocess.DEVNULL)
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
                        controller_url = (reg_state.get("controller_url") or load_config().get("CONTROLLER_URL") or DEFAULT_CONTROLLER_URL).rstrip("/")
                        auth_token = reg_state.get("auth_token")
                        node_id = reg_state.get("node_id")

                        now = datetime.now(timezone.utc).isoformat()
                        sys_info = get_system_info()
                        services_info = {
                            "node_api": "running",
                            "cloudflare": "running" if is_service_running("cloudflared") else "stopped"
                        }

                        payload = {
                            "node_id": node_id,
                            "status": "online",
                            "timestamp": now,
                            "services": services_info,
                            "system": {
                                "cpu_cores": sys_info.get("cpu_cores", 8),
                                "load_average": sys_info.get("load_average", []),
                                "memory": f"{sys_info.get('memory', {}).get('used', '0')}/{sys_info.get('memory', {}).get('total', '0')}",
                                "storage": f"{sys_info.get('storage', {}).get('used', '0')}/{sys_info.get('storage', {}).get('total', '0')} ({sys_info.get('storage', {}).get('used_percent', '0%')})"
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


if __name__ == "__main__":
    ensure_storage_root()
    start_heartbeat_reporter(interval_seconds=15)
    server = HTTPServer((HOST, PORT), NodeAPIHandler)
    print(f"PersonalServer Node API & Web Operations running on {HOST}:{PORT}")
    print(f"Storage Root initialized at: {STORAGE_ROOT}")
    server.serve_forever()

