#!/usr/bin/env python3
"""
Controller Node & Cluster Route Handlers
========================================
HTTP request handling for node registration, heartbeats, status query,
diagnostics, cluster utilization, deboarding safety, drain, and removal.
"""

import ipaddress
import json
import re
import secrets
import urllib.error
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
from controller.ha_helper import get_ha_manager
from controller.lease_sweeper import sweep_expired_leases
from controller.node_manager import (
    evaluate_node_deboarding_safety,
    get_cluster_summary,
    get_cluster_utilization,
    sanitize_node_record,
)
from controller.onboarding import (
    append_audit_event,
    build_initial_capability_permissions,
    create_enrollment_session,
    get_audit_log,
    get_enrollment_session,
    get_or_create_enrollment_token,
    list_enrollment_sessions,
    revoke_enrollment_session,
    validate_and_consume_onboarding_details,
)
from controller.leader_ranking import (
    apply_default_leadership_metadata,
    calculate_node_performance_score,
    evaluate_node_eligibility,
)
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db, save_nodes_db
from scheduler.scheduler import ResourceScheduler

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_private_mesh_request(handler: Any) -> bool:
    """Rejects public Cloudflare/proxy ingress; permits loopback, private RFC1918, and Tailscale mesh."""
    hdrs = getattr(handler, "headers", None) or {}
    for h_name in ("Cf-Ray", "Cf-Connecting-Ip", "CF-Visitor"):
        if hdrs.get(h_name) or hdrs.get(h_name.lower()):
            return False

    client_addr = getattr(handler, "client_address", None)
    client_ip = client_addr[0] if client_addr else ""
    if not client_ip or client_ip in ("localhost", "::1"):
        return True
    try:
        ip_obj = ipaddress.ip_address(client_ip.split("%")[0])
        return ip_obj.is_loopback or ip_obj.is_private or (ip_obj in _TAILSCALE_NET)
    except ValueError:
        return False


def _require_admin_auth(handler: Any, token: str, err_msg: str) -> bool:
    """Verifies constant-time admin enrollment credential or registered node token on sensitive owner routes."""
    enrollment_token = get_or_create_enrollment_token()
    if (token and constant_time_compare(token, enrollment_token)) or (hasattr(handler, "is_authenticated_admin_or_node") and handler.is_authenticated_admin_or_node()):
        return True
    handler.send_json(401, {"error": err_msg})
    return False


def build_cluster_state(handler: Any) -> Dict[str, Any]:
    """
    Constructs the single authoritative cluster state contract:
    - cluster_id, state_version, snapshot_timestamp
    - leader: { node_id, name, term, address, is_local, lease_expires_at, lease_valid, election_mode }
    - quorum: { voters, healthy_voters, minimum_required, quorum_achieved }
    - cluster: { total_nodes, online_nodes, active_cores, memory_total_mb, memory_used_mb }
    - nodes: array of { node_id, name, address, role, status, is_leader, leader_eligible, merit_score, ineligibility_reason, last_heartbeat }
    - controller: { local_node_id, local_role, local_term, lease_valid }
    """
    from config.platform_config import get_controller_paths
    paths = get_controller_paths()
    nodes_db = load_nodes_db(paths=paths)
    jobs_db = load_jobs_db(paths=paths)
    timeout_sec = getattr(handler, "heartbeat_timeout", 30)
    sweep_expired_leases(jobs_db, nodes_db, timeout_sec)
    util = get_cluster_utilization(nodes_db, jobs_db, timeout_sec)
    summary = get_cluster_summary(nodes_db, timeout_sec)
    mgr = getattr(handler, "ha_manager", None) or get_ha_manager(paths=paths)

    # Authoritative Leader info
    leader_id = mgr.current_leader_id
    leader_name, is_online, is_reg = mgr.resolve_leader_name(leader_id, nodes=nodes_db.get("nodes", {}))
    lease_valid = bool(mgr.current_lease.is_valid())
    is_local_leader = bool(mgr.is_leader and lease_valid and (not leader_id or leader_id == mgr.node_id))

    # Display name resolution
    if is_local_leader and (not leader_name or leader_name in ("None", "unknown", "")):
        local_node = nodes_db.get("nodes", {}).get(mgr.node_id, {})
        leader_name = local_node.get("name") or local_node.get("node_name") or mgr.node_id

    leader_obj = {
        "node_id": (mgr.node_id if is_local_leader else leader_id),
        "name": leader_name if (leader_id or is_local_leader) else "Electing...",
        "term": mgr.current_term,
        "address": (mgr.node_url if is_local_leader else (mgr.current_leader_url or "")),
        "is_local": is_local_leader,
        "lease_expires_at": getattr(mgr.current_lease, "lease_expires_at", None) or getattr(mgr.current_lease, "expires_at", None),
        "lease_valid": lease_valid,
        "election_mode": getattr(mgr, "cluster_mode", "auto")
    }

    # Quorum info
    voters_map = mgr.load_voting_members()
    voters_list = []
    healthy_voters_count = 0
    for vid, vdata in voters_map.items():
        v_url = vdata.get("url", "")
        is_healthy = False
        if vid == mgr.node_id:
            is_healthy = True
        else:
            n_entry = nodes_db.get("nodes", {}).get(vid)
            if n_entry and n_entry.get("status") == STATE_ONLINE:
                is_healthy = True
        if is_healthy:
            healthy_voters_count += 1
        voters_list.append({
            "node_id": vid,
            "name": vdata.get("name", vid),
            "url": v_url,
            "role": vdata.get("role", "voter"),
            "healthy": is_healthy
        })

    req_quorum = mgr.quorum_size
    quorum_achieved = (healthy_voters_count >= req_quorum)
    quorum_obj = {
        "voters": voters_list,
        "healthy_voters": healthy_voters_count,
        "minimum_required": req_quorum,
        "quorum_achieved": quorum_achieved
    }

    # Cluster capacity info
    cluster_util = util.get("cluster", {})
    cluster_obj = {
        "total_nodes": summary.get("cluster", {}).get("total_nodes", 0),
        "online_nodes": summary.get("cluster", {}).get("online", 0),
        "active_cores": cluster_util.get("cpu_cores_total", 0),
        "memory_total_mb": cluster_util.get("memory_total_mb", 0.0),
        "memory_used_mb": cluster_util.get("memory_used_mb", 0.0)
    }

    # Nodes array
    nodes_array = []
    raw_nodes = nodes_db.get("nodes", {})
    for nid, raw_node in raw_nodes.items():
        if raw_node.get("status") == STATE_REMOVED:
            continue
        apply_default_leadership_metadata(nid, raw_node)
        s_node = sanitize_node_record(raw_node, timeout_sec)

        eligible, reason = evaluate_node_eligibility(raw_node, timeout_sec)
        score = calculate_node_performance_score(raw_node, timeout_sec) if eligible else 0.0

        # Authoritative leader is strictly determined by valid lease matching node_id
        is_node_leader = bool(lease_valid and (nid == leader_obj["node_id"]))

        addr = raw_node.get("tailscale_ip") or raw_node.get("ip") or raw_node.get("host") or ""

        nodes_array.append({
            "node_id": nid,
            "name": raw_node.get("name") or raw_node.get("node_name") or nid,
            "address": addr,
            "role": raw_node.get("role", "compute"),
            "status": s_node.get("status", "UNKNOWN"),
            "is_leader": is_node_leader,
            "leader_eligible": eligible,
            "merit_score": score,
            "ineligibility_reason": reason,
            "last_heartbeat": s_node.get("last_heartbeat") or raw_node.get("last_seen") or ""
        })

    # Sort nodes: leader first, then online by merit score desc, then name asc
    nodes_array.sort(key=lambda n: (
        not n["is_leader"],
        n["status"] != STATE_ONLINE,
        -n["merit_score"],
        n["name"].lower()
    ))

    # Controller status
    controller_obj = {
        "local_node_id": mgr.node_id,
        "local_role": mgr.state,
        "local_term": mgr.current_term,
        "lease_valid": lease_valid
    }

    return {
        "cluster_id": nodes_db.get("cluster_name", "PersonalServer"),
        "state_version": getattr(mgr, "state_version", 1),
        "snapshot_timestamp": get_current_iso_timestamp(),
        "leader": leader_obj,
        "quorum": quorum_obj,
        "cluster": cluster_obj,
        "nodes": nodes_array,
        "controller": controller_obj
    }


