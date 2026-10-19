#!/usr/bin/env python3
"""
Controller High Availability (HA) & Health Route Handlers
=========================================================
Handles GET and POST requests for HA state consensus, leader election,
heartbeats, standby state synchronization, and cluster health checks.
"""

from typing import Any, Dict, Optional

from common.crypto_utils import constant_time_compare
from common.time_utils import get_current_iso_timestamp
from config.structured_logger import controller_logger
from controller.ha_helper import get_ha_manager
from controller.node_manager import get_cluster_summary
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import load_jobs_db, load_nodes_db


def handle_ha_get(handler: Any, path: str) -> bool:
    """
    Handles GET requests for cluster health and High Availability endpoints.
    Returns True if handled, False otherwise.
    """
    # GET /health or GET /status
    if path in ("/health", "/status"):
        nodes_db = load_nodes_db()
        jobs_db = load_jobs_db()
        summary = get_cluster_summary(nodes_db, handler.heartbeat_timeout)
        jobs = jobs_db.get("jobs", {})
        job_states: Dict[str, int] = {}
        for j in jobs.values():
            st = j.get("status", "UNKNOWN")
            job_states[st] = job_states.get(st, 0) + 1
        recent_errors = len(controller_logger.get_recent_events(limit=50, level="ERROR"))
        handler.send_json(200, {
            "status": "online",
            "service": "PersonalServer Controller",
            "cluster": summary["cluster"],
            "total_jobs": len(jobs),
            "job_states": job_states,
            "recent_errors": recent_errors,
            "timestamp": get_current_iso_timestamp()
        })
        return True

    # GET /ha/status or GET /api/ha/status
    if path in ("/ha/status", "/api/ha/status"):
        mgr = get_ha_manager()
        handler.send_json(200, mgr.get_status_dict())
        return True

    # GET /ha/leader or GET /api/ha/leader
    if path in ("/ha/leader", "/api/ha/leader"):
        mgr = get_ha_manager()
        leader_id = mgr.current_leader_id
        leader_name, leader_online, leader_registered = (
            mgr.resolve_leader_name(leader_id) if hasattr(mgr, "resolve_leader_name") else (leader_id, False, False)
        )
        handler.send_json(200, {
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
        return True

    # GET /ha/sync or GET /api/ha/sync
    if path in ("/ha/sync", "/api/ha/sync"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for HA state sync"})
            return True
        mgr = get_ha_manager()
        snapshot = mgr.export_state_snapshot()
        handler.send_json(200, snapshot)
        return True

    return False


def handle_ha_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: Optional[str] = None) -> bool:
    """
    Handles POST requests for High Availability management endpoints.
    These endpoints execute prior to leader write permission check.
    Returns True if handled, False otherwise.
    """
    token = token or handler.parse_auth_token()
    if path in ("/ha/heartbeat", "/ha/renew"):
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for HA heartbeat"})
            return True
        mgr = get_ha_manager()
        standby_id = body.get("node_id") if body else None
        standby_url = body.get("node_url") if body else None
        standby_version = body.get("state_version", 0) if body else 0
        if standby_id and standby_url:
            mgr.register_standby_heartbeat(standby_id, standby_url, standby_version)
        handler.send_json(200, {
            "status": "ok",
            "role": mgr.state,
            "term": mgr.current_term,
            "state_version": mgr.state_version,
            "leader_id": mgr.current_leader_id,
            "leader_url": mgr.current_leader_url,
            "lease_remaining_sec": round(mgr.current_lease.time_remaining_sec(), 1)
        })
        return True

    if path == "/ha/failover":
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for HA failover"})
            return True
        mgr = get_ha_manager()
        target_url = body.get("target_url") if body else None
        result = mgr.manual_failover(target_node_url=target_url)
        status_code = 200 if result.get("status") == "success" else 400
        handler.send_json(status_code, result)
        return True

    if path == "/ha/promote":
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for HA promote"})
            return True
        mgr = get_ha_manager()
        reason = body.get("reason", "API promotion request") if body else "API promotion request"
        mgr.elect_self(reason=reason)
        handler.send_json(200, {
            "status": "promoted",
            "node_id": mgr.node_id,
            "role": mgr.state,
            "term": mgr.current_term,
            "state_version": mgr.state_version
        })
        return True

    return False


__all__ = [
    "handle_ha_get",
    "handle_ha_post",
]
