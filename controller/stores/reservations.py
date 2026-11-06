#!/usr/bin/env python3
"""
Controller Resource Reservation Store & State Machine
======================================================
Manages atomic resource allocations, provisional holds, committed leases,
and lifecycle state machine for the Universal Scheduler (J3.3).

States:
- PROVISIONAL: Ephemeral evaluation hold during candidate scoring pass (30s commit window).
- COMMITTED: Lease granted, job ASSIGNED, awaiting process startup ack (30s TTL).
- ACTIVE: Worker acknowledged RUNNING; actively executing. Does NOT expire by commit TTL.
- RELEASING: Workload terminated or cancelled; cleaning up scratch and output verification.
- RELEASED: Terminal state; resources restored to node allocatable pool.
- EXPIRED: Provisional/committed timer elapsed without acknowledgment; sweeper reclaims.
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from controller.stores.base import DB_LOCK
from controller.stores.jobs_store import load_jobs_db, save_jobs_db


class ReservationState:
    PROVISIONAL = "PROVISIONAL"
    COMMITTED = "COMMITTED"
    ACTIVE = "ACTIVE"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


PROVISIONAL_COMMIT_TTL_SEC = 30.0
RELEASING_GRACE_PERIOD_SEC = 15.0


def atomic_reservation_update(update_fn):
    """
    Executes an atomic read-modify-write operation on the jobs DB with DB_LOCK held.
    Guarantees consistency across scheduler passes, lease sweeper passes, and worker requests.
    """
    with DB_LOCK:
        jobs_db = load_jobs_db()
        changed, result = update_fn(jobs_db)
        if changed:
            save_jobs_db(jobs_db)
        return result


def calculate_node_reserved_resources(jobs_db: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    """
    Sums all actively committed, provisional, active, or releasing resource allocations for a node.
    EXPIRED and RELEASED reservations do not consume capacity.
    """
    now = time.time()
    res_map = jobs_db.get("reservations", {})
    total_mem = 0.0
    total_cpu = 0
    total_storage = 0.0
    exclusive_devices = set()

    for res in res_map.values():
        if res.get("node_id") != node_id:
            continue
        state = res.get("state")
        # Check if provisional/committed expired in-memory before sweep
        commit_exp = res.get("commit_expires_at")
        if state in (ReservationState.PROVISIONAL, ReservationState.COMMITTED):
            if commit_exp is not None and now > float(commit_exp):
                continue

        if state in (ReservationState.PROVISIONAL, ReservationState.COMMITTED, ReservationState.ACTIVE, ReservationState.RELEASING):
            reqs = res.get("resources", {})
            total_mem += float(reqs.get("memory_mb", 0.0))
            total_cpu += int(reqs.get("cpu_cores", 0))
            total_storage += float(reqs.get("storage_gb", 0.0))
            for dev in reqs.get("exclusive_devices", []):
                exclusive_devices.add(dev)

    return {
        "reserved_memory_mb": total_mem,
        "reserved_cpu_cores": total_cpu,
        "reserved_storage_gb": total_storage,
        "exclusive_devices": list(exclusive_devices)
    }


def create_or_update_reservation(
    jobs_db: Dict[str, Any],
    job_id: str,
    node_id: str,
    attempt: int,
    fencing_token: int,
    resources: Dict[str, Any],
    state: str = ReservationState.COMMITTED,
    ttl_sec: float = PROVISIONAL_COMMIT_TTL_SEC
) -> Dict[str, Any]:
    """
    Creates or updates a reservation record in jobs_db["reservations"].
    Must be called within an atomic transaction.
    """
    now_epoch = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    reservations = jobs_db.setdefault("reservations", {})

    # Check for existing reservation for this job and attempt
    res_id = None
    for r_id, r in reservations.items():
        if r.get("job_id") == job_id and r.get("attempt") == attempt:
            res_id = r_id
            break

    if not res_id:
        res_id = f"res-{job_id}-att{attempt}-{uuid.uuid4().hex[:8]}"

    commit_exp = now_epoch + ttl_sec if state in (ReservationState.PROVISIONAL, ReservationState.COMMITTED) else None

    record = {
        "reservation_id": res_id,
        "job_id": job_id,
        "attempt": attempt,
        "node_id": node_id,
        "fencing_token": int(fencing_token),
        "state": state,
        "created_at": reservations.get(res_id, {}).get("created_at", now_iso),
        "updated_at": now_iso,
        "commit_expires_at": commit_exp,
        "resources": {
            "memory_mb": float(resources.get("memory_mb", resources.get("min_memory_mb", 0.0))),
            "storage_gb": float(resources.get("storage_gb", resources.get("min_storage_gb", 0.0))),
            "cpu_cores": int(resources.get("cpu_cores", resources.get("min_cpu_cores", 0))),
            "exclusive_devices": list(resources.get("exclusive_devices", []))
        }
    }

    reservations[res_id] = record
    return record


def activate_reservation(
    jobs_db: Dict[str, Any],
    job_id: str,
    attempt: int,
    fencing_token: int
) -> Tuple[bool, Optional[str]]:
    """
    Transitions a reservation from COMMITTED/PROVISIONAL to ACTIVE upon worker process startup acknowledgment.
    Validates strictly against the authoritative fencing_token.
    """
    reservations = jobs_db.setdefault("reservations", {})
    target_res = None
    for res in reservations.values():
        if res.get("job_id") == job_id and res.get("attempt") == attempt:
            target_res = res
            break

    if not target_res:
        return False, "Reservation not found"

    # Fencing check
    if int(target_res.get("fencing_token", 0)) != int(fencing_token):
        return False, f"Fencing token mismatch: reservation holds {target_res.get('fencing_token')}, caller provided {fencing_token}"

    curr_state = target_res.get("state")
    if curr_state in (ReservationState.COMMITTED, ReservationState.PROVISIONAL):
        target_res["state"] = ReservationState.ACTIVE
        target_res["commit_expires_at"] = None  # ACTIVE reservations do NOT expire by commit TTL
        target_res["updated_at"] = datetime.now(timezone.utc).isoformat()
        return True, None
    elif curr_state == ReservationState.ACTIVE:
        return True, None  # Idempotent re-affirmation

    return False, f"Cannot activate reservation in state '{curr_state}'"


def release_reservation(
    jobs_db: Dict[str, Any],
    job_id: str,
    attempt: Optional[int] = None,
    fencing_token: Optional[int] = None,
    force: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Transitions a reservation to RELEASED.
    Guarantees that a stale worker attempt cannot release a newer attempt's reservation.
    """
    reservations = jobs_db.setdefault("reservations", {})
    changed = False

    for res in list(reservations.values()):
        if res.get("job_id") == job_id:
            if attempt is not None and res.get("attempt") != attempt:
                continue
            if fencing_token is not None and not force:
                if int(res.get("fencing_token", 0)) != int(fencing_token):
                    return False, f"Stale fencing token {fencing_token} cannot release reservation for generation {res.get('fencing_token')}"

            res["state"] = ReservationState.RELEASED
            res["updated_at"] = datetime.now(timezone.utc).isoformat()
            res["commit_expires_at"] = None
            changed = True

    return changed, None


