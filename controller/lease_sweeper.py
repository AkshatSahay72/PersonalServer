#!/usr/bin/env python3
"""
Controller Lease Sweeper & Job Recovery Subsystem
================================================
Periodic background evaluation of active leases, detection of crashed/disconnected
workers, exponential backoff recalculation, and resilient job rescheduling.
"""

import sys
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_runtime_config
from config.structured_logger import controller_logger, generate_correlation_id
from controller.constants import (
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RUNNING,
    STATE_OFFLINE,
    STATE_REMOVED,
)
from controller.ha_helper import get_ha_manager
from controller.notifications import notify_job_failure
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db
from scheduler.scheduler import ResourceScheduler, compute_node_liveness

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout


def sweep_expired_leases(
    jobs_db: Dict[str, Any],
    nodes_db: Dict[str, Any],
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
    default_backoff_sec: float = 0.0
) -> bool:
    """
    Evaluates active CLAIMED / RUNNING jobs for lease expiration or worker node disconnection.
    Recovers eligible jobs or marks them FAILED deterministically.
    Returns True if database was modified.
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
            corr_id = job.get("correlation_id") or generate_correlation_id()

            controller_logger.warning(
                "lease.expired",
                f"Job {job_id} on node '{target_node}' failure detected: {reason} (Attempt {attempt}/{max_attempts})",
                correlation_id=corr_id,
                job_id=job_id,
                node_id=target_node,
                attempt=attempt,
                max_attempts=max_attempts,
                reason=reason
            )

            # Deduplicated failure alert
            notify_job_failure(job, reason, is_terminal=False)

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
            is_retryable = job.get("retryable", True) is not False
            if attempt < max_attempts and is_retryable:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["retry_reason"] = reason
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                # Compute backoff
                base_backoff = float(job.get("backoff_sec") if job.get("backoff_sec") is not None else default_backoff_sec)
                backoff_sec = min(60.0, base_backoff * (2 ** (attempt - 1)))
                job["retry_after"] = now_epoch + backoff_sec
                job["retry_after_iso"] = datetime.fromtimestamp(now_epoch + backoff_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

                controller_logger.info(
                    "job.recovery_rescheduled",
                    f"Job {job_id} queued for retry attempt {job['attempt']}/{max_attempts} with backoff {backoff_sec:.1f}s",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=job.get("target_node"),
                    attempt=job["attempt"],
                    max_attempts=max_attempts,
                    backoff_sec=backoff_sec
                )

            else:
                # Max retries exhausted or non-retryable
                job["status"] = JOB_STATE_FAILED
                job["finished_at"] = now_iso
                job["retry_reason"] = f"Max retries ({max_attempts}) exceeded after: {reason}" if attempt >= max_attempts else f"Non-retryable failure: {reason}"
                job["lease_expires_at"] = None
                print(f"[CONTROLLER-RECOVERY] Job {job_id} permanently marked FAILED ({job['retry_reason']}).")
                controller_logger.error(
                    "job.recovery_exhausted",
                    f"Job {job_id} permanently marked FAILED ({job['retry_reason']}).",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=target_node,
                    reason=job["retry_reason"]
                )
                notify_job_failure(job, job["retry_reason"], is_terminal=True)

            changed = True

    if changed:
        save_jobs_db(jobs_db)

    return changed


def start_background_lease_sweeper(timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT) -> threading.Thread:
    """Starts a daemon thread to periodically sweep expired leases every 3 seconds."""
    def _sweeper():
        while True:
            try:
                time.sleep(3)
                mgr = get_ha_manager()
                if mgr and (not mgr.is_leader or not mgr.current_lease.is_valid()):
                    continue  # Standby and fenced controllers do not sweep or reschedule leases
                jobs_db = load_jobs_db()
                nodes_db = load_nodes_db()
                sweep_expired_leases(jobs_db, nodes_db, timeout_seconds)
            except Exception as e:
                print(f"[SWEEPER-ERR] {e}", file=sys.stderr)

    t = threading.Thread(target=_sweeper, daemon=True, name="LeaseSweeper")
    t.start()
    return t


__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "sweep_expired_leases",
    "start_background_lease_sweeper",
]