def _resolve_dynamic_controller_url(handler: Any) -> str:
    """Derives the active leader URL dynamically without hardcoded addresses."""
    mgr = get_ha_manager()
    if mgr.current_leader_url:
        return mgr.current_leader_url.rstrip("/")
    if mgr.node_url:
        return mgr.node_url.rstrip("/")
    hdrs = getattr(handler, "headers", None) or {}
    host_hdr = hdrs.get("Host") or hdrs.get("host")
    if host_hdr:
        return f"http://{host_hdr}".rstrip("/")
    return "http://127.0.0.1:8000"


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

    # GET /enrollment/readiness
    if path == "/enrollment/readiness":
        token = handler.parse_auth_token()
        if not _require_admin_auth(handler, token, "Unauthorized: Authentication required for recruitment readiness"):
            return True
        mgr = get_ha_manager()
        lease_valid = bool(mgr.current_lease.is_valid())
        leader_available = bool(mgr.is_leader and lease_valid)
        blockers = []
        if not mgr.is_leader:
            blockers.append(f"Controller role is '{mgr.state}', not active leader.")
        if not lease_valid:
            blockers.append("Leader lease is not currently valid.")
        handler.send_json(200, {
            "leader_available": leader_available,
            "recruitment_ready": leader_available,
            "role": mgr.state,
            "node_id": mgr.node_id,
            "node_url": mgr.node_url,
            "leader_id": mgr.current_leader_id,
            "leader_name": getattr(mgr.current_lease, "leader_name", None) or mgr.node_id,
            "leader_url": mgr.current_leader_url or mgr.node_url,
            "lease_valid": lease_valid,
            "lease_remaining_sec": round(mgr.current_lease.time_remaining_sec(), 1),
            "term": mgr.current_term,
            "candidate_urls": mgr.candidate_urls,
            "tailscale_private_required": True,
            "blockers": blockers,
        })
        return True

    # GET /enrollment/sessions
    if path == "/enrollment/sessions":
        token = handler.parse_auth_token()
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required for enrollment sessions"):
            return True
        nodes_db = load_nodes_db()
        sessions = list_enrollment_sessions(nodes_db=nodes_db, timeout_seconds=handler.heartbeat_timeout)
        handler.send_json(200, {"sessions": sessions, "count": len(sessions)})
        return True

    # GET /enrollment/sessions/<session_id>
    if path.startswith("/enrollment/sessions/"):
        token = handler.parse_auth_token()
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required for enrollment session"):
            return True
        session_id = path[len("/enrollment/sessions/"):].strip("/")
        if not session_id or "/" in session_id:
            handler.send_json(404, {"error": "Enrollment session not found"})
            return True
        nodes_db = load_nodes_db()
        sess = get_enrollment_session(session_id, nodes_db=nodes_db, timeout_seconds=handler.heartbeat_timeout)
        if not sess:
            handler.send_json(404, {"error": f"Enrollment session '{session_id}' not found"})
            return True
        handler.send_json(200, {"session": sess})
        return True

    # GET /enrollment/audit
    if path == "/enrollment/audit":
        token = handler.parse_auth_token()
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required for enrollment audit"):
            return True
        limit = int(query.get("limit", [100])[0])
        filter_node_id = query.get("node_id", [None])[0]
        entries = get_audit_log(limit=limit, node_id=filter_node_id)
        handler.send_json(200, {"audit": entries, "count": len(entries)})
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

    # GET /cluster/state or /api/cluster/state
    if path in ("/cluster/state", "/api/cluster/state"):
        state = build_cluster_state(handler)
        handler.send_json(200, state)
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
        include_removed = str(query.get("include_removed", ["false"])[0]).lower() in ("1", "true", "yes")

        if filter_status:
            filter_status = filter_status.upper()
            nodes_list = [n for n in nodes_list if n["status"] == filter_status]
        elif not include_removed:
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

    # GET /nodes/<node_id>/hardware or /api/nodes/<node_id>/hardware
    is_hw = False
    hw_node_id = None
    if norm_path.endswith("/hardware"):
        if norm_path.startswith("/api/nodes/"):
            hw_node_id = norm_path[11:-9].strip("/")
            is_hw = True
        elif norm_path.startswith("/nodes/"):
            hw_node_id = norm_path[7:-9].strip("/")
            is_hw = True

    if is_hw and hw_node_id:
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for hardware access"})
            return True

        node_id = hw_node_id
        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

        if is_local:
            try:
                from agent.hardware.manager import HardwareCapabilityManager
                mgr = HardwareCapabilityManager.get_instance()
                status = mgr.get_full_status()
                handler.send_json(200, {"node_id": node_id, "hardware": status})
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Local hardware query error: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        node_status = (node.get("status") or "").upper()
        if node_status == "OFFLINE":
            handler.send_json(200, {
                "node_id": node_id,
                "status": "offline",
                "error": f"Node '{node_id}' is OFFLINE. Cannot query live hardware.",
                "hardware": node.get("verified_capabilities", {}).get("hardware", {})
            })
            return True

        node_ip = node.get("ip")
        if node_ip:
            try:
                node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
                req = urllib.request.Request(
                    f"http://{node_ip}:8080/hardware",
                    headers={
                        "User-Agent": "PersonalServer-Controller/1.0",
                        "Authorization": f"Bearer {node_auth_token}"
                    }
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    handler.send_json(200, {"node_id": node_id, "hardware": data.get("hardware", data)})
                    return True
            except Exception as e:
                handler.send_json(200, {
                    "node_id": node_id,
                    "status": "cached",
                    "warning": f"Live hardware query to {node_ip}:8080 timed out or failed: {e}",
                    "hardware": node.get("verified_capabilities", {}).get("hardware", {})
                })
                return True

        handler.send_json(200, {
            "node_id": node_id,
            "status": "cached",
            "hardware": node.get("verified_capabilities", {}).get("hardware", {})
        })
        return True

    # GET /nodes/<node_id>/hardware/microphone/recording or /api/nodes/<node_id>/hardware/microphone/recording
    is_mic_rec = False
    mic_rec_node_id = None
    if norm_path.endswith("/hardware/microphone/recording"):
        if norm_path.startswith("/api/nodes/"):
            mic_rec_node_id = norm_path[11:-len("/hardware/microphone/recording")].strip("/")
            is_mic_rec = True
        elif norm_path.startswith("/nodes/"):
            mic_rec_node_id = norm_path[7:-len("/hardware/microphone/recording")].strip("/")
            is_mic_rec = True

    if is_mic_rec and mic_rec_node_id:
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for recording download"})
            return True

        session_id = (query.get("session_id", [""])[0] or "").strip()
        if session_id and not re.match(r"^[A-Za-z0-9_-]{4,64}$", session_id):
            handler.send_json(400, {"error": "Invalid 'session_id' parameter"})
            return True
        download_flag = query.get("download", ["0"])[0] == "1"

        node_id = mic_rec_node_id
        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

        if is_local:
            try:
                from agent.hardware.manager import HardwareCapabilityManager
                from agent.device_runtime.android.models import MIC_MAX_FILE_BYTES
                mgr = HardwareCapabilityManager.get_instance()
                wav_path, meta = mgr.microphone.get_recording_file(session_id or None)
                if not wav_path or not wav_path.exists():
                    handler.send_json(404, {"error": "Completed recording not found or expired"})
                    return True
                raw_bytes = wav_path.read_bytes()
                if len(raw_bytes) > MIC_MAX_FILE_BYTES:
                    handler.send_json(413, {"error": "Recording exceeds maximum transfer size bound"})
                    return True
                disp = "attachment" if download_flag else "inline"
                fname = (meta or {}).get("file_name") or wav_path.name
                handler.send_response(200)
                handler.send_header("Content-Type", "audio/wav")
                handler.send_header("Content-Length", str(len(raw_bytes)))
                handler.send_header("Content-Disposition", f'{disp}; filename="{fname}"')
                if meta and meta.get("sha256"):
                    handler.send_header("X-Recording-Sha256", str(meta["sha256"]))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Access-Control-Allow-Origin", "*")
                handler.end_headers()
                handler.wfile.write(raw_bytes)
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Local recording retrieval error: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True
        if (node.get("status") or "").upper() == "OFFLINE":
            handler.send_json(503, {"error": f"Node '{node_id}' is OFFLINE. Recording download unavailable."})
            return True
        node_ip = node.get("ip")
        if not node_ip:
            handler.send_json(502, {"error": f"Node '{node_id}' has no reachable IP address."})
            return True

        try:
            from agent.device_runtime.android.models import MIC_MAX_FILE_BYTES
            q_params = {}
            if session_id:
                q_params["session_id"] = session_id
            if download_flag:
                q_params["download"] = "1"
            q_str = urllib.parse.urlencode(q_params)
            node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
            remote_url = f"http://{node_ip}:8080/hardware/microphone/recording" + (f"?{q_str}" if q_str else "")
            req = urllib.request.Request(
                remote_url,
                headers={
                    "User-Agent": "PersonalServer-Controller/1.0",
                    "Authorization": f"Bearer {node_auth_token}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw_bytes = resp.read(MIC_MAX_FILE_BYTES + 1024)
                if len(raw_bytes) > MIC_MAX_FILE_BYTES:
                    handler.send_json(413, {"error": "Remote recording exceeds maximum transfer size bound"})
                    return True
                if len(raw_bytes) < 12 or raw_bytes[:4] != b"RIFF" or raw_bytes[8:12] != b"WAVE":
                    handler.send_json(502, {"error": "Remote audio failed RIFF/WAVE container verification"})
                    return True
                handler.send_response(200)
                handler.send_header("Content-Type", resp.headers.get("Content-Type", "audio/wav"))
                handler.send_header("Content-Length", str(len(raw_bytes)))
                cd = resp.headers.get("Content-Disposition") or f'inline; filename="mic_{session_id or "latest"}.wav"'
                handler.send_header("Content-Disposition", cd)
                sha_hdr = resp.headers.get("X-Recording-Sha256")
                if sha_hdr:
                    handler.send_header("X-Recording-Sha256", sha_hdr)
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Access-Control-Allow-Origin", "*")
                handler.end_headers()
                handler.wfile.write(raw_bytes)
                return True
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_data = {"error": f"Node returned HTTP {e.code}: {e.reason}"}
            handler.send_json(e.code, {"node_id": node_id, **err_data})
            return True
        except Exception as e:
            handler.send_json(503, {"error": f"Failed to retrieve recording from node '{node_id}': {e}"})
            return True

    # GET /nodes/<node_id>/live-camera or /api/nodes/<node_id>/live-camera
    is_live_cam = False
    live_cam_node_id = None
    if norm_path.endswith("/live-camera"):
        if norm_path.startswith("/api/nodes/"):
            live_cam_node_id = norm_path[11:-12].strip("/")
            is_live_cam = True
        elif norm_path.startswith("/nodes/"):
            live_cam_node_id = norm_path[7:-12].strip("/")
            is_live_cam = True

    if is_live_cam and live_cam_node_id:
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for live camera streaming"})
            return True

        node_id = live_cam_node_id
        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

        if is_local:
            try:
                from agent.hardware.manager import HardwareCapabilityManager
                mgr = HardwareCapabilityManager.get_instance()
                status = mgr.camera.get_live_camera_status()
                handler.send_json(200, {"node_id": node_id, "live_camera": status})
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Local live camera query error: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        node_status = (node.get("status") or "").upper()
        if node_status == "OFFLINE":
            handler.send_json(200, {
                "node_id": node_id,
                "status": "OFFLINE",
                "error": f"Node '{node_id}' is OFFLINE. Live camera unavailable."
            })
            return True

        node_ip = node.get("ip")
        if node_ip:
            try:
                node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
                req = urllib.request.Request(
                    f"http://{node_ip}:8080/live-camera",
                    headers={
                        "User-Agent": "PersonalServer-Controller/1.0",
                        "Authorization": f"Bearer {node_auth_token}"
                    }
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    handler.send_json(200, {"node_id": node_id, **data})
                    return True
            except Exception as e:
                handler.send_json(503, {
                    "node_id": node_id,
                    "error": f"Failed to reach live camera on node '{node_id}' ({node_ip}:8080): {e}"
                })
                return True

        handler.send_json(404, {"error": f"Node '{node_id}' has no reachable IP."})
        return True

    # GET /nodes/<node_id>/live-call/frame, /stream, /audio, or /status (E07) and CCTV aliases
    matched_get_sub = next((tag for tag in ("/live-call/", "/camera/", "/cctv/") if tag in norm_path), None)
    if matched_get_sub:
        is_stream = norm_path.endswith("/stream")
        is_frame = norm_path.endswith("/frame")
        is_audio = norm_path.endswith("/audio")
        is_status = norm_path.endswith("/status")
        if is_stream or is_frame or is_audio or is_status:
            prefix = "/api/nodes/" if norm_path.startswith("/api/nodes/") else "/nodes/"
            sub_after_prefix = norm_path[len(prefix):]
            node_id = sub_after_prefix.split(matched_get_sub)[0].strip("/")

            token = handler.parse_auth_token()
            enrollment_token = get_or_create_enrollment_token()
            if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                handler.send_json(401, {"error": "Unauthorized: Authentication required for live call access"})
                return True

            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

            if is_local:
                try:
                    from agent.hardware.manager import HardwareCapabilityManager
                    mgr = HardwareCapabilityManager.get_instance()
                    if is_status:
                        handler.send_json(200, {"node_id": node_id, "status": mgr.live_call.get_status()})
                        return True
                    elif is_audio:
                        import base64
                        chunk, sr, ch = mgr.live_call.get_audio_chunk(max_bytes=16000)
                        b64_data = base64.b64encode(chunk).decode("ascii") if chunk else ""
                        handler.send_json(200, {
                            "node_id": node_id,
                            "active": True,
                            "pcm_base64": b64_data,
                            "sample_rate": sr,
                            "channels": ch,
                            "bytes": len(chunk)
                        })
                        return True
                    elif is_stream:
                        handler.send_response(200)
                        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0")
                        handler.send_header("Pragma", "no-cache")
                        handler.send_header("Connection", "close")
                        handler.send_header("Access-Control-Allow-Origin", "*")
                        handler.end_headers()

                        last_ts = 0.0
                        stream_active = True
                        while stream_active:
                            cur_sess = mgr.live_call.current_session
                            if not cur_sess or cur_sess.state.value != "ACTIVE":
                                break
                            frame, ts = mgr.live_call.get_latest_frame_with_timestamp()
                            if frame and ts > last_ts:
                                last_ts = ts
                                part_header = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode("utf-8")
                                try:
                                    handler.wfile.write(part_header + frame + b"\r\n")
                                    handler.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                                    stream_active = False
                                    break
                            mgr.live_call.wait_for_next_frame(timeout=0.08)
                        return True
                    else:
                        frame = mgr.live_call.get_latest_frame()
                        if not frame:
                            handler.send_json(503, {"error": "No video frame available yet"})
                            return True
                        handler.send_response(200)
                        handler.send_header("Content-Type", "image/jpeg")
                        handler.send_header("Content-Length", str(len(frame)))
                        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                        handler.send_header("Access-Control-Allow-Origin", "*")
                        handler.end_headers()
                        try:
                            handler.wfile.write(frame)
                        except Exception:
                            pass
                        return True
                except Exception as e:
                    handler.send_json(500, {"error": f"Local live call error: {e}"})
                    return True

            if not node:
                handler.send_json(404, {"error": f"Node '{node_id}' not found"})
                return True

            node_ip = node.get("ip")
            if not node_ip:
                handler.send_json(502, {"error": f"Node '{node_id}' has no reachable IP"})
                return True

            node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()

            if is_stream:
                remote_path = f"http://{node_ip}:8080/hardware/live-call/stream"
            elif is_frame:
                remote_path = f"http://{node_ip}:8080/hardware/live-call/frame"
            elif is_audio:
                remote_path = f"http://{node_ip}:8080/hardware/live-call/audio"
            else:
                remote_path = f"http://{node_ip}:8080/hardware"

            try:
                if is_status:
                    req_data = json.dumps({"operation": "GET_LIVE_CALL_STATUS", "params": {}}).encode("utf-8")
                    req = urllib.request.Request(
                        remote_path,
                        data=req_data,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "PersonalServer-Controller/1.0",
                            "Authorization": f"Bearer {node_auth_token}"
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        handler.send_json(200, {"node_id": node_id, **data})
                        return True
                elif is_audio:
                    req = urllib.request.Request(
                        remote_path,
                        headers={
                            "User-Agent": "PersonalServer-Controller/1.0",
                            "Authorization": f"Bearer {node_auth_token}"
                        }
                    )
                    with urllib.request.urlopen(req, timeout=2.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        handler.send_json(200, {"node_id": node_id, **data})
                        return True
                elif is_stream:
                    req = urllib.request.Request(
                        remote_path,
                        headers={
                            "User-Agent": "PersonalServer-Controller/1.0",
                            "Authorization": f"Bearer {node_auth_token}"
                        }
                    )
                    handler.send_response(200)
                    handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0")
                    handler.send_header("Pragma", "no-cache")
                    handler.send_header("Connection", "close")
                    handler.send_header("Access-Control-Allow-Origin", "*")
                    handler.end_headers()

                    stream_active = True
                    with urllib.request.urlopen(req, timeout=60.0) as resp:
                        while stream_active:
                            try:
                                chunk = resp.read(8192)
                                if not chunk:
                                    break
                                handler.wfile.write(chunk)
                                handler.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                                stream_active = False
                                break
                    return True
                else:
                    req = urllib.request.Request(
                        remote_path,
                        headers={
                            "User-Agent": "PersonalServer-Controller/1.0",
                            "Authorization": f"Bearer {node_auth_token}"
                        }
                    )
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        raw_frame = resp.read()
                        handler.send_response(200)
                        handler.send_header("Content-Type", "image/jpeg")
                        handler.send_header("Content-Length", str(len(raw_frame)))
                        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                        handler.send_header("Access-Control-Allow-Origin", "*")
                        handler.end_headers()
                        try:
                            handler.wfile.write(raw_frame)
                        except Exception:
                            pass
                        return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                handler.close_connection = True
                return True
            except Exception as e:
                handler.send_json(503, {"error": f"Failed reaching live call on node '{node_id}': {e}"})
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
        if not _is_private_mesh_request(handler):
            handler.send_json(403, {"error": "Public enrollment is disabled. Enroll over the private Tailscale network."})
            return True

        onboarding_code = handler.headers.get("X-Onboarding-Code", "").strip()

        node_id = body.get("node_id")
        if not node_id:
            handler.send_json(400, {"error": "Missing required field 'node_id'"})
            return True

        client_ip = (
            handler.client_address[0]
            if (handler.client_address and handler.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0"))
            else None
        )

        session_meta = None
        if onboarding_code:
            is_valid, err, session_meta = validate_and_consume_onboarding_details(
                onboarding_code, node_id, client_ip=client_ip or (handler.client_address[0] if handler.client_address else None)
            )
            if not is_valid:
                handler.send_json(401, {"error": f"Unauthorized: {err}"})
                return True
        else:
            enrollment_token = get_or_create_enrollment_token()
            if not token or not constant_time_compare(token, enrollment_token):
                handler.send_json(401, {"error": "Unauthorized: Invalid enrollment token"})
                return True

        nodes_db = load_nodes_db()
        existing_entry = nodes_db.get("nodes", {}).get(node_id)

        default_name = (session_meta or {}).get("node_name") or (existing_entry.get("name") if existing_entry else None) or "unknown"
        node_name = body.get("name") if body.get("name") not in (None, "", "unknown") else default_name
        if session_meta and session_meta.get("node_name"):
            node_name = session_meta["node_name"]

        node_role = (session_meta or {}).get("role") or body.get("role") or (existing_entry.get("role") if existing_entry else "compute")
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

        now = get_current_iso_timestamp()
        node_auth_token = secrets.token_hex(20)

        is_experimental = (node_role == "experimental-worker")
        if session_meta and "worker_enabled" in session_meta:
            worker_enabled = bool(session_meta["worker_enabled"])
        else:
            worker_enabled = bool(body.get("worker_enabled", False if is_experimental else True))

        if session_meta and "scheduler_eligible" in session_meta:
            scheduler_eligible = bool(session_meta["scheduler_eligible"])
        else:
            scheduler_eligible = bool(body.get("scheduler_eligible", False if is_experimental else True))

        if not worker_enabled or is_experimental:
            if not (session_meta and session_meta.get("scheduler_eligible") and not is_experimental):
                scheduler_eligible = False if (is_experimental or not worker_enabled) else scheduler_eligible

        leader_eligible = bool(body.get("leader_eligible", existing_entry.get("leader_eligible", False) if existing_entry else False))
        preferred_leader = bool(body.get("preferred_leader", existing_entry.get("preferred_leader", False) if existing_entry else False))
        default_part = "OFF" if (is_experimental or not worker_enabled) else "AVAILABLE"
        participation_state = str(body.get("participation_state", default_part)).upper()
        if not worker_enabled:
            participation_state = "OFF"

        replaces_node_id = (session_meta or {}).get("replaces_node_id")
        if replaces_node_id and replaces_node_id in nodes_db.get("nodes", {}) and replaces_node_id != node_id:
            old_node = nodes_db["nodes"][replaces_node_id]
            old_node["status"] = STATE_REMOVED
            old_node["auth_token"] = None
            old_node["removed_at"] = now
            old_node["replaced_at"] = now
            old_node["replaced_by_node_id"] = node_id
            old_node["worker_enabled"] = False
            old_node["scheduler_eligible"] = False
            old_node["participation_state"] = "OFF"
            append_audit_event(
                "node.replaced",
                node_id=replaces_node_id,
                session_id=(session_meta or {}).get("session_id"),
                actor="owner",
                correlation_id=getattr(handler, "current_correlation_id", None),
                details={"replaced_by_node_id": node_id},
            )

        existing_perms = existing_entry.get("capability_permissions") if existing_entry else None
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
            "worker_enabled": worker_enabled,
            "scheduler_eligible": scheduler_eligible,
            "leader_eligible": leader_eligible,
            "preferred_leader": preferred_leader,
            "participation_state": participation_state,
            "auth_token": node_auth_token,
            "ip": (body.get("ip") or body.get("tailscale_ip") or client_ip),
            "status": STATE_ONLINE,
            "registered_at": (existing_entry.get("registered_at") if existing_entry else None) or now,
            "last_seen": now,
            "last_heartbeat": {
                "timestamp": now,
                "services": {},
                "system": resources
            },
            "rejoin_count": (existing_entry.get("rejoin_count", 0) + 1) if existing_entry else 0,
        }
        if existing_entry:
            node_record["rejoined_at"] = now
            if existing_entry.get("replaced_from_node_id"):
                node_record["replaced_from_node_id"] = existing_entry["replaced_from_node_id"]
        if replaces_node_id:
            node_record["replaced_from_node_id"] = replaces_node_id

        node_record["capability_permissions"] = build_initial_capability_permissions(
            node_record, existing_permissions=existing_perms
        )

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

        mgr = get_ha_manager()
        ctrl_urls = [u for u in ([mgr.current_leader_url or mgr.node_url] + list(mgr.candidate_urls or [])) if u]
        handler.send_json(200, {
            "status": "registered",
            "node_id": node_id,
            "auth_token": node_auth_token,
            "registered_at": node_record["registered_at"],
            "controller_urls": list(dict.fromkeys(ctrl_urls)),
            "canonical_node": {
                "node_id": node_id,
                "name": node_name,
                "role": node_role,
                "worker_enabled": worker_enabled,
                "scheduler_eligible": scheduler_eligible,
                "participation_state": participation_state,
            },
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

        if "participation_state" in body:
            node_entry["participation_state"] = str(body["participation_state"]).upper()

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
        node_entry["capability_permissions"] = build_initial_capability_permissions(
            node_entry, existing_permissions=node_entry.get("capability_permissions")
        )

        save_nodes_db(nodes_db)

        handler.send_json(200, {
            "status": "ok",
            "ack": True,
            "node_id": node_id,
            "timestamp": now
        })
        return True

    # 2a. POST /enrollment/sessions (Create scoped single-use enrollment session)
    if path == "/enrollment/sessions":
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to create enrollment sessions"):
            return True
        if not isinstance(body, dict):
            body = {}
        try:
            ctrl_url = _resolve_dynamic_controller_url(handler)
            res = create_enrollment_session(
                mode=body.get("mode", "new"),
                platform_name=body.get("platform", "linux"),
                node_name=body.get("node_name") or body.get("name"),
                role=body.get("role", "compute"),
                target_node_id=body.get("target_node_id") or body.get("node_id"),
                replaces_node_id=body.get("replaces_node_id"),
                worker_enabled=body.get("worker_enabled"),
                scheduler_eligible=body.get("scheduler_eligible"),
                ttl_seconds=body.get("ttl_seconds", 900),
                actor=body.get("actor") or "owner",
                controller_url=ctrl_url,
                gateway_url=body.get("gateway_url", ""),
                correlation_id=getattr(handler, "current_correlation_id", None),
            )
            handler.send_json(201, res)
        except ValueError as e:
            handler.send_json(400, {"error": str(e)})
        return True

    # 2b. POST /enrollment/sessions/<session_id>/revoke
    if path.startswith("/enrollment/sessions/") and path.endswith("/revoke"):
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to revoke enrollment sessions"):
            return True
        session_id = path[len("/enrollment/sessions/"):-len("/revoke")].strip("/")
        reason = (body or {}).get("reason", "Revoked by owner") if isinstance(body, dict) else "Revoked by owner"
        revoked = revoke_enrollment_session(
            session_id,
            actor="owner",
            reason=reason,
            correlation_id=getattr(handler, "current_correlation_id", None),
        )
        if not revoked:
            handler.send_json(404, {"error": f"Enrollment session '{session_id}' not found"})
            return True
        handler.send_json(200, {"status": "REVOKED", "session": revoked})
        return True

    # 3. POST /nodes/<node_id>/drain
    if path.startswith("/nodes/") and path.endswith("/drain"):
        node_id = path[7:-6].strip()
        if not _require_admin_auth(handler, token, "Unauthorized: Admin enrollment token required to drain a node"):
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
        append_audit_event(
            "node.drained",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"jobs_rescheduled": jobs_rescheduled},
        )
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
        if not _require_admin_auth(handler, token, "Unauthorized: Admin enrollment token required to deboard a node"):
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
        append_audit_event(
            "node.deboarded",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"can_remove": eval_res["can_remove"], "blockers": eval_res["blockers"]},
        )
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
        if not _require_admin_auth(handler, token, "Unauthorized: Admin enrollment token required to resume a node"):
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

        append_audit_event(
            "node.resumed",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
        )
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

        append_audit_event(
            "node.removed",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
        )
        print(f"[CONTROLLER] Safely removed node: {node_id}")
        handler.send_json(200, {
            "status": STATE_REMOVED,
            "node_id": node_id,
            "message": f"Node '{node_id}' has been safely removed from active cluster membership."
        })
        return True

    # 6a. POST /nodes/<node_id>/rename (Preserves immutable node_id)
    if (path.startswith("/nodes/") or path.startswith("/api/nodes/")) and path.endswith("/rename"):
        raw_prefix = "/api/nodes/" if path.startswith("/api/nodes/") else "/nodes/"
        node_id = path[len(raw_prefix):-7].strip("/")
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to rename a node"):
            return True
        if not isinstance(body, dict) or not str(body.get("name", "")).strip():
            handler.send_json(400, {"error": "Missing non-empty 'name' field"})
            return True
        new_name = str(body["name"]).strip()
        if len(new_name) > 80:
            handler.send_json(400, {"error": "Node name must be 80 characters or fewer"})
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True
        old_name = node_entry.get("name")
        node_entry["name"] = new_name
        node_entry["renamed_at"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        append_audit_event(
            "node.renamed",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"old_name": old_name, "new_name": new_name},
        )
        handler.send_json(200, {
            "status": "renamed",
            "node_id": node_id,
            "old_name": old_name,
            "name": new_name,
            "node": sanitize_node_record(node_entry, handler.heartbeat_timeout),
        })
        return True

    # 6b. POST /nodes/<node_id>/disable and /enable
    if (path.startswith("/nodes/") or path.startswith("/api/nodes/")) and (path.endswith("/disable") or path.endswith("/enable")):
        is_disable = path.endswith("/disable")
        suffix = "/disable" if is_disable else "/enable"
        raw_prefix = "/api/nodes/" if path.startswith("/api/nodes/") else "/nodes/"
        node_id = path[len(raw_prefix):-len(suffix)].strip("/")
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to toggle node participation"):
            return True
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True
        if node_entry.get("status") == STATE_REMOVED:
            handler.send_json(400, {"error": f"Cannot change removed node '{node_id}'. Re-enroll first."})
            return True

        now = get_current_iso_timestamp()
        if is_disable:
            node_entry["disabled"] = True
            node_entry["disabled_at"] = now
            node_entry["worker_enabled"] = False
            node_entry["scheduler_eligible"] = False
            node_entry["participation_state"] = "OFF"
            node_entry.setdefault("capability_permissions", {})["scheduling"] = "DENIED"
        else:
            node_entry["disabled"] = False
            node_entry.pop("disabled_at", None)
            req_worker = bool(body.get("worker_enabled", True)) if isinstance(body, dict) else True
            # Never silently grant scheduling when enabling unless explicitly opted in
            req_sched = bool(body.get("scheduler_eligible", False)) if isinstance(body, dict) else False
            node_entry["worker_enabled"] = req_worker
            node_entry["scheduler_eligible"] = bool(req_worker and req_sched)
            node_entry["participation_state"] = "AVAILABLE" if req_worker else "OFF"
            if node_entry["scheduler_eligible"]:
                node_entry.setdefault("capability_permissions", {})["scheduling"] = "APPROVED"

        save_nodes_db(nodes_db)
        append_audit_event(
            "node.disabled" if is_disable else "node.enabled",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={
                "worker_enabled": node_entry["worker_enabled"],
                "scheduler_eligible": node_entry["scheduler_eligible"],
                "participation_state": node_entry["participation_state"],
            },
        )
        handler.send_json(200, {
            "status": "disabled" if is_disable else "enabled",
            "node_id": node_id,
            "node": sanitize_node_record(node_entry, handler.heartbeat_timeout),
        })
        return True

    # 6c. POST /nodes/<node_id>/credentials/rotate
    if (path.startswith("/nodes/") or path.startswith("/api/nodes/")) and path.endswith("/credentials/rotate"):
        raw_prefix = "/api/nodes/" if path.startswith("/api/nodes/") else "/nodes/"
        node_id = path[len(raw_prefix):-len("/credentials/rotate")].strip("/")
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to rotate node credentials"):
            return True
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        now = get_current_iso_timestamp()
        node_entry["auth_token"] = None
        node_entry["credentials_rotated_at"] = now
        save_nodes_db(nodes_db)

        raw_plat = (node_entry.get("platform") or "linux").lower()
        plat_name = "android-termux" if "termux" in raw_plat or "android" in raw_plat else ("windows" if "win" in raw_plat else "linux")
        ttl_sec = (body or {}).get("ttl_seconds", 900) if isinstance(body, dict) else 900
        ctrl_url = _resolve_dynamic_controller_url(handler)
        gw_url = (body or {}).get("gateway_url", "") if isinstance(body, dict) else ""

        sess_res = create_enrollment_session(
            mode="rejoin",
            platform_name=plat_name,
            node_name=node_entry.get("name"),
            role=node_entry.get("role", "compute"),
            target_node_id=node_id,
            worker_enabled=node_entry.get("worker_enabled"),
            scheduler_eligible=node_entry.get("scheduler_eligible"),
            ttl_seconds=ttl_sec,
            actor="owner",
            controller_url=ctrl_url,
            gateway_url=gw_url,
            correlation_id=getattr(handler, "current_correlation_id", None),
        )
        append_audit_event(
            "node.credentials_rotated",
            node_id=node_id,
            session_id=sess_res["session"]["session_id"],
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"old_token_revoked": True},
        )
        handler.send_json(200, {
            "status": "rotated",
            "node_id": node_id,
            "old_token_revoked": True,
            **sess_res,
        })
        return True

    # 6d. POST /nodes/<node_id>/capabilities/review
    if (path.startswith("/nodes/") or path.startswith("/api/nodes/")) and path.endswith("/capabilities/review"):
        raw_prefix = "/api/nodes/" if path.startswith("/api/nodes/") else "/nodes/"
        node_id = path[len(raw_prefix):-len("/capabilities/review")].strip("/")
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to review capabilities"):
            return True
        if not isinstance(body, dict):
            handler.send_json(400, {"error": "Invalid JSON body"})
            return True
        decisions = body.get("decisions")
        if not isinstance(decisions, dict):
            cap = str(body.get("capability", "")).strip()
            dec = str(body.get("decision", "")).strip().upper()
            decisions = {cap: dec} if cap else {}
        if not decisions:
            handler.send_json(400, {"error": "Provide 'decisions' map or 'capability' and 'decision'"})
            return True

        valid_decisions = {"APPROVED", "DENIED", "PENDING_REVIEW", "REVOKED"}
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        perms = build_initial_capability_permissions(node_entry, node_entry.get("capability_permissions"))
        applied = {}
        for cap, raw_dec in decisions.items():
            dec_up = str(raw_dec).strip().upper()
            if dec_up not in valid_decisions:
                handler.send_json(400, {"error": f"Invalid decision '{raw_dec}' for capability '{cap}'"})
                return True
            perms[str(cap)] = dec_up
            applied[str(cap)] = dec_up

        if "scheduling" in applied:
            node_entry["scheduler_eligible"] = (applied["scheduling"] == "APPROVED" and bool(node_entry.get("worker_enabled", True)) and not node_entry.get("disabled"))

        node_entry["capability_permissions"] = perms
        node_entry["capabilities_reviewed_at"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        append_audit_event(
            "node.capabilities_reviewed",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"decisions": applied},
        )
        handler.send_json(200, {
            "status": "ok",
            "node_id": node_id,
            "capability_permissions": perms,
            "node": sanitize_node_record(node_entry, handler.heartbeat_timeout),
        })
        return True

    # 7. POST /nodes/<node_id>/policy or POST /api/nodes/<node_id>/policy
    if (path.startswith("/nodes/") or path.startswith("/api/nodes/")) and path.endswith("/policy"):
        raw_prefix = "/api/nodes/" if path.startswith("/api/nodes/") else "/nodes/"
        node_id = path[len(raw_prefix):-7].strip()
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to configure node policy"):
            return True

        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(node_id)
        if not node_entry:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        if not isinstance(body, dict):
            handler.send_json(400, {"error": "Invalid request body: Expected JSON object"})
            return True

        from controller.leader_ranking import apply_default_leadership_metadata
        apply_default_leadership_metadata(node_id, node_entry)

        if "role" in body:
            new_role = str(body["role"]).strip().lower()
            if new_role not in VALID_ROLES:
                handler.send_json(400, {"error": f"Invalid role '{new_role}'. Allowed: {VALID_ROLES}"})
                return True
            node_entry["role"] = new_role
        if "leader_eligible" in body:
            node_entry["leader_eligible"] = bool(body["leader_eligible"])
        if "priority" in body:
            try:
                node_entry["priority"] = max(0, min(100, int(body["priority"])))
            except (ValueError, TypeError):
                handler.send_json(400, {"error": "Invalid priority: Expected integer 0-100"})
                return True
        if "preferred_leader" in body:
            pref = bool(body["preferred_leader"])
            node_entry["preferred_leader"] = pref
            if pref:
                # Exclusive preferred leader: reset others
                for other_id, other_node in nodes_db.get("nodes", {}).items():
                    if other_id != node_id:
                        other_node["preferred_leader"] = False
        if "worker_enabled" in body:
            node_entry["worker_enabled"] = bool(body["worker_enabled"])
            if not node_entry["worker_enabled"]:
                node_entry["scheduler_eligible"] = False
        if "scheduler_eligible" in body:
            req_sched = bool(body["scheduler_eligible"])
            node_entry["scheduler_eligible"] = bool(req_sched and node_entry.get("worker_enabled", True) and not node_entry.get("disabled"))
            node_entry.setdefault("capability_permissions", {})["scheduling"] = "APPROVED" if node_entry["scheduler_eligible"] else "DENIED"
        if "participation_state" in body:
            ps = str(body["participation_state"]).strip().upper()
            if ps not in ("READY", "AVAILABLE", "PAUSED", "OFF"):
                handler.send_json(400, {"error": f"Invalid participation_state '{ps}'"})
                return True
            node_entry["participation_state"] = ps
        if "build_worker_enabled" in body:
            node_entry["build_worker_enabled"] = bool(body["build_worker_enabled"])

        node_entry["policy_updated_at"] = get_current_iso_timestamp()
        save_nodes_db(nodes_db)

        append_audit_event(
            "node.configured",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={k: body[k] for k in ("role", "worker_enabled", "scheduler_eligible", "participation_state", "leader_eligible", "priority", "build_worker_enabled") if k in body},
        )

        sanitized = sanitize_node_record(node_entry, handler.heartbeat_timeout)
        handler.send_json(200, {
            "status": "ok",
            "node_id": node_id,
            "policy": {
                "role": sanitized.get("role"),
                "leader_eligible": sanitized.get("leader_eligible"),
                "preferred_leader": sanitized.get("preferred_leader"),
                "priority": sanitized.get("priority"),
                "performance_score": sanitized.get("performance_score"),
                "worker_enabled": sanitized.get("worker_enabled"),
                "scheduler_eligible": sanitized.get("scheduler_eligible"),
                "participation_state": sanitized.get("participation_state"),
                "build_worker_enabled": sanitized.get("build_worker_enabled"),
            },
            "node": sanitized
        })
        return True

    # 8. POST /nodes/<node_id>/hardware or POST /api/nodes/<node_id>/hardware
    norm_path = path.rstrip("/")
    if (norm_path.startswith("/nodes/") or norm_path.startswith("/api/nodes/")) and norm_path.endswith("/hardware"):
        raw_prefix = "/api/nodes/" if norm_path.startswith("/api/nodes/") else "/nodes/"
        node_id = norm_path[len(raw_prefix):-9].strip("/")
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to execute hardware operations"})
            return True

        if not isinstance(body, dict):
            handler.send_json(400, {"error": "Invalid request body: Expected JSON object"})
            return True

        operation = body.get("operation")
        if not operation:
            handler.send_json(400, {"error": "Missing required field 'operation'"})
            return True

        params = body.get("params", body)
        if isinstance(params, dict) and "operation" in params and params is body:
            params = {k: v for k, v in params.items() if k != "operation"}

        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

        op_upper = str(operation).upper().strip()
        if op_upper in ("START_RECORDING", "START_MIC"):
            if node and isinstance(node.get("capability_permissions"), dict):
                mic_perm = node["capability_permissions"].get("microphone")
                if mic_perm is not None:
                    perm_str = mic_perm.get("status") if isinstance(mic_perm, dict) else str(mic_perm)
                    if str(perm_str).upper() != "APPROVED":
                        handler.send_json(403, {
                            "error": f"Microphone capability permission is '{perm_str}'; explicit owner approval is required first.",
                            "code": "CAPABILITY_NOT_APPROVED",
                            "node_id": node_id,
                        })
                        return True
            if op_upper == "START_RECORDING" and (not isinstance(params, dict) or params.get("authorized") is not True):
                handler.send_json(400, {
                    "error": "Explicit user authorization ('authorized': true) is required to start microphone recording",
                    "code": "UNAUTHORIZED",
                    "node_id": node_id,
                })
                return True

        if is_local:
            try:
                from agent.hardware.manager import HardwareCapabilityManager
                mgr = HardwareCapabilityManager.get_instance()
                res = mgr.execute_operation(operation, parameters=params)
                status_code = 200 if res.get("success") else 400
                handler.send_json(status_code, {"node_id": node_id, **res})
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Failed to execute local hardware operation: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        node_status = (node.get("status") or "").upper()
        if node_status == "OFFLINE":
            handler.send_json(503, {
                "error": f"Cannot execute hardware operation: Node '{node_id}' is OFFLINE.",
                "node_id": node_id,
                "operation": operation
            })
            return True

        node_ip = node.get("ip")
        if not node_ip:
            handler.send_json(502, {"error": f"Node '{node_id}' has no reachable IP address recorded."})
            return True

        try:
            node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
            req_data = json.dumps({"operation": operation, "params": params}).encode("utf-8")
            req = urllib.request.Request(
                f"http://{node_ip}:8080/hardware",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "PersonalServer-Controller/1.0",
                    "Authorization": f"Bearer {node_auth_token}"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                handler.send_json(resp.status, {"node_id": node_id, **data})
                return True
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_data = {"error": f"Node returned HTTP {e.code}: {e.reason}"}
            handler.send_json(e.code, {"node_id": node_id, **err_data})
            return True
        except Exception as e:
            handler.send_json(503, {
                "error": f"Failed to dispatch hardware operation to node '{node_id}' ({node_ip}:8080): {e}",
                "node_id": node_id,
                "operation": operation
            })
            return True

    # 9. POST /nodes/<node_id>/live-camera or POST /api/nodes/<node_id>/live-camera
    if (norm_path.startswith("/nodes/") or norm_path.startswith("/api/nodes/")) and norm_path.endswith("/live-camera"):
        raw_prefix = "/api/nodes/" if norm_path.startswith("/api/nodes/") else "/nodes/"
        node_id = norm_path[len(raw_prefix):-12].strip("/")
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required for live camera streaming"})
            return True

        if not isinstance(body, dict):
            handler.send_json(400, {"error": "Invalid request body: Expected JSON object"})
            return True

        op = body.get("operation") or body.get("action") or "START_LIVE_CAMERA"
        params = body.get("parameters") or body.get("params") or body
        if isinstance(params, dict) and params is body:
            params = {k: v for k, v in params.items() if k not in ("operation", "action", "parameters", "params")}

        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)
        is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

        if is_local:
            try:
                from agent.hardware.manager import HardwareCapabilityManager
                mgr = HardwareCapabilityManager.get_instance()
                res = mgr.execute_operation(op, parameters=params)
                status_code = 200 if res.get("success") else 400
                handler.send_json(status_code, {"node_id": node_id, **res})
                return True
            except Exception as e:
                handler.send_json(500, {"error": f"Failed to execute local live camera operation: {e}"})
                return True

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        node_status = (node.get("status") or "").upper()
        if node_status == "OFFLINE":
            handler.send_json(503, {
                "error": f"Cannot stream camera: Node '{node_id}' is OFFLINE.",
                "node_id": node_id
            })
            return True

        node_ip = node.get("ip")
        if not node_ip:
            handler.send_json(502, {"error": f"Node '{node_id}' has no reachable IP address."})
            return True

        try:
            node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
            req_data = json.dumps({"operation": op, "params": params}).encode("utf-8")
            req = urllib.request.Request(
                f"http://{node_ip}:8080/live-camera",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "PersonalServer-Controller/1.0",
                    "Authorization": f"Bearer {node_auth_token}"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                handler.send_json(resp.status, {"node_id": node_id, **data})
                return True
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_data = {"error": f"Node returned HTTP {e.code}: {e.reason}"}
            handler.send_json(e.code, {"node_id": node_id, **err_data})
            return True
        except Exception as e:
            handler.send_json(503, {
                "error": f"Failed to connect to live camera bridge on node '{node_id}' ({node_ip}:8080): {e}",
                "node_id": node_id
            })
            return True

    # 10. POST /nodes/<node_id>/live-call/<action> or /camera/<action> or /cctv/<action>
    matched_post_sub = next((tag for tag in ("/live-call/", "/camera/", "/cctv/") if tag in norm_path), None)
    if matched_post_sub:
        prefix = "/api/nodes/" if norm_path.startswith("/api/nodes/") else "/nodes/"
        sub = norm_path[len(prefix):]
        if matched_post_sub in sub:
            parts = sub.split(matched_post_sub, 1)
            node_id = parts[0].strip("/")
            action = parts[1].strip("/").lower()
            enrollment_token = get_or_create_enrollment_token()
            if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
                handler.send_json(401, {"error": "Unauthorized: Authentication required for live call"})
                return True

            if action == "ice":
                handler.send_json(200, {"success": True, "node_id": node_id, "ack": True})
                return True

            op_map = {
                "start": "START_LIVE_CALL",
                "stop": "STOP_LIVE_CALL",
                "update": "UPDATE_LIVE_CALL",
                "speak": "LIVE_CALL_SPEAK"
            }
            op_name = op_map.get(action)
            if not op_name:
                handler.send_json(400, {"error": f"Unknown live call action: '{action}'"})
                return True

            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            is_local = (node_id in ("local", "self") or (node and node.get("ip") in ("127.0.0.1", "localhost", "::1")))

            if is_local:
                try:
                    from agent.hardware.manager import HardwareCapabilityManager
                    mgr = HardwareCapabilityManager.get_instance()
                    res = mgr.execute_operation(op_name, parameters=body)
                    status_code = 200 if res.get("success") else 400
                    handler.send_json(status_code, {"node_id": node_id, **res})
                    return True
                except Exception as e:
                    handler.send_json(500, {"error": f"Local live call execution error: {e}"})
                    return True

            if not node:
                handler.send_json(404, {"error": f"Node '{node_id}' not found"})
                return True

            node_status = (node.get("status") or "").upper()
            if node_status == "OFFLINE":
                handler.send_json(503, {
                    "error": f"Cannot execute live call: Node '{node_id}' is OFFLINE.",
                    "node_id": node_id,
                    "action": action
                })
                return True

            node_ip = node.get("ip")
            if not node_ip:
                handler.send_json(502, {"error": f"Node '{node_id}' has no reachable IP recorded."})
                return True

            node_auth_token = (node.get("auth_token") or enrollment_token or "").strip()
            try:
                if action == "speak":
                    remote_url = f"http://{node_ip}:8080/hardware/live-call/speak"
                    req_data = json.dumps(body).encode("utf-8")
                else:
                    remote_url = f"http://{node_ip}:8080/hardware"
                    req_data = json.dumps({"operation": op_name, "params": body}).encode("utf-8")

                req = urllib.request.Request(
                    remote_url,
                    data=req_data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "PersonalServer-Controller/1.0",
                        "Authorization": f"Bearer {node_auth_token}"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    handler.send_json(resp.status, {"node_id": node_id, **data})
                    return True
            except urllib.error.HTTPError as e:
                try:
                    err_data = json.loads(e.read().decode("utf-8"))
                except Exception:
                    err_data = {"error": f"Node returned HTTP {e.code}: {e.reason}"}
                if action == "start" and ((err_data.get("result") or {}).get("error") == "SESSION_ALREADY_ACTIVE" or err_data.get("error") == "SESSION_ALREADY_ACTIVE"):
                    active_sess = (err_data.get("result") or {}).get("session", {})
                    handler.send_json(200, {
                        "node_id": node_id,
                        "success": True,
                        "status": "ACTIVE",
                        "session_id": active_sess.get("session_id", "active"),
                        "session": active_sess,
                        "already_active": True
                    })
                    return True
                handler.send_json(e.code, {"node_id": node_id, **err_data})
                return True
            except Exception as e:
                handler.send_json(503, {
                    "error": f"Failed dispatching live call action '{action}' to node '{node_id}': {e}",
                    "node_id": node_id
                })
                return True

    return False


def handle_node_delete(handler: Any, path: str, token: str) -> bool:
    """
    Handles DELETE requests for revoking enrollment sessions and removing cluster nodes.
    Returns True if handled, False otherwise.
    """
    if path.startswith("/enrollment/sessions/"):
        if not _require_admin_auth(handler, token, "Unauthorized: Admin authentication required to revoke enrollment sessions"):
            return True
        session_id = path[len("/enrollment/sessions/"):].strip("/")
        revoked = revoke_enrollment_session(
            session_id,
            actor="owner",
            reason="Revoked via DELETE",
            correlation_id=getattr(handler, "current_correlation_id", None),
        )
        if not revoked:
            handler.send_json(404, {"error": f"Enrollment session '{session_id}' not found"})
            return True
        handler.send_json(200, {"status": "REVOKED", "session": revoked})
        return True

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

        append_audit_event(
            "node.removed",
            node_id=node_id,
            actor="owner",
            correlation_id=getattr(handler, "current_correlation_id", None),
            details={"method": "DELETE"},
        )
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
