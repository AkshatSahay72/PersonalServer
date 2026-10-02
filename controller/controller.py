#!/usr/bin/env python3
"""
PersonalServer Controller
=========================
Cluster controller for multi-node inventory, authenticated registration,
heartbeat liveness monitoring, node removal, and cluster state management.
"""

import sys
import os
import json
import secrets
import argparse
import shutil
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = CONTROLLER_DIR / "data"

NODES_FILE = DATA_DIR / "nodes.json"
BACKUP_NODES_FILE = DATA_DIR / "nodes.json.bak"
ENROLLMENT_TOKEN_FILE = SECRETS_DIR / "enrollment.token"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds

# Centralized Node State Constants
STATE_REGISTERING = "REGISTERING"
STATE_ONLINE = "ONLINE"
STATE_OFFLINE = "OFFLINE"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_UNKNOWN = "UNKNOWN"
STATE_REMOVED = "REMOVED"

VALID_ROLES = ["compute", "storage", "gateway", "controller", "hybrid"]


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
    """Load the registered nodes database with backup rollback fallback."""
    ensure_directories()
    if NODES_FILE.exists():
        try:
            with open(NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {NODES_FILE} ({e}). Attempting rollback to backup...", file=sys.stderr)
            if BACKUP_NODES_FILE.exists():
                try:
                    with open(BACKUP_NODES_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"cluster_name": "PersonalServer", "nodes": {}}


def save_nodes_db(db):
    """Save the registered nodes database atomically with automatic backup safeguard."""
    ensure_directories()
    temp_file = NODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    # Maintain a backup of the current database before replacing
    if NODES_FILE.exists():
        try:
            shutil.copy2(NODES_FILE, BACKUP_NODES_FILE)
        except Exception:
            pass

    temp_file.replace(NODES_FILE)


def compute_node_liveness(node, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Evaluate live status based on heartbeat timestamp and service state."""
    raw_status = node.get("status", STATE_UNKNOWN)
    if raw_status == STATE_REMOVED:
        return STATE_REMOVED

    last_seen_str = node.get("last_seen")
    if not last_seen_str:
        return STATE_UNKNOWN

    try:
        last_seen_dt = datetime.fromisoformat(last_seen_str)
        now = datetime.now(timezone.utc)
        delta_seconds = (now - last_seen_dt).total_seconds()

        if delta_seconds > timeout_seconds:
            return STATE_OFFLINE

        # Check services state in last heartbeat
        last_hb = node.get("last_heartbeat", {})
        services = last_hb.get("services", {})
        if services and any(v in ("unresponsive", "degraded", "stopped") for v in services.values()):
            return STATE_UNHEALTHY

        return STATE_ONLINE
    except Exception:
        return STATE_UNKNOWN


def sanitize_node_record(node, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Create a sanitized copy of node record without credentials, with computed live status."""
    node_copy = dict(node)
    node_copy.pop("auth_token", None)
    node_copy["status"] = compute_node_liveness(node, timeout_seconds)
    return node_copy


def get_cluster_summary(db, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Aggregate cluster statistics."""
    nodes = db.get("nodes", {})
    online_count = 0
    offline_count = 0
    unhealthy_count = 0
    removed_count = 0
    active_count = 0

    sanitized_nodes = []
    for nid, node in nodes.items():
        s_node = sanitize_node_record(node, timeout_seconds)
        st = s_node["status"]
        if st == STATE_ONLINE:
            online_count += 1
            active_count += 1
        elif st == STATE_OFFLINE:
            offline_count += 1
            active_count += 1
        elif st == STATE_UNHEALTHY:
            unhealthy_count += 1
            active_count += 1
        elif st == STATE_REMOVED:
            removed_count += 1

        sanitized_nodes.append(s_node)

    return {
        "cluster": {
            "name": db.get("cluster_name", "PersonalServer"),
            "total_nodes": active_count,
            "all_registered": len(nodes),
            "online": online_count,
            "offline": offline_count,
            "unhealthy": unhealthy_count,
            "removed": removed_count,
            "timestamp": get_current_iso_timestamp()
        },
        "nodes": sanitized_nodes
    }


class ControllerHandler(BaseHTTPRequestHandler):
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT

    def send_json(self, status_code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_auth_token(self):
        """Extract Bearer token from Authorization header."""
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
        query = urllib.parse.parse_qs(parsed.query)

        # ----------------------------------------------------------------------
        # GET /health or GET /status
        # ----------------------------------------------------------------------
        if path in ("/health", "/status"):
            db = load_nodes_db()
            summary = get_cluster_summary(db, self.heartbeat_timeout)
            self.send_json(200, {
                "status": "online",
                "service": "PersonalServer Controller",
                "cluster": summary["cluster"],
                "timestamp": get_current_iso_timestamp()
            })
            return

        # ----------------------------------------------------------------------
        # GET /cluster
        # ----------------------------------------------------------------------
        if path == "/cluster":
            db = load_nodes_db()
            summary = get_cluster_summary(db, self.heartbeat_timeout)
            self.send_json(200, summary)
            return

        # ----------------------------------------------------------------------
        # GET /nodes (supports ?status=online, ?role=compute, etc.)
        # ----------------------------------------------------------------------
        if path == "/nodes":
            db = load_nodes_db()
            summary = get_cluster_summary(db, self.heartbeat_timeout)
            nodes_list = summary["nodes"]

            filter_status = query.get("status", [None])[0]
            filter_role = query.get("role", [None])[0]

            if filter_status:
                filter_status = filter_status.upper()
                nodes_list = [n for n in nodes_list if n["status"] == filter_status]
            else:
                # By default, exclude removed nodes from generic list unless requested
                nodes_list = [n for n in nodes_list if n["status"] != STATE_REMOVED]

            if filter_role:
                nodes_list = [n for n in nodes_list if n.get("role") == filter_role.lower()]

            self.send_json(200, {
                "nodes": nodes_list,
                "count": len(nodes_list)
            })
            return

        # ----------------------------------------------------------------------
        # GET /nodes/<node_id>
        # ----------------------------------------------------------------------
        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            db = load_nodes_db()
            node = db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            s_node = sanitize_node_record(node, self.heartbeat_timeout)
            self.send_json(200, {"node": s_node})
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()

        if body is None and path not in ("/nodes/"):
            self.send_json(400, {"error": "Invalid or missing JSON payload"})
            return

        token = self.parse_auth_token()
        if not token and body:
            token = body.get("enrollment_token") or body.get("auth_token") or ""

        # ----------------------------------------------------------------------
        # 1. Node Registration / Re-registration: POST /register
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
            if node_role not in VALID_ROLES:
                node_role = "compute"

            platform = body.get("platform", "unknown")
            os_name = body.get("os", "unknown")
            arch = body.get("architecture", "unknown")

            resources = {
                "cpu_cores": int(body.get("cpu_cores", 0)),
                "ram_mb": int(body.get("ram_mb", 0)),
                "storage_gb": int(body.get("storage_gb", 0))
            }

            capabilities = body.get("capabilities", {
                "compute": True,
                "storage": True,
                "network": True
            })

            db = load_nodes_db()
            now = get_current_iso_timestamp()

            # Generate a fresh dedicated per-node auth token
            node_auth_token = secrets.token_hex(20)

            node_record = {
                "node_id": node_id,
                "name": node_name,
                "role": node_role,
                "platform": platform,
                "os": os_name,
                "architecture": arch,
                "resources": resources,
                "capabilities": capabilities,
                "auth_token": node_auth_token,
                "status": STATE_ONLINE,
                "registered_at": now,
                "last_seen": now,
                "last_heartbeat": {
                    "timestamp": now,
                    "services": {},
                    "system": resources
                }
            }

            db.setdefault("nodes", {})[node_id] = node_record
            save_nodes_db(db)

            print(f"[CONTROLLER] Registered node: {node_id} ({node_name}, role: {node_role})")

            self.send_json(200, {
                "status": "registered",
                "node_id": node_id,
                "auth_token": node_auth_token,
                "registered_at": now,
                "message": f"Node '{node_id}' successfully registered in cluster '{db.get('cluster_name')}'."
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

            # Reject if node was removed
            if node_entry.get("status") == STATE_REMOVED or not node_entry.get("auth_token"):
                self.send_json(403, {"error": f"Node '{node_id}' was removed from the cluster. Re-registration required."})
                return

            expected_auth_token = node_entry.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()

            if not token or (token != expected_auth_token and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            now = get_current_iso_timestamp()
            node_entry["status"] = STATE_ONLINE
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

        # ----------------------------------------------------------------------
        # 3. Node Removal: POST /nodes/<node_id>/remove
        # ----------------------------------------------------------------------
        if path.startswith("/nodes/") and path.endswith("/remove"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to remove a node"})
                return

            db = load_nodes_db()
            node_entry = db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            # Invalidate auth token and mark REMOVED (preserving historical metadata)
            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(db)

            print(f"[CONTROLLER] Removed node: {node_id}")
            self.send_json(200, {
                "status": "removed",
                "node_id": node_id,
                "message": f"Node '{node_id}' has been removed from active cluster membership."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        token = self.parse_auth_token()

        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required"})
                return

            db = load_nodes_db()
            node_entry = db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(db)

            print(f"[CONTROLLER] Removed node via DELETE: {node_id}")
            self.send_json(200, {
                "status": "removed",
                "node_id": node_id,
                "message": f"Node '{node_id}' removed."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def log_message(self, format, *args):
        print(f"[HTTP {self.command}] {self.path} - {args[0] if args else ''}")


def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_HEARTBEAT_TIMEOUT):
    ensure_directories()
    token = get_or_create_enrollment_token()
    ControllerHandler.heartbeat_timeout = timeout

    print("==========================================")
    print(" PersonalServer Cluster Controller")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Heartbeat timeout:     {timeout} seconds")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print(f"Cluster database:      {NODES_FILE}")
    print("==========================================")

    server = HTTPServer((host, port), ControllerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
        server.server_close()


def list_nodes(filter_status=None):
    db = load_nodes_db()
    summary = get_cluster_summary(db)
    nodes = summary["nodes"]

    if filter_status and filter_status.lower() != "all":
        nodes = [n for n in nodes if n["status"] == filter_status.upper()]
    elif not filter_status or filter_status.lower() != "all":
        nodes = [n for n in nodes if n["status"] != STATE_REMOVED]

    print("==========================================")
    print(f" PersonalServer Cluster Nodes ({len(nodes)})")
    print("==========================================")
    if not nodes:
        print("No matching nodes.")
    else:
        for node in nodes:
            nid = node.get("node_id")
            name = node.get("name")
            role = node.get("role", "compute")
            platform = f"{node.get('platform')} ({node.get('architecture')})"
            status = node.get("status")
            last_seen = node.get("last_seen")
            caps = ", ".join([k for k, v in node.get("capabilities", {}).items() if v]) or "none"
            res = node.get("resources", {})

            print(f"Node ID:      {nid}")
            print(f"  Name:       {name}")
            print(f"  Role:       {role}")
            print(f"  Status:     {status}")
            print(f"  Platform:   {platform}")
            print(f"  CPU / RAM:  {res.get('cpu_cores', 'N/A')} cores | {res.get('ram_mb', 'N/A')} MB")
            print(f"  Caps:       {caps}")
            print(f"  Last Seen:  {last_seen}")
            print()
    print("==========================================")


def show_node_details(node_id):
    db = load_nodes_db()
    node = db.get("nodes", {}).get(node_id)
    if not node:
        print(f"Error: Node '{node_id}' not found.", file=sys.stderr)
        return 1

    s_node = sanitize_node_record(node)
    print("==========================================")
    print(f" Node Details: {node_id}")
    print("==========================================")
    print(json.dumps(s_node, indent=2))
    print("==========================================")
    return 0


def show_cluster():
    db = load_nodes_db()
    summary = get_cluster_summary(db)
    print("==========================================")
    print(" PersonalServer Cluster Summary")
    print("==========================================")
    c = summary["cluster"]
    print(f"Cluster:        {c['name']}")
    print(f"Active Nodes:   {c['total_nodes']}")
    print(f"  ONLINE:       {c['online']}")
    print(f"  OFFLINE:      {c['offline']}")
    print(f"  UNHEALTHY:    {c['unhealthy']}")
    print(f"  REMOVED:      {c['removed']}")
    print(f"Timestamp:      {c['timestamp']}")
    print("==========================================")


def remove_node(node_id):
    db = load_nodes_db()
    node = db.get("nodes", {}).get(node_id)
    if not node:
        print(f"Error: Node '{node_id}' not found.", file=sys.stderr)
        return 1

    node["status"] = STATE_REMOVED
    node["auth_token"] = None
    node["removed_at"] = get_current_iso_timestamp()
    save_nodes_db(db)
    print(f"Node '{node_id}' has been removed from active cluster membership.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="controller",
        description="PersonalServer Cluster Controller"
    )
    subparsers = parser.add_subparsers(dest="command", help="Controller commands")

    # start command
    p_start = subparsers.add_parser("start", help="Start the cluster controller HTTP service")
    p_start.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    p_start.add_argument("--timeout", type=int, default=DEFAULT_HEARTBEAT_TIMEOUT, help=f"Heartbeat timeout in seconds (default: {DEFAULT_HEARTBEAT_TIMEOUT})")

    # nodes command
    p_nodes = subparsers.add_parser("nodes", help="List cluster nodes")
    p_nodes.add_argument("--status", choices=["online", "offline", "unhealthy", "removed", "all"], help="Filter by node status")

    # node command
    p_node = subparsers.add_parser("node", help="Inspect single node details")
    p_node.add_argument("node_id", help="ID of node to inspect")

    # cluster command
    subparsers.add_parser("cluster", help="Display cluster overview")

    # remove command
    p_remove = subparsers.add_parser("remove", help="Remove a node from active cluster")
    p_remove.add_argument("node_id", help="ID of node to remove")

    # token command
    subparsers.add_parser("token", help="Display enrollment token file path")

    args = parser.parse_args()

    if not args.command or args.command == "start":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        timeout = getattr(args, "timeout", DEFAULT_HEARTBEAT_TIMEOUT)
        start_server(host, port, timeout)
    elif args.command == "nodes":
        list_nodes(getattr(args, "status", None))
    elif args.command == "node":
        sys.exit(show_node_details(args.node_id))
    elif args.command == "cluster":
        show_cluster()
    elif args.command == "remove":
        sys.exit(remove_node(args.node_id))
    elif args.command == "token":
        get_or_create_enrollment_token()
        print(f"Enrollment token stored at: {ENROLLMENT_TOKEN_FILE}")


if __name__ == "__main__":
    main()
