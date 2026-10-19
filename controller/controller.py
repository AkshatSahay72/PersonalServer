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
import math
import hmac
import shutil
import threading
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from common.time_utils import get_current_iso_timestamp
from common.crypto_utils import constant_time_compare
from common.storage_utils import resolve_safe_path, validate_app_id
from controller.stores import (
    DB_LOCK,
    ONBOARDING_LOCK,
    APPS_LOCK,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    load_apps_db,
    save_apps_db,
    load_onboarding_codes_db,
    save_onboarding_codes_db,
    set_state_version_bumper,
)

# Import ResourceScheduler
from scheduler.scheduler import ResourceScheduler, compute_node_liveness, extract_node_telemetry
from config.platform_config import (
    get_platform_config,
    get_controller_paths,
    get_controller_runtime_config,
    redact_sensitive_dict
)
from config.structured_logger import (
    controller_logger,
    extract_correlation_id,
    generate_correlation_id,
    redact_sensitive_text,
    redact_sensitive_data
)
try:
    from controller.image_inspector import (
        validate_image_reference,
        detect_image_architectures,
        detect_application_port,
        is_architecture_compatible,
        normalize_architecture
    )
    from controller.env_manager import (
        parse_env_file_content,
        sanitize_env_vars_input,
        is_secret_variable
    )
except (ImportError, ModuleNotFoundError):
    from image_inspector import (
        validate_image_reference,
        detect_image_architectures,
        detect_application_port,
        is_architecture_compatible,
        normalize_architecture
    )
    from env_manager import (
        parse_env_file_content,
        sanitize_env_vars_input,
        is_secret_variable
    )

PATHS = get_controller_paths()
CONFIG_DIR = PATHS.config_dir
SECRETS_DIR = PATHS.secrets_dir
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = PATHS.data_dir

NODES_FILE = PATHS.nodes_file
BACKUP_NODES_FILE = PATHS.backup_nodes_file
JOBS_FILE = PATHS.jobs_file
BACKUP_JOBS_FILE = PATHS.backup_jobs_file
APPS_FILE = PATHS.apps_file
BACKUP_APPS_FILE = PATHS.backup_apps_file
ONBOARDING_CODES_FILE = PATHS.onboarding_codes_file
BACKUP_ONBOARDING_CODES_FILE = PATHS.backup_onboarding_codes_file
ENROLLMENT_TOKEN_FILE = PATHS.enrollment_token_file

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HOST = RUNTIME_CFG.host
DEFAULT_PORT = RUNTIME_CFG.port
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout  # seconds
DEFAULT_LEASE_GRACE_SEC = RUNTIME_CFG.lease_grace_sec    # seconds beyond job timeout
DEFAULT_ONBOARDING_TTL_SEC = RUNTIME_CFG.onboarding_ttl_sec # 15 minutes (seconds)

# ------------------------------------------------------------------------------
# Modular Controller Subsystems (Constants, HA, Blueprints, Onboarding, Nodes, Sweeper)
# ------------------------------------------------------------------------------
from controller.constants import (
    STATE_REGISTERING,
    STATE_ONLINE,
    STATE_DRAINING,
    STATE_DEBOARDING,
    STATE_OFFLINE,
    STATE_UNHEALTHY,
    STATE_UNKNOWN,
    STATE_REMOVED,
    VALID_ROLES,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_CANCELLED,
    JOB_STATE_RECOVERING,
    JOB_STATE_REJECTED,
    VALID_JOB_TRANSITIONS,
    validate_job_transition,
    APP_STATE_CREATED,
    APP_STATE_FETCHING_SOURCE,
    APP_STATE_CONFIGURING,
    APP_STATE_BUILDING,
    APP_STATE_SCHEDULING,
    APP_STATE_DEPLOYING,
    APP_STATE_RUNNING,
    APP_STATE_STOPPED,
    APP_STATE_FAILED,
    APP_STATE_REMOVING,
    PORT_RANGE_START,
    PORT_RANGE_END,
    ALLOWLISTED_WORKLOADS,
)
from controller.ha_helper import (
    HA_MANAGER,
    _bump_ha_state_version,
    get_ha_manager,
    HAManager,
    HAState,
    LeaderLease,
    find_active_leader,
    resolve_controller_node_id,
)
from controller.blueprints import (
    RESERVED_ROUTES,
    APP_STORAGE_ROOT,
    resolve_safe_app_storage_path,
    allocate_host_port,
    validate_app_route,
    parse_yaml_or_json,
    parse_and_validate_blueprint,
    fetch_github_source,
    mask_app_record,
)
from controller.onboarding import (
    DEFAULT_ONBOARDING_TTL_SEC,
    hash_onboarding_code,
    get_or_create_enrollment_token,
    generate_onboarding_code,
    validate_and_consume_onboarding_code,
)
from controller.node_manager import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    sanitize_node_record,
    inspect_node_storage_safety,
    evaluate_node_deboarding_safety,
    get_cluster_summary,
    get_cluster_utilization,
)
from controller.lease_sweeper import (
    sweep_expired_leases,
    start_background_lease_sweeper,
)


def ensure_directories():
    PATHS.ensure_directories()


# ------------------------------------------------------------------------------
# HTTP Request Handler
# ------------------------------------------------------------------------------

