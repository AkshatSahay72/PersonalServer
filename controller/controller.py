#!/usr/bin/env python3
"""
PersonalServer Controller
=========================
Cluster controller for multi-node inventory, authenticated registration,
heartbeat liveness monitoring, node removal, resource-aware workload scheduling,
persistent job lifecycle, lease-based failure detection, and automatic recovery.
"""

import sys
import os
import json
import time
import secrets
import argparse
import hashlib
import hmac
import shutil
import threading
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Import ResourceScheduler
from scheduler.scheduler import ResourceScheduler, compute_node_liveness

CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = CONTROLLER_DIR / "data"

NODES_FILE = DATA_DIR / "nodes.json"
BACKUP_NODES_FILE = DATA_DIR / "nodes.json.bak"
JOBS_FILE = DATA_DIR / "jobs.json"
BACKUP_JOBS_FILE = DATA_DIR / "jobs.json.bak"
APPS_FILE = DATA_DIR / "apps.json"
BACKUP_APPS_FILE = DATA_DIR / "apps.json.bak"
ONBOARDING_CODES_FILE = DATA_DIR / "onboarding_codes.json"
BACKUP_ONBOARDING_CODES_FILE = DATA_DIR / "onboarding_codes.json.bak"
ENROLLMENT_TOKEN_FILE = SECRETS_DIR / "enrollment.token"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds
DEFAULT_LEASE_GRACE_SEC = 20    # seconds beyond job timeout
DEFAULT_ONBOARDING_TTL_SEC = 900 # 15 minutes (seconds)

PORT_RANGE_START = 18000
PORT_RANGE_END = 18999

ONBOARDING_LOCK = threading.Lock()
APPS_LOCK = threading.Lock()

# Centralized Node State Constants
STATE_REGISTERING = "REGISTERING"
STATE_ONLINE = "ONLINE"
STATE_OFFLINE = "OFFLINE"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_UNKNOWN = "UNKNOWN"
STATE_REMOVED = "REMOVED"

VALID_ROLES = ["compute", "storage", "gateway", "controller", "hybrid"]

# Centralized Job States (V1.0 Lifecycle)
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_CLAIMED = "CLAIMED"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMEOUT = "TIMEOUT"
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_RECOVERING = "RECOVERING"
JOB_STATE_REJECTED = "REJECTED"

# Centralized Application States (Phase 11B Lifecycle)
APP_STATE_CREATED = "CREATED"
APP_STATE_DEPLOYING = "DEPLOYING"
APP_STATE_RUNNING = "RUNNING"
APP_STATE_STOPPED = "STOPPED"
APP_STATE_FAILED = "FAILED"
APP_STATE_REMOVING = "REMOVING"

# Allowlisted Workload Types
ALLOWLISTED_WORKLOADS = {
    "system-info": "Gather system hardware and OS metrics",
    "health-check": "Run node health check script",
    "node-status": "Run node status check script",
    "python-script": "Execute safe inline python script",
    "echo": "Echo back test message",
    "failing-test": "Controlled error-handling test workload",
    "timeout-test": "Controlled timeout test workload",
    "docker-deploy": "Pull image and safely launch containerized application",
    "docker-stop": "Stop running containerized application",
    "docker-restart": "Restart containerized application",
    "docker-remove": "Remove containerized application",
    "docker-logs": "Fetch bounded container logs"
}


def get_current_iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


def ensure_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)


def get_or_create_enrollment_token():
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


# ------------------------------------------------------------------------------
# Storage Helpers
# ------------------------------------------------------------------------------

def load_nodes_db():
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
    ensure_directories()
    temp_file = NODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if NODES_FILE.exists():
        try:
            shutil.copy2(NODES_FILE, BACKUP_NODES_FILE)
        except Exception:
            pass

    temp_file.replace(NODES_FILE)


def load_jobs_db():
    ensure_directories()
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {JOBS_FILE} ({e}). Rolling back to backup...", file=sys.stderr)
            if BACKUP_JOBS_FILE.exists():
                try:
                    with open(BACKUP_JOBS_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"jobs": {}}


def save_jobs_db(db):
    ensure_directories()
    temp_file = JOBS_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if JOBS_FILE.exists():
        try:
            shutil.copy2(JOBS_FILE, BACKUP_JOBS_FILE)
        except Exception:
            pass

    temp_file.replace(JOBS_FILE)


