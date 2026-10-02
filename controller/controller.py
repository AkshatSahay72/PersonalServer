#!/usr/bin/env python3
"""
PersonalServer Controller
=========================
Lightweight controller for node registration, authentication, heartbeat collection, and node state management.
"""

import sys
import os
import json
import secrets
import argparse
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = CONTROLLER_DIR / "data"

NODES_FILE = DATA_DIR / "nodes.json"
ENROLLMENT_TOKEN_FILE = SECRETS_DIR / "enrollment.token"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def get_current_iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


def ensure_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)


def get_or_create_enrollment_token():
    """Retrieve existing enrollment token or generate a secure new one."""
    ensure_directories()
    if ENROLLMENT_TOKEN_FILE.exists():
        try:
            token = ENROLLMENT_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_hex(24)
    ENROLLMENT_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(ENROLLMENT_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


def load_nodes_db():
    """Load the registered nodes database."""
    ensure_directories()
    if NODES_FILE.exists():
        try:
            with open(NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"nodes": {}}


def save_nodes_db(db):
    """Save the registered nodes database atomically."""
    ensure_directories()
    temp_file = NODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    temp_file.replace(NODES_FILE)


class ControllerHandler(BaseHTTPRequestHandler):

    def send_json(self, status_code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_auth_token(self):
        """Extract Bearer token from Authorization header or query param."""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return ""

    def read_json_body(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len == 0:
                return None
            raw = self.rfile.read(content_len).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health" or path == "/status":
            db = load_nodes_db()
            self.send_json(200, {
                "status": "online",
                "service": "PersonalServer Controller",
                "registered_nodes": len(db.get("nodes", {})),
                "timestamp": get_current_iso_timestamp()
            })
            return

        if path == "/nodes":
            db = load_nodes_db()
            safe_nodes = {}
            for nid, node in db.get("nodes", {}).items():
                node_copy = dict(node)
                node_copy.pop("auth_token", None)
                safe_nodes[nid] = node_copy
            self.send_json(200, {"nodes": safe_nodes, "count": len(safe_nodes)})
            return

        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            db = load_nodes_db()
            node = db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            node_copy = dict(node)
            node_copy.pop("auth_token", None)
            self.send_json(200, {"node": node_copy})
            return

        self.send_json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()

        if body is None:
            self.send_json(400, {"error": "Invalid or missing JSON payload"})
            return

        token = self.parse_auth_token() or body.get("enrollment_token", "") or body.get("auth_token", "")

        # ----------------------------------------------------------------------
        # 1. Node Registration: POST /register
        # ----------------------------------------------------------------------
        if path == "/register":
            enrollment_token = get_or_create_enrollment_token()
            if not token or token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Invalid enrollment token"})
                return

            node_id = body.get("node_id")
            if not node_id:
                self.send_json(400, {"error": "Missing required field 'node_id'"})
                return

            node_name = body.get("name", "unknown")
            node_role = body.get("role", "compute")
            platform = body.get("platform", "unknown")
            os_name = body.get("os", "unknown")
            arch = body.get("architecture", "unknown")
            cpu_cores = body.get("cpu_cores", 0)
            ram_mb = body.get("ram_mb", 0)
            storage_gb = body.get("storage_gb", 0)

            db = load_nodes_db()
            now = get_current_iso_timestamp()

            # Generate a dedicated auth token for subsequent heartbeats
            node_auth_token = secrets.token_hex(20)

            node_record = {
                "node_id": node_id,
                "name": node_name,
                "role": node_role,
                "platform": platform,
                "os": os_name,
                "architecture": arch,
                "cpu_cores": cpu_cores,
                "ram_mb": ram_mb,
                "storage_gb": storage_gb,
                "auth_token": node_auth_token,
                "status": "registered",
                "registered_at": now,
                "last_seen": now,
                "last_heartbeat": {}
            }

            db["nodes"][node_id] = node_record
            save_nodes_db(db)

            print(f"[CONTROLLER] Registered node: {node_id} ({node_name})")

            self.send_json(200, {
                "status": "registered",
                "node_id": node_id,
                "auth_token": node_auth_token,
                "registered_at": now,
                "message": f"Node '{node_id}' successfully registered with controller."
            })
            return

        # ----------------------------------------------------------------------
        # 2. Node Heartbeat: POST /heartbeat
        # ----------------------------------------------------------------------
        if path == "/heartbeat":
            node_id = body.get("node_id")
            if not node_id:
                self.send_json(400, {"error": "Missing required field 'node_id'"})
                return

            db = load_nodes_db()
            node_entry = db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' is not registered. Please register first."})
                return

            # Validate node auth_token (or enrollment token as fallback)
            expected_auth_token = node_entry.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()

            if not token or (token != expected_auth_token and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            now = get_current_iso_timestamp()
            node_entry["status"] = body.get("status", "online")
            node_entry["last_seen"] = now
            node_entry["last_heartbeat"] = {
                "timestamp": body.get("timestamp", now),
                "services": body.get("services", {}),
                "system": body.get("system", {})
            }

            save_nodes_db(db)
            print(f"[CONTROLLER] Heartbeat received from {node_id} at {now}")

            self.send_json(200, {
                "status": "ok",
                "ack": True,
                "node_id": node_id,
                "timestamp": now
            })
            return

        self.send_json(404, {"error": "Not Found"})

    def log_message(self, format, *args):
        # Concise logging without printing headers or tokens
        print(f"[HTTP {self.command}] {self.path} - {args[0] if args else ''}")


def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT):
    ensure_directories()
    token = get_or_create_enrollment_token()
    print("==========================================")
    print(" PersonalServer Controller")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print("Ready to accept node registrations and heartbeats.")
    print("==========================================")

    server = HTTPServer((host, port), ControllerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
        server.server_close()


def list_nodes():
    db = load_nodes_db()
    nodes = db.get("nodes", {})
    print("==========================================")
    print(f" Registered Nodes ({len(nodes)})")
    print("==========================================")
    if not nodes:
        print("No registered nodes.")
    else:
        for nid, node in nodes.items():
            print(f"Node ID:    {nid}")
            print(f"  Name:     {node.get('name')}")
            print(f"  Role:     {node.get('role')}")
            print(f"  Platform: {node.get('platform')} ({node.get('architecture')})")
            print(f"  Status:   {node.get('status')}")
            print(f"  Last Seen:{node.get('last_seen')}")
            print(f"  Services: {node.get('last_heartbeat', {}).get('services', {})}")
            print()
    print("==========================================")


def main():
    parser = argparse.ArgumentParser(
        prog="controller",
        description="PersonalServer Controller"
    )
    subparsers = parser.add_subparsers(dest="command", help="Controller commands")

    # start command
    p_start = subparsers.add_parser("start", help="Start the controller HTTP service")
    p_start.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")

    # nodes command
    subparsers.add_parser("nodes", help="List registered nodes")

    # token command
    subparsers.add_parser("token", help="Display enrollment token file path")

    args = parser.parse_args()

    if not args.command or args.command == "start":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        start_server(host, port)
    elif args.command == "nodes":
        list_nodes()
    elif args.command == "token":
        token = get_or_create_enrollment_token()
        print(f"Enrollment token stored at: {ENROLLMENT_TOKEN_FILE}")
        # Note: Do not display token contents in general output unless necessary


if __name__ == "__main__":
    main()
