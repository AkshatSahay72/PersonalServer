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

from common.artifact_manager import cleanup_orphan_staging_artifacts
from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_runtime_config
from config.structured_logger import controller_logger, generate_correlation_id
from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CANCELLED,
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
    default_backoff_sec: float = 0.0,
    paths: Optional[Any] = None
) -> bool:
    """
    Evaluates:
    1. Active ASSIGNED / CLAIMED / RUNNING jobs for lease expiration or worker node disconnection.
       Preserves valid active leases whose nodes are healthy and whose deadline has not elapsed.
    2. Queued & Recovering Auto / Capability Jobs, matching available eligible workers.
    3. Queued jobs targeting offline pinned nodes, setting waiting reasons and transitioning to NEEDS_ATTENTION.
    4. Workflow DAG progression, unblocking steps whose parent dependencies have succeeded and resolving artifacts.
    5. Multi-pass convergence to ensure steps unblocked during DAG evaluation are immediately scheduled without delay.
    
    Returns True if database was modified.
    """
    overall_changed = False
    max_passes = 5

    for pass_idx in range(max_passes):
        pass_changed = False
        now_epoch = time.time()
        now_iso = get_current_iso_timestamp()

        # 1. Sweep active in-flight jobs and queued offline pinned jobs
        for job_id, job in list(jobs_db.get("jobs", {}).items()):
            raw_status = job.get("status")
            canon_status = canonicalize_job_state(raw_status)

            target_node = job.get("assigned_node") or job.get("target_node")
            node_entry = nodes_db.get("nodes", {}).get(target_node, {}) if target_node else {}
            node_live = compute_node_liveness(node_entry, timeout_seconds) if node_entry else (STATE_OFFLINE if target_node else STATE_ONLINE)

            # A: Active Lease Evaluation (ASSIGNED / CLAIMED / RUNNING)
            if canon_status in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
                lease_exp = job.get("lease_expires_at")
                lease_expired = (lease_exp is None) or (now_epoch > float(lease_exp))
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
                        # Advance fencing token immediately so superseded attempts are rejected
                        job["lease_generation"] = int(job.get("lease_generation", 0)) + 1

                        base_backoff = float(job.get("backoff_sec") if job.get("backoff_sec") is not None else default_backoff_sec)
                        backoff_sec = min(60.0, base_backoff * (2 ** (attempt - 1)))
                        job["retry_after"] = now_epoch + backoff_sec
                        job["retry_after_iso"] = datetime.fromtimestamp(now_epoch + backoff_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                        target_mode = str(job.get("target", "auto")).lower()
                        is_dynamic_target = (target_mode == "auto" or target_mode.startswith(("capability:", "cap:", "role:")))
                        if is_dynamic_target:
                            reqs = job.get("requirements", {})
                            decision = ResourceScheduler.select_node(reqs, nodes_db, timeout_seconds, cluster_jobs=jobs_db.get("jobs", {}))
                            if decision.get("selected_node"):
                                job["target_node"] = decision["selected_node"]
                                job["assigned_node"] = decision["selected_node"]
                                mode_name = "capability-aware-recovery" if target_mode.startswith(("capability:", "cap:", "role:")) else "resource-aware-recovery"
                                job["scheduler"] = {
                                    "mode": mode_name,
                                    "selected_node": decision["selected_node"],
                                    "score": decision["score"],
                                    "reason": f"Recovered from {target_node} ({reason})"
                                }
                            else:
                                job["target_node"] = None
                                job["assigned_node"] = None
                                job["waiting_reason"] = f"Awaiting eligible worker for recovery retry: {decision.get('reason')}"
                        else:
                            job["target_node"] = job.get("target")
                            job["scheduler"] = {
                                "mode": "explicit-recovery",
                                "target_node": job.get("target"),
                                "reason": f"Awaiting recovery on explicit target {job.get('target')}"
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
                        job["result"] = {
                            "status": JOB_STATE_FAILED,
                            "exit_code": -1,
                            "stdout": "",
                            "stderr": job["retry_reason"],
                            "duration_ms": 0,
                        }
                        controller_logger.error(
                            "job.recovery_exhausted",
                            f"Job {job_id} permanently marked FAILED ({job['retry_reason']}).",
                            correlation_id=corr_id,
                            job_id=job_id,
                            node_id=target_node,
                            reason=job["retry_reason"]
                        )
                        notify_job_failure(job, job["retry_reason"], is_terminal=True)

                    pass_changed = True
                    overall_changed = True

            # B: Queued & Recovering Auto / Capability Jobs (Evaluate newly eligible workers)
            target_mode = str(job.get("target", "auto")).lower()
            is_dynamic_target = (target_mode == "auto" or target_mode.startswith(("capability:", "cap:", "role:")))
            if canon_status in (JOB_STATE_QUEUED, JOB_STATE_RETRY_WAIT) and not job.get("target_node") and is_dynamic_target:
                decision = ResourceScheduler.select_node(
                    job.get("requirements", {}),
                    nodes_db,
                    timeout_seconds,
                    cluster_jobs=jobs_db.get("jobs", {})
                )
                if decision.get("selected_node"):
                    sel_node = decision["selected_node"]
                    job["target_node"] = sel_node
                    job["assigned_node"] = sel_node
                    job["waiting_reason"] = None
                    mode_name = "capability-aware" if target_mode.startswith(("capability:", "cap:", "role:")) else "resource-aware"
                    job["scheduler"] = {
                        "mode": mode_name,
                        "selected_node": sel_node,
                        "score": decision["score"],
                        "reason": decision["reason"],
                        "candidates_count": len(decision.get("candidates", []))
                    }
                    pass_changed = True
                    overall_changed = True
                else:
                    new_waiting = f"Awaiting eligible worker: {decision['reason']}"
                    if job.get("waiting_reason") != new_waiting:
                        job["waiting_reason"] = new_waiting
                        if "scheduler" in job and isinstance(job["scheduler"], dict):
                            job["scheduler"]["reason"] = decision["reason"]
                            job["scheduler"]["rejected"] = decision.get("rejected", {})
                        pass_changed = True
                        overall_changed = True

            # B2: Recovering Auto / Capability Jobs with Offline Target (Reset target so another worker can claim)
            elif canon_status == JOB_STATE_RETRY_WAIT and target_node and is_dynamic_target:
                if node_live in (STATE_OFFLINE, STATE_REMOVED):
                    job["target_node"] = None
                    job["assigned_node"] = None
                    job["waiting_reason"] = f"Recovery worker '{target_node}' went {node_live}. Awaiting eligible worker."
                    pass_changed = True
                    overall_changed = True

            # C: Queued Pinned Node Evaluation (Prevent infinite silent queueing)
            elif canon_status in (JOB_STATE_QUEUED, JOB_STATE_NEEDS_ATTENTION):
                target_mode = job.get("target", "auto")
                is_pinned = job.get("pinned", False) or target_mode != "auto" or bool(job.get("target_node") and not job.get("target"))
                if is_pinned and target_node:
                    if node_live in (STATE_OFFLINE, STATE_REMOVED):
                        job["waiting_reason"] = f"Pinned node '{target_node}' is {node_live}. Awaiting reconnect."
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
                            pass_changed = True
                            overall_changed = True
                    else:
                        if canon_status == JOB_STATE_NEEDS_ATTENTION and node_live == STATE_ONLINE:
                            job["status"] = JOB_STATE_QUEUED
                            job["state"] = JOB_STATE_QUEUED
                            job["attention_reason"] = None
                            job["waiting_reason"] = None
                            pass_changed = True
                            overall_changed = True
                        elif job.get("waiting_reason") and "Pinned node" in str(job.get("waiting_reason")):
                            job["waiting_reason"] = None
                            pass_changed = True
                            overall_changed = True

        # 2. Sweep Workflows DAG progression
        try:
            workflows_db = load_workflows_db(paths=paths)
            wf_changed = False
            for wf_id, wf in list(workflows_db.get("workflows", {}).items()):
                if evaluate_workflow_progress(wf, jobs_db, paths=paths):
                    wf_changed = True
                    pass_changed = True
                    overall_changed = True
            if wf_changed:
                save_workflows_db(workflows_db, paths=paths)
        except Exception as e:
            print(f"[WORKFLOW-SWEEP-ERR] {e}", file=sys.stderr)

        if not pass_changed:
            break

    if overall_changed:
        save_jobs_db(jobs_db, paths=paths)

    return overall_changed


def reconcile_startup_recovery(
    jobs_db: Optional[Dict[str, Any]] = None,
    nodes_db: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
    paths: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Durable controller restart recovery:
    1. Inspects existing persisted stores (jobs.json, nodes.json, workflows.json, artifacts.json).
    2. Identifies and preserves valid active leases (does not reset valid in-flight jobs).
    3. Detects expired leases or dead workers, increments attempt and advances lease_generation fencing token.
    4. Evaluates and unblocks dependent DAG workflow steps with verified artifact references.
    5. Converges multi-pass scheduling so newly unblocked steps are immediately assigned to eligible nodes.
    6. Ensures terminal workflows are safely finalized and saves stores atomically.
    
    Returns a recovery summary dictionary.
    """
    if jobs_db is None:
        jobs_db = load_jobs_db(paths=paths)
    if nodes_db is None:
        nodes_db = load_nodes_db(paths=paths)

    now_epoch = time.time()
    active_preserved = 0
    expired_count = 0
    init_waiting_steps = 0

    for job_id, job in jobs_db.get("jobs", {}).items():
        st = canonicalize_job_state(job.get("status"))
        if st in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
            t_node = job.get("assigned_node") or job.get("target_node")
            n_entry = nodes_db.get("nodes", {}).get(t_node, {}) if t_node else {}
            n_live = compute_node_liveness(n_entry, timeout_seconds) if n_entry else (STATE_OFFLINE if t_node else STATE_ONLINE)
            l_exp = job.get("lease_expires_at")
            if l_exp is not None and now_epoch <= float(l_exp) and n_live not in (STATE_OFFLINE, STATE_REMOVED):
                active_preserved += 1
            else:
                expired_count += 1
        elif st == JOB_STATE_WAITING_FOR_DEPENDENCY:
            init_waiting_steps += 1

    # Sweep unpromoted temporary staging artifacts left by crashed uploads
    curr_paths = paths
    if curr_paths is None:
        try:
            from config.platform_config import get_controller_paths
            curr_paths = get_controller_paths()
        except Exception:
            curr_paths = None

    staging_cleaned = 0
    if curr_paths and hasattr(curr_paths, "artifacts_dir"):
        staging_cleaned = cleanup_orphan_staging_artifacts(curr_paths.artifacts_dir, max_age_seconds=0.0)

    # Run multi-pass reconciliation
    changed = sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=timeout_seconds, paths=paths)

    # Post-reconciliation survey
    workflows_db = load_workflows_db(paths=paths)
    wf_succeeded = 0
    wf_failed = 0
    post_waiting_steps = 0

    for wf in workflows_db.get("workflows", {}).values():
        wf_st = canonicalize_job_state(wf.get("status"))
        if wf_st == JOB_STATE_SUCCEEDED:
            wf_succeeded += 1
        elif wf_st in (JOB_STATE_FAILED, JOB_STATE_CANCELLED):
            wf_failed += 1

    for job in jobs_db.get("jobs", {}).values():
        if canonicalize_job_state(job.get("status")) == JOB_STATE_WAITING_FOR_DEPENDENCY:
            post_waiting_steps += 1

    steps_unblocked = max(0, init_waiting_steps - post_waiting_steps)

    summary = {
        "active_leases_preserved": active_preserved,
        "expired_leases_swept": expired_count,
        "staging_artifacts_cleaned": staging_cleaned,
        "steps_unblocked": steps_unblocked,
        "workflows_succeeded": wf_succeeded,
        "workflows_failed": wf_failed,
        "changed": changed
    }

    controller_logger.info(
        "controller.startup_recovery",
        f"Controller restart recovery reconciled: {active_preserved} active lease(s) preserved, "
        f"{expired_count} expired lease(s) swept, {staging_cleaned} staging artifact(s) cleaned, "
        f"{steps_unblocked} workflow step(s) unblocked, {wf_succeeded} workflow(s) in SUCCEEDED state.",
        **summary
    )

    return summary


def start_background_lease_sweeper(timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT, paths: Optional[Any] = None) -> threading.Thread:
    """Starts a daemon thread to periodically sweep expired leases every 3 seconds."""
    def _sweeper():
        while True:
            try:
                time.sleep(3)
                mgr = get_ha_manager(paths=paths)
                if mgr and (not mgr.is_leader or not mgr.current_lease.is_valid()):
                    continue  # Standby and fenced controllers do not sweep or reschedule leases
                jobs_db = load_jobs_db(paths=paths)
                nodes_db = load_nodes_db(paths=paths)
                sweep_expired_leases(jobs_db, nodes_db, timeout_seconds, paths=paths)
            except Exception as e:
                print(f"[SWEEPER-ERR] {e}", file=sys.stderr)

    t = threading.Thread(target=_sweeper, daemon=True, name="LeaseSweeper")
    t.start()
    return t


__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "sweep_expired_leases",
    "reconcile_startup_recovery",
    "start_background_lease_sweeper",
]
