#!/usr/bin/env python3
"""
Controller Onboarding & Enrollment Subsystem
===========================================
Single-use cryptographic onboarding tokens, scoped enrollment sessions,
QR/link setup generation, revocation, and audit logging.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from common.qr_utils import generate_qr_svg
from config.platform_config import get_controller_paths, get_controller_runtime_config
from controller.constants import (
    JOB_STATE_CLAIMED,
    JOB_STATE_RUNNING,
    STATE_ONLINE,
    STATE_REMOVED,
    VALID_ROLES,
)
from controller.stores import (
    ONBOARDING_LOCK,
    load_jobs_db,
    load_nodes_db,
    load_onboarding_codes_db,
    save_onboarding_codes_db,
)

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_ONBOARDING_TTL_SEC = RUNTIME_CFG.onboarding_ttl_sec  # default: 15 minutes (900s)
MAX_AUDIT_EVENTS = 500
SUPPORTED_PLATFORMS = ("android-termux", "windows", "linux")
ENROLLMENT_MODES = ("new", "rejoin", "replace")
SENSITIVE_HARDWARE_KEYS = (
    "camera",
    "live_camera",
    "microphone",
    "speaker",
    "sensors",
    "sensor.accelerometer",
    "sensor.gyroscope",
    "sensor.magnetometer",
    "sensor.ambient_light",
    "sensor.proximity",
)


def hash_onboarding_code(code: str) -> str:
    """Computes a SHA-256 hash of the normalized onboarding code for secure storage."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def get_or_create_enrollment_token() -> str:
    """
    Retrieves or generates the persistent cluster enrollment token stored at
    config/secrets/enrollment.token with 0600 permissions.
    """
    paths = get_controller_paths()
    paths.ensure_directories()
    token_file = paths.enrollment_token_file
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_hex(24)
    token_file.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except Exception:
        pass
    return token


def _mint_code_string() -> str:
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    return f"PS-{part1}-{part2}"


def _prune_stale_codes_locked(db: Dict[str, Any], now_ts: float) -> None:
    filtered_codes = {}
    for h, info in db.get("codes", {}).items():
        try:
            exp_ts = datetime.fromisoformat(info["expires_at"]).timestamp()
            if now_ts - exp_ts < 86400:
                filtered_codes[h] = info
        except Exception:
            pass
    db["codes"] = filtered_codes

    filtered_sessions = {}
    for sid, sess in db.get("sessions", {}).items():
        try:
            exp_ts = datetime.fromisoformat(sess["expires_at"]).timestamp()
            # Keep consumed/revoked/recent sessions for up to 7 days for UI diagnostics
            if now_ts - exp_ts < 86400 * 7 or sess.get("consumed_at"):
                filtered_sessions[sid] = sess
        except Exception:
            pass
    if "sessions" in db:
        db["sessions"] = filtered_sessions


