#!/usr/bin/env python3
"""
Controller Node & Cluster Route Handlers
========================================
HTTP request handling for node registration, heartbeats, status query,
diagnostics, cluster utilization, deboarding safety, drain, and removal.
"""

import json
import secrets
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from common.crypto_utils import constant_time_compare
from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_platform_config
from config.structured_logger import controller_logger
from controller.constants import (
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_ONLINE,
    STATE_REMOVED,
    VALID_ROLES,
)
from controller.lease_sweeper import sweep_expired_leases
from controller.node_manager import (
    evaluate_node_deboarding_safety,
    get_cluster_summary,
    get_cluster_utilization,
    sanitize_node_record,
)
from controller.onboarding import (
    get_or_create_enrollment_token,
    validate_and_consume_onboarding_code,
)
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db, save_nodes_db
from scheduler.scheduler import ResourceScheduler


def handle_node_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles GET requests for node, cluster, platform, and diagnostic endpoints.
    Returns True if handled, False otherwise.
    """
    # GET /diagnostics
    if path == "/diagnostics":
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for diagnostics"})
            return True
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
        handler.send_json(200, {
            "subsystem": "Controller",
            "events_count": len(events),
            "events": events
        })
        return True

    # GET /platform or GET /api/platform
    if path in ("/platform", "/api/platform"):
        handler.send_json(200, get_platform_config().to_dict())
        return True

    # GET /cluster/utilization
    if path == "/cluster/utilization":
        nodes_db = load_nodes_db()
        jobs_db = load_jobs_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)
        util = get_cluster_utilization(nodes_db, jobs_db, handler.heartbeat_timeout)
        handler.send_json(200, util)
        return True

    # GET /cluster
    if path == "/cluster":
        nodes_db = load_nodes_db()
        summary = get_cluster_summary(nodes_db, handler.heartbeat_timeout)
        handler.send_json(200, summary)
        return True

    # GET /nodes
    if path == "/nodes":
        nodes_db = load_nodes_db()
        summary = get_cluster_summary(nodes_db, handler.heartbeat_timeout)
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

        handler.send_json(200, {
            "nodes": nodes_list,
            "count": len(nodes_list)
        })
        return True

    # GET /nodes/<node_id>/deboard
    if path.startswith("/nodes/") and path.endswith("/deboard"):
        node_id = path[7:-8].strip()
        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True
        eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
        handler.send_json(200, eval_res)
        return True

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
                handler.send_json(200, {"node_id": node_id, "diagnostics": diag_res})
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Local diagnostics error: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        node_status = (node.get("status") or "").upper()
        hb = node.get("last_heartbeat", {}) if node else {}
        hb_sys = hb.get("system", {})
        bat = hb.get("battery") or hb_sys.get("battery") or {}
        net = hb.get("network") or hb_sys.get("network") or {}
        cpu = hb.get("cpu_details") or hb_sys.get("cpu_details") or {}
        thm = hb.get("thermals") or hb_sys.get("thermals") or {}

        if node_status == "OFFLINE":
            handler.send_json(200, {
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
            return True

        node_ip = node.get("ip") if node else None
        if node_ip and node_ip not in ("127.0.0.1", "localhost", "::1"):
            try:
                req = urllib.request.Request(f"http://{node_ip}:8080/diagnostics", headers={"User-Agent": "PersonalServer-Controller/1.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    diag_payload = data.get("diagnostics", data)
                    diag_payload["live"] = True
                    handler.send_json(200, {"node_id": node_id, "diagnostics": diag_payload})
                    return True
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
        handler.send_json(200, {"node_id": node_id, "diagnostics": diag_obj})
        return True

    # GET /nodes/<node_id>
    if norm_path.startswith("/nodes/") or norm_path.startswith("/api/nodes/"):
        prefix_len = 11 if norm_path.startswith("/api/nodes/") else 7
        rest = norm_path[prefix_len:].strip("/")
        if "/" not in rest and rest:
            node_id = rest
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            if not node:
                handler.send_json(404, {"error": f"Node '{node_id}' not found"})
                return True
            s_node = sanitize_node_record(node, handler.heartbeat_timeout)
            s_node["deboarding"] = evaluate_node_deboarding_safety(node_id, nodes_db)
            handler.send_json(200, {"node": s_node})
            return True

    return False


def handle_node_post(handler: Any, path: str, body: Dict[str, Any], token: str) -> bool:
    """
    Handles POST requests for node registration, heartbeats, and lifecycle management.
    Returns True if handled, False otherwise.
    """
    # 1. Node Registration: POST /register
    if path == "/register":
        onboarding_code = handler.headers.get("X-Onboarding-Code", "").strip()

        node_id = body.get("node_id")
        if not node_id:
            handler.send_json(400, {"error": "Missing required field 'node_id'"})
            return True

        if onboarding_code:
            is_valid, err = validate_and_consume_onboarding_code(onboarding_code, node_id)
            if not is_valid:
                handler.send_json(401, {"error": f"Unauthorized: {err}"})
                return True
        else:
            enrollment_token = get_or_create_enrollment_token()
            if not token or not constant_time_compare(token, enrollment_token):
                handler.send_json(401, {"error": "Unauthorized: Invalid enrollment token"})
                return True

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
        verified_capabilities = body.get("verified_capabilities", {})
        advertised_capabilities = body.get("advertised_capabilities") or capabilities
        probe_results = body.get("probe_results", {})

        nodes_db = load_nodes_db()
        now = get_current_iso_timestamp()

        node_auth_token = secrets.token_hex(20)

        client_ip = (
            handler.client_address[0]
            if (handler.client_address and handler.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0"))
            else None
        )

        node_record = {
            "node_id": node_id,
            "name": node_name,
            "role": node_role,
            "platform": platform,
            "os": os_name,
            "architecture": arch,
            "resources": resources,
            "capabilities": capabilities,
            "verified_capabilities": verified_capabilities,
            "advertised_capabilities": advertised_capabilities,
            "probe_results": probe_results,
            "auth_token": node_auth_token,
            "ip": (body.get("ip") or body.get("tailscale_ip") or client_ip),
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
            correlation_id=getattr(handler, "current_correlation_id", None),
            node_id=node_id,
            role=node_role,
            platform=platform,
            architecture=arch,
            ip=node_record.get("ip")
        )

        handler.send_json(200, {
            "status": "registered",
            "node_id": node_id,
            "auth_token": node_auth_token,
            "registered_at": now,
            "message": f"Node '{node_id}' successfully registered in cluster '{nodes_db.get('cluster_name')}'."
        })
        return True

    # 2. Node Heartbeat: POST /heartbeat or POST /nodes/<node_id>/heartbeat
    if path == "/heartbeat" or (path.startswith("/nodes/") and path.endswith("/heartbeat")):
        node_id = body.get("node_id")
        if not node_id and path.startswith("/nodes/"):
            node_id = path[7:-10].strip()
        if not node_id:
            handler.send_json(400, {"error": "Missing required field 'node_id'"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' is not registered. Please register first."})
            return True

        if node_entry.get("status") == STATE_REMOVED or not node_entry.get("auth_token"):
            handler.send_json(403, {"error": f"Node '{node_id}' was removed from cluster. Re-registration required."})
            return True

        expected_auth_token = node_entry.get("auth_token")
        enrollment_token = get_or_create_enrollment_token()

        if not token or (not constant_time_compare(token, expected_auth_token) and not constant_time_compare(token, enrollment_token)):
            handler.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
            return True

        now = get_current_iso_timestamp()
        current_st = node_entry.get("status")
        if current_st not in (STATE_DRAINING, STATE_DEBOARDING):
            node_entry["status"] = STATE_ONLINE
        node_entry["last_seen"] = now
        reported_ip = body.get("ip") or body.get("tailscale_ip")
        if reported_ip:
            node_entry["ip"] = reported_ip
        elif handler.client_address and handler.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0"):
            node_entry["ip"] = handler.client_address[0]
        elif not node_entry.get("ip"):
            node_entry["ip"] = handler.client_address[0] if handler.client_address else "127.0.0.1"

        if "capabilities" in body and isinstance(body["capabilities"], dict):
            node_entry.setdefault("capabilities", {}).update(body["capabilities"])

        if "verified_capabilities" in body and isinstance(body["verified_capabilities"], dict):
            node_entry.setdefault("verified_capabilities", {}).update(body["verified_capabilities"])

        if "advertised_capabilities" in body and isinstance(body["advertised_capabilities"], dict):
            node_entry.setdefault("advertised_capabilities", {}).update(body["advertised_capabilities"])

        if "probe_results" in body and isinstance(body["probe_results"], dict):
            node_entry.setdefault("probe_results", {}).update(body["probe_results"])

        if "resources" in body and isinstance(body["resources"], dict):
            node_entry.setdefault("resources", {}).update(body["resources"])

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

        handler.send_json(200, {
            "status": "ok",
            "ack": True,
            "node_id": node_id,
            "timestamp": now
        })
        return True

    # 3. POST /nodes/<node_id>/drain
    if path.startswith("/nodes/") and path.endswith("/drain"):
        node_id = path[7:-6].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Admin enrollment token required to drain a node"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        if node_entry.get("status") == STATE_REMOVED:
            handler.send_json(400, {"error": f"Cannot drain node '{node_id}': Node is already REMOVED."})
            return True

        node_entry["status"] = STATE_DRAINING
        node_entry["draining_since"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        # Handle queued jobs safely
        jobs_db = load_jobs_db()
        jobs_rescheduled = 0
        for jid, job in list(jobs_db.get("jobs", {}).items()):
            if job.get("target_node") == node_id and job.get("status") in (JOB_STATE_QUEUED, JOB_STATE_RECOVERING):
                target_mode = job.get("target", "auto")
                if target_mode == "auto":
                    reqs = job.get("requirements", {})
                    decision = ResourceScheduler.select_node(reqs, nodes_db, handler.heartbeat_timeout)
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
        handler.send_json(200, {
            "status": STATE_DRAINING,
            "node_id": node_id,
            "jobs_rescheduled": jobs_rescheduled,
            "message": f"Node '{node_id}' is now DRAINING. No new workloads will be assigned.",
            "deboarding": eval_res
        })
        return True

    # 4. POST /nodes/<node_id>/deboard
    if path.startswith("/nodes/") and path.endswith("/deboard"):
        node_id = path[7:-8].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Admin enrollment token required to deboard a node"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        if node_entry.get("status") == STATE_REMOVED:
            handler.send_json(400, {"error": f"Cannot deboard node '{node_id}': Node is already REMOVED."})
            return True

        node_entry["status"] = STATE_DEBOARDING
        node_entry["deboarding_since"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
        print(f"[CONTROLLER-DEBOARD] Node '{node_id}' transitioned to DEBOARDING. Can remove: {eval_res['can_remove']}")
        handler.send_json(200, {
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
        return True

    # 5. POST /nodes/<node_id>/resume
    if path.startswith("/nodes/") and path.endswith("/resume"):
        node_id = path[7:-7].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Admin enrollment token required to resume a node"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        if node_entry.get("status") == STATE_REMOVED:
            handler.send_json(400, {"error": f"Cannot resume node '{node_id}': Node is REMOVED. Re-registration required."})
            return True

        node_entry["status"] = STATE_ONLINE
        node_entry.pop("draining_since", None)
        node_entry.pop("deboarding_since", None)
        save_nodes_db(nodes_db)

        print(f"[CONTROLLER-LIFECYCLE] Resumed node '{node_id}' to ONLINE.")
        handler.send_json(200, {
            "status": STATE_ONLINE,
            "node_id": node_id,
            "message": f"Node '{node_id}' has resumed active ONLINE state."
        })
        return True

    # 6. POST /nodes/<node_id>/remove
    if path.startswith("/nodes/") and path.endswith("/remove"):
        node_id = path[7:-7].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Admin enrollment token required to remove a node"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
        if not eval_res["can_remove"]:
            handler.send_json(409, {
                "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                "status": "BLOCKED",
                "node_id": node_id,
                "blockers": eval_res["blockers"],
                "active_jobs": eval_res["active_jobs"],
                "applications": eval_res["applications"],
                "storage_used_gb": eval_res["storage_used_gb"],
                "deboarding": eval_res
            })
            return True

        node_entry["status"] = STATE_REMOVED
        node_entry["auth_token"] = None
        node_entry["removed_at"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        # Trigger lease sweep to recover any jobs assigned to removed node
        jobs_db = load_jobs_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

        print(f"[CONTROLLER] Safely removed node: {node_id}")
        handler.send_json(200, {
            "status": STATE_REMOVED,
            "node_id": node_id,
            "message": f"Node '{node_id}' has been safely removed from active cluster membership."
        })
        return True

    return False


def handle_node_delete(handler: Any, path: str, token: str) -> bool:
    """
    Handles DELETE requests for removing cluster nodes.
    Returns True if handled, False otherwise.
    """
    if path.startswith("/nodes/"):
        node_id = path[7:].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Admin enrollment token required"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
        if not eval_res["can_remove"]:
            handler.send_json(409, {
                "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                "status": "BLOCKED",
                "node_id": node_id,
                "blockers": eval_res["blockers"],
                "active_jobs": eval_res["active_jobs"],
                "applications": eval_res["applications"],
                "storage_used_gb": eval_res["storage_used_gb"],
                "deboarding": eval_res
            })
            return True

        node_entry["status"] = STATE_REMOVED
        node_entry["auth_token"] = None
        node_entry["removed_at"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        jobs_db = load_jobs_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

        print(f"[CONTROLLER] Safely removed node via DELETE: {node_id}")
        handler.send_json(200, {
            "status": STATE_REMOVED,
            "node_id": node_id,
            "message": f"Node '{node_id}' removed safely."
        })
        return True

    return False


__all__ = [
    "handle_node_get",
    "handle_node_post",
    "handle_node_delete",
]
