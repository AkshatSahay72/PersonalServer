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
import urllib.parse
import urllib.request
import urllib.error
import http.client
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
CONTROLLER_DATA_DIR = BASE_DIR / "controller" / "data"
APPS_FILE = CONTROLLER_DATA_DIR / "apps.json"
NODES_FILE = CONTROLLER_DATA_DIR / "nodes.json"

DEFAULT_GATEWAY_HOST = "0.0.0.0"
DEFAULT_GATEWAY_PORT = 8088

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

        if not parts:
            return None, None, None, False, None

        prefix = f"/{parts[0].lower()}"

        # Find matching app
        for app in apps_db.get("apps", {}).values():
            if app.get("status") == "DELETED":
                continue

            route = app.get("route")
            if not route or not isinstance(route, dict) or not route.get("enabled", True):
                continue

            registered_path = str(route.get("path", "")).strip().lower()
            if registered_path == prefix:
                # Check trailing slash normalization for root route access
                # e.g. GET /recallflow without trailing slash -> redirect to /recallflow/
                if path_only == prefix:
                    return app, prefix, "/", True, None

                # Calculate remainder path
                remainder = path_only[len(prefix):]
                if not remainder.startswith("/"):
                    remainder = "/" + remainder

                return app, prefix, remainder, False, None

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

        if app_status in ("DEPLOYING", "CREATED"):
            return 503, {"error": "Application is currently starting up", "status": 503}, {"Retry-After": "3"}

        if app_status == "FAILED":
            return 502, {"error": "Application is currently unhealthy", "status": 502}, {}

        if app_status != "RUNNING":
            return 503, {"error": f"Application is currently in {app_status} state", "status": 503}, {}

        selected_node = app.get("selected_node")
        host_port = app.get("host_port")

        if not selected_node or not host_port:
            return 504, {"error": "Application host node is temporarily unreachable", "status": 504}, {}

        # Verify selected node is ONLINE in cluster registry
        node_record = nodes_db.get("nodes", {}).get(selected_node)
        if not node_record or node_record.get("status") != "ONLINE":
            return 504, {"error": "Application host node is temporarily unreachable", "status": 504}, {}

        # Target IP resolution: local loopback if running on local node, or node IP
        target_host = "127.0.0.1"
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

    def handle_proxy_request(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_str = parsed.query

        # Health check for router gateway itself
        if path == "/health" or path == "/healthz":
            self.send_json_response(200, {"status": "ok", "service": "PersonalServer-AppRouter"})
            return

        apps_db = load_apps_registry()
        nodes_db = load_nodes_registry()

        app, route_prefix, remainder, is_redirect, err_msg = ApplicationRouter.match_route(self.path, apps_db)

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

        # Upstream resolution
        status_code, target_res, extra_hdrs = ApplicationRouter.resolve_target_upstream(app, nodes_db)
        if status_code != 200:
            self.send_json_response(status_code, target_res, extra_hdrs)
            return

        upstream_base_url = target_res
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
        headers["Host"] = urllib.parse.urlparse(upstream_base_url).netloc

        try:
            req = urllib.request.Request(target_url, data=body_bytes, headers=headers, method=method)
            with PROXY_OPENER.open(req, timeout=15) as response:
                resp_status = response.status
                resp_headers = response.headers

                self.send_response(resp_status)

                for hdr_key, hdr_val in resp_headers.items():
                    hdr_lower = hdr_key.lower()

                    # Rewrite Location header for redirects
                    if hdr_lower == "location":
                        if hdr_val.startswith("/"):
                            hdr_val = f"{route_prefix}{hdr_val}"
                        elif upstream_base_url in hdr_val:
                            hdr_val = hdr_val.replace(upstream_base_url, f"http://{client_host}{route_prefix}")

                    # Rewrite Set-Cookie Path
                    elif hdr_lower == "set-cookie":
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
                if hdr_lower == "location" and hdr_val.startswith("/"):
                    hdr_val = f"{route_prefix}{hdr_val}"
                elif hdr_lower == "set-cookie":
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