def load_apps_db():
    ensure_directories()
    if APPS_FILE.exists():
        try:
            with open(APPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {APPS_FILE} ({e}). Rolling back to backup...", file=sys.stderr)
            if BACKUP_APPS_FILE.exists():
                try:
                    with open(BACKUP_APPS_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"apps": {}}


def save_apps_db(db):
    ensure_directories()
    temp_file = APPS_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if APPS_FILE.exists():
        try:
            shutil.copy2(APPS_FILE, BACKUP_APPS_FILE)
        except Exception:
            pass

    temp_file.replace(APPS_FILE)


def allocate_host_port(apps_db):
    """
    Finds the lowest available host port in range PORT_RANGE_START to PORT_RANGE_END.
    """
    allocated = set()
    for app in apps_db.get("apps", {}).values():
        if app.get("status") != "DELETED" and "host_port" in app:
            try:
                allocated.add(int(app["host_port"]))
            except (ValueError, TypeError):
                pass
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in allocated:
            return port
    raise RuntimeError(f"Port exhaustion: All host ports in range {PORT_RANGE_START}-{PORT_RANGE_END} are allocated.")


# ------------------------------------------------------------------------------
# Onboarding Code Store & Validation
# ------------------------------------------------------------------------------

def hash_onboarding_code(code: str) -> str:
    """Computes a SHA-256 hash of the normalized onboarding code for secure storage."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def load_onboarding_codes_db():
    ensure_directories()
    if ONBOARDING_CODES_FILE.exists():
        try:
            with open(ONBOARDING_CODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {ONBOARDING_CODES_FILE} ({e}). Checking backup...", file=sys.stderr)
            if BACKUP_ONBOARDING_CODES_FILE.exists():
                try:
                    with open(BACKUP_ONBOARDING_CODES_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"codes": {}}


def save_onboarding_codes_db(db):
    ensure_directories()
    temp_file = ONBOARDING_CODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if ONBOARDING_CODES_FILE.exists():
        try:
            shutil.copy2(ONBOARDING_CODES_FILE, BACKUP_ONBOARDING_CODES_FILE)
        except Exception:
            pass

    temp_file.replace(ONBOARDING_CODES_FILE)


def generate_onboarding_code(ttl_seconds=DEFAULT_ONBOARDING_TTL_SEC):
    """
    Generates a cryptographically secure, single-use onboarding code.
    Format: PS-XXXX-XXXX
    Stores SHA-256 hash in onboarding_codes.json.
    Returns (code_str, ttl_seconds).
    """
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    code = f"PS-{part1}-{part2}"

    code_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        # Clean up stale codes older than 24 hours
        now_ts = now_dt.timestamp()
        filtered = {}
        for h, info in db.get("codes", {}).items():
            try:
                exp_ts = datetime.fromisoformat(info["expires_at"]).timestamp()
                if now_ts - exp_ts < 86400:
                    filtered[h] = info
            except Exception:
                pass
        db["codes"] = filtered

        db.setdefault("codes", {})[code_hash] = {
            "created_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "used": False,
            "used_at": None,
            "used_by_node": None
        }
        save_onboarding_codes_db(db)

    return code, ttl_seconds


def validate_and_consume_onboarding_code(code: str, node_id: str) -> tuple:
    """
    Thread-safe validation and single-use consumption of an onboarding code.
    Returns (is_valid: bool, error_reason: str).
    """
    if not code or not code.strip():
        return False, "Missing onboarding code"

    input_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        codes = db.get("codes", {})

        matched_entry = None
        for h, entry in codes.items():
            if hmac.compare_digest(h, input_hash):
                matched_entry = entry
                break

        if not matched_entry:
            return False, "Invalid onboarding code"

        if matched_entry.get("used"):
            return False, "Onboarding code has already been used"

        try:
            expires_at = datetime.fromisoformat(matched_entry["expires_at"])
            if now_dt > expires_at:
                return False, "Onboarding code has expired"
        except Exception:
            return False, "Corrupt expiration timestamp on onboarding code"

        # Atomically consume the code
        matched_entry["used"] = True
        matched_entry["used_at"] = now_dt.isoformat()
        matched_entry["used_by_node"] = node_id
        save_onboarding_codes_db(db)

        return True, ""


def sanitize_node_record(node, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    node_copy = dict(node)
    node_copy.pop("auth_token", None)
    node_copy["status"] = compute_node_liveness(node, timeout_seconds)
    return node_copy


def get_cluster_summary(db, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
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


# ------------------------------------------------------------------------------
# Lease Sweeping & Recovery Subsystem
# ------------------------------------------------------------------------------

def sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """
    Evaluates active CLAIMED / RUNNING jobs for lease expiration or worker node disconnection.
    Recovers eligible jobs or marks them FAILED deterministically.
    """
    changed = False
    now_epoch = time.time()
    now_iso = get_current_iso_timestamp()

    for job_id, job in list(jobs_db.get("jobs", {}).items()):
        status = job.get("status")
        if status not in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING):
            continue

        lease_exp = job.get("lease_expires_at")
        target_node = job.get("target_node")
        node_entry = nodes_db.get("nodes", {}).get(target_node, {})
        node_live = compute_node_liveness(node_entry, timeout_seconds) if node_entry else STATE_OFFLINE

        # Check conditions for abandoned lease / worker failure
        lease_expired = lease_exp and now_epoch > lease_exp
        worker_lost = node_live in (STATE_OFFLINE, STATE_REMOVED)

        if lease_expired or worker_lost:
            attempt = job.get("attempt", 1)
            max_attempts = job.get("max_attempts", 3)
            reason = "Worker lease expired" if lease_expired else f"Worker node '{target_node}' went OFFLINE"

            print(f"[CONTROLLER-RECOVERY] Job {job_id} on node '{target_node}' failure detected: {reason} (Attempt {attempt}/{max_attempts})")

            # Record history
            attempts_history = job.setdefault("attempts_history", [])
            attempts_history.append({
                "attempt": attempt,
                "node": target_node,
                "reason": reason,
                "claimed_at": job.get("claimed_at"),
                "failed_at": now_iso
            })

            # Check if retry is allowed
            if attempt < max_attempts:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["retry_reason"] = reason
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                # Policy: Auto-target vs Explicit Target
                target_mode = job.get("target", "auto")
                if target_mode == "auto":
                    # Reschedule onto eligible online node
                    reqs = job.get("requirements", {})
                    decision = ResourceScheduler.select_node(reqs, nodes_db, timeout_seconds)
                    if decision.get("selected_node"):
                        job["target_node"] = decision["selected_node"]
                        job["scheduler"] = {
                            "mode": "resource-aware-recovery",
                            "selected_node": decision["selected_node"],
                            "score": decision["score"],
                            "reason": f"Recovered from {target_node} ({reason})"
                        }
                        print(f"[CONTROLLER-RECOVERY] Rescheduled job {job_id} to node '{decision['selected_node']}'")
                    else:
                        print(f"[CONTROLLER-RECOVERY] No eligible online node available for auto-recovery of {job_id}. Retaining in RECOVERING state.")
                else:
                    # Explicit target: DO NOT silently migrate! Keep same target node.
                    job["target_node"] = target_mode
                    job["scheduler"] = {
                        "mode": "explicit-recovery",
                        "target_node": target_mode,
                        "reason": f"Awaiting recovery on explicit target {target_mode}"
                    }
                    print(f"[CONTROLLER-RECOVERY] Explicit target job {job_id} queued for recovery on '{target_mode}' (no migration).")

            else:
                # Max retries exhausted
                job["status"] = JOB_STATE_FAILED
                job["finished_at"] = now_iso
                job["retry_reason"] = f"Max retries ({max_attempts}) exceeded after: {reason}"
                job["lease_expires_at"] = None
                print(f"[CONTROLLER-RECOVERY] Job {job_id} permanently marked FAILED (max retries reached).")

            changed = True

    if changed:
        save_jobs_db(jobs_db)


def start_background_lease_sweeper(timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Starts a daemon thread to periodically sweep expired leases every 3 seconds."""
    def _sweeper():
        while True:
            try:
                time.sleep(3)
                jobs_db = load_jobs_db()
                nodes_db = load_nodes_db()
                sweep_expired_leases(jobs_db, nodes_db, timeout_seconds)
            except Exception as e:
                print(f"[SWEEPER-ERR] {e}", file=sys.stderr)

    t = threading.Thread(target=_sweeper, daemon=True, name="LeaseSweeper")
    t.start()


# ------------------------------------------------------------------------------
# HTTP Request Handler
# ------------------------------------------------------------------------------

class ControllerHandler(BaseHTTPRequestHandler):
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT

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

    def parse_auth_token(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return self.headers.get("X-Auth-Token", "").strip()

    def is_authenticated_admin_or_node(self):
        token = self.parse_auth_token()
        if not token:
            return False
        enrollment_token = get_or_create_enrollment_token()
        if token == enrollment_token:
            return True
        nodes_db = load_nodes_db()
        for node in nodes_db.get("nodes", {}).values():
            if node.get("auth_token") and node.get("auth_token") == token:
                return True
        return False

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

        # GET /health or GET /status
        if path in ("/health", "/status"):
            nodes_db = load_nodes_db()
            jobs_db = load_jobs_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            self.send_json(200, {
                "status": "online",
                "service": "PersonalServer Controller",
                "cluster": summary["cluster"],
                "total_jobs": len(jobs_db.get("jobs", {})),
                "timestamp": get_current_iso_timestamp()
            })
            return

        # GET /cluster
        if path == "/cluster":
            nodes_db = load_nodes_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            self.send_json(200, summary)
            return

        # GET /nodes
        if path == "/nodes":
            nodes_db = load_nodes_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            nodes_list = summary["nodes"]

            filter_status = query.get("status", [None])[0]
            filter_role = query.get("role", [None])[0]

            if filter_status:
                filter_status = filter_status.upper()
                nodes_list = [n for n in nodes_list if n["status"] == filter_status]
            else:
                nodes_list = [n for n in nodes_list if n["status"] != STATE_REMOVED]

            if filter_role:
                nodes_list = [n for n in nodes_list if n.get("role") == filter_role.lower()]

            self.send_json(200, {
                "nodes": nodes_list,
                "count": len(nodes_list)
            })
            return

        # GET /nodes/<node_id>/jobs/next (Node Agent fetching its assigned job with Lease)
        if path.startswith("/nodes/") and path.endswith("/jobs/next"):
            node_id = path[7:-10].strip()
            token = self.parse_auth_token()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)

            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            expected_auth = node.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()
            if not token or (token != expected_auth and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            jobs_db = load_jobs_db()
            # Perform sweep before claiming
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            # Find jobs waiting for this node
            claimable_jobs = [
                j for j in jobs_db.get("jobs", {}).values()
                if j.get("target_node") == node_id and j.get("status") in (JOB_STATE_QUEUED, JOB_STATE_RECOVERING)
            ]

            if not claimable_jobs:
                self.send_json(200, {"job": None, "message": "No pending jobs for this node."})
                return

            # Atomic claim: pick oldest job and assign lease
            next_job = claimable_jobs[0]
            now_iso = get_current_iso_timestamp()
            now_epoch = time.time()
            timeout_sec = next_job.get("timeout", 60)
            lease_duration = timeout_sec + DEFAULT_LEASE_GRACE_SEC

            next_job["status"] = JOB_STATE_CLAIMED
            next_job["claimed_at"] = now_iso
            next_job["started_at"] = now_iso
            next_job["lease_expires_at"] = now_epoch + lease_duration

            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Job {next_job['job_id']} ({next_job['type']}) CLAIMED by node '{node_id}' (Attempt {next_job.get('attempt', 1)}, Lease {lease_duration}s)")
            self.send_json(200, {"job": next_job})
            return

        # GET /nodes/<node_id>
        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            s_node = sanitize_node_record(node, self.heartbeat_timeout)
            self.send_json(200, {"node": s_node})
            return

        # GET /jobs
        if path == "/jobs":
            jobs_db = load_jobs_db()
            nodes_db = load_nodes_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            all_jobs = list(jobs_db.get("jobs", {}).values())
            filter_node = query.get("node", [None])[0]
            filter_status = query.get("status", [None])[0]

            if filter_node:
                all_jobs = [j for j in all_jobs if j.get("target_node") == filter_node]
            if filter_status:
                all_jobs = [j for j in all_jobs if j.get("status") == filter_status.upper()]

            self.send_json(200, {
                "jobs": all_jobs,
                "count": len(all_jobs)
            })
            return

        # GET /jobs/<job_id>
        if path.startswith("/jobs/"):
            job_id = path[6:].strip()
            jobs_db = load_jobs_db()
            nodes_db = load_nodes_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return
            self.send_json(200, {"job": job})
            return

        # GET /apps
        if path == "/apps":
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            apps_db = load_apps_db()
            apps_list = list(apps_db.get("apps", {}).values())
            self.send_json(200, {
                "apps": apps_list,
                "count": len(apps_list)
            })
            return

        # GET /apps/<app_id>/logs
        if path.startswith("/apps/") and path.endswith("/logs"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-5].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            # Check if there's a recent docker-logs or deployment result in jobs_db
            jobs_db = load_jobs_db()
            target_node = app.get("selected_node")
            container_id = app.get("container_id") or f"ps-{app.get('name')}"

            # Look up recent job output for this app
            app_jobs = [
                j for j in jobs_db.get("jobs", {}).values()
                if j.get("parameters", {}).get("app_id") == app_id or j.get("parameters", {}).get("container_name") == container_id
            ]
            app_jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)

            log_output = "No logs recorded yet."
            if app_jobs:
                latest_job = app_jobs[0]
                res = latest_job.get("result", {})
                log_output = res.get("stdout") or res.get("stderr") or f"Job {latest_job.get('job_id')} status: {latest_job.get('status')}"

            self.send_json(200, {
                "app_id": app_id,
                "name": app.get("name"),
                "status": app.get("status"),
                "node": app.get("selected_node"),
                "logs": log_output
            })
            return

        # GET /apps/<app_id>
        if path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            self.send_json(200, {"app": app})
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()
        if body is None:
            # Endpoints that do not strictly require a request body
            no_body_endpoints = (
                (path.startswith("/nodes/") and path.endswith("/remove")),
                (path.startswith("/jobs/") and path.endswith("/cancel")),
                (path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/stop") or path.endswith("/restart")))
            )
            if not any(no_body_endpoints):
                self.send_json(400, {"error": "Invalid or missing JSON payload"})
                return
            body = {}

        token = self.parse_auth_token()
        if not token and body:
            token = body.get("enrollment_token") or body.get("auth_token") or ""

        # ----------------------------------------------------------------------
        # 1. Node Registration: POST /register
        # ----------------------------------------------------------------------
        if path == "/register":
            onboarding_code = self.headers.get("X-Onboarding-Code", "").strip()

            node_id = body.get("node_id")
            if not node_id:
                self.send_json(400, {"error": "Missing required field 'node_id'"})
                return

            if onboarding_code:
                is_valid, err = validate_and_consume_onboarding_code(onboarding_code, node_id)
                if not is_valid:
                    self.send_json(401, {"error": f"Unauthorized: {err}"})
                    return
            else:
                enrollment_token = get_or_create_enrollment_token()
                if not token or token != enrollment_token:
                    self.send_json(401, {"error": "Unauthorized: Invalid enrollment token"})
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

            nodes_db = load_nodes_db()
            now = get_current_iso_timestamp()

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

            nodes_db.setdefault("nodes", {})[node_id] = node_record
            save_nodes_db(nodes_db)

            print(f"[CONTROLLER] Registered node: {node_id} ({node_name}, role: {node_role})")

            self.send_json(200, {
                "status": "registered",
                "node_id": node_id,
                "auth_token": node_auth_token,
                "registered_at": now,
                "message": f"Node '{node_id}' successfully registered in cluster '{nodes_db.get('cluster_name')}'."
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

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' is not registered. Please register first."})
                return

            if node_entry.get("status") == STATE_REMOVED or not node_entry.get("auth_token"):
                self.send_json(403, {"error": f"Node '{node_id}' was removed from cluster. Re-registration required."})
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

            save_nodes_db(nodes_db)

            self.send_json(200, {
                "status": "ok",
                "ack": True,
                "node_id": node_id,
                "timestamp": now
            })
            return

        # ----------------------------------------------------------------------
        # 3. Submit Workload / Job: POST /jobs (Scheduler + Lifecycle)
        # ----------------------------------------------------------------------
        if path == "/jobs":
            enrollment_token = get_or_create_enrollment_token()
            # Allow admin token or valid request
            if token and token != enrollment_token:
                pass  # allow web UI proxy if auth passes

            target = body.get("target") or body.get("target_node") or "auto"
            job_type = body.get("type")
            job_name = body.get("name", job_type or "job")
            timeout_sec = int(body.get("timeout", 60))
            max_attempts = int(body.get("max_attempts", 3))
            parameters = body.get("parameters", {}) or {}
            requirements = body.get("requirements", {}) or {}

            # Validation: Safe allowlisted workload type rejection
            if job_type not in ALLOWLISTED_WORKLOADS:
                self.send_json(400, {
                    "error": f"Job type '{job_type}' is not in the safe allowlist. Allowed: {list(ALLOWLISTED_WORKLOADS.keys())}",
                    "status": JOB_STATE_REJECTED
                })
                return

            nodes_db = load_nodes_db()
            target_node = None
            scheduler_info = None

            # Path A: Automatic Resource-Aware Scheduling
            if target.lower() == "auto":
                decision = ResourceScheduler.select_node(requirements, nodes_db, self.heartbeat_timeout)
                if not decision["selected_node"]:
                    self.send_json(400, {
                        "error": f"Scheduling Failed: {decision['reason']}",
                        "status": JOB_STATE_REJECTED,
                        "scheduler": decision
                    })
                    return
                target_node = decision["selected_node"]
                scheduler_info = {
                    "mode": "resource-aware",
                    "selected_node": target_node,
                    "score": decision["score"],
                    "reason": decision["reason"],
                    "candidates_count": len(decision["candidates"])
                }
                print(f"[CONTROLLER-SCHEDULER] Auto-selected node '{target_node}': {decision['reason']}")

            # Path B: Explicit Target Node (Preserves explicit target policy)
            else:
                target_node = target
                node_entry = nodes_db.get("nodes", {}).get(target_node)

                if not node_entry:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' does not exist in cluster.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                if node_entry.get("status") == STATE_REMOVED:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' was removed from the cluster.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                live_status = compute_node_liveness(node_entry, self.heartbeat_timeout)
                if live_status == STATE_OFFLINE:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' is currently OFFLINE (heartbeat timeout). Cannot submit job.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                scheduler_info = {
                    "mode": "explicit",
                    "target_node": target_node,
                    "reason": "Explicit node specified by client"
                }

            timeout_sec = max(5, min(timeout_sec, 300))
            job_id = f"job-{secrets.token_hex(6)}"
            now = get_current_iso_timestamp()

            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": job_name,
                "type": job_type,
                "parameters": parameters,
                "requirements": requirements,
                "target": target,
                "target_node": target_node,
                "assigned_node": target_node,
                "scheduler": scheduler_info,
                "timeout": timeout_sec,
                "max_attempts": max_attempts,
                "attempt": 1,
                "claimed_at": None,
                "lease_expires_at": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "status": JOB_STATE_QUEUED,
                "state": JOB_STATE_QUEUED,
                "retry_reason": None,
                "attempts_history": [],
                "result": None
            }

            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Queued job {job_id} ({job_type}) for target node {target_node}")
            self.send_json(200, {
                "status": JOB_STATE_QUEUED,
                "job_id": job_id,
                "id": job_id,
                "target_node": target_node,
                "scheduler": scheduler_info,
                "job": job_record
            })
            return

        # ----------------------------------------------------------------------
        # 4. Job Result Submission & Retry Handling: POST /jobs/<job_id>/result
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/result"):
            job_id = path[6:-7].strip()
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)

            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return

            target_node = job.get("target_node")
            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(target_node, {})

            expected_auth = node_entry.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()
            if not token or (token != expected_auth and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
                return

            job_status = body.get("status", JOB_STATE_FAILED)
            now_iso = get_current_iso_timestamp()

            # Record attempt
            attempt = job.get("attempt", 1)
            max_attempts = job.get("max_attempts", 3)
            attempts_history = job.setdefault("attempts_history", [])
            attempts_history.append({
                "attempt": attempt,
                "node": target_node,
                "status": job_status,
                "exit_code": body.get("exit_code", 0),
                "duration_ms": body.get("duration_ms", 0),
                "started_at": body.get("started_at"),
                "finished_at": now_iso
            })

            # Check for failure retry
            if job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT) and attempt < max_attempts:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["state"] = JOB_STATE_RECOVERING
                job["retry_reason"] = f"Attempt {attempt} completed with status: {job_status}"
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                target_mode = job.get("target", "auto")
                if target_mode == "auto":
                    reqs = job.get("requirements", {})
                    decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                    if decision.get("selected_node"):
                        job["target_node"] = decision["selected_node"]
                        job["assigned_node"] = decision["selected_node"]
                        job["scheduler"] = {
                            "mode": "resource-aware-recovery",
                            "selected_node": decision["selected_node"],
                            "score": decision["score"],
                            "reason": f"Retry attempt {attempt+1} scheduled on {decision['selected_node']}"
                        }
                print(f"[CONTROLLER-RETRY] Job {job_id} failed on node {target_node}. Queued for retry attempt {attempt+1}/{max_attempts}")

            else:
                # Terminal Success / Final Failure
                job["status"] = job_status
                job["state"] = job_status
                job["finished_at"] = now_iso
                job["lease_expires_at"] = None
                job["result"] = {
                    "status": job_status,
                    "exit_code": body.get("exit_code", 0),
                    "stdout": body.get("stdout", ""),
                    "stderr": body.get("stderr", ""),
                    "duration_ms": body.get("duration_ms", 0),
                }

            # Update application state if this was a docker lifecycle job
            app_id = job.get("parameters", {}).get("app_id")
            if app_id:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if app:
                    if job.get("type") == "docker-deploy":
                        if job_status == JOB_STATE_SUCCEEDED:
                            app["status"] = APP_STATE_RUNNING
                            app["error"] = None
                            app["updated_at"] = now_iso
                        elif job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                            app["status"] = APP_STATE_FAILED
                            app["error"] = body.get("stderr") or f"Deployment job {job_status}"
                            app["updated_at"] = now_iso
                    elif job.get("type") == "docker-stop" and job_status == JOB_STATE_SUCCEEDED:
                        app["status"] = APP_STATE_STOPPED
                        app["updated_at"] = now_iso
                    elif job.get("type") == "docker-restart" and job_status == JOB_STATE_SUCCEEDED:
                        app["status"] = APP_STATE_RUNNING
                        app["updated_at"] = now_iso
                    save_apps_db(apps_db)

            save_jobs_db(jobs_db)

            self.send_json(200, {
                "status": "ok",
                "ack": True,
                "job_id": job_id,
                "job_status": job["status"]
            })
            return

        # ----------------------------------------------------------------------
        # 5. Application Management: POST /apps and POST /apps/<app_id>/...
        # ----------------------------------------------------------------------
        if path == "/apps" or path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

        if path == "/apps":
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = int(body.get("container_port") or body.get("port") or 8000)
            target = (body.get("target") or "auto").strip()
            env = body.get("env", {})
            cpu_limit = str(body.get("cpu_limit", "0.5"))
            memory_limit = str(body.get("memory_limit", "256m"))

            if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
                self.send_json(400, {"error": "Invalid application name. Must contain only alphanumeric characters, dashes, and underscores."})
                return

            if not image or not re.match(r'^[a-zA-Z0-9_./:-]+$', image):
                self.send_json(400, {"error": "Invalid Docker image reference."})
                return

            if not (1 <= port <= 65535):
                self.send_json(400, {"error": "Invalid container port. Must be between 1 and 65535."})
                return

            with APPS_LOCK:
                apps_db = load_apps_db()
                # Check for duplicate name
                for existing in apps_db.get("apps", {}).values():
                    if existing.get("name") == name and existing.get("status") != "DELETED":
                        self.send_json(409, {"error": f"Application with name '{name}' already exists."})
                        return

                try:
                    host_port = allocate_host_port(apps_db)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to allocate host port: {e}"})
                    return

                app_id = f"app-{secrets.token_hex(6)}"
                now_iso = get_current_iso_timestamp()

                app_record = {
                    "app_id": app_id,
                    "id": app_id,
                    "name": name,
                    "image": image,
                    "port": host_port,
                    "container_port": port,
                    "host_port": host_port,
                    "target": target,
                    "status": APP_STATE_CREATED,
                    "selected_node": None,
                    "container_id": f"ps-{name}",
                    "env": env if isinstance(env, dict) else {},
                    "cpu_limit": cpu_limit,
                    "memory_limit": memory_limit,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "failure_reason": None,
                    "error": None
                }

                apps_db.setdefault("apps", {})[app_id] = app_record
                save_apps_db(apps_db)

            print(f"[CONTROLLER] Created application '{name}' ({app_id}) on host port {host_port}")
            self.send_json(201, {
                "status": "created",
                "app_id": app_id,
                "app": app_record
            })
            return

        if path.startswith("/apps/") and path.endswith("/deploy"):
            app_id = path[6:-7].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                # Schedule onto Docker-capable node
                nodes_db = load_nodes_db()
                reqs = {"capabilities": ["container_runtime:docker"]}
                decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)

                now_iso = get_current_iso_timestamp()

                if not decision.get("selected_node"):
                    reason = "No Docker-capable node is currently available in the cluster."
                    app["status"] = APP_STATE_FAILED
                    app["failure_reason"] = reason
                    app["error"] = reason
                    app["updated_at"] = now_iso
                    save_apps_db(apps_db)
                    print(f"[CONTROLLER-APP] Deployment of '{app['name']}' ({app_id}) failed: {reason}")
                    self.send_json(400, {
                        "error": reason,
                        "status": APP_STATE_FAILED,
                        "app": app,
                        "scheduler": decision
                    })
                    return

                selected_node = decision["selected_node"]
                app["status"] = APP_STATE_DEPLOYING
                app["selected_node"] = selected_node
                app["updated_at"] = now_iso
                app["error"] = None
                save_apps_db(apps_db)

                # Create docker-deploy job
                job_id = f"job-{secrets.token_hex(6)}"
                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"deploy-{app['name']}",
                    "type": "docker-deploy",
                    "parameters": {
                        "app_id": app_id,
                        "image": app["image"],
                        "container_name": app.get("container_id") or f"ps-{app['name']}",
                        "host_port": app["host_port"],
                        "port": app["port"],
                        "env": app.get("env", {}),
                        "cpu_limit": app.get("cpu_limit", "0.5"),
                        "memory_limit": app.get("memory_limit", "256m")
                    },
                    "requirements": reqs,
                    "target": selected_node,
                    "target_node": selected_node,
                    "assigned_node": selected_node,
                    "scheduler": {
                        "mode": "resource-aware-docker",
                        "selected_node": selected_node,
                        "score": decision["score"],
                        "reason": decision["reason"]
                    },
                    "timeout": 120,
                    "max_attempts": 2,
                    "attempt": 1,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "created_at": now_iso,
                    "started_at": None,
                    "finished_at": None,
                    "status": JOB_STATE_QUEUED,
                    "state": JOB_STATE_QUEUED,
                    "retry_reason": None,
                    "attempts_history": [],
                    "result": None
                }

                jobs_db = load_jobs_db()
                jobs_db.setdefault("jobs", {})[job_id] = job_record
                save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Queued docker-deploy job {job_id} for app '{app['name']}' to node '{selected_node}'")
            self.send_json(200, {
                "status": APP_STATE_DEPLOYING,
                "app": app,
                "job_id": job_id
            })
            return

        if path.startswith("/apps/") and path.endswith("/stop"):
            app_id = path[6:-5].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                target_node = app.get("selected_node")
                if not target_node:
                    app["status"] = APP_STATE_STOPPED
                    save_apps_db(apps_db)
                    self.send_json(200, {"status": "STOPPED", "app": app})
                    return

                app["status"] = APP_STATE_STOPPED
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

                # Queue docker-stop job
                job_id = f"job-{secrets.token_hex(6)}"
                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"stop-{app['name']}",
                    "type": "docker-stop",
                    "parameters": {
                        "app_id": app_id,
                        "container_name": app.get("container_id") or f"ps-{app['name']}"
                    },
                    "target": target_node,
                    "target_node": target_node,
                    "assigned_node": target_node,
                    "timeout": 30,
                    "max_attempts": 1,
                    "attempt": 1,
                    "created_at": get_current_iso_timestamp(),
                    "status": JOB_STATE_QUEUED
                }
                jobs_db = load_jobs_db()
                jobs_db.setdefault("jobs", {})[job_id] = job_record
                save_jobs_db(jobs_db)

            self.send_json(200, {"status": "STOPPED", "app": app, "job_id": job_id})
            return

        if path.startswith("/apps/") and path.endswith("/restart"):
            app_id = path[6:-8].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                target_node = app.get("selected_node")
                if not target_node:
                    self.send_json(400, {"error": "Application is not deployed on any node."})
                    return

                app["status"] = APP_STATE_RUNNING
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

                job_id = f"job-{secrets.token_hex(6)}"
                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"restart-{app['name']}",
                    "type": "docker-restart",
                    "parameters": {
                        "app_id": app_id,
                        "container_name": app.get("container_id") or f"ps-{app['name']}"
                    },
                    "target": target_node,
                    "target_node": target_node,
                    "assigned_node": target_node,
                    "timeout": 30,
                    "max_attempts": 1,
                    "attempt": 1,
                    "created_at": get_current_iso_timestamp(),
                    "status": JOB_STATE_QUEUED
                }
                jobs_db = load_jobs_db()
                jobs_db.setdefault("jobs", {})[job_id] = job_record
                save_jobs_db(jobs_db)

            self.send_json(200, {"status": "RUNNING", "app": app, "job_id": job_id})
            return

        # ----------------------------------------------------------------------
        # 6. Cancel Job: POST /jobs/<job_id>/cancel
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/cancel"):
            job_id = path[6:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token and token != enrollment_token:
                pass

            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return

            if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED):
                self.send_json(400, {
                    "error": f"Cannot cancel job '{job_id}' in terminal state '{job.get('status')}'."
                })
                return

            job["status"] = JOB_STATE_CANCELLED
            job["state"] = JOB_STATE_CANCELLED
            job["finished_at"] = get_current_iso_timestamp()
            job["lease_expires_at"] = None
            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Job {job_id} cancelled by admin.")
            self.send_json(200, {
                "status": "cancelled",
                "job_id": job_id,
                "job": job
            })
            return

        # ----------------------------------------------------------------------
        # 7. Node Removal: POST /nodes/<node_id>/remove
        # ----------------------------------------------------------------------
        if path.startswith("/nodes/") and path.endswith("/remove"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to remove a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            # Trigger lease sweep to recover any jobs assigned to removed node
            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

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

        if path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

            app_id = path[6:].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                target_node = app.get("selected_node")
                if target_node:
                    job_id = f"job-{secrets.token_hex(6)}"
                    job_record = {
                        "id": job_id,
                        "job_id": job_id,
                        "name": f"remove-{app['name']}",
                        "type": "docker-remove",
                        "parameters": {
                            "container_name": app.get("container_id") or f"ps-{app['name']}"
                        },
                        "target": target_node,
                        "target_node": target_node,
                        "assigned_node": target_node,
                        "timeout": 30,
                        "max_attempts": 1,
                        "attempt": 1,
                        "created_at": get_current_iso_timestamp(),
                        "status": JOB_STATE_QUEUED
                    }
                    jobs_db = load_jobs_db()
                    jobs_db.setdefault("jobs", {})[job_id] = job_record
                    save_jobs_db(jobs_db)

                del apps_db["apps"][app_id]
                save_apps_db(apps_db)

            print(f"[CONTROLLER] Deleted application '{app['name']}' ({app_id}). Released host port {app.get('host_port')}")
            self.send_json(200, {
                "status": "deleted",
                "app_id": app_id,
                "message": f"Application '{app['name']}' deleted."
            })
            return

        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

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


# ------------------------------------------------------------------------------
# CLI Actions
# ------------------------------------------------------------------------------

def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_HEARTBEAT_TIMEOUT):
    ensure_directories()
    token = get_or_create_enrollment_token()
    ControllerHandler.heartbeat_timeout = timeout

    # Recover orphaned jobs from previous shutdown
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db, timeout)

    # Start background lease sweeper thread
    start_background_lease_sweeper(timeout)

    print("==========================================")
    print(" PersonalServer Cluster Controller & Scheduler (v1.0)")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Heartbeat timeout:     {timeout} seconds")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print(f"Cluster database:      {NODES_FILE}")
    print(f"Jobs database:         {JOBS_FILE}")
    print("==========================================")

    server = HTTPServer((host, port), ControllerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
        server.server_close()


def schedule_test(requirements_json=None):
    nodes_db = load_nodes_db()
    reqs = {}
    if requirements_json:
        try:
            reqs = json.loads(requirements_json)
        except Exception as e:
            print(f"Error parsing requirements JSON: {e}", file=sys.stderr)
            return 1

    print("==========================================")
    print(" PersonalServer Scheduler Evaluation")
    print("==========================================")
    print(f"Requirements: {json.dumps(reqs, indent=2) if reqs else 'None (Default Resource-Aware)'}")
    print()

    decision = ResourceScheduler.select_node(reqs, nodes_db)

    print(f"Selected Node: {decision['selected_node'] or 'NONE'}")
    print(f"Score:         {decision['score']}/100")
    print(f"Reason:        {decision['reason']}")
    print()

    print("Candidates:")
    for c in decision["candidates"]:
        print(f"  * {c['node_id']} ({c['name']}) -> Score: {c['score']}/100 [{c['reason']}]")
    if not decision["candidates"]:
        print("  (None)")
    print()

    print("Rejected Nodes:")
    for nid, r in decision["rejected"].items():
        print(f"  * {nid}: {r}")
    if not decision["rejected"]:
        print("  (None)")
    print("==========================================")
    return 0


def submit_job(target_or_auto, job_type, name=None, timeout=60, params_json=None, reqs_json=None, max_attempts=3):
    nodes_db = load_nodes_db()

    if job_type not in ALLOWLISTED_WORKLOADS:
        print(f"Error: Job type '{job_type}' is not allowlisted.", file=sys.stderr)
        print(f"Allowed types: {list(ALLOWLISTED_WORKLOADS.keys())}")
        return 1

    params = {}
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception as e:
            print(f"Error parsing params JSON: {e}", file=sys.stderr)
            return 1

    reqs = {}
    if reqs_json:
        try:
            reqs = json.loads(reqs_json)
        except Exception as e:
            print(f"Error parsing requirements JSON: {e}", file=sys.stderr)
            return 1

    target_node = None
    scheduler_info = None

    if not target_or_auto or target_or_auto.lower() == "auto":
        decision = ResourceScheduler.select_node(reqs, nodes_db)
        if not decision["selected_node"]:
            print(f"Scheduling Error: {decision['reason']}", file=sys.stderr)
            return 1
        target_node = decision["selected_node"]
        scheduler_info = {
            "mode": "resource-aware",
            "selected_node": target_node,
            "score": decision["score"],
            "reason": decision["reason"]
        }
    else:
        target_node = target_or_auto
        node = nodes_db.get("nodes", {}).get(target_node)
        if not node:
            print(f"Error: Node '{target_node}' not found in cluster.", file=sys.stderr)
            return 1
        if node.get("status") == STATE_REMOVED:
            print(f"Error: Node '{target_node}' is removed from cluster.", file=sys.stderr)
            return 1
        live_status = compute_node_liveness(node)
        if live_status == STATE_OFFLINE:
            print(f"Error: Node '{target_node}' is OFFLINE. Cannot submit job.", file=sys.stderr)
            return 1
        scheduler_info = {"mode": "explicit", "target_node": target_node, "reason": "Explicit node"}

    job_id = f"job-{secrets.token_hex(6)}"
    now = get_current_iso_timestamp()

    job_record = {
        "id": job_id,
        "job_id": job_id,
        "name": name or job_type,
        "type": job_type,
        "parameters": params,
        "requirements": reqs,
        "target": target_or_auto or "auto",
        "target_node": target_node,
        "assigned_node": target_node,
        "scheduler": scheduler_info,
        "timeout": int(timeout),
        "max_attempts": int(max_attempts),
        "attempt": 1,
        "claimed_at": None,
        "lease_expires_at": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "status": JOB_STATE_QUEUED,
        "state": JOB_STATE_QUEUED,
        "retry_reason": None,
        "attempts_history": [],
        "result": None
    }

    jobs_db = load_jobs_db()
    jobs_db.setdefault("jobs", {})[job_id] = job_record
    save_jobs_db(jobs_db)

    print("==========================================")
    print(" PersonalServer Job Submitted")
    print("==========================================")
    print(f"Job ID:          {job_id}")
    print(f"Type:            {job_type}")
    print(f"Selected Target: {target_node}")
    print(f"Schedule Mode:   {scheduler_info['mode']}")
    print(f"Reason:          {scheduler_info['reason']}")
    print(f"Status:          {JOB_STATE_QUEUED}")
    print(f"Max Attempts:    {max_attempts}")
    print(f"Timeout:         {timeout}s")
    print("==========================================")
    return 0


def list_jobs(filter_node=None, filter_status=None):
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db)

    jobs = list(jobs_db.get("jobs", {}).values())

    if filter_node:
        jobs = [j for j in jobs if j.get("target_node") == filter_node]
    if filter_status:
        jobs = [j for j in jobs if j.get("status") == filter_status.upper()]

    print("==========================================")
    print(f" PersonalServer Jobs ({len(jobs)})")
    print("==========================================")
    if not jobs:
        print("No jobs found.")
    else:
        for j in reversed(jobs):
            res_summary = ""
            if j.get("result"):
                res_summary = f"| Exit: {j['result'].get('exit_code')} | Duration: {j['result'].get('duration_ms')}ms"
            mode = j.get("scheduler", {}).get("mode", "explicit")
            attempt_str = f"Attempt {j.get('attempt', 1)}/{j.get('max_attempts', 3)}"
            print(f"Job ID:      {j.get('job_id')}")
            print(f"  Type:      {j.get('type')} ({attempt_str})")
            print(f"  Target:    {j.get('target_node')} ({mode})")
            print(f"  Status:    {j.get('status')} {res_summary}")
            if j.get("retry_reason"):
                print(f"  Retry Info:{j.get('retry_reason')}")
            print(f"  Created:   {j.get('created_at')}")
            print()
    print("==========================================")


