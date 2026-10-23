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
    JOB_STATE_ASSIGNED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMED_OUT,
    JOB_STATE_TIMEOUT,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_REMOVED,
    canonicalize_job_state,
)
from controller.ha_helper import get_ha_manager
from controller.notifications import notify_job_failure
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db, load_workflows_db, save_workflows_db
from controller.workflow_engine import evaluate_workflow_progress
from scheduler.scheduler import ResourceScheduler, compute_node_liveness

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout
OFFLINE_PINNED_ATTENTION_SEC = 300  # 5 minutes offline threshold before NEEDS_ATTENTION


def sweep_expired_leases(
    jobs_db: Dict[str, Any],
    nodes_db: Dict[str, Any],
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
    default_backoff_sec: float = 0.0
) -> bool:
    """
    Evaluates:
    1. Active ASSIGNED / CLAIMED / RUNNING jobs for lease expiration or worker node disconnection.
    2. Queued jobs targeting offline pinned nodes, setting waiting reasons and transitioning to NEEDS_ATTENTION.
    3. Workflow DAG progression, unblocking steps whose parent dependencies have succeeded.
    
    Returns True if database was modified.
    """
    changed = False
    now_epoch = time.time()
    now_iso = get_current_iso_timestamp()

    # 1. Sweep active in-flight jobs and queued offline pinned jobs
    for job_id, job in list(jobs_db.get("jobs", {}).items()):
        raw_status = job.get("status")
        canon_status = canonicalize_job_state(raw_status)

        target_node = job.get("target_node")
        node_entry = nodes_db.get("nodes", {}).get(target_node, {}) if target_node else {}
        node_live = compute_node_liveness(node_entry, timeout_seconds) if node_entry else (STATE_OFFLINE if target_node else STATE_ONLINE)

        # A: Active Lease Evaluation (ASSIGNED / CLAIMED / RUNNING)
        if canon_status in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
            lease_exp = job.get("lease_expires_at")
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

                notify_job_failure(job, reason, is_terminal=False)

                attempts_history = job.setdefault("attempts_history", [])
                attempts_history.append({
                    "attempt": attempt,
                    "node": target_node,
                    "reason": reason,
                    "claimed_at": job.get("claimed_at"),
                    "failed_at": now_iso
                })

                is_retryable = job.get("retryable", True) is not False
                if attempt < max_attempts and is_retryable:
                    job["attempt"] = attempt + 1
                    job["status"] = JOB_STATE_RECOVERING
                    job["state"] = JOB_STATE_RETRY_WAIT
                    job["retry_reason"] = reason
                    job["claimed_at"] = None
                    job["lease_expires_at"] = None

                    base_backoff = float(job.get("backoff_sec") if job.get("backoff_sec") is not None else default_backoff_sec)
                    backoff_sec = min(60.0, base_backoff * (2 ** (attempt - 1)))
                    job["retry_after"] = now_epoch + backoff_sec
                    job["retry_after_iso"] = datetime.fromtimestamp(now_epoch + backoff_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    target_mode = job.get("target", "auto")
                    if target_mode == "auto":
                        reqs = job.get("requirements", {})
                        decision = ResourceScheduler.select_node(reqs, nodes_db, timeout_seconds, cluster_jobs=jobs_db.get("jobs", {}))
                        if decision.get("selected_node"):
                            job["target_node"] = decision["selected_node"]
                            job["assigned_node"] = decision["selected_node"]
                            job["scheduler"] = {
                                "mode": "resource-aware-recovery",
                                "selected_node": decision["selected_node"],
                                "score": decision["score"],
                                "reason": f"Recovered from {target_node} ({reason})"
                            }
                        else:
                            job["waiting_reason"] = f"Awaiting eligible worker for recovery retry: {decision.get('reason')}"
                    else:
                        job["target_node"] = target_mode
                        job["scheduler"] = {
                            "mode": "explicit-recovery",
                            "target_node": target_mode,
                            "reason": f"Awaiting recovery on explicit target {target_mode}"
                        }

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
                    job["status"] = JOB_STATE_FAILED
                    job["state"] = JOB_STATE_FAILED
                    job["finished_at"] = now_iso
                    job["retry_reason"] = f"Max retries ({max_attempts}) exceeded after: {reason}" if attempt >= max_attempts else f"Non-retryable failure: {reason}"
                    job["lease_expires_at"] = None
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

        # B: Queued Pinned Node Evaluation (Prevent infinite silent queueing)
        elif canon_status in (JOB_STATE_QUEUED, JOB_STATE_NEEDS_ATTENTION):
            target_mode = job.get("target", "auto")
            is_pinned = job.get("pinned", False) or target_mode != "auto" or bool(job.get("target_node") and not job.get("target"))
            if is_pinned and target_node:
                if node_live in (STATE_OFFLINE, STATE_REMOVED):
                    job["waiting_reason"] = f"Pinned node '{target_node}' is {node_live}. Awaiting reconnect."
                    # Calculate how long the job has been queued/waiting
                    created_at_str = job.get("created_at") or job.get("submitted_at")
                    age_sec = 0.0
                    if created_at_str:
                        try:
                            created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                            age_sec = (datetime.now(timezone.utc) - created_dt).total_seconds()
                        except Exception:
                            pass

                    if age_sec > OFFLINE_PINNED_ATTENTION_SEC and canon_status != JOB_STATE_NEEDS_ATTENTION:
                        job["status"] = JOB_STATE_NEEDS_ATTENTION
                        job["state"] = JOB_STATE_NEEDS_ATTENTION
                        job["attention_reason"] = f"Pinned target node '{target_node}' is {node_live}. Job has been waiting for {int(age_sec)}s."
                        changed = True
                else:
                    # Node is online! Restore to clean QUEUED if was in NEEDS_ATTENTION
                    if canon_status == JOB_STATE_NEEDS_ATTENTION and node_live == STATE_ONLINE:
                        job["status"] = JOB_STATE_QUEUED
                        job["state"] = JOB_STATE_QUEUED
                        job["attention_reason"] = None
                        job["waiting_reason"] = None
                        changed = True
                    elif job.get("waiting_reason") and "Pinned node" in str(job.get("waiting_reason")):
                        job["waiting_reason"] = None
                        changed = True

    # 2. Sweep Workflows DAG progression
    try:
        workflows_db = load_workflows_db()
        wf_changed = False
        for wf_id, wf in list(workflows_db.get("workflows", {}).items()):
            if evaluate_workflow_progress(wf, jobs_db):
                wf_changed = True
                changed = True
        if wf_changed:
            save_workflows_db(workflows_db)
    except Exception as e:
        print(f"[WORKFLOW-SWEEP-ERR] {e}", file=sys.stderr)

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