def _append_audit_unlocked(
    db: Dict[str, Any],
    event: str,
    node_id: Optional[str] = None,
    session_id: Optional[str] = None,
    actor: str = "owner",
    details: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    audit_list = db.setdefault("audit_log", [])
    entry = {
        "audit_id": f"aud-{secrets.token_hex(6)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "actor": actor or "owner",
        "node_id": node_id,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "details": details or {},
    }
    audit_list.append(entry)
    if len(audit_list) > MAX_AUDIT_EVENTS:
        db["audit_log"] = audit_list[-MAX_AUDIT_EVENTS:]
    return entry


def append_audit_event(
    event: str,
    node_id: Optional[str] = None,
    session_id: Optional[str] = None,
    actor: str = "owner",
    details: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Thread-safe persistent audit record writer for node and enrollment lifecycle."""
    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        entry = _append_audit_unlocked(
            db,
            event=event,
            node_id=node_id,
            session_id=session_id,
            actor=actor,
            details=details,
            correlation_id=correlation_id,
        )
        save_onboarding_codes_db(db)
        return entry


def get_audit_log(limit: int = 100, node_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns newest-first enrollment and node lifecycle audit entries."""
    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        entries = list(db.get("audit_log", []))
    if node_id:
        entries = [e for e in entries if e.get("node_id") == node_id]
    entries.reverse()
    return entries[: max(1, min(int(limit or 100), MAX_AUDIT_EVENTS))]


def generate_onboarding_code(ttl_seconds: int = DEFAULT_ONBOARDING_TTL_SEC) -> Tuple[str, int]:
    """
    Generates a cryptographically secure, single-use onboarding code.
    Format: PS-XXXX-XXXX
    Stores SHA-256 hash in onboarding_codes.json.
    Returns (code_str, ttl_seconds).
    """
    code = _mint_code_string()
    code_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        _prune_stale_codes_locked(db, now_dt.timestamp())
        db.setdefault("codes", {})[code_hash] = {
            "created_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "used": False,
            "used_at": None,
            "used_by_node": None,
            "revoked": False,
        }
        save_onboarding_codes_db(db)

    return code, ttl_seconds


def build_device_setup_instructions(
    platform_name: str,
    code: str,
    controller_url: str,
    gateway_url: str,
    session_id: str,
    expected_node_id: str,
    mode: str = "new",
    role: str = "compute",
    node_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates OS-specific zero-SSH instructions, copyable commands, and private QR data."""
    plat = (platform_name or "linux").strip().lower()
    if plat not in SUPPORTED_PLATFORMS:
        plat = "linux"

    ctrl = (controller_url or "").rstrip("/")
    gw = (gateway_url or "").rstrip("/")
    base_link_host = gw or ctrl
    join_url = f"{base_link_host}/enroll?session={session_id}#code={code}" if base_link_host else ""

    role_flag = f" --role {role}" if role else ""
    name_flag = f' --name "{node_name}"' if node_name else ""
    id_flag = f" --node-id {expected_node_id}" if expected_node_id else ""

    if plat == "windows":
        bootstrap_cmd = "powershell -ExecutionPolicy Bypass -Command \"python -m pip install -e .\""
        stage_cmd = f"[IO.File]::WriteAllText('config\\secrets\\onboarding.code', '{code}')"
        run_cmd = f"ps-worker run --controller {ctrl}{id_flag}{role_flag}{name_flag}"
        one_liner = f"{stage_cmd}; {run_cmd}"
        steps = [
            "Open PowerShell in the PersonalServer checkout on the Windows worker (already on Tailscale).",
            "Stage the single-use code (automatically deleted on enrollment) and start ps-worker:",
            "Wait for registration and first heartbeat on the wizard progress tracker, then approve capabilities.",
        ]
    elif plat == "android-termux":
        bootstrap_cmd = "./install/termux.sh"
        enroll_cmd = f"printf '%s\\n' '{code}' | python3 agent/node-agent.py onboard --controller {ctrl}{id_flag}{role_flag}{name_flag} --code -"
        one_liner = f"cd ~/PersonalServer && {enroll_cmd}"
        stage_cmd = bootstrap_cmd
        run_cmd = enroll_cmd
        steps = [
            "Connect the Android device to the same Tailscale tailnet and open Termux.",
            "If first-time setup, run ./install/termux.sh inside ~/PersonalServer.",
            "Scan the QR code from the device (over Tailscale) or paste the single-use command below:",
        ]
    else:
        bootstrap_cmd = "./bootstrap.sh"
        enroll_cmd = f"printf '%s\\n' '{code}' | python3 agent/node-agent.py onboard --controller {ctrl}{id_flag}{role_flag}{name_flag} --code -"
        one_liner = f"cd ~/PersonalServer && {enroll_cmd}"
        stage_cmd = bootstrap_cmd
        run_cmd = enroll_cmd
        steps = [
            "Ensure the Linux host is connected to the private Tailscale network.",
            "If first-time setup, run ./bootstrap.sh inside ~/PersonalServer.",
            "Paste the single-use command below to bind the scoped node identity and start services:",
        ]

    qr_svg = generate_qr_svg(join_url) if join_url else ""

    return {
        "platform": plat,
        "mode": mode,
        "controller_url": ctrl,
        "join_url": join_url,
        "qr_svg": qr_svg,
        "bootstrap_command": bootstrap_cmd,
        "stage_command": stage_cmd,
        "enroll_command": run_cmd,
        "one_liner": one_liner,
        "steps": steps,
    }


def create_enrollment_session(
    mode: str = "new",
    platform_name: str = "linux",
    node_name: Optional[str] = None,
    role: str = "compute",
    target_node_id: Optional[str] = None,
    replaces_node_id: Optional[str] = None,
    worker_enabled: Optional[bool] = None,
    scheduler_eligible: Optional[bool] = None,
    ttl_seconds: int = DEFAULT_ONBOARDING_TTL_SEC,
    actor: str = "owner",
    controller_url: str = "",
    gateway_url: str = "",
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates an authenticated, scoped, single-use, expiring enrollment session.
    Returns dict containing session metadata, single-use plaintext code (returned ONCE),
    QR SVG, and device setup commands. Plaintext code is never persisted.
    """
    norm_mode = (mode or "new").strip().lower()
    if norm_mode not in ENROLLMENT_MODES:
        raise ValueError(f"Invalid enrollment mode '{mode}'. Expected one of {ENROLLMENT_MODES}.")

    norm_plat = (platform_name or "linux").strip().lower()
    if norm_plat not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform '{platform_name}'. Expected one of {SUPPORTED_PLATFORMS}.")

    norm_role = (role or "compute").strip().lower()
    if norm_role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Expected one of {VALID_ROLES}.")

    try:
        ttl_val = int(ttl_seconds or DEFAULT_ONBOARDING_TTL_SEC)
    except (TypeError, ValueError):
        raise ValueError("Invalid TTL seconds.")
    if ttl_val < 60 or ttl_val > 3600:
        raise ValueError("TTL must be between 60 and 3600 seconds.")

    nodes_db = load_nodes_db()
    nodes = nodes_db.get("nodes", {})

    clean_target = (target_node_id or "").strip() or None
    clean_replaces = (replaces_node_id or "").strip() or None
    clean_name = (node_name or "").strip() or None

    if norm_mode == "rejoin":
        if not clean_target or clean_target not in nodes:
            raise ValueError("Rejoin requires an existing registered/historical target_node_id.")
        existing = nodes[clean_target]
        if existing.get("replaced_by_node_id"):
            raise ValueError(
                f"Node '{clean_target}' was permanently replaced by '{existing['replaced_by_node_id']}'. "
                "Recruit a new node instead."
            )
        expected_node_id = clean_target
        if not clean_name:
            clean_name = existing.get("name")
        if role is None:
            norm_role = existing.get("role", norm_role)
    elif norm_mode == "replace":
        if not clean_replaces or clean_replaces not in nodes:
            raise ValueError("Replacement requires an existing replaces_node_id in the registry.")
        old_node = nodes[clean_replaces]
        if old_node.get("replaced_by_node_id"):
            raise ValueError(f"Node '{clean_replaces}' was already replaced by '{old_node['replaced_by_node_id']}'.")
        # Prevent replacing a node that currently owns running/claimed jobs
        jobs_db = load_jobs_db()
        active_jobs = [
            jid for jid, job in jobs_db.get("jobs", {}).items()
            if job.get("target_node") == clean_replaces and job.get("status") in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING)
        ]
        if active_jobs:
            raise ValueError(
                f"Cannot replace '{clean_replaces}' while {len(active_jobs)} job(s) are active. Drain it first."
            )
        plat_tag = norm_plat.split("-")[0]
        expected_node_id = clean_target or f"server-{plat_tag}-{secrets.token_hex(5)}"
        if expected_node_id in nodes:
            raise ValueError(f"Replacement target node_id '{expected_node_id}' already exists.")
        if not clean_name:
            clean_name = f"{old_node.get('name', clean_replaces)} (Replacement)"
    else:
        plat_tag = norm_plat.split("-")[0]
        expected_node_id = clean_target or f"server-{plat_tag}-{secrets.token_hex(5)}"
        if clean_target and clean_target in nodes and nodes[clean_target].get("status") != STATE_REMOVED:
            raise ValueError(
                f"Node '{clean_target}' already exists in cluster. Use Rejoin mode to preserve its identity."
            )

    is_experimental = norm_role == "experimental-worker"
    eff_worker_enabled = bool(False if worker_enabled is None and is_experimental else (True if worker_enabled is None else worker_enabled))
    eff_scheduler_eligible = bool(False if scheduler_eligible is None and is_experimental else (False if scheduler_eligible is None else scheduler_eligible))
    if not eff_worker_enabled:
        eff_scheduler_eligible = False

    session_id = f"enr-{secrets.token_hex(6)}"
    code = _mint_code_string()
    code_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_val, tz=timezone.utc)

    session_record = {
        "session_id": session_id,
        "code_hash_prefix": code_hash[:12],
        "mode": norm_mode,
        "platform": norm_plat,
        "expected_node_id": expected_node_id,
        "node_name": clean_name or expected_node_id,
        "role": norm_role,
        "replaces_node_id": clean_replaces if norm_mode == "replace" else None,
        "worker_enabled": eff_worker_enabled,
        "scheduler_eligible": eff_scheduler_eligible,
        "status": "PENDING",
        "created_at": now_dt.isoformat(),
        "expires_at": expires_dt.isoformat(),
        "ttl_seconds": ttl_val,
        "created_by": actor or "owner",
        "consumed_at": None,
        "consumed_by_node": None,
        "consumed_ip": None,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
        "last_error": None,
        "attempts": [],
        "controller_url": controller_url,
    }

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        _prune_stale_codes_locked(db, now_dt.timestamp())
        db.setdefault("codes", {})[code_hash] = {
            "session_id": session_id,
            "created_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "expected_node_id": expected_node_id,
            "mode": norm_mode,
            "replaces_node_id": clean_replaces if norm_mode == "replace" else None,
            "used": False,
            "used_at": None,
            "used_by_node": None,
            "revoked": False,
        }
        db.setdefault("sessions", {})[session_id] = session_record
        _append_audit_unlocked(
            db,
            event="session.created",
            node_id=expected_node_id,
            session_id=session_id,
            actor=actor or "owner",
            correlation_id=correlation_id,
            details={
                "mode": norm_mode,
                "platform": norm_plat,
                "role": norm_role,
                "replaces_node_id": clean_replaces if norm_mode == "replace" else None,
                "ttl_seconds": ttl_val,
                "worker_enabled": eff_worker_enabled,
                "scheduler_eligible": eff_scheduler_eligible,
            },
        )
        save_onboarding_codes_db(db)

    instructions = build_device_setup_instructions(
        platform_name=norm_plat,
        code=code,
        controller_url=controller_url,
        gateway_url=gateway_url,
        session_id=session_id,
        expected_node_id=expected_node_id,
        mode=norm_mode,
        role=norm_role,
        node_name=clean_name,
    )

    return {
        "session": compute_session_live_view(session_record, nodes_db=nodes_db),
        "onboarding_code": code,
        "instructions": instructions,
    }


def validate_and_consume_onboarding_details(
    code: str,
    node_id: str,
    client_ip: Optional[str] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Thread-safe validation and atomic single-use consumption of an onboarding code.
    Enforces revocation, expiry, single-use, and node_id scoping.
    Returns (is_valid, error_reason, session_metadata).
    """
    if not code or not code.strip():
        return False, "Missing onboarding code", None

    input_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        codes = db.get("codes", {})
        sessions = db.setdefault("sessions", {})

        matched_entry = None
        for h, entry in codes.items():
            if hmac.compare_digest(h, input_hash):
                matched_entry = entry
                break

        if not matched_entry:
            _append_audit_unlocked(
                db,
                event="session.failed_attempt",
                node_id=node_id,
                actor="node",
                details={"reason": "Invalid onboarding code", "client_ip": client_ip},
            )
            save_onboarding_codes_db(db)
            return False, "Invalid onboarding code", None

        session_id = matched_entry.get("session_id")
        session = sessions.get(session_id) if session_id else None

        def record_failure(reason: str) -> None:
            if session is not None:
                session["last_error"] = reason
                attempts = session.setdefault("attempts", [])
                attempts.append({
                    "timestamp": now_dt.isoformat(),
                    "node_id": node_id,
                    "ip": client_ip,
                    "result": "REJECTED",
                    "reason": reason,
                })
                if len(attempts) > 20:
                    session["attempts"] = attempts[-20:]
            _append_audit_unlocked(
                db,
                event="session.failed_attempt",
                node_id=node_id,
                session_id=session_id,
                actor="node",
                details={"reason": reason, "client_ip": client_ip},
            )
            save_onboarding_codes_db(db)

        if matched_entry.get("revoked") or (session and session.get("revoked_at")):
            msg = "Onboarding session has been revoked"
            record_failure(msg)
            return False, msg, None

        if matched_entry.get("used"):
            msg = "Onboarding code has already been used"
            record_failure(msg)
            return False, msg, None

        try:
            expires_at = datetime.fromisoformat(matched_entry["expires_at"])
            if now_dt > expires_at:
                msg = "Onboarding code has expired"
                record_failure(msg)
                return False, msg, None
        except Exception:
            msg = "Corrupt expiration timestamp on onboarding code"
            record_failure(msg)
            return False, msg, None

        expected_node_id = matched_entry.get("expected_node_id") or (session.get("expected_node_id") if session else None)
        if expected_node_id and node_id != expected_node_id:
            msg = f"Onboarding code is scoped to node '{expected_node_id}', rejected '{node_id}'"
            record_failure(msg)
            return False, msg, None

        # Atomically consume code & mark session registered
        matched_entry["used"] = True
        matched_entry["used_at"] = now_dt.isoformat()
        matched_entry["used_by_node"] = node_id

        session_snapshot = None
        if session is not None:
            session["status"] = "REGISTERED"
            session["consumed_at"] = now_dt.isoformat()
            session["consumed_by_node"] = node_id
            session["consumed_ip"] = client_ip
            session["last_error"] = None
            attempts = session.setdefault("attempts", [])
            attempts.append({
                "timestamp": now_dt.isoformat(),
                "node_id": node_id,
                "ip": client_ip,
                "result": "ACCEPTED",
                "reason": "Registered successfully",
            })
            session_snapshot = dict(session)
        elif expected_node_id or matched_entry.get("mode"):
            session_snapshot = {
                "expected_node_id": expected_node_id,
                "mode": matched_entry.get("mode", "new"),
                "replaces_node_id": matched_entry.get("replaces_node_id"),
            }

        _append_audit_unlocked(
            db,
            event="session.consumed",
            node_id=node_id,
            session_id=session_id,
            actor="node",
            details={
                "client_ip": client_ip,
                "mode": matched_entry.get("mode", "new"),
                "replaces_node_id": matched_entry.get("replaces_node_id"),
            },
        )
        save_onboarding_codes_db(db)
        return True, "", session_snapshot


def validate_and_consume_onboarding_code(code: str, node_id: str) -> Tuple[bool, str]:
    """Backwards-compatible wrapper returning (is_valid, error_reason)."""
    ok, err, _ = validate_and_consume_onboarding_details(code, node_id)
    return ok, err


def revoke_enrollment_session(
    session_id: str,
    actor: str = "owner",
    reason: str = "Revoked by owner",
    correlation_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Revokes an active enrollment session and invalidates its hashed code immediately."""
    now_dt = datetime.now(timezone.utc)
    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        session = db.get("sessions", {}).get(session_id)
        if not session:
            return None

        if not session.get("revoked_at") and not session.get("consumed_at"):
            session["revoked_at"] = now_dt.isoformat()
            session["revoked_by"] = actor or "owner"
            session["revoke_reason"] = reason
            session["status"] = "REVOKED"
            for entry in db.get("codes", {}).values():
                if entry.get("session_id") == session_id:
                    entry["revoked"] = True

            _append_audit_unlocked(
                db,
                event="session.revoked",
                node_id=session.get("expected_node_id"),
                session_id=session_id,
                actor=actor or "owner",
                correlation_id=correlation_id,
                details={"reason": reason},
            )
            save_onboarding_codes_db(db)

        return dict(session)


def compute_session_live_view(
    session: Dict[str, Any],
    nodes_db: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 45,
) -> Dict[str, Any]:
    """Enriches a stored enrollment session with real-time progress, TTL, and node diagnostics."""
    from scheduler.scheduler import compute_node_liveness

    view = dict(session)
    now_dt = datetime.now(timezone.utc)
    try:
        exp_dt = datetime.fromisoformat(session["expires_at"])
        remaining_sec = max(0, int((exp_dt - now_dt).total_seconds()))
    except Exception:
        remaining_sec = 0

    view["remaining_sec"] = remaining_sec
    view["is_expired"] = remaining_sec <= 0 and not session.get("consumed_at") and not session.get("revoked_at")

    live_status = session.get("status", "PENDING")
    diagnostics = []
    node_snapshot = None

    if session.get("revoked_at"):
        live_status = "REVOKED"
        diagnostics.append(f"Revoked at {session['revoked_at']}: {session.get('revoke_reason') or 'owner action'}")
    elif session.get("consumed_at"):
        target_id = session.get("consumed_by_node") or session.get("expected_node_id")
        if nodes_db and target_id and target_id in nodes_db.get("nodes", {}):
            raw_node = nodes_db["nodes"][target_id]
            liveness = compute_node_liveness(raw_node, timeout_seconds)
            if liveness == STATE_ONLINE:
                live_status = "ONLINE"
            elif liveness == STATE_REMOVED:
                live_status = "REMOVED"
            else:
                live_status = "REGISTERED"
                try:
                    cons_dt = datetime.fromisoformat(session["consumed_at"])
                    if (now_dt - cons_dt).total_seconds() > 30:
                        diagnostics.append(
                            "Node registered, but no live heartbeat received yet. Verify worker/daemon is running over Tailscale."
                        )
                except Exception:
                    pass
            caps = raw_node.get("verified_capabilities") or raw_node.get("capabilities") or {}
            perms = raw_node.get("capability_permissions") or {}
            pending_review = [k for k, v in perms.items() if v == "PENDING_REVIEW"]
            node_snapshot = {
                "node_id": target_id,
                "name": raw_node.get("name"),
                "role": raw_node.get("role"),
                "status": liveness,
                "ip": raw_node.get("ip"),
                "last_seen": raw_node.get("last_seen"),
                "worker_enabled": raw_node.get("worker_enabled"),
                "scheduler_eligible": raw_node.get("scheduler_eligible"),
                "participation_state": raw_node.get("participation_state"),
                "capability_count": len(caps),
                "capability_permissions": perms,
                "pending_permission_reviews": pending_review,
            }
        else:
            live_status = "REGISTERED"
    elif remaining_sec <= 0:
        live_status = "EXPIRED"
        diagnostics.append("Enrollment credential expired before a node completed registration.")

    if session.get("last_error"):
        diagnostics.append(f"Last error: {session['last_error']}")

    view["live_status"] = live_status
    view["diagnostics"] = diagnostics
    view["node"] = node_snapshot
    return view


def list_enrollment_sessions(nodes_db: Optional[Dict[str, Any]] = None, timeout_seconds: int = 45) -> List[Dict[str, Any]]:
    """Returns newest-first enrollment sessions enriched with live node progress."""
    if nodes_db is None:
        nodes_db = load_nodes_db()
    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        sessions = list(db.get("sessions", {}).values())
    sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return [compute_session_live_view(s, nodes_db=nodes_db, timeout_seconds=timeout_seconds) for s in sessions]


def get_enrollment_session(
    session_id: str,
    nodes_db: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 45,
) -> Optional[Dict[str, Any]]:
    """Returns a single enriched enrollment session or None if missing."""
    if nodes_db is None:
        nodes_db = load_nodes_db()
    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        sess = db.get("sessions", {}).get(session_id)
        sess_copy = dict(sess) if sess else None
    if not sess_copy:
        return None
    return compute_session_live_view(sess_copy, nodes_db=nodes_db, timeout_seconds=timeout_seconds)


def build_initial_capability_permissions(
    node_record: Dict[str, Any],
    existing_permissions: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Initializes explicit permission review states for discovered capabilities.
    Sensitive hardware & scheduling capabilities are NEVER silently approved on enrollment.
    """
    perms: Dict[str, str] = dict(existing_permissions or {})
    discovered = set()
    for src in (
        node_record.get("capabilities") or {},
        node_record.get("verified_capabilities") or {},
        node_record.get("advertised_capabilities") or {},
    ):
        if isinstance(src, dict):
            for k, v in src.items():
                if v:
                    discovered.add(str(k))

    for cap in sorted(discovered):
        if cap in SENSITIVE_HARDWARE_KEYS or cap.startswith("sensor."):
            perms.setdefault(cap, "PENDING_REVIEW")
        else:
            perms.setdefault(cap, "APPROVED")

    if "scheduling" not in perms:
        perms["scheduling"] = "APPROVED" if node_record.get("scheduler_eligible") else "PENDING_REVIEW"

    return perms


__all__ = [
    "DEFAULT_ONBOARDING_TTL_SEC",
    "ENROLLMENT_MODES",
    "SENSITIVE_HARDWARE_KEYS",
    "SUPPORTED_PLATFORMS",
    "append_audit_event",
    "build_device_setup_instructions",
    "build_initial_capability_permissions",
    "compute_session_live_view",
    "create_enrollment_session",
    "generate_onboarding_code",
    "get_audit_log",
    "get_enrollment_session",
    "get_or_create_enrollment_token",
    "hash_onboarding_code",
    "list_enrollment_sessions",
    "revoke_enrollment_session",
    "validate_and_consume_onboarding_code",
    "validate_and_consume_onboarding_details",
]