def show_job_details(job_id):
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db)

    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        print(f"Error: Job '{job_id}' not found.", file=sys.stderr)
        return 1

    print("==========================================")
    print(f" Job Details: {job_id}")
    print("==========================================")
    print(json.dumps(job, indent=2))
    print("==========================================")
    return 0


def cancel_job(job_id):
    jobs_db = load_jobs_db()
    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        print(f"Error: Job '{job_id}' not found.", file=sys.stderr)
        return 1

    if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED):
        print(f"Error: Job '{job_id}' is already in finished state '{job.get('status')}'.", file=sys.stderr)
        return 1

    job["status"] = JOB_STATE_CANCELLED
    job["state"] = JOB_STATE_CANCELLED
    job["finished_at"] = get_current_iso_timestamp()
    job["lease_expires_at"] = None
    save_jobs_db(jobs_db)
    print(f"Job '{job_id}' has been marked as CANCELLED.")
    return 0


def list_nodes(filter_status=None):
    nodes_db = load_nodes_db()
    summary = get_cluster_summary(nodes_db)
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
            last_hb = node.get("last_heartbeat", {}).get("system", {})
            mem_info = last_hb.get("memory", f"{res.get('ram_mb', 'N/A')} MB")

            print(f"Node ID:      {nid}")
            print(f"  Name:       {name}")
            print(f"  Role:       {role}")
            print(f"  Status:     {status}")
            print(f"  Platform:   {platform}")
            print(f"  Resources:  {res.get('cpu_cores', 'N/A')} cores | RAM: {mem_info}")
            print(f"  Caps:       {caps}")
            print(f"  Last Seen:  {last_seen}")
            print()
    print("==========================================")


