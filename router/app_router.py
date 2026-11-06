#!/usr/bin/env python3
"""
PersonalServer Application Gateway & Dynamic Path Router
========================================================
Data-plane reverse proxy for containerized application traffic.
Routes public path-based URLs (/app-name/*) dynamically to the
appropriate host node and container port.

Security & Architecture:
- Strict registry-only routing (SSRF protection; no arbitrary upstreams)
- Path traversal normalization & sanitization
- Automatic prefix stripping and reverse-proxy header injection
- Response rewriting (Location redirect headers, Cookie paths)
- Multi-node dynamic resolution from controller registry
"""

import sys
import os
import re
import json
import time
import socket
import select
import shutil
import mimetypes
import urllib.parse
import urllib.request
import urllib.error
import http.client
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from config.platform_config import get_platform_config, get_controller_paths
from config.ha_manager import LeaderLease, resolve_controller_node_id
PATHS = get_controller_paths()
CONTROLLER_DATA_DIR = PATHS.data_dir
APPS_FILE = PATHS.apps_file
NODES_FILE = PATHS.nodes_file
RUNTIME_DIR = PATHS.runtime_dir

DEFAULT_GATEWAY_HOST = "0.0.0.0"
DEFAULT_GATEWAY_PORT = 8088
DEFAULT_PROXY_TIMEOUT = 90

SENSITIVE_HEADERS_TO_STRIP = {
    "authorization",
    "x-auth-token",
    "x-onboarding-code",
    "cf-access-authenticated-user-email",
    "cf-access-jwt-assertion",
    "cf-access-user",
    "cookie-secret"
}

RESERVED_SYSTEM_ROUTES = {
    "/", "/api", "/static", "/storage", "/health", "/status",
    "/cluster", "/nodes", "/jobs", "/admin", "/login", "/ws", "/apps"
}

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}


def guess_static_mime(path_or_filename: str) -> str:
    ext = os.path.splitext(str(path_or_filename).lower())[1]
    if ext in MIME_MAP:
        return MIME_MAP[ext]
    guessed, _ = mimetypes.guess_type(path_or_filename)
    return guessed or "application/octet-stream"


class ProxyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevents proxy from auto-following upstream redirects so they can be rewritten for client."""
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


def load_apps_registry():
    if APPS_FILE.exists():
        try:
            with open(APPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"apps": {}}


def load_nodes_registry():
    if NODES_FILE.exists():
        try:
            with open(NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"nodes": {}}


def resolve_leader_gateway(nodes_db=None):
    """
    Determines if this node is leader, follower, or if no valid leader exists.
    Returns: (is_local_leader: bool, leader_gateway_url: Optional[str], leader_state: str)
    - ("LOCAL_LEADER"): active leader or standalone mode.
    - ("FOLLOWER"): follower node, leader_gateway_url points to leader's gateway.
    - ("NO_VALID_LEADER"): HA cluster active but lease expired or leader offline.
    """
    paths = get_controller_paths()
    lease_file = getattr(paths, "leader_lease_file", paths.data_dir / "leader_lease.json")
    local_id = resolve_controller_node_id(paths=paths)

    if not lease_file.exists():
        return True, None, "LOCAL_LEADER"

    lease = LeaderLease.load_from_file(lease_file)
    if not lease.leader_id or lease.term == 0:
        return True, None, "LOCAL_LEADER"

    if not lease.is_valid():
        return False, None, "NO_VALID_LEADER"

    if lease.leader_id == local_id:
        return True, None, "LOCAL_LEADER"

    # Follower: resolve leader private network address
    if nodes_db is None:
        nodes_db = load_nodes_registry()

    leader_node = nodes_db.get("nodes", {}).get(lease.leader_id, {})
    leader_ip = leader_node.get("tailscale_ip") or leader_node.get("ip") or leader_node.get("host")
    if not leader_ip and lease.leader_url:
        try:
            parsed_leader = urllib.parse.urlparse(lease.leader_url)
            leader_ip = parsed_leader.hostname
        except Exception:
            pass

    if not leader_ip or leader_ip in ("127.0.0.1", "localhost", "0.0.0.0"):
        return False, None, "NO_VALID_LEADER"

    leader_gateway_url = f"http://{leader_ip}:{DEFAULT_GATEWAY_PORT}"
    return False, leader_gateway_url, "FOLLOWER"


class ApplicationRouter:
    """Core routing logic decoupled for testing and execution."""

    @staticmethod
    def sanitize_and_validate_path(raw_path: str):
        """
        Validates path against directory traversal, encoded traversal, and null bytes.
        Returns normalized unquoted path or raises ValueError.
        """
        if not raw_path or not isinstance(raw_path, str):
            raise ValueError("Empty or invalid request path")

        # Check for null bytes or backslashes
        if "\x00" in raw_path or "\\" in raw_path:
            raise ValueError("Malformed characters in path")

        # Decode URL-encoded segments safely
        decoded = urllib.parse.unquote(raw_path)
        if "\x00" in decoded or "\\" in decoded:
            raise ValueError("Malformed characters in decoded path")

        # Reject path traversal tokens
        segments = decoded.split("/")
        for seg in segments:
            if seg in ("..", "."):
                raise ValueError("Path traversal attempt detected")

        return decoded

    @staticmethod
    def match_route(raw_path: str, apps_db: dict):
        """
        Matches incoming request path against registered active application routes.
        Returns tuple: (app, matched_route_path, remainder_path, is_exact_no_slash_redirect)
        """
        try:
            clean_path = ApplicationRouter.sanitize_and_validate_path(raw_path)
        except ValueError as e:
            return None, None, None, False, str(e)

        # Parse root path segment: e.g. /recallflow/api/items -> prefix = /recallflow
        parsed = urllib.parse.urlparse(raw_path)
        path_only = parsed.path
        parts = [p for p in path_only.split("/") if p]

        # 1. Match sub-applications by longest matching prefix
        sub_apps = []
        for app in apps_db.get("apps", {}).values():
            if app.get("status") in ("DELETED", "deleted"):
                continue
            reg_path = str(app.get("public_path") or app.get("route", {}).get("path") or f"/{app.get('name', '')}").strip()
            if reg_path and reg_path != "/":
                if not reg_path.startswith("/"):
                    reg_path = "/" + reg_path
                sub_apps.append((reg_path.rstrip("/"), app))

            # Support standard /apps/<name> and /apps/<id> namespace routes as well as direct /<name>
            app_name = str(app.get("name") or "").strip().lower()
            if app_name:
                apps_alias = f"/apps/{app_name}"
                if apps_alias != reg_path.rstrip("/").lower():
                    sub_apps.append((apps_alias, app))
                name_alias = f"/{app_name}"
                if name_alias not in RESERVED_SYSTEM_ROUTES and name_alias != reg_path.rstrip("/").lower():
                    sub_apps.append((name_alias, app))
            app_id = str(app.get("app_id") or app.get("id") or "").strip().lower()
            if app_id and app_id != app_name:
                app_id_alias = f"/apps/{app_id}"
                if app_id_alias != reg_path.rstrip("/").lower():
                    sub_apps.append((app_id_alias, app))
                id_alias = f"/{app_id}"
                if id_alias not in RESERVED_SYSTEM_ROUTES and id_alias != reg_path.rstrip("/").lower():
                    sub_apps.append((id_alias, app))

        # Sort by length of registered path descending for longest-prefix match
        sub_apps.sort(key=lambda x: len(x[0]), reverse=True)

        path_only_lower = path_only.lower()
        for reg_path_clean, app in sub_apps:
            reg_lower = reg_path_clean.lower()
            if path_only_lower == reg_lower or path_only_lower.startswith(reg_lower + "/"):
                if path_only_lower == reg_lower:
                    # Missing trailing slash -> redirect to /reg_path/
                    return app, reg_path_clean, "/", True, None

                remainder = path_only[len(reg_path_clean):]
                if not remainder.startswith("/"):
                    remainder = "/" + remainder

                # Check component routing if present
                components = app.get("components")
                if components and isinstance(components, dict):
                    # 1. API route precedence: check backend components first
                    for comp_name, comp in components.items():
                        c_type = str(comp.get("type", "")).lower()
                        c_routing = comp.get("routing", {})
                        if c_type == "backend" and c_routing.get("api_priority", True):
                            api_prefix = str(c_routing.get("path_prefix", "/api")).strip().lower()
                            if not api_prefix.startswith("/"):
                                api_prefix = "/" + api_prefix
                            rem_lower = remainder.lower()
                            if rem_lower == api_prefix or rem_lower.startswith(api_prefix + "/"):
                                matched_app = dict(app)
                                matched_app["_active_component"] = comp
                                matched_app["_is_api"] = True
                                return matched_app, reg_path_clean, remainder, False, None

                    # 2. Frontend route evaluation
                    for comp_name, comp in components.items():
                        c_type = str(comp.get("type", "")).lower()
                        if c_type == "frontend":
                            matched_app = dict(app)
                            matched_app["_active_component"] = comp
                            matched_app["_is_api"] = False
                            return matched_app, reg_path_clean, remainder, False, None

                return app, reg_path_clean, remainder, False, None

        # 2. Application namespace boundary: requests under /apps/ must not fall through to root
        if path_only_lower == "/apps" or path_only_lower.startswith("/apps/"):
            return None, None, None, False, None

        # 3. Root application fallback (longest-prefix: serves '/' and all unreserved top-level paths)
        for app in apps_db.get("apps", {}).values():
            if app.get("status") in ("DELETED", "deleted"):
                continue
            reg_path = str(app.get("public_path") or app.get("route", {}).get("path", "")).strip()
            if reg_path == "/":
                components = app.get("components")
                if components and isinstance(components, dict):
                    # Check backend components if path starts with API prefix
                    for comp_name, comp in components.items():
                        c_type = str(comp.get("type", "")).lower()
                        c_routing = comp.get("routing", {})
                        if c_type == "backend":
                            api_prefix = str(c_routing.get("path_prefix", "/api")).strip().lower()
                            if path_only.lower() == api_prefix or path_only.lower().startswith(api_prefix + "/"):
                                matched_app = dict(app)
                                matched_app["_active_component"] = comp
                                matched_app["_is_api"] = True
                                return matched_app, "/", path_only, False, None

                    # Check frontend/web component
                    for comp_name, comp in components.items():
                        c_type = str(comp.get("type", "")).lower()
                        if c_type in ("frontend", "web"):
                            matched_app = dict(app)
                            matched_app["_active_component"] = comp
                            matched_app["_is_api"] = False
                            return matched_app, "/", path_only, False, None

                # Legacy single container root app
                return app, "/", path_only, False, None

        return None, None, None, False, None

    @staticmethod
    def resolve_target_upstream(app: dict, nodes_db: dict):
        """
        Resolves upstream endpoint dynamically from application state and node registry.
        Returns (status_code, target_base_url_or_error_dict, extra_headers)
        """
        app_status = app.get("status", "UNKNOWN")

        if app_status == "STOPPED":
            return 503, {"error": "Application is currently stopped", "status": 503}, {}

        serving_prior_active = bool(
            app_status in ("DEPLOYING", "CREATED") and
            app.get("active_deployment_id") and
            app.get("selected_node") and
            (app.get("host_port") is not None or app.get("port") is not None)
        )

        if app_status in ("DEPLOYING", "CREATED"):
            # Zero-downtime protection: If an active healthy deployment is already running, continue serving it!
            if not serving_prior_active:
                return 503, {"error": "Application is currently starting up", "status": 503}, {"Retry-After": "3"}

        if app_status == "FAILED":
            return 502, {"error": "Application is currently unhealthy", "status": 502}, {}

        if app_status not in ("RUNNING", "active", "ACTIVE") and not serving_prior_active:
            return 503, {"error": f"Application is currently in {app_status} state", "status": 503}, {}

        # If component specified with external/direct destination URL
        active_comp = app.get("_active_component")
        if active_comp:
            dest = str(active_comp.get("destination") or "").strip().rstrip("/")
            if dest.startswith("http://") or dest.startswith("https://"):
                return 200, dest, {}

        selected_node = app.get("selected_node")
        is_static = str(app.get("runtime", "")).lower() in ("static", "static-web", "html", "web")
        raw_port = app.get("host_port") if app.get("host_port") is not None else app.get("port")
        if raw_port is None and is_static:
            raw_port = DEFAULT_GATEWAY_PORT

        if raw_port is None:
            return 504, {"error": "Application host port is missing or unassigned", "status": 504}, {}

        try:
            host_port = int(raw_port)
            if not (1 <= host_port <= 65535):
                raise ValueError("Port out of range")
        except (TypeError, ValueError):
            return 504, {"error": f"Application host port '{raw_port}' is invalid", "status": 504}, {}

        if not selected_node:
            return 504, {"error": "Application host node is temporarily unreachable", "status": 504}, {}

        # Verify selected node is ONLINE in cluster registry
        node_record = nodes_db.get("nodes", {}).get(selected_node)
        if not node_record or str(node_record.get("status", "")).upper() != "ONLINE":
            return 504, {"error": "Application host node is temporarily unreachable", "status": 504}, {}

        # Target IP resolution: local loopback if running on local node, or node IP / Tailscale IP
        local_node_id = None
        try:
            reg_f = RUNTIME_DIR / "registration.json"
            if reg_f.exists():
                with open(reg_f, "r", encoding="utf-8") as f:
                    local_node_id = json.load(f).get("node_id")
        except Exception:
            pass

        target_host = "127.0.0.1"
        if selected_node != local_node_id:
            node_ip = node_record.get("ip") or node_record.get("tailscale_ip") or node_record.get("host")
            if node_ip and node_ip not in ("127.0.0.1", "localhost", "0.0.0.0"):
                target_host = node_ip

        return 200, f"http://{target_host}:{host_port}", {}


class RouterHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json_response(self, status_code, data, extra_headers=None):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        apps_db = load_apps_registry()
        app, route_prefix, remainder, is_redirect, err_msg = ApplicationRouter.match_route(self.path, apps_db)
        if app:
            self.handle_proxy_request("OPTIONS")
        else:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD")
            self.end_headers()

    def do_GET(self):
        self.handle_proxy_request("GET")

    def do_POST(self):
        self.handle_proxy_request("POST")

    def do_PUT(self):
        self.handle_proxy_request("PUT")

    def do_DELETE(self):
        self.handle_proxy_request("DELETE")

    def do_PATCH(self):
        self.handle_proxy_request("PATCH")

    def do_HEAD(self):
        self.handle_proxy_request("HEAD")

    def forward_to_leader(self, leader_gateway_url: str, method: str):
        target_url = f"{leader_gateway_url.rstrip('/')}{self.path}"

        proxy_hops_str = self.headers.get("X-PersonalServer-Proxy-Hops", "0")
        try:
            proxy_hops = int(proxy_hops_str)
        except ValueError:
            proxy_hops = 0
        if proxy_hops >= 3:
            self.send_json_response(508, {
                "error": "Proxy loop detected: request traversed multiple router hops",
                "status": 508,
                "proxy_hops": proxy_hops
            })
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None

        headers = {}
        for k, v in self.headers.items():
            if k.lower() in SENSITIVE_HEADERS_TO_STRIP:
                continue
            headers[k] = v

        client_host = self.headers.get("Host", "localhost")
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        existing_fwd = self.headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = f"{existing_fwd}, {client_ip}" if existing_fwd else client_ip
        headers["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "http")
        headers["X-Forwarded-Host"] = client_host
        headers["X-PersonalServer-Routed-By"] = "follower-gateway"
        headers["X-PersonalServer-Proxy-Hops"] = str(proxy_hops + 1)
        headers["Host"] = client_host

        req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
        try:
            with PROXY_OPENER.open(req, timeout=DEFAULT_PROXY_TIMEOUT) as upstream_resp:
                resp_bytes = upstream_resp.read()
                self.send_response(upstream_resp.status)
                for header_key, header_val in upstream_resp.headers.items():
                    if header_key.lower() in ("transfer-encoding", "content-length", "connection"):
                        continue
                    self.send_header(header_key, header_val)
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                try:
                    self.wfile.write(resp_bytes)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                    self.close_connection = True
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            for header_key, header_val in e.headers.items():
                if header_key.lower() in ("transfer-encoding", "content-length", "connection"):
                    continue
                self.send_header(header_key, header_val)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            try:
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                self.close_connection = True
        except Exception as e:
            self.send_json_response(503, {
                "error": "Failed to reach cluster leader via private network",
                "status": 503,
                "leader_url": leader_gateway_url,
                "detail": str(e)
            }, extra_headers={"Retry-After": "5"})

    def serve_static_app_file(self, app: dict, route_prefix: str, remainder: str, method: str):
        """
        Safely serves static assets for a native static application from local storage.
        Guards against directory traversal, prevents directory listing, handles SPA fallback,
        and sets appropriate Content-Type, Content-Length, and caching headers.
        """
        app_id = app.get("app_id") or app.get("id") or app.get("name")
        active_dep_id = app.get("active_deployment_id")
        if not active_dep_id:
            deps = app.get("deployments", [])
            for d in reversed(deps):
                if d.get("status") in ("SUCCESS", "RUNNING"):
                    active_dep_id = d.get("deployment_id")
                    break

        if not active_dep_id:
            self.send_json_response(503, {"error": "No active release available for static application", "status": 503})
            return

        release_dir = (BASE_DIR / "storage" / "applications" / app_id / "releases" / active_dep_id).resolve()
        if not release_dir.exists():
            self.send_json_response(503, {"error": f"Release directory not found on host worker: {active_dep_id}", "status": 503})
            return

        # Determine doc_root
        static_root_val = app.get("static_root")
        if static_root_val is None:
            meta_f = release_dir / "release_metadata.json"
            if meta_f.exists():
                try:
                    with open(meta_f, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                        static_root_val = meta_data.get("static_root", "")
                except Exception:
                    pass

        doc_root = (release_dir / static_root_val).resolve() if static_root_val else release_dir

        # Fallback search if doc_root doesn't exist
        if not doc_root.exists():
            for cand in ("dist", "build", "public", "."):
                cand_dir = (release_dir / cand).resolve()
                if cand_dir.is_dir() and (cand_dir / "index.html").is_file():
                    doc_root = cand_dir
                    break

        # Synthetic health check support for /health
        clean_rem = remainder.split("?")[0].strip()
        if clean_rem in ("/health", "/health/"):
            health_f = doc_root / "health"
            health_html = doc_root / "health.html"
            if not health_f.is_file() and not health_html.is_file():
                local_id = resolve_controller_node_id(paths=get_controller_paths())
                self.send_json_response(200, {
                    "status": "healthy",
                    "app": app_id,
                    "runtime": "static",
                    "worker_node": app.get("selected_node") or local_id,
                    "active_deployment_id": active_dep_id
                })
                return

        # Unquote URL path and normalize
        unquoted = urllib.parse.unquote(clean_rem).lstrip("/")
        if unquoted == "":
            rel_target = "index.html"
        else:
            rel_target = unquoted

        # Path traversal guard
        try:
            target_file = (doc_root / rel_target).resolve()
            target_file.relative_to(doc_root)
        except (ValueError, RuntimeError):
            self.send_json_response(403, {"error": "Access forbidden: path traversal detected", "status": 403})
            return

        # If target is a directory, look for index.html inside it
        if target_file.is_dir():
            index_candidate = target_file / "index.html"
            if index_candidate.is_file():
                target_file = index_candidate
            else:
                self.send_json_response(404, {"error": "Directory listing forbidden", "status": 404})
                return

        # Check if file exists
        if not target_file.is_file():
            spa_fallback = app.get("spa_fallback")
            if spa_fallback is None:
                meta_f = release_dir / "release_metadata.json"
                if meta_f.exists():
                    try:
                        with open(meta_f, "r", encoding="utf-8") as f:
                            spa_fallback = json.load(f).get("spa_fallback", False)
                    except Exception:
                        pass

            if bool(spa_fallback):
                index_p = doc_root / "index.html"
                if index_p.is_file():
                    target_file = index_p
                else:
                    self.send_json_response(404, {"error": "File not found and index.html missing for SPA fallback", "status": 404})
                    return
            else:
                self.send_json_response(404, {"error": f"File '{unquoted}' not found", "status": 404})
                return

        try:
            file_size = target_file.stat().st_size
            mime_type = guess_static_mime(str(target_file))
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Access-Control-Allow-Origin", "*")
            if "html" in mime_type:
                self.send_header("Cache-Control", "no-cache, must-revalidate")
            else:
                self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()

            if method != "HEAD":
                with open(target_file, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True
        except Exception as e:
            self.send_json_response(500, {"error": f"Failed to read static file: {e}", "status": 500})

    def handle_proxy_request(self, method: str):
        proxy_hops_str = self.headers.get("X-PersonalServer-Proxy-Hops", "0")
        try:
            proxy_hops = int(proxy_hops_str)
        except ValueError:
            proxy_hops = 0
        if proxy_hops >= 3:
            self.send_json_response(508, {
                "error": "Proxy loop detected: request traversed multiple router hops",
                "status": 508,
                "proxy_hops": proxy_hops
            })
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_str = parsed.query

        # Health check for router gateway itself
        if path == "/health" or path == "/healthz":
            self.send_json_response(200, {"status": "ok", "service": "PersonalServer-AppRouter"})
            return

        nodes_db = load_nodes_registry()
        is_local_leader, leader_gateway_url, leader_state = resolve_leader_gateway(nodes_db=nodes_db)
        apps_db = load_apps_registry()
        app, route_prefix, remainder, is_redirect, err_msg = ApplicationRouter.match_route(self.path, apps_db)

        # Resolve local node ID to check for local static workloads
        local_node_id = None
        try:
            reg_f = RUNTIME_DIR / "registration.json"
            if reg_f.exists():
                with open(reg_f, "r", encoding="utf-8") as f:
                    local_node_id = json.load(f).get("node_id")
        except Exception:
            pass

        is_static_app = bool(app and str(app.get("runtime", "")).lower() in ("static", "static-web", "html", "web"))
        is_local_static = bool(is_static_app and (app.get("selected_node") in (local_node_id, None, "127.0.0.1", "localhost")))

        if is_redirect:
            # Trailing slash redirect: /recallflow -> /recallflow/
            redirect_url = f"{route_prefix}/"
            if query_str:
                redirect_url += f"?{query_str}"
            self.send_response(308)
            self.send_header("Location", redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if is_local_static:
            self.serve_static_app_file(app, route_prefix, remainder, method)
            return

        if leader_state == "NO_VALID_LEADER":
            self.send_json_response(503, {
                "error": "Cluster leader temporarily unavailable",
                "status": 503,
                "leader_state": "unavailable",
                "message": "No active leader lease currently exists across the cluster."
            }, extra_headers={"Retry-After": "5"})
            return

        if leader_state == "FOLLOWER" and leader_gateway_url and not self.headers.get("X-PersonalServer-Routed-By"):
            self.forward_to_leader(leader_gateway_url, method)
            return

        if err_msg:
            self.send_json_response(400, {"error": "Invalid request path", "status": 400})
            return

        if is_redirect:
            # Trailing slash redirect: /recallflow -> /recallflow/
            redirect_url = f"{route_prefix}/"
            if query_str:
                redirect_url += f"?{query_str}"
            self.send_response(308)
            self.send_header("Location", redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not app:
            self.send_json_response(404, {"error": "Application route not found", "status": 404})
            return

        # Direct application/route redirect support (e.g. root domain portfolio redirect)
        redirect_target = app.get("redirect") or (app.get("route", {}) if isinstance(app.get("route"), dict) else {}).get("redirect")
        if redirect_target:
            target_url = f"{str(redirect_target).rstrip('/')}{path}"
            if query_str:
                target_url += f"?{query_str}"
            self.send_response(307)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Handle static web applications
        if is_static_app:
            if is_local_static:
                self.serve_static_app_file(app, route_prefix, remainder, method)
                return

            # Static app is hosted on a remote worker node -> proxy to worker router
            selected_node = app.get("selected_node")
            node_record = nodes_db.get("nodes", {}).get(selected_node)
            if not node_record or str(node_record.get("status", "")).upper() != "ONLINE":
                self.send_json_response(504, {
                    "error": "Application host worker node is temporarily unreachable",
                    "status": 504
                })
                return

            worker_ip = node_record.get("ip") or node_record.get("tailscale_ip") or node_record.get("host")
            if not worker_ip:
                self.send_json_response(504, {
                    "error": "Application host worker node IP unresolved",
                    "status": 504
                })
                return

            upstream_base_url = f"http://{worker_ip}:{DEFAULT_GATEWAY_PORT}"
            target_url = f"{upstream_base_url}{self.path}"

            headers = {}
            for k, v in self.headers.items():
                if k.lower() in SENSITIVE_HEADERS_TO_STRIP:
                    continue
                headers[k] = v
            client_host = self.headers.get("Host", "localhost")
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            existing_fwd = self.headers.get("X-Forwarded-For")
            headers["X-Forwarded-For"] = f"{existing_fwd}, {client_ip}" if existing_fwd else client_ip
            headers["X-Forwarded-Proto"] = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
            headers["X-Forwarded-Host"] = client_host
            headers["X-Forwarded-Prefix"] = route_prefix
            headers["X-PersonalServer-Routed-By"] = "router-gateway"
            headers["X-PersonalServer-Proxy-Hops"] = str(proxy_hops + 1)
            headers["Host"] = f"{worker_ip}:{DEFAULT_GATEWAY_PORT}"

            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else None

            try:
                req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
                with PROXY_OPENER.open(req, timeout=DEFAULT_PROXY_TIMEOUT) as response:
                    self.send_response(response.status)
                    for hdr_key, hdr_val in response.headers.items():
                        if hdr_key.lower() in ("transfer-encoding", "connection"):
                            continue
                        self.send_header(hdr_key, hdr_val)
                    self.end_headers()
                    shutil.copyfileobj(response, self.wfile, length=64 * 1024)
            except urllib.error.HTTPError as e:
                err_body = e.read()
                self.send_response(e.code)
                for hdr_key, hdr_val in e.headers.items():
                    if hdr_key.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(hdr_key, hdr_val)
                self.end_headers()
                self.wfile.write(err_body)
            except Exception as e:
                self.send_json_response(504, {
                    "error": f"Failed to proxy static request to worker node: {e}",
                    "status": 504
                })
            return

        # Upstream resolution
        status_code, target_res, extra_hdrs = ApplicationRouter.resolve_target_upstream(app, nodes_db)
        if status_code != 200:
            self.send_json_response(status_code, target_res, extra_hdrs)
            return

        upstream_base_url = target_res
        active_comp = app.get("_active_component")
        if active_comp:
            c_routing = active_comp.get("routing", {})
            strip_mode = c_routing.get("strip_mode", "strip_app")
            if strip_mode == "strip_app":
                forwarded_path = remainder
            elif strip_mode == "strip_all":
                api_pfx = str(c_routing.get("path_prefix", "/api")).strip().lower()
                rem_lower = remainder.lower()
                if rem_lower.startswith(api_pfx):
                    forwarded_path = remainder[len(api_pfx):]
                else:
                    forwarded_path = remainder
                if not forwarded_path.startswith("/"):
                    forwarded_path = "/" + forwarded_path
            else:  # preserve
                forwarded_path = path
        else:
            route_meta = app.get("route", {})
            strip_prefix = route_meta.get("strip_prefix", True)
            forwarded_path = remainder if strip_prefix else path

        if query_str:
            forwarded_path += f"?{query_str}"

        target_url = f"{upstream_base_url}{forwarded_path}"

        # Read body if present
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else None

        # Prepare forwarded headers (SSRF / Credential isolation)
        headers = {}
        client_host = self.headers.get("Host", "localhost")
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"

        for k, v in self.headers.items():
            if k.lower() in SENSITIVE_HEADERS_TO_STRIP:
                continue
            headers[k] = v

        # Standard reverse-proxy headers
        existing_fwd = self.headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = f"{existing_fwd}, {client_ip}" if existing_fwd else client_ip
        headers["X-Forwarded-Proto"] = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        headers["X-Forwarded-Host"] = client_host
        headers["X-Forwarded-Prefix"] = route_prefix
        headers["X-PersonalServer-Proxy-Hops"] = str(proxy_hops + 1)
        headers["Host"] = urllib.parse.urlparse(upstream_base_url).netloc

        try:
            req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
            with PROXY_OPENER.open(req, timeout=DEFAULT_PROXY_TIMEOUT) as response:
                resp_status = response.status
                resp_headers = response.headers

                self.send_response(resp_status)

                for hdr_key, hdr_val in resp_headers.items():
                    hdr_lower = hdr_key.lower()

                    # Rewrite Location header for redirects
                    if hdr_lower == "location":
                        client_proto = self.headers.get("X-Forwarded-Proto") or get_platform_config().app_scheme
                        if route_prefix == "/":
                            pass  # Root application already has root-relative location
                        elif hdr_val.startswith("/"):
                            hdr_val = f"{route_prefix}{hdr_val}"
                        elif upstream_base_url in hdr_val:
                            hdr_val = hdr_val.replace(upstream_base_url, f"{client_proto}://{client_host}{route_prefix}")

                    # Rewrite Set-Cookie Path
                    elif hdr_lower == "set-cookie":
                        if route_prefix != "/":
                            hdr_val = re.sub(r'Path=\/([^;]*)', f'Path={route_prefix}\\1', hdr_val, flags=re.IGNORECASE)

                    self.send_header(hdr_key, hdr_val)

                self.end_headers()

                # Stream response body
                shutil.copyfileobj(response, self.wfile, length=64 * 1024)

        except urllib.error.HTTPError as e:
            # Upstream HTTP error response (e.g. 404/500 from inside container)
            err_body = e.read()
            self.send_response(e.code)
            for hdr_key, hdr_val in e.headers.items():
                hdr_lower = hdr_key.lower()
                client_proto = self.headers.get("X-Forwarded-Proto") or get_platform_config().app_scheme
                if hdr_lower == "location":
                    if route_prefix == "/":
                        pass
                    elif hdr_val.startswith("/"):
                        hdr_val = f"{route_prefix}{hdr_val}"
                    elif upstream_base_url in hdr_val:
                        hdr_val = hdr_val.replace(upstream_base_url, f"{client_proto}://{client_host}{route_prefix}")
                elif hdr_lower == "set-cookie":
                    if route_prefix != "/":
                        hdr_val = re.sub(r'Path=\/([^;]*)', f'Path={route_prefix}\\1', hdr_val, flags=re.IGNORECASE)
                self.send_header(hdr_key, hdr_val)
            self.end_headers()
            self.wfile.write(err_body)

        except (urllib.error.URLError, ConnectionRefusedError, socket.timeout) as e:
            self.send_json_response(504, {
                "error": "Application host node is temporarily unreachable",
                "status": 504
            })
        except Exception as e:
            self.send_json_response(502, {
                "error": "Application is currently unhealthy",
                "status": 502
            })

    def log_message(self, format, *args):
        # Clean stdout logger for gateway
        print(f"[APP-ROUTER] {self.command} {self.path} - {args[0] if args else ''}")


class PersonalServerRouter:
    """Encapsulates the HTTP gateway daemon process/thread."""

    def __init__(self, host=DEFAULT_GATEWAY_HOST, port=DEFAULT_GATEWAY_PORT):
        self.host = host
        self.port = port
        self.server = None
        self.thread = None

    def start(self, blocking=False):
        self.server = ThreadingHTTPServer((self.host, self.port), RouterHTTPHandler)
        self.port = self.server.server_address[1]
        print(f"[PersonalServer App Router] Gateway listening on http://{self.host}:{self.port}")
        if blocking:
            self.server.serve_forever()
        else:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        return self.port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            print("[PersonalServer App Router] Gateway stopped.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PersonalServer Application Gateway & Path Router")
    parser.add_argument("--host", default=DEFAULT_GATEWAY_HOST, help=f"Host to bind (default: {DEFAULT_GATEWAY_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT, help=f"Port to bind (default: {DEFAULT_GATEWAY_PORT})")
    args = parser.parse_args()

    router = PersonalServerRouter(host=args.host, port=args.port)
    try:
        router.start(blocking=True)
    except KeyboardInterrupt:
        router.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