class ControllerHandler(BaseHTTPRequestHandler):
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT

    def send_json(self, status_code, data):
        corr_id = getattr(self, "current_correlation_id", None)
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
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Correlation-ID, X-Request-ID")
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
        if constant_time_compare(token, enrollment_token):
            return True
        nodes_db = load_nodes_db()
        for node in nodes_db.get("nodes", {}).values():
            if node.get("auth_token") and constant_time_compare(node.get("auth_token"), token):
                return True
        return False

    def check_leader_write_permission(self) -> bool:
        """
        Guards cluster mutating routes (POST, PUT, DELETE).
        Rejects mutating operations when controller is in STANDBY or FENCED to prevent split-brain.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # Exempt HA election and heartbeat management routes
        if path in (
            "/ha/promote", "/ha/failover", "/ha/heartbeat", "/ha/renew",
            "/api/ha/promote", "/api/ha/failover", "/api/ha/heartbeat", "/api/ha/renew"
        ):
            return True

        mgr = get_ha_manager()
        if mgr.state == HAState.FENCED:
            self.send_json(503, {
                "error": "Leader lease expired. Controller is self-fenced to prevent split-brain.",
                "role": "fenced",
                "leader_id": mgr.current_leader_id,
                "leader_url": mgr.current_leader_url,
                "term": mgr.current_term,
                "lease_valid": False
            })
            return False

        if not mgr.is_leader:
            self.send_json(503, {
                "error": "Controller is in STANDBY mode. Mutating operations are rejected.",
                "role": mgr.state,
                "leader_id": mgr.current_leader_id,
                "leader_url": mgr.current_leader_url,
                "term": mgr.current_term,
                "lease_valid": mgr.current_lease.is_valid()
            })
            return False

        if not mgr.current_lease.is_valid():
            if mgr.state == HAState.LEADER and (not mgr.current_leader_id or mgr.current_leader_id == mgr.node_id):
                mgr.renew_lease()
            else:
                self.send_json(503, {
                    "error": "Leader lease expired. Controller is self-fenced to prevent split-brain.",
                    "role": "fenced",
                    "leader_id": mgr.current_leader_id,
                    "leader_url": mgr.current_leader_url,
                    "term": mgr.current_term,
                    "lease_valid": False
                })
                return False

        return True

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
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        query = urllib.parse.parse_qs(parsed.query)

        # GET /health or GET /status
        if path in ("/health", "/status"):
            nodes_db = load_nodes_db()
            jobs_db = load_jobs_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            jobs = jobs_db.get("jobs", {})
            job_states = {}
            for j in jobs.values():
                st = j.get("status", "UNKNOWN")
                job_states[st] = job_states.get(st, 0) + 1
            recent_errors = len(controller_logger.get_recent_events(limit=50, level="ERROR"))
            self.send_json(200, {
                "status": "online",
                "service": "PersonalServer Controller",
                "cluster": summary["cluster"],
                "total_jobs": len(jobs),
                "job_states": job_states,
                "recent_errors": recent_errors,
                "timestamp": get_current_iso_timestamp()
            })
            return

        # GET /ha/status or GET /api/ha/status
        if path in ("/ha/status", "/api/ha/status"):
            mgr = get_ha_manager()
            self.send_json(200, mgr.get_status_dict())
            return

        # GET /ha/leader or GET /api/ha/leader
        if path in ("/ha/leader", "/api/ha/leader"):
            mgr = get_ha_manager()
            leader_id = mgr.current_leader_id
            leader_name, leader_online, leader_registered = mgr.resolve_leader_name(leader_id) if hasattr(mgr, "resolve_leader_name") else (leader_id, False, False)
            self.send_json(200, {
                "leader_id": leader_id,
                "leader_name": leader_name,
                "leader_online": leader_online,
                "leader_registered": leader_registered,
                "leader_url": mgr.current_leader_url,
                "term": mgr.current_term,
                "state_version": mgr.state_version,
                "is_leader": mgr.is_leader,
                "is_valid": mgr.current_lease.is_valid(),
                "time_remaining_sec": round(mgr.current_lease.time_remaining_sec(), 1)
            })
            return

        # GET /ha/sync or GET /api/ha/sync
        if path in ("/ha/sync", "/api/ha/sync"):
            token = self.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required for HA state sync"})
                return
            mgr = get_ha_manager()
            snapshot = mgr.export_state_snapshot()
            self.send_json(200, snapshot)
            return

        # GET /diagnostics
        if path == "/diagnostics":
            token = self.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required for diagnostics"})
                return
            limit = int(query.get("limit", [50])[0])
            req_event = query.get("event", [None])[0]
            req_corr = query.get("correlation_id", [None])[0]
            req_job = query.get("job_id", [None])[0]
            req_level = query.get("level", [None])[0]
            events = controller_logger.get_recent_events(
                limit=min(limit, 100),
                event=req_event,
                correlation_id=req_corr,
                job_id=req_job,
                level=req_level
            )
            self.send_json(200, {
                "subsystem": "Controller",
                "events_count": len(events),
                "events": events
            })
            return

        # GET /platform or GET /api/platform
        if path in ("/platform", "/api/platform"):
            self.send_json(200, get_platform_config().to_dict())
            return

        # GET /cluster/utilization
        if path == "/cluster/utilization":
            nodes_db = load_nodes_db()
            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)
            util = get_cluster_utilization(nodes_db, jobs_db, self.heartbeat_timeout)
            self.send_json(200, util)
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
            if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            jobs_db = load_jobs_db()
            # Perform sweep before claiming
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            now_epoch = time.time()
            # Find jobs waiting for this node (respecting retry_after backoff)
            claimable_jobs = [
                j for j in jobs_db.get("jobs", {}).values()
                if j.get("target_node") == node_id and (
                    j.get("status") == JOB_STATE_QUEUED
                    or (
                        j.get("status") == JOB_STATE_RECOVERING
                        and now_epoch >= j.get("retry_after", 0)
                    )
                )
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

            corr_id = next_job.get("correlation_id") or getattr(self, "current_correlation_id", None) or generate_correlation_id()
            next_job["correlation_id"] = corr_id
            controller_logger.info(
                "job.claimed",
                f"Job {next_job['job_id']} ({next_job['type']}) CLAIMED by node '{node_id}' (Attempt {next_job.get('attempt', 1)}, Lease {lease_duration}s)",
                correlation_id=corr_id,
                job_id=next_job["job_id"],
                node_id=node_id,
                attempt=next_job.get("attempt", 1),
                lease_duration_sec=lease_duration
            )
            self.send_json(200, {"job": next_job})
            return

        # GET /nodes/<node_id>/deboard
        if path.startswith("/nodes/") and path.endswith("/deboard"):
            node_id = path[7:-8].strip()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            self.send_json(200, eval_res)
            return

        # GET /nodes/<node_id>/diagnostics or /api/nodes/<node_id>/diagnostics
        norm_path = path.rstrip("/")
        is_diag = False
        diag_node_id = None
        if norm_path.endswith("/diagnostics"):
            if norm_path.startswith("/api/nodes/"):
                diag_node_id = norm_path[11:-12].strip("/")
                is_diag = True
            elif norm_path.startswith("/nodes/"):
                diag_node_id = norm_path[7:-12].strip("/")
                is_diag = True

        if is_diag and diag_node_id:
            node_id = diag_node_id
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))
            if is_local:
                try:
                    from config.node_telemetry import run_live_diagnostics
                    diag_res = run_live_diagnostics()
                    self.send_json(200, {"node_id": node_id, "diagnostics": diag_res})
                    return
                except Exception as e:
                    self.send_json(500, {"error": f"Local diagnostics error: {e}"})
                    return

            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            node_status = (node.get("status") or "").upper()
            hb = node.get("last_heartbeat", {}) if node else {}
            hb_sys = hb.get("system", {})
            bat = hb.get("battery") or hb_sys.get("battery") or {}
            net = hb.get("network") or hb_sys.get("network") or {}
            cpu = hb.get("cpu_details") or hb_sys.get("cpu_details") or {}
            thm = hb.get("thermals") or hb_sys.get("thermals") or {}

            if node_status == "OFFLINE":
                self.send_json(200, {
                    "node_id": node_id,
                    "diagnostics": {
                        "status": "offline",
                        "live": False,
                        "duration_ms": 0,
                        "error": f"Node '{node_id}' is OFFLINE. Cannot run live diagnostics.",
                        "timestamp": node.get("last_seen"),
                        "battery": bat,
                        "network": {
                            **net,
                            "status": "offline",
                            "ip": node.get("ip"),
                            "ping_ms": None,
                            "transfer_speed_mbps": 0
                        },
                        "cpu": cpu,
                        "thermals": thm
                    }
                })
                return

            node_ip = node.get("ip") if node else None
            if node_ip and node_ip not in ("127.0.0.1", "localhost", "::1"):
                try:
                    req = urllib.request.Request(f"http://{node_ip}:8080/diagnostics", headers={"User-Agent": "PersonalServer-Controller/1.0"})
                    with urllib.request.urlopen(req, timeout=2.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        diag_payload = data.get("diagnostics", data)
                        diag_payload["live"] = True
                        self.send_json(200, {"node_id": node_id, "diagnostics": diag_payload})
                        return
                except Exception:
                    pass

            ping_ms = None
            if node_ip:
                try:
                    from config.node_telemetry import _measure_ping_latency_ms
                    ping_ms = _measure_ping_latency_ms(node_ip, 8080, timeout=1.0)
                except Exception:
                    pass

            diag_obj = {
                "status": "cached" if ping_ms is not None else "unreachable",
                "live": False,
                "error": None if ping_ms is not None else f"Live telemetry unreachable on {node_ip or 'node'}:8080. Returning cached heartbeat metrics.",
                "timestamp": hb.get("timestamp") or (node.get("last_seen") if node else None),
                "battery": bat,
                "network": {
                    **net,
                    "ip": node_ip or net.get("ip"),
                    "ping_ms": ping_ms if ping_ms is not None else net.get("ping_ms")
                },
                "cpu": cpu,
                "thermals": thm
            }
            self.send_json(200, {"node_id": node_id, "diagnostics": diag_obj})
            return

        # GET /nodes/<node_id>
        if norm_path.startswith("/nodes/") or norm_path.startswith("/api/nodes/"):
            prefix_len = 11 if norm_path.startswith("/api/nodes/") else 7
            rest = norm_path[prefix_len:].strip("/")
            if "/" not in rest and rest:
                node_id = rest
                nodes_db = load_nodes_db()
                node = nodes_db.get("nodes", {}).get(node_id)
                if not node:
                    self.send_json(404, {"error": f"Node '{node_id}' not found"})
                    return
                s_node = sanitize_node_record(node, self.heartbeat_timeout)
                s_node["deboarding"] = evaluate_node_deboarding_safety(node_id, nodes_db)
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
            filter_type = query.get("type", [None])[0]
            search_q = query.get("search", query.get("q", [None]))[0]
            sort_by = query.get("sort", ["recent"])[0]

            if filter_node and filter_node != "all":
                all_jobs = [j for j in all_jobs if j.get("target_node") == filter_node or j.get("assigned_node") == filter_node]
            if filter_status and filter_status != "all":
                all_jobs = [j for j in all_jobs if j.get("status") == filter_status.upper()]
            if filter_type and filter_type != "all":
                all_jobs = [j for j in all_jobs if j.get("type") == filter_type]
            if search_q:
                q_lower = search_q.strip().lower()
                all_jobs = [
                    j for j in all_jobs
                    if q_lower in (j.get("job_id") or "").lower()
                    or q_lower in (j.get("type") or "").lower()
                    or q_lower in (j.get("target_node") or "").lower()
                    or q_lower in (j.get("assigned_node") or "").lower()
                ]

            # Sorting: default recent first
            if sort_by == "oldest":
                all_jobs.sort(key=lambda j: j.get("created_at") or j.get("submitted_at") or "")
            elif sort_by == "duration":
                all_jobs.sort(key=lambda j: (j.get("result", {}) or {}).get("duration_ms") or j.get("execution_duration_sec") or 0, reverse=True)
            else:
                all_jobs.sort(key=lambda j: j.get("created_at") or j.get("submitted_at") or "", reverse=True)

            total_count = len(all_jobs)

            # Optional server-side pagination: limit, page, offset
            limit_str = query.get("limit", query.get("page_size", [None]))[0]
            page_str = query.get("page", [None])[0]
            offset_str = query.get("offset", [None])[0]

            if limit_str is not None or page_str is not None or offset_str is not None:
                limit = max(1, min(100, int(limit_str) if limit_str else 25))
                if offset_str is not None:
                    offset = max(0, int(offset_str))
                    page = (offset // limit) + 1
                elif page_str is not None:
                    page = max(1, int(page_str))
                    offset = (page - 1) * limit
                else:
                    offset = 0
                    page = 1

                paginated_jobs = all_jobs[offset : offset + limit]
                total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1

                self.send_json(200, {
                    "jobs": paginated_jobs,
                    "count": len(paginated_jobs),
                    "total_count": total_count,
                    "page": page,
                    "page_size": limit,
                    "total_pages": total_pages
                })
                return

            self.send_json(200, {
                "jobs": all_jobs,
                "count": len(all_jobs),
                "total_count": total_count
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
            diagnostics = {
                "attempts_total": len(job.get("attempts_history", [])),
                "current_attempt": job.get("attempt", 1),
                "is_active": job.get("status") in (JOB_STATE_QUEUED, JOB_STATE_CLAIMED, JOB_STATE_RUNNING, JOB_STATE_RECOVERING),
                "last_failure_reason": job.get("retry_reason"),
                "correlation_id": job.get("correlation_id"),
                "lease_active": bool(job.get("lease_expires_at") and time.time() < job.get("lease_expires_at")),
            }
            self.send_json(200, {"job": job, "diagnostics": diagnostics})
            return

        # GET /apps
        if path == "/apps":
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            apps_db = load_apps_db()
            apps_list = [mask_app_record(app) for app in apps_db.get("apps", {}).values() if app.get("status") != "DELETED"]
            self.send_json(200, {
                "apps": apps_list,
                "count": len(apps_list)
            })
            return

        # GET /apps/<app_id>/deployments
        if path.startswith("/apps/") and path.endswith("/deployments"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-12].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            deployments = app.get("deployments", [])
            self.send_json(200, {
                "app_id": app_id,
                "deployments": deployments,
                "count": len(deployments)
            })
            return

        # GET /apps/<app_id>/env
        if path.startswith("/apps/") and path.endswith("/env"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-4].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            masked_app = mask_app_record(app)
            self.send_json(200, {
                "app_id": app_id,
                "env_vars": masked_app.get("env_vars", {}),
                "env": masked_app.get("env", {})
            })
            return

        # GET /apps/<app_id>
        if path.startswith("/apps/") and not any(path.endswith(s) for s in ("/logs", "/files", "/env", "/deployments")) and "/" not in path[6:]:
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            self.send_json(200, {
                "app": mask_app_record(app)
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

        # GET /apps/<app_id>/files or /apps/<app_id>/files/download
        if path.startswith("/apps/") and "/files" in path:
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            
            is_download = path.endswith("/files/download")
            parts = path.split("/")
            app_id = parts[2] if len(parts) > 2 else ""
            
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            req_path = query.get("path", [""])[0]
            try:
                target_path, app_base = resolve_safe_app_storage_path(app_id, req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_path.exists():
                self.send_json(404, {"error": f"Path not found: {req_path}"})
                return

            if is_download:
                if not target_path.is_file():
                    self.send_json(400, {"error": "Target is not a file"})
                    return
                file_size = target_path.stat().st_size
                filename = target_path.name
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                with open(target_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return

            if not target_path.is_dir():
                self.send_json(400, {"error": f"Path is a file, not a directory: {req_path}"})
                return

            items = []
            try:
                with os.scandir(target_path) as entries:
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

            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            rel_display = str(target_path.relative_to(app_base)).replace("\\", "/")
            if rel_display == ".":
                rel_display = ""

            self.send_json(200, {
                "status": "ok",
                "app_id": app_id,
                "name": app.get("name"),
                "path": rel_display,
                "items": items,
                "count": len(items)
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
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        query = urllib.parse.parse_qs(parsed.query)
        body = self.read_json_body()
        if body and isinstance(body, dict):
            self.current_correlation_id = extract_correlation_id(self.headers, body)
        if body is None:
            # Endpoints that do not strictly require a request body
            no_body_endpoints = (
                path in ("/ha/heartbeat", "/ha/renew", "/ha/failover", "/ha/promote"),
                (path.startswith("/nodes/") and path.endswith("/remove")),
                (path.startswith("/nodes/") and path.endswith("/drain")),
                (path.startswith("/nodes/") and path.endswith("/deboard")),
                (path.startswith("/nodes/") and path.endswith("/resume")),
                (path.startswith("/jobs/") and path.endswith("/cancel")),
                (path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/stop") or path.endswith("/restart") or path.endswith("/rollback")))
            )
            if not any(no_body_endpoints):
                self.send_json(400, {"error": "Invalid or missing JSON payload"})
                return
            body = {}

        token = self.parse_auth_token()
        if not token and body:
            token = body.get("enrollment_token") or body.get("auth_token") or ""

        # ----------------------------------------------------------------------
        # High Availability Endpoints: POST /ha/...
        # ----------------------------------------------------------------------
        if path in ("/ha/heartbeat", "/ha/renew"):
            token = self.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required for HA heartbeat"})
                return
            mgr = get_ha_manager()
            standby_id = body.get("node_id") if body else None
            standby_url = body.get("node_url") if body else None
            standby_version = body.get("state_version", 0) if body else 0
            if standby_id and standby_url:
                mgr.register_standby_heartbeat(standby_id, standby_url, standby_version)
            self.send_json(200, {
                "status": "ok",
                "role": mgr.state,
                "term": mgr.current_term,
                "state_version": mgr.state_version,
                "leader_id": mgr.current_leader_id,
                "leader_url": mgr.current_leader_url,
                "lease_remaining_sec": round(mgr.current_lease.time_remaining_sec(), 1)
            })
            return

        if path == "/ha/failover":
            token = self.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required for HA failover"})
                return
            mgr = get_ha_manager()
            target_url = body.get("target_url") if body else None
            result = mgr.manual_failover(target_node_url=target_url)
            status_code = 200 if result.get("status") == "success" else 400
            self.send_json(status_code, result)
            return

        if path == "/ha/promote":
            token = self.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required for HA promote"})
                return
            mgr = get_ha_manager()
            reason = body.get("reason", "API promotion request") if body else "API promotion request"
            mgr.elect_self(reason=reason)
            self.send_json(200, {
                "status": "promoted",
                "node_id": mgr.node_id,
                "role": mgr.state,
                "term": mgr.current_term,
                "state_version": mgr.state_version
            })
            return

        # ----------------------------------------------------------------------
        # Leader Lease & Standby Write-Gating Check
        # Standby and fenced controllers reject mutations to prevent split-brain writes.
        # ----------------------------------------------------------------------
        if not self.check_leader_write_permission():
            return

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
                if not token or not constant_time_compare(token, enrollment_token):
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
                "ip": (body.get("ip") or body.get("tailscale_ip") or (self.client_address[0] if (self.client_address and self.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0")) else None)),
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

            controller_logger.info(
                "node.registered",
                f"Registered node: {node_id} ({node_name}, role: {node_role})",
                correlation_id=self.current_correlation_id,
                node_id=node_id,
                role=node_role,
                platform=platform,
                architecture=arch,
                ip=node_record.get("ip")
            )

            self.send_json(200, {
                "status": "registered",
                "node_id": node_id,
                "auth_token": node_auth_token,
                "registered_at": now,
                "message": f"Node '{node_id}' successfully registered in cluster '{nodes_db.get('cluster_name')}'."
            })
            return

        # ----------------------------------------------------------------------
        # 2. Node Heartbeat: POST /heartbeat or POST /nodes/<node_id>/heartbeat
        # ----------------------------------------------------------------------
        if path == "/heartbeat" or (path.startswith("/nodes/") and path.endswith("/heartbeat")):
            node_id = body.get("node_id")
            if not node_id and path.startswith("/nodes/"):
                node_id = path[7:-10].strip()
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

            if not token or (not constant_time_compare(token, expected_auth_token) and not constant_time_compare(token, enrollment_token)):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            now = get_current_iso_timestamp()
            current_st = node_entry.get("status")
            if current_st not in (STATE_DRAINING, STATE_DEBOARDING):
                node_entry["status"] = STATE_ONLINE
            node_entry["last_seen"] = now
            reported_ip = body.get("ip") or body.get("tailscale_ip")
            if reported_ip:
                node_entry["ip"] = reported_ip
            elif self.client_address and self.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0"):
                node_entry["ip"] = self.client_address[0]
            elif not node_entry.get("ip"):
                node_entry["ip"] = self.client_address[0] if self.client_address else "127.0.0.1"

            if "capabilities" in body and isinstance(body["capabilities"], dict):
                node_entry.setdefault("capabilities", {}).update(body["capabilities"])

            node_entry["last_heartbeat"] = {
                "timestamp": body.get("timestamp", now),
                "services": body.get("services", {}),
                "system": body.get("system", {}),
                "workloads": body.get("workloads", {}),
                "battery": body.get("battery") or body.get("system", {}).get("battery", {}),
                "network": body.get("network") or body.get("system", {}).get("network", {}),
                "cpu_details": body.get("cpu_details") or body.get("system", {}).get("cpu_details", {}),
                "thermals": body.get("thermals") or body.get("system", {}).get("thermals", {})
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
            if token and not constant_time_compare(token, enrollment_token):
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

                if live_status in (STATE_DRAINING, STATE_DEBOARDING) or node_entry.get("status") in (STATE_DRAINING, STATE_DEBOARDING):
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' is currently {live_status or node_entry.get('status')} (no new workloads accepted). Cannot submit job.",
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
                "correlation_id": self.current_correlation_id,
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
                "retryable": bool(body.get("retryable", True)),
                "backoff_sec": body.get("backoff_sec"),
                "retry_reason": None,
                "attempts_history": [],
                "result": None
            }

            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

            controller_logger.info(
                "job.scheduled",
                f"Queued job {job_id} ({job_type}) for target node {target_node} [mode={scheduler_info['mode']}]",
                correlation_id=self.current_correlation_id,
                job_id=job_id,
                node_id=target_node,
                job_type=job_type,
                mode=scheduler_info["mode"],
                score=scheduler_info.get("score"),
                reason=scheduler_info.get("reason")
            )
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
            if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
                return

            # Stale worker protection: check attempt number if provided
            reported_attempt = body.get("attempt")
            current_attempt = job.get("attempt", 1)
            corr_id = job.get("correlation_id") or getattr(self, "current_correlation_id", None) or generate_correlation_id()
            if reported_attempt is not None and int(reported_attempt) < current_attempt:
                controller_logger.warning(
                    "worker.stale_attempt",
                    f"Stale worker result for job '{job_id}': reported attempt {reported_attempt} < current attempt {current_attempt}",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=target_node,
                    reported_attempt=reported_attempt,
                    current_attempt=current_attempt
                )
                self.send_json(409, {
                    "error": f"Stale worker result: reported attempt {reported_attempt} is older than current attempt {current_attempt}.",
                    "job_id": job_id,
                    "current_attempt": current_attempt,
                    "stale_attempt": True
                })
                return

            # Idempotency / Stale report protection: Ignore duplicate reports if job is already terminal
            if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED) or (job.get("status") == JOB_STATE_FAILED and job.get("result") is not None and current_attempt >= job.get("max_attempts", 3)):
                self.send_json(200, {
                    "status": job.get("status"),
                    "message": f"Job '{job_id}' has already reached terminal state '{job.get('status')}'. Duplicate report ignored.",
                    "job_id": job_id,
                    "duplicate": True
                })
                return

            job_status = body.get("status", JOB_STATE_FAILED)
            now_iso = get_current_iso_timestamp()
            now_epoch = time.time()

            # Record attempt
            attempt = current_attempt
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

            # Bounded output logs (64KB maximum)
            stdout_bounded = redact_sensitive_text(str(body.get("stdout", ""))[:65536])
            stderr_bounded = redact_sensitive_text(str(body.get("stderr", ""))[:65536])

            # Check for failure retry
            is_retryable = body.get("retryable", True) and (job.get("retryable", True) is not False)
            if job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT) and attempt < max_attempts and is_retryable:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["state"] = JOB_STATE_RECOVERING
                job["retry_reason"] = f"Attempt {attempt} completed with status: {job_status}"
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                # Compute backoff
                base_backoff = float(job.get("backoff_sec") if job.get("backoff_sec") is not None else getattr(ControllerHandler, "default_backoff_sec", 0.0))
                backoff_sec = min(60.0, base_backoff * (2 ** (attempt - 1)))
                job["retry_after"] = now_epoch + backoff_sec
                job["retry_after_iso"] = datetime.fromtimestamp(now_epoch + backoff_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

                controller_logger.warning(
                    "worker.failed",
                    f"Job {job_id} failed on node {target_node} (attempt {attempt}/{max_attempts}): status={job_status}",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=target_node,
                    attempt=attempt,
                    status=job_status,
                    exit_code=body.get("exit_code", 0),
                    duration_ms=body.get("duration_ms", 0)
                )
                controller_logger.info(
                    "job.recovery_rescheduled",
                    f"Queued retry attempt {attempt+1}/{max_attempts} for job '{job_id}' on node '{job['target_node']}' (backoff {backoff_sec:.1f}s)",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=job["target_node"],
                    attempt=attempt+1,
                    backoff_sec=backoff_sec
                )

            else:
                # Terminal Success / Final Failure
                job["status"] = job_status
                job["state"] = job_status
                job["finished_at"] = now_iso
                job["lease_expires_at"] = None
                if not is_retryable and job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                    job["retry_reason"] = f"Non-retryable failure on attempt {attempt}: {body.get('stderr') or job_status}"
                job["result"] = {
                    "status": job_status,
                    "exit_code": body.get("exit_code", 0),
                    "stdout": stdout_bounded,
                    "stderr": stderr_bounded,
                    "duration_ms": body.get("duration_ms", 0),
                }
                if job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                    controller_logger.error(
                        "job.failed_terminal",
                        f"Job {job_id} failed permanently on node {target_node} after {attempt} attempt(s): {job.get('retry_reason')}",
                        correlation_id=corr_id,
                        job_id=job_id,
                        node_id=target_node,
                        status=job_status,
                        exit_code=body.get("exit_code", 0)
                    )
                else:
                    controller_logger.info(
                        "job.succeeded",
                        f"Job {job_id} SUCCEEDED on node {target_node} in {body.get('duration_ms', 0)}ms",
                        correlation_id=corr_id,
                        job_id=job_id,
                        node_id=target_node,
                        duration_ms=body.get("duration_ms", 0)
                    )
                job["result"] = {
                    "status": job_status,
                    "exit_code": body.get("exit_code", 0),
                    "stdout": stdout_bounded,
                    "stderr": stderr_bounded,
                    "duration_ms": body.get("duration_ms", 0),
                }

            # Update application state if this was a docker lifecycle job
            app_id = job.get("parameters", {}).get("app_id")
            if app_id:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if app:
                    if job.get("type") == "image-build":
                        if job_status == JOB_STATE_SUCCEEDED:
                            meta = body.get("metadata") or {}
                            img_ref = meta.get("image_ref") or job.get("parameters", {}).get("image_tag")
                            img_digest = meta.get("digest")
                            img_arch = meta.get("architecture") or "amd64"

                            app["image"] = img_ref
                            if img_digest:
                                app["image_digest"] = img_digest
                            app["architecture"] = img_arch
                            app["status"] = APP_STATE_DEPLOYING
                            app["updated_at"] = now_iso

                            if app.get("deployments"):
                                last_dep = app["deployments"][-1]
                                last_dep["image"] = img_ref
                                last_dep["digest"] = img_digest
                                last_dep["architecture"] = img_arch
                                last_dep["status"] = "DEPLOYING"

                            # Stage 5 & 6: Architecture compatibility check and worker scheduling
                            nodes_db = load_nodes_db()
                            reqs = {
                                "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"],
                                "supported_architectures": [img_arch]
                            }
                            decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                            if decision.get("selected_node"):
                                sel_node = decision["selected_node"]
                                app["selected_node"] = sel_node
                                deploy_job_id = f"job-{secrets.token_hex(6)}"
                                container_env = dict(app.get("env", {}))
                                for k, v in (app.get("env_vars") or {}).items():
                                    if isinstance(v, dict):
                                        container_env[k] = v.get("value", "")
                                    else:
                                        container_env[k] = str(v)

                                dep_job_record = {
                                    "id": deploy_job_id,
                                    "job_id": deploy_job_id,
                                    "name": f"deploy-{app['name']}",
                                    "type": "docker-deploy",
                                    "parameters": {
                                        "app_id": app_id,
                                        "image": img_ref,
                                        "image_digest": img_digest,
                                        "architecture": img_arch,
                                        "container_name": app.get("container_id") or f"ps-{app['name']}",
                                        "host_port": app["host_port"],
                                        "container_port": app.get("container_port", 8000),
                                        "env": container_env,
                                        "cpu_limit": app.get("cpu_limit", "0.5"),
                                        "memory_limit": app.get("memory_limit", "256m")
                                    },
                                    "requirements": reqs,
                                    "target": sel_node,
                                    "target_node": sel_node,
                                    "assigned_node": sel_node,
                                    "scheduler": {
                                        "mode": "resource-aware-docker",
                                        "selected_node": sel_node,
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
                                jobs_db.setdefault("jobs", {})[deploy_job_id] = dep_job_record
                                print(f"[CONTROLLER] Automatically queued deploy job {deploy_job_id} after successful image-build for app '{app['name']}'")
                            else:
                                app["status"] = APP_STATE_FAILED
                                app["failure_reason"] = f"No compatible node available for architecture {img_arch}"
                                if app.get("deployments"):
                                    app["deployments"][-1]["status"] = "FAILED"
                        else:
                            app["status"] = APP_STATE_FAILED
                            app["error"] = body.get("stderr") or "Image build failed"
                            app["failure_reason"] = app["error"]
                            app["updated_at"] = now_iso
                            if app.get("deployments"):
                                app["deployments"][-1]["status"] = "FAILED"

                    elif job.get("type") in ("docker-deploy", "docker-build-deploy"):
                        if job_status == JOB_STATE_SUCCEEDED:
                            app["status"] = APP_STATE_RUNNING
                            app["error"] = None
                            app["updated_at"] = now_iso
                            if app.get("deployments"):
                                last_dep = app["deployments"][-1]
                                last_dep["status"] = "SUCCESS"
                                last_dep["finished_at"] = now_iso
                                last_dep["duration_ms"] = body.get("duration_ms", 0)
                                last_dep["container_id"] = job.get("parameters", {}).get("container_name") or f"ps-{app['name']}"
                                if job.get("parameters", {}).get("architecture"):
                                    last_dep["architecture"] = job["parameters"]["architecture"]
                                if job.get("parameters", {}).get("image_digest"):
                                    last_dep["digest"] = job["parameters"]["image_digest"]
                                app["active_deployment_id"] = last_dep.get("deployment_id")
                        elif job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                            app["status"] = APP_STATE_FAILED
                            app["error"] = body.get("stderr") or f"Deployment job {job_status}"
                            app["failure_reason"] = app["error"]
                            app["updated_at"] = now_iso
                            if app.get("deployments"):
                                last_dep = app["deployments"][-1]
                                last_dep["status"] = "FAILED"
                                last_dep["finished_at"] = now_iso
                                last_dep["duration_ms"] = body.get("duration_ms", 0)

                            # Find previous working deployment for rollback preservation
                            successful_deps = [
                                d for d in app.get("deployments", [])
                                if d.get("status") == "SUCCESS" and d.get("deployment_id") != (last_dep.get("deployment_id") if app.get("deployments") else None)
                            ]
                            if successful_deps:
                                prev_working = successful_deps[-1]
                                app["previous_working_deployment"] = prev_working["deployment_id"]
                                print(f"[CONTROLLER-ROLLBACK] Preserving previous working deployment '{prev_working['deployment_id']}' for app '{app_id}'")

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
        # 5. Cancel Job: POST /jobs/<job_id>/cancel
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/cancel"):
            job_id = path[6:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if not self.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Authentication required to cancel job"})
                return

            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return

            current_st = job.get("status")

            # Idempotent repeat cancellation check
            if current_st == JOB_STATE_CANCELLED:
                if (body and body.get("idempotent")) or query.get("idempotent", [False])[0]:
                    self.send_json(200, {
                        "status": JOB_STATE_CANCELLED,
                        "job_id": job_id,
                        "duplicate": True,
                        "message": f"Job '{job_id}' is already cancelled.",
                        "job": job
                    })
                    return
                self.send_json(400, {
                    "error": f"Invalid state transition: Cannot cancel job '{job_id}' in terminal state '{current_st}'.",
                    "status": current_st
                })
                return

            # Terminal states: SUCCEEDED, FAILED, REJECTED cannot be cancelled
            if current_st in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_REJECTED) or not validate_job_transition(current_st, JOB_STATE_CANCELLED):
                self.send_json(400, {
                    "error": f"Invalid state transition: Cannot cancel job '{job_id}' in terminal state '{current_st}'.",
                    "status": current_st
                })
                return

            now_iso = get_current_iso_timestamp()
            reason = (body.get("reason") if body else "") or "Cancelled by operator request"

            # Attempt active process / container termination if job was actively executing
            termination_info = {"supported": True, "terminated": False}
            if current_st in (JOB_STATE_RUNNING, JOB_STATE_CLAIMED):
                try:
                    from agent.job_executor import JobExecutor
                    termination_info = JobExecutor.terminate_active_job(job_id, reason=reason)
                except Exception as e:
                    print(f"[CONTROLLER] Active job termination attempt note for {job_id}: {e}", file=sys.stderr)

            job["status"] = JOB_STATE_CANCELLED
            job["state"] = JOB_STATE_CANCELLED
            job["cancellation_requested"] = True
            job["cancelled_at"] = now_iso
            job["finished_at"] = now_iso
            job["lease_expires_at"] = None
            job["cancel_reason"] = reason
            if termination_info.get("terminated"):
                job["terminated_active_workload"] = True
            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Job {job_id} CANCELLED: {reason}")
            self.send_json(200, {
                "status": JOB_STATE_CANCELLED,
                "job_id": job_id,
                "message": f"Job '{job_id}' successfully cancelled.",
                "termination": termination_info,
                "job": job
            })
            return

        # ----------------------------------------------------------------------
        # 6. Update Job Status: POST /jobs/<job_id>/status (e.g. CLAIMED -> RUNNING)
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/status"):
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
            if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
                return

            new_status = (body.get("status") or "").upper()
            current_st = job.get("status")
            if not validate_job_transition(current_st, new_status):
                self.send_json(400, {
                    "error": f"Invalid state transition from '{current_st}' to '{new_status}'.",
                    "current_status": current_st,
                    "target_status": new_status
                })
                return

            now_iso = get_current_iso_timestamp()
            job["status"] = new_status
            job["state"] = new_status
            if new_status == JOB_STATE_RUNNING and not job.get("started_at"):
                job["started_at"] = now_iso
            save_jobs_db(jobs_db)

            self.send_json(200, {
                "status": "ok",
                "job_id": job_id,
                "current_status": new_status
            })
            return

        # ----------------------------------------------------------------------
        # 5. Application Management: POST /apps and POST /apps/<app_id>/...
        # ----------------------------------------------------------------------
        if path == "/apps" or path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

        # ----------------------------------------------------------------------
        # GitHub Inspect: POST /apps/github/inspect
        # ----------------------------------------------------------------------
        if path == "/apps/github/inspect":
            repo = (body.get("repository") or "").strip()
            branch = (body.get("branch") or "main").strip()
            root_dir = (body.get("root_directory") or ".").strip()
            if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
                self.send_json(400, {"error": "Invalid repository format. Must be 'owner/repository'."})
                return
            try:
                inspect_id = f"inspect-{secrets.token_hex(4)}"
                res = fetch_github_source(inspect_id, repo, branch, root_dir)
                temp_dir = APP_STORAGE_ROOT / inspect_id
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                bp = res.get("blueprint")
                has_bp = res.get("has_blueprint", False)
                self.send_json(200, {
                    "status": "ok",
                    "repository": repo,
                    "branch": branch,
                    "root_directory": root_dir,
                    "commit": res.get("commit", "main"),
                    "blueprint": bp,
                    "has_blueprint": has_bp,
                    "dockerfile_found": res.get("dockerfile_found", False),
                    "env_vars_needed": bp.get("services", [{}])[0].get("envVars", []) if bp else []
                })
            except Exception as e:
                self.send_json(400, {"error": f"Inspection failed: {e}"})
            return

        if path == "/apps":
            name = (body.get("name") or "").strip()
            source = body.get("source") or {"type": "manual"}
            blueprint_input = body.get("blueprint")
            image = (body.get("image") or "").strip()
            port = int(body.get("container_port") or body.get("port") or 8000)
            target = (body.get("target") or "auto").strip()
            raw_env_vars = body.get("env_vars") or body.get("env") or {}
            cpu_limit = str(body.get("cpu_limit", "0.5"))
            memory_limit = str(body.get("memory_limit", "256m"))

            if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
                self.send_json(400, {"error": "Invalid application name. Must contain only alphanumeric characters, dashes, and underscores."})
                return

            if not (1 <= port <= 65535):
                self.send_json(400, {"error": "Invalid container port. Must be between 1 and 65535."})
                return

            # Normalize env_vars
            env_vars = {}
            flat_env = {}
            if isinstance(raw_env_vars, dict):
                for k, v in raw_env_vars.items():
                    k_str = str(k).strip()
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k_str):
                        continue
                    if isinstance(v, dict):
                        is_sec = bool(v.get("is_secret", True if any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")) else False))
                        val_str = str(v.get("value", ""))
                        env_vars[k_str] = {"value": val_str, "is_secret": is_sec}
                        flat_env[k_str] = val_str
                    else:
                        is_sec = bool(any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")))
                        env_vars[k_str] = {"value": str(v), "is_secret": is_sec}
                        flat_env[k_str] = str(v)
            elif isinstance(raw_env_vars, list):
                for item in raw_env_vars:
                    if isinstance(item, dict) and "key" in item:
                        k_str = str(item["key"]).strip()
                        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k_str):
                            continue
                        val_str = str(item.get("value", ""))
                        is_sec = bool(item.get("is_secret", True if any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")) else False))
                        env_vars[k_str] = {"value": val_str, "is_secret": is_sec}
                        flat_env[k_str] = val_str

            with APPS_LOCK:
                apps_db = load_apps_db()
                for existing in apps_db.get("apps", {}).values():
                    if existing.get("name") == name and existing.get("status") != "DELETED":
                        self.send_json(409, {"error": f"Application with name '{name}' already exists."})
                        return

                app_id = f"app-{secrets.token_hex(6)}"
                now_iso = get_current_iso_timestamp()

                source_type = str(source.get("type", "manual")).lower()
                validated_blueprint = None

                if source_type == "github":
                    repo = str(source.get("repository", "")).strip()
                    branch = str(source.get("branch", "main")).strip()
                    root_dir = str(source.get("root_directory", ".")).strip()
                    if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
                        self.send_json(400, {"error": "Invalid GitHub repository format. Must be 'owner/repository'."})
                        return
                    try:
                        fetch_res = fetch_github_source(app_id, repo, branch, root_dir)
                        validated_blueprint = fetch_res.get("blueprint")
                        if blueprint_input:
                            validated_blueprint = parse_and_validate_blueprint(blueprint_input, apps_db, app_id)
                    except Exception as e:
                        self.send_json(400, {"error": f"GitHub source error: {e}"})
                        return
                    source = {
                        "type": "github",
                        "repository": repo,
                        "branch": branch,
                        "root_directory": root_dir
                    }
                else:
                    # Container Image Deployment
                    raw_img = (image or source.get("image") or "").strip()
                    if not raw_img:
                        self.send_json(400, {"error": "Missing container image reference."})
                        return
                    try:
                        parsed_img = validate_image_reference(raw_img)
                        image = parsed_img["raw"]
                    except ValueError as e:
                        self.send_json(400, {"error": str(e)})
                        return

                    # Discover supported architectures and exposed port
                    supp_archs = detect_image_architectures(image)
                    detected_port = detect_application_port(image, explicit_port=body.get("container_port"))
                    port = detected_port

                    source = {
                        "type": "image",
                        "image": image,
                        "supported_architectures": supp_archs
                    }

                route_data = body.get("route")
                if not route_data and validated_blueprint:
                    bp_route = validated_blueprint.get("services", [{}])[0].get("route")
                    if bp_route:
                        route_data = {"enabled": True, "type": "path", "path": bp_route, "strip_prefix": True, "public_access": True}
                elif not route_data:
                    route_data = {"enabled": True, "type": "path", "path": f"/{name.lower()}", "strip_prefix": True, "public_access": True}

                try:
                    validated_route = validate_app_route(route_data, apps_db)
                except ValueError as e:
                    self.send_json(400, {"error": str(e)})
                    return

                try:
                    host_port = allocate_host_port(apps_db)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to allocate host port: {e}"})
                    return

                app_record = {
                    "app_id": app_id,
                    "id": app_id,
                    "name": name,
                    "source": source,
                    "blueprint": validated_blueprint,
                    "image": image or f"personalserver/{name}:latest",
                    "port": host_port,
                    "container_port": port,
                    "host_port": host_port,
                    "target": target,
                    "status": APP_STATE_CREATED,
                    "selected_node": None,
                    "container_id": f"ps-{name}",
                    "route": validated_route,
                    "env_vars": env_vars,
                    "env": flat_env,
                    "deployments": [],
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
                "app": mask_app_record(app_record)
            })
            return

        # POST /apps/env/parse - Parse .env file server-side without saving
        if path == "/apps/env/parse":
            content_str = body.get("content") or ""
            parsed = parse_env_file_content(content_str)
            self.send_json(200, {
                "status": "ok",
                "env_vars": parsed,
                "count": len(parsed)
            })
            return

        # POST /apps/<app_id>/env/import - Import .env content into existing app
        if path.startswith("/apps/") and path.endswith("/env/import"):
            app_id = path[6:-11].strip()
            content_str = body.get("content") or ""
            parsed = parse_env_file_content(content_str)
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return
                app.setdefault("env_vars", {}).update(parsed)
                for k, v in parsed.items():
                    app.setdefault("env", {})[k] = v.get("value", "")
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)
            self.send_json(200, {
                "status": "imported",
                "count": len(parsed),
                "app": mask_app_record(app)
            })
            return

        # POST /apps/<app_id>/env
        if path.startswith("/apps/") and path.endswith("/env"):
            app_id = path[6:-4].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                key = (body.get("key") or "").strip()
                val = str(body.get("value") or "")
                is_sec = bool(body.get("is_secret", True if any(s in key.lower() for s in ("key", "secret", "password", "token", "auth")) else False))

                if not key or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', key):
                    self.send_json(400, {"error": "Invalid environment variable name. Must start with letter/underscore."})
                    return

                app.setdefault("env_vars", {})[key] = {"value": val, "is_secret": is_sec}
                app.setdefault("env", {})[key] = val
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

            masked_app = mask_app_record(app)
            self.send_json(200, {
                "status": "updated",
                "app_id": app_id,
                "env_vars": masked_app.get("env_vars", {})
            })
            return

        # POST /apps/<app_id>/env/reveal
        if path.startswith("/apps/") and path.endswith("/env/reveal"):
            app_id = path[6:-11].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            key = (body.get("key") or "").strip()
            env_vars = app.get("env_vars", {})
            if key not in env_vars:
                self.send_json(404, {"error": f"Variable '{key}' not found"})
                return

            entry = env_vars[key]
            val = entry.get("value") if isinstance(entry, dict) else str(entry)
            is_sec = entry.get("is_secret", False) if isinstance(entry, dict) else False
            self.send_json(200, {
                "key": key,
                "value": val,
                "is_secret": is_sec
            })
            return

        if path.startswith("/apps/") and path.endswith("/route"):
            app_id = path[6:-6].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                route_data = body.get("route") if body else None
                try:
                    validated_route = validate_app_route(route_data, apps_db, current_app_id=app_id)
                except ValueError as e:
                    self.send_json(400, {"error": str(e)})
                    return

                app["route"] = validated_route
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

            self.send_json(200, {
                "status": "updated",
                "app_id": app_id,
                "route": validated_route,
                "app": mask_app_record(app)
            })
            return

        if path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/redeploy")):
            is_redeploy = path.endswith("/redeploy")
            app_id = path[6:-9].strip() if is_redeploy else path[6:-7].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                source = app.get("source") or {"type": "manual"}
                source_type = source.get("type", "manual")
                now_iso = get_current_iso_timestamp()
                commit_sha = "latest"

                # If GitHub source app, fetch latest source
                if source_type == "github":
                    app["status"] = APP_STATE_FETCHING_SOURCE
                    save_apps_db(apps_db)
                    repo = source.get("repository")
                    branch = source.get("branch", "main")
                    root_dir = source.get("root_directory", ".")
                    try:
                        fetch_res = fetch_github_source(app_id, repo, branch, root_dir)
                        if fetch_res.get("blueprint"):
                            app["blueprint"] = fetch_res["blueprint"]
                        commit_sha = fetch_res.get("commit", "latest")
                        app["status"] = APP_STATE_CONFIGURING
                        save_apps_db(apps_db)
                    except Exception as e:
                        app["status"] = APP_STATE_FAILED
                        app["failure_reason"] = f"Failed to fetch GitHub source: {e}"
                        app["error"] = app["failure_reason"]
                        app["updated_at"] = now_iso
                        save_apps_db(apps_db)
                        self.send_json(400, {
                            "error": app["failure_reason"],
                            "status": APP_STATE_FAILED,
                            "app": mask_app_record(app)
                        })
                        return

                # Schedule onto container-capable node (Docker or udocker) with architecture matching
                t_sched_start = time.time()
                nodes_db = load_nodes_db()
                reqs = {
                    "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"]
                }
                if source_type == "image" or app.get("image"):
                    img_to_check = app.get("image") or source.get("image", "")
                    supp_archs = source.get("supported_architectures") or detect_image_architectures(img_to_check)
                    if supp_archs:
                        reqs["supported_architectures"] = supp_archs

                decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                sched_ms = int((time.time() - t_sched_start) * 1000)

                if not decision.get("selected_node"):
                    # Check if failure was caused by architecture mismatch
                    rejections = list(decision.get("rejected", {}).values())
                    if any("Image does not support this node architecture" in r for r in rejections):
                        reason = "Image does not support this node architecture."
                    else:
                        reason = f"No container-capable node is currently available in the cluster: {decision.get('reason', '')}".strip()
                    app["status"] = APP_STATE_FAILED
                    app["failure_reason"] = reason
                    app["error"] = reason
                    app["updated_at"] = now_iso
                    save_apps_db(apps_db)
                    print(f"[CONTROLLER-APP] Deployment of '{app['name']}' ({app_id}) failed: {reason}")
                    self.send_json(400, {
                        "error": reason,
                        "status": APP_STATE_FAILED,
                        "app": mask_app_record(app),
                        "scheduler": decision
                    })
                    return

                selected_node = decision["selected_node"]
                app["status"] = APP_STATE_DEPLOYING
                app["selected_node"] = selected_node
                app["updated_at"] = now_iso
                app["error"] = None

                # Record deployment entry in history
                dep_num = len(app.get("deployments", [])) + 1
                dep_id = f"dep-{secrets.token_hex(4)}"
                dep_record = {
                    "deployment_id": dep_id,
                    "number": dep_num,
                    "commit": commit_sha,
                    "branch": source.get("branch", "main") if source_type == "github" else "manual",
                    "trigger": "redeploy" if is_redeploy else (source_type),
                    "status": "BUILDING" if source_type == "github" else "DEPLOYING",
                    "started_at": now_iso,
                    "finished_at": None,
                    "duration_ms": None,
                    "stage_timings": {
                        "scheduling_ms": sched_ms,
                        "fetch_source_ms": 0,
                        "build_ms": 0,
                        "pull_image_ms": 0,
                        "start_container_ms": 0,
                        "health_check_ms": 0,
                        "total_ms": sched_ms
                    }
                }
                app.setdefault("deployments", []).append(dep_record)
                save_apps_db(apps_db)

                # Prepare environment variables unmasked for Docker container
                container_env = dict(app.get("env", {}))
                for k, v in (app.get("env_vars") or {}).items():
                    if isinstance(v, dict):
                        container_env[k] = v.get("value", "")
                    else:
                        container_env[k] = str(v)

                job_id = f"job-{secrets.token_hex(6)}"

                if source_type == "github":
                    # Build and deploy from source context
                    src_context, _ = resolve_safe_app_storage_path(app_id, f"source/{source.get('root_directory', '.')}".rstrip("/."))
                    dockerfile_name = "Dockerfile"
                    if app.get("blueprint"):
                        dockerfile_name = app["blueprint"].get("services", [{}])[0].get("dockerfile", "Dockerfile")
                    
                    job_record = {
                        "id": job_id,
                        "job_id": job_id,
                        "name": f"build-{app['name']}",
                        "type": "image-build",
                        "parameters": {
                            "app_id": app_id,
                            "app_name": app["name"],
                            "source_dir": str(src_context),
                            "dockerfile": dockerfile_name,
                            "image_tag": f"personalserver/{app['name']}:v{dep_num}"
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
                        "timeout": 180,
                        "max_attempts": 1,
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
                else:
                    # Direct image deployment
                    parsed_img = validate_image_reference(app["image"])
                    img_ref = parsed_img["raw"].split("@")[0]
                    img_digest = parsed_img.get("digest")
                    dep_record["image"] = img_ref
                    if img_digest:
                        dep_record["digest"] = img_digest
                    dep_record["architecture"] = app.get("architecture") or "amd64"

                    job_record = {
                        "id": job_id,
                        "job_id": job_id,
                        "name": f"deploy-{app['name']}",
                        "type": "docker-deploy",
                        "parameters": {
                            "app_id": app_id,
                            "image": img_ref,
                            "image_digest": img_digest,
                            "architecture": app.get("architecture"),
                            "container_name": app.get("container_id") or f"ps-{app['name']}",
                            "host_port": app["host_port"],
                            "container_port": app.get("container_port", 8000),
                            "env": container_env,
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

            print(f"[CONTROLLER] Queued {job_record['type']} job {job_id} for app '{app['name']}' to node '{selected_node}'")
            self.send_json(200, {
                "status": APP_STATE_DEPLOYING,
                "app": mask_app_record(app),
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
        # Application Rollback: POST /apps/<app_id>/rollback
        # ----------------------------------------------------------------------
        if path.startswith("/apps/") and path.endswith("/rollback"):
            app_id = path[6:-9].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                deployments = app.get("deployments", [])
                target_dep_id = (body.get("deployment_id") or body.get("target_deployment_id") if body else None)
                target_dep = None

                if target_dep_id:
                    for d in deployments:
                        if d.get("deployment_id") == target_dep_id:
                            target_dep = d
                            break
                    if not target_dep:
                        self.send_json(404, {"error": f"Specified deployment '{target_dep_id}' not found for application '{app_id}'"})
                        return
                else:
                    # Find most recent successful deployment prior to active/failed deployment
                    current_dep_id = app.get("active_deployment_id")
                    candidate_deps = [
                        d for d in deployments
                        if d.get("status") == "SUCCESS" and d.get("deployment_id") != current_dep_id
                    ]
                    if not candidate_deps:
                        # Fallback: any successful deployment
                        candidate_deps = [d for d in deployments if d.get("status") == "SUCCESS"]

                    if not candidate_deps:
                        self.send_json(400, {
                            "error": "No previous successful deployment found to roll back to.",
                            "app_id": app_id,
                            "deployments_count": len(deployments)
                        })
                        return
                    target_dep = candidate_deps[-1]

                now_iso = get_current_iso_timestamp()
                rollback_img = target_dep.get("image") or app.get("image")
                rollback_digest = target_dep.get("digest") or app.get("image_digest")
                rollback_arch = target_dep.get("architecture") or app.get("architecture") or "amd64"

                # Schedule onto container-capable node matching architecture
                nodes_db = load_nodes_db()
                reqs = {
                    "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"],
                    "supported_architectures": [rollback_arch] if rollback_arch else ["amd64", "arm64"]
                }
                decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                if not decision.get("selected_node"):
                    reason = f"No container-capable node available for rollback: {decision.get('reason', '')}".strip()
                    self.send_json(400, {
                        "error": reason,
                        "status": APP_STATE_FAILED,
                        "app": mask_app_record(app)
                    })
                    return

                selected_node = decision["selected_node"]
                dep_num = len(deployments) + 1
                dep_id = f"dep-{secrets.token_hex(4)}"

                rollback_record = {
                    "deployment_id": dep_id,
                    "number": dep_num,
                    "commit": target_dep.get("commit", "rollback"),
                    "branch": target_dep.get("branch", "rollback"),
                    "trigger": "rollback",
                    "status": "DEPLOYING",
                    "rollback_target": target_dep.get("deployment_id"),
                    "image": rollback_img,
                    "digest": rollback_digest,
                    "architecture": rollback_arch,
                    "started_at": now_iso,
                    "finished_at": None,
                    "duration_ms": None,
                    "stage_timings": {
                        "scheduling_ms": 0,
                        "pull_image_ms": 0,
                        "start_container_ms": 0,
                        "health_check_ms": 0,
                        "total_ms": 0
                    }
                }
                app.setdefault("deployments", []).append(rollback_record)
                app["status"] = APP_STATE_DEPLOYING
                app["selected_node"] = selected_node
                app["updated_at"] = now_iso
                app["error"] = None
                app["image"] = rollback_img
                if rollback_digest:
                    app["image_digest"] = rollback_digest
                app["architecture"] = rollback_arch
                save_apps_db(apps_db)

                # Prepare environment variables
                container_env = dict(app.get("env", {}))
                for k, v in (app.get("env_vars") or {}).items():
                    if isinstance(v, dict):
                        container_env[k] = v.get("value", "")
                    else:
                        container_env[k] = str(v)

                job_id = f"job-{secrets.token_hex(6)}"
                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"rollback-{app['name']}",
                    "type": "docker-deploy",
                    "parameters": {
                        "app_id": app_id,
                        "image": rollback_img,
                        "image_digest": rollback_digest,
                        "architecture": rollback_arch,
                        "container_name": app.get("container_id") or f"ps-{app['name']}",
                        "host_port": app["host_port"],
                        "container_port": app.get("container_port", 8000),
                        "env": container_env,
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
                        "reason": f"Rollback to deployment {target_dep.get('deployment_id')}"
                    },
                    "timeout": 120,
                    "max_attempts": 2,
                    "attempt": 1,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "created_at": now_iso,
                    "status": JOB_STATE_QUEUED,
                    "state": JOB_STATE_QUEUED
                }

                jobs_db = load_jobs_db()
                jobs_db.setdefault("jobs", {})[job_id] = job_record
                save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Queued rollback deployment {dep_id} for app '{app['name']}' restoring deployment {target_dep.get('deployment_id')}")
            self.send_json(200, {
                "status": APP_STATE_DEPLOYING,
                "app": mask_app_record(app),
                "job_id": job_id,
                "rollback_target": target_dep.get("deployment_id")
            })
            return

        # POST /apps/<app_id>/files/mkdir
        if path.startswith("/apps/") and path.endswith("/files/mkdir"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path.split("/")[2]
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            folder_name = (body.get("name") or "").strip()
            parent_rel = body.get("path") or ""

            if not folder_name or re.search(r'[\\/:\*\?"<>\|\x00]', folder_name) or ".." in folder_name:
                self.send_json(400, {"error": "Invalid folder name"})
                return

            try:
                target_dir, app_base = resolve_safe_app_storage_path(app_id, os.path.join(parent_rel, folder_name))
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
                    "path": str(target_dir.relative_to(app_base)).replace("\\", "/")
                })
            except Exception as e:
                self.send_json(500, {"error": f"Failed to create directory: {e}"})
            return

        # POST /apps/<app_id>/files/upload
        if path.startswith("/apps/") and path.endswith("/files/upload"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path.split("/")[2]
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            parsed_q = urllib.parse.parse_qs(parsed.query)
            target_rel = parsed_q.get("path", [""])[0]
            try:
                target_dir, app_base = resolve_safe_app_storage_path(app_id, target_rel)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_dir.exists() or not target_dir.is_dir():
                self.send_json(400, {"error": "Upload target directory does not exist"})
                return

            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 100 * 1024 * 1024:
                self.send_json(413, {"error": "Upload exceeds 100 MB limit"})
                return

            content_type = self.headers.get("Content-Type", "")
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
                    "target_dir": str(target_dir.relative_to(app_base)).replace("\\", "/")
                })
                return
            else:
                filename = parsed_q.get("filename", ["upload.dat"])[0]
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

        # ----------------------------------------------------------------------
        # 7. Node Lifecycle: Drain, Deboard, Resume, and Safe Remove
        # ----------------------------------------------------------------------
        # POST /nodes/<node_id>/drain
        if path.startswith("/nodes/") and path.endswith("/drain"):
            node_id = path[7:-6].strip()
            enrollment_token = get_or_create_enrollment_token()
            if not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to drain a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot drain node '{node_id}': Node is already REMOVED."})
                return

            node_entry["status"] = STATE_DRAINING
            node_entry["draining_since"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            # Handle queued jobs safely:
            # - Auto-target jobs must be rescheduled to eligible online nodes
            # - Explicit-target jobs must NOT silently migrate
            jobs_db = load_jobs_db()
            jobs_rescheduled = 0
            for jid, job in list(jobs_db.get("jobs", {}).items()):
                if job.get("target_node") == node_id and job.get("status") in (JOB_STATE_QUEUED, JOB_STATE_RECOVERING):
                    target_mode = job.get("target", "auto")
                    if target_mode == "auto":
                        reqs = job.get("requirements", {})
                        decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                        if decision.get("selected_node"):
                            job["target_node"] = decision["selected_node"]
                            job["scheduler"] = {
                                "mode": "drain-reschedule",
                                "selected_node": decision["selected_node"],
                                "score": decision["score"],
                                "reason": f"Rescheduled from draining node '{node_id}'"
                            }
                            jobs_rescheduled += 1
                            print(f"[CONTROLLER-DRAIN] Rescheduled auto-target job {jid} to node '{decision['selected_node']}'")
            if jobs_rescheduled > 0:
                save_jobs_db(jobs_db)

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db, jobs_db)
            print(f"[CONTROLLER-DRAIN] Node '{node_id}' transitioned to DRAINING (rescheduled {jobs_rescheduled} queued auto job(s))")
            self.send_json(200, {
                "status": STATE_DRAINING,
                "node_id": node_id,
                "jobs_rescheduled": jobs_rescheduled,
                "message": f"Node '{node_id}' is now DRAINING. No new workloads will be assigned.",
                "deboarding": eval_res
            })
            return

        # POST /nodes/<node_id>/deboard
        if path.startswith("/nodes/") and path.endswith("/deboard"):
            node_id = path[7:-8].strip()
            enrollment_token = get_or_create_enrollment_token()
            if not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to deboard a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot deboard node '{node_id}': Node is already REMOVED."})
                return

            node_entry["status"] = STATE_DEBOARDING
            node_entry["deboarding_since"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            print(f"[CONTROLLER-DEBOARD] Node '{node_id}' transitioned to DEBOARDING. Can remove: {eval_res['can_remove']}")
            self.send_json(200, {
                "status": STATE_DEBOARDING,
                "node_id": node_id,
                "message": f"Node '{node_id}' is now in DEBOARDING state.",
                "can_remove": eval_res["can_remove"],
                "blockers": eval_res["blockers"],
                "active_jobs": eval_res["active_jobs"],
                "applications": eval_res["applications"],
                "storage_used_gb": eval_res["storage_used_gb"],
                "deboarding": eval_res
            })
            return

        # POST /nodes/<node_id>/resume
        if path.startswith("/nodes/") and path.endswith("/resume"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to resume a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot resume node '{node_id}': Node is REMOVED. Re-registration required."})
                return

            node_entry["status"] = STATE_ONLINE
            node_entry.pop("draining_since", None)
            node_entry.pop("deboarding_since", None)
            save_nodes_db(nodes_db)

            print(f"[CONTROLLER-LIFECYCLE] Resumed node '{node_id}' to ONLINE.")
            self.send_json(200, {
                "status": STATE_ONLINE,
                "node_id": node_id,
                "message": f"Node '{node_id}' has resumed active ONLINE state."
            })
            return

        # Safe Removal: POST /nodes/<node_id>/remove
        if path.startswith("/nodes/") and path.endswith("/remove"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to remove a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            if not eval_res["can_remove"]:
                self.send_json(409, {
                    "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                    "status": "BLOCKED",
                    "node_id": node_id,
                    "blockers": eval_res["blockers"],
                    "active_jobs": eval_res["active_jobs"],
                    "applications": eval_res["applications"],
                    "storage_used_gb": eval_res["storage_used_gb"],
                    "deboarding": eval_res
                })
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            # Trigger lease sweep to recover any jobs assigned to removed node
            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            print(f"[CONTROLLER] Safely removed node: {node_id}")
            self.send_json(200, {
                "status": STATE_REMOVED,
                "node_id": node_id,
                "message": f"Node '{node_id}' has been safely removed from active cluster membership."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        token = self.parse_auth_token()

        if not self.check_leader_write_permission():
            return

        if path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

            # DELETE /apps/<app_id>/files?path=<rel_path>
            if "/files" in path:
                app_id = path.split("/")[2]
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                parsed_q = urllib.parse.parse_qs(parsed.query)
                req_path = parsed_q.get("path", [""])[0]
                if not req_path:
                    self.send_json(400, {"error": "Missing 'path' parameter"})
                    return

                try:
                    target_item, app_base = resolve_safe_app_storage_path(app_id, req_path)
                except ValueError as e:
                    self.send_json(403, {"error": str(e)})
                    return

                if target_item == app_base:
                    self.send_json(403, {"error": "Cannot delete application storage root"})
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

            # DELETE /apps/<app_id>/env/<key>
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) == 4 and parts[0] == "apps" and parts[2] == "env":
                app_id = parts[1]
                env_key = parts[3]
                with APPS_LOCK:
                    apps_db = load_apps_db()
                    app = apps_db.get("apps", {}).get(app_id)
                    if not app or app.get("status") == "DELETED":
                        self.send_json(404, {"error": f"Application '{app_id}' not found"})
                        return

                    if env_key in app.get("env_vars", {}):
                        del app["env_vars"][env_key]
                    if env_key in app.get("env", {}):
                        del app["env"][env_key]
                    app["updated_at"] = get_current_iso_timestamp()
                    save_apps_db(apps_db)

                self.send_json(200, {
                    "status": "deleted",
                    "key": env_key,
                    "app_id": app_id,
                    "env_vars": mask_app_record(app).get("env_vars", {})
                })
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
            if not constant_time_compare(token, enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            if not eval_res["can_remove"]:
                self.send_json(409, {
                    "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                    "status": "BLOCKED",
                    "node_id": node_id,
                    "blockers": eval_res["blockers"],
                    "active_jobs": eval_res["active_jobs"],
                    "applications": eval_res["applications"],
                    "storage_used_gb": eval_res["storage_used_gb"],
                    "deboarding": eval_res
                })
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            print(f"[CONTROLLER] Safely removed node via DELETE: {node_id}")
            self.send_json(200, {
                "status": STATE_REMOVED,
                "node_id": node_id,
                "message": f"Node '{node_id}' removed safely."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def log_message(self, format, *args):
        print(f"[HTTP {self.command}] {self.path} - {args[0] if args else ''}")


# ------------------------------------------------------------------------------
# CLI Actions
# ------------------------------------------------------------------------------

def start_server(host=None, port=None, timeout=None, role=None):
    paths = get_controller_paths()
    paths.validate()
    cfg = get_controller_runtime_config()
    host = host if host is not None else cfg.host
    port = port if port is not None else cfg.port
    timeout = timeout if timeout is not None else cfg.heartbeat_timeout

    ensure_directories()
    token = get_or_create_enrollment_token()
    ControllerHandler.heartbeat_timeout = timeout

    # Recover orphaned jobs from previous shutdown
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db, timeout)

    # Start background lease sweeper thread
    start_background_lease_sweeper(timeout)

    # Initialize High Availability (HA) Manager
    ha_role = role or os.environ.get("PERSONALSERVER_HA_ROLE", "auto")
    ha_node_id = resolve_controller_node_id(node_id=None, paths=paths)
    ha_node_url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    ha_mgr = get_ha_manager(paths=paths, role=ha_role, node_id=ha_node_id, node_url=ha_node_url, force_reload=True)
    ha_mgr.initialize()
    ha_mgr.start_monitor()

    print("==========================================")
    print(" PersonalServer Cluster Controller & Scheduler (v1.0)")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Heartbeat timeout:     {timeout} seconds")
    print(f"HA Failover Role:      {ha_mgr.state.upper()} (Term {ha_mgr.current_term})")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print(f"Cluster database:      {NODES_FILE}")
    print(f"Jobs database:         {JOBS_FILE}")
    print("==========================================")

    server = HTTPServer((host, port), ControllerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
    finally:
        ha_mgr.stop_monitor()
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
    p_start.add_argument("--role", choices=["auto", "leader", "standby"], default=None, help="HA failover role (default: auto)")

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
        role = getattr(args, "role", None)
        start_server(host, port, timeout, role)
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