def show_node_details(node_id):
    nodes_db = load_nodes_db()
    node = nodes_db.get("nodes", {}).get(node_id)
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
    nodes_db = load_nodes_db()
    summary = get_cluster_summary(nodes_db)
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
    nodes_db = load_nodes_db()
    node = nodes_db.get("nodes", {}).get(node_id)
    if not node:
        print(f"Error: Node '{node_id}' not found.", file=sys.stderr)
        return 1

    node["status"] = STATE_REMOVED
    node["auth_token"] = None
    node["removed_at"] = get_current_iso_timestamp()
    save_nodes_db(nodes_db)

    jobs_db = load_jobs_db()
    sweep_expired_leases(jobs_db, nodes_db)

    print(f"Node '{node_id}' has been removed from active cluster membership.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="controller",
        description="PersonalServer Cluster Controller & Resource Scheduler (v1.0)"
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

    # submit command
    p_submit = subparsers.add_parser("submit", help="Submit a workload job to an explicit node or 'auto'")
    p_submit.add_argument("--node", default="auto", help="Target Node ID or 'auto' for scheduler (default: auto)")
    p_submit.add_argument("--type", required=True, choices=list(ALLOWLISTED_WORKLOADS.keys()), help="Allowlisted workload type")
    p_submit.add_argument("--name", help="Optional friendly name for the job")
    p_submit.add_argument("--timeout", type=int, default=60, help="Timeout in seconds (default 60)")
    p_submit.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts (default 3)")
    p_submit.add_argument("--params", help="JSON string of parameters")
    p_submit.add_argument("--requirements", help="JSON string of scheduler requirements")

    # schedule-test command
    p_sched = subparsers.add_parser("schedule-test", help="Test scheduler evaluation against current cluster")
    p_sched.add_argument("--requirements", help="JSON string of scheduler requirements")

    # jobs command
    p_jobs = subparsers.add_parser("jobs", help="List jobs")
    p_jobs.add_argument("--node", help="Filter jobs by target node ID")
    p_jobs.add_argument("--status", choices=[JOB_STATE_QUEUED, JOB_STATE_CLAIMED, JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED, JOB_STATE_RECOVERING, JOB_STATE_REJECTED], help="Filter jobs by status")

    # job command
    p_job = subparsers.add_parser("job", help="Inspect single job details")
    p_job.add_argument("job_id", help="Job ID to inspect")

    # cancel command
    p_cancel = subparsers.add_parser("cancel", help="Cancel a queued or running job")
    p_cancel.add_argument("job_id", help="Job ID to cancel")

    # token command
    subparsers.add_parser("token", help="Display enrollment token file path")

    # onboard-code command
    p_onboard = subparsers.add_parser("onboard-code", help="Generate a short-lived single-use node onboarding code")
    p_onboard.add_argument("--ttl", type=int, default=DEFAULT_ONBOARDING_TTL_SEC, help="Time to live in seconds (default: 900 / 15m)")

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
    elif args.command == "schedule-test":
        sys.exit(schedule_test(args.requirements))
    elif args.command == "submit":
        sys.exit(submit_job(args.node, args.type, args.name, args.timeout, args.params, args.requirements, getattr(args, "max_attempts", 3)))
    elif args.command == "jobs":
        list_jobs(getattr(args, "node", None), getattr(args, "status", None))
    elif args.command == "job":
        sys.exit(show_job_details(args.job_id))
    elif args.command == "cancel":
        sys.exit(cancel_job(args.job_id))
    elif args.command == "token":
        get_or_create_enrollment_token()
        print(f"Enrollment token stored at: {ENROLLMENT_TOKEN_FILE}")
    elif args.command == "onboard-code":
        ttl = getattr(args, "ttl", DEFAULT_ONBOARDING_TTL_SEC)
        code, ttl_sec = generate_onboarding_code(ttl)
        minutes = ttl_sec // 60
        print("==========================================")
        print(f"Onboarding code: {code}")
        print(f"Expires in:      {minutes} minutes")
        print("==========================================")


if __name__ == "__main__":
    main()