def sweep_reservation_ledger(jobs_db: Dict[str, Any], timeout_seconds: int = 60) -> bool:
    """
    Sweeps the reservation ledger:
    1. Reclaims COMMITTED/PROVISIONAL reservations whose commit_expires_at has elapsed (30s window).
    2. Preserves ACTIVE reservations even if the worker drops offline, until the job is formally failed/rescheduled.
    3. Cleans up RELEASED and EXPIRED entries older than 1 hour.
    """
    now = time.time()
    reservations = jobs_db.get("reservations", {})
    changed = False

    for res_id, res in list(reservations.items()):
        state = res.get("state")
        commit_exp = res.get("commit_expires_at")

        # 1. Expire unacknowledged COMMITTED/PROVISIONAL reservations
        if state in (ReservationState.PROVISIONAL, ReservationState.COMMITTED):
            if commit_exp is not None and now > float(commit_exp):
                res["state"] = ReservationState.EXPIRED
                res["updated_at"] = datetime.now(timezone.utc).isoformat()
                changed = True

        # 2. Reclaim RELEASED / EXPIRED reservations from active ledger after retention
        elif state in (ReservationState.RELEASED, ReservationState.EXPIRED):
            updated_str = res.get("updated_at")
            if updated_str:
                try:
                    updated_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                    if (now - updated_dt.timestamp()) > 3600:
                        del reservations[res_id]
                        changed = True
                except Exception:
                    pass

    return changed

