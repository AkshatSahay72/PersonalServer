#!/usr/bin/env python3
"""
Controller Job Route Handlers
=============================
HTTP request handling for job submission, querying, next-job lease claims,
execution result reporting, retry recovery, and cancellation.
"""

import math
import base64
import math
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from common.artifact_manager import (
    generate_artifact_id,
    store_artifact_bytes,
    store_artifact_stream,
    verify_artifact_integrity,
)
from controller.http.workflow_routes import handle_workflow_get, handle_workflow_post
from controller.http.artifact_routes import handle_artifact_get, handle_artifact_post
from controller.http.job_query_routes import handle_job_query_get
from controller.http.job_submission_routes import handle_job_submission_post
from common.crypto_utils import constant_time_compare
from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths, get_controller_runtime_config
from config.structured_logger import (
    controller_logger,
    generate_correlation_id,
    redact_sensitive_text,
)
from controller.constants import (
    ALLOWLISTED_WORKLOADS,
    APP_STATE_DEPLOYING,
    APP_STATE_FAILED,
    APP_STATE_RUNNING,
    APP_STATE_STOPPED,
    JOB_STATE_ASSIGNED,
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_REJECTED,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMEOUT,
    JOB_STATE_TIMED_OUT,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_OFFLINE,
    STATE_REMOVED,
    canonicalize_job_state,
    validate_job_transition,
)

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_LEASE_GRACE_SEC = RUNTIME_CFG.lease_grace_sec
from controller.lease_sweeper import sweep_expired_leases
from controller.notifications import notify_job_failure
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import (
    load_apps_db,
    load_artifacts_db,
    load_jobs_db,
    load_nodes_db,
    load_workflows_db,
    save_apps_db,
    save_artifacts_db,
    save_jobs_db,
    save_workflows_db,
)
from controller.workflow_engine import (
    WorkflowValidationError,
    cancel_workflow,
    instantiate_workflow,
    progress_workflows,
)
from scheduler.scheduler import (
    ResourceScheduler,
    compute_node_liveness,
    is_node_eligible,
)


def handle_job_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles GET requests for worker job lease claims, and delegates read-only
    job queries, workflows, and artifacts.
    Returns True if handled, False otherwise.
    """
    # Delegated Read-Only Job Queries (metrics, job list, filtering, pagination, detail)
    if handle_job_query_get(handler, path, query):
        return True

    # Delegated Workflow Routes
    if path == "/workflows" or path.startswith("/workflows/"):
        return handle_workflow_get(handler, path, query)

    # Delegated Artifact Routes
    if path == "/artifacts" or path.startswith("/artifacts/"):
        return handle_artifact_get(handler, path, query)

    # GET /nodes/<node_id>/jobs/next (Node Agent fetching its assigned job with Lease)
    if path.startswith("/nodes/") and path.endswith("/jobs/next"):
        node_id = path[7:-10].strip()
        token = handler.parse_auth_token()
        nodes_db = load_nodes_db()
        node = nodes_db.get("nodes", {}).get(node_id)

        if not node:
            handler.send_json(404, {"error": f"Node '{node_id}' not found"})
            return True

        expected_auth = node.get("auth_token")
        enrollment_token = get_or_create_enrollment_token()
        if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
            handler.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
            return True

        jobs_db = load_jobs_db()
        # Progress workflows so any newly unblocked jobs transition to QUEUED before selecting claimable jobs
        try:
            workflows_db = load_workflows_db()
            if progress_workflows(workflows_db, jobs_db):
                save_workflows_db(workflows_db)
                save_jobs_db(jobs_db)
        except Exception as wf_err:
            print(f"[CONTROLLER-WORKFLOW] Error evaluating workflows during poll: {wf_err}", file=sys.stderr)

        # Perform sweep before claiming
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

        now_epoch = time.time()
        # Find jobs waiting for this node (respecting retry_after backoff)
        # Also auto-match jobs targeting "auto" if no node was assigned yet and this node is eligible!
        claimable_jobs = [
            j for j in jobs_db.get("jobs", {}).values()
            if (
                j.get("target_node") == node_id
                or (
                    not j.get("target_node")
                    and is_node_eligible(node, j.get("requirements", {}), handler.heartbeat_timeout)
                )
            ) and (
                j.get("status") in (JOB_STATE_QUEUED, JOB_STATE_ASSIGNED)
                or (
                    j.get("status") in (JOB_STATE_RECOVERING, JOB_STATE_RETRY_WAIT)
                    and now_epoch >= j.get("retry_after", 0)
                )
            ) and j.get("status") != JOB_STATE_WAITING_FOR_DEPENDENCY
        ]

        if not claimable_jobs:
            handler.send_json(200, {"job": None, "message": "No pending jobs for this node."})
            return True

        # Atomic claim: pick oldest job and assign lease
        next_job = claimable_jobs[0]
        now_iso = get_current_iso_timestamp()
        now_epoch = time.time()
        timeout_sec = next_job.get("timeout", 60)
        lease_duration = timeout_sec + DEFAULT_LEASE_GRACE_SEC

        next_job["target_node"] = node_id
        next_job["assigned_node"] = node_id
        next_job["status"] = JOB_STATE_CLAIMED
        next_job["state"] = JOB_STATE_ASSIGNED
        next_job["claimed_at"] = now_iso
        next_job["started_at"] = now_iso
        next_job["lease_expires_at"] = now_epoch + lease_duration
        next_job["waiting_reason"] = None
        # Increment lease generation as a fencing token to prevent duplicate / stale execution
        next_job["lease_generation"] = int(next_job.get("lease_generation", 0)) + 1

        if next_job.get("target") == "auto":
            next_job["scheduler"] = {
                "mode": "resource-aware-claimed",
                "selected_node": node_id,
                "reason": f"Claimed by eligible worker '{node_id}'"
            }

        save_jobs_db(jobs_db)

        corr_id = next_job.get("correlation_id") or getattr(handler, "current_correlation_id", None) or generate_correlation_id()
        next_job["correlation_id"] = corr_id
        controller_logger.info(
            "job.claimed",
            f"Job {next_job['job_id']} ({next_job.get('type', next_job.get('job_type', 'unknown'))}) CLAIMED by node '{node_id}' (Attempt {next_job.get('attempt', 1)}, Lease {lease_duration}s)",
            correlation_id=corr_id,
            job_id=next_job["job_id"],
            node_id=node_id,
            attempt=next_job.get("attempt", 1),
            lease_duration_sec=lease_duration
        )
        handler.send_json(200, {"job": next_job})
        return True

    # Delegated Artifact Routes
    if path.startswith("/jobs/") and path.endswith("/artifacts"):
        return handle_artifact_get(handler, path, query)

    return False


def handle_job_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: str, query: Dict[str, list]) -> bool:
    """
    Handles POST requests for job submission, result submission, retry scheduling,
    and cancellation.
    Returns True if handled, False otherwise.
    """
    # Delegated Job Submission Route (POST /jobs)
    if handle_job_submission_post(handler, path, body, token, query):
        return True

    # POST /jobs/<job_id>/result - Job Result Submission & Retry Handling
    if path.startswith("/jobs/") and path.endswith("/result"):
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True

        job_id = path[6:-7].strip()
        jobs_db = load_jobs_db()
        job = jobs_db.get("jobs", {}).get(job_id)

        if not job:
            handler.send_json(404, {"error": f"Job '{job_id}' not found"})
            return True

        target_node = job.get("target_node")
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(target_node, {})

        expected_auth = node_entry.get("auth_token")
        enrollment_token = get_or_create_enrollment_token()
        if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
            handler.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
            return True

        corr_id = job.get("correlation_id") or getattr(handler, "current_correlation_id", None) or generate_correlation_id()
        current_attempt = int(job.get("attempt", 1))
        canon_curr_status = canonicalize_job_state(job.get("status"))

        # 1. Idempotency / Terminal state protection
        # If job is already in a terminal state, guard against overwriting terminal states or acknowledge duplicates.
        if canon_curr_status in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED, JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_REJECTED):
            reported_st = canonicalize_job_state(body.get("status"))
            if canon_curr_status in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED):
                handler.send_json(200, {
                    "status": job.get("status"),
                    "message": f"Job '{job_id}' has already reached terminal state '{job.get('status')}'. Duplicate report ignored.",
                    "job_id": job_id,
                    "duplicate": True
                })
                return True
            elif reported_st == JOB_STATE_SUCCEEDED:
                controller_logger.warning(
                    "worker.late_success_on_terminal",
                    f"Rejected late result for terminal job '{job_id}': current state is '{job.get('status')}', reported status is '{body.get('status')}'",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=target_node,
                    current_status=job.get("status"),
                    reported_status=body.get("status")
                )
                handler.send_json(409, {
                    "error": f"Job '{job_id}' has already reached terminal state '{job.get('status')}'. Late result rejected.",
                    "job_id": job_id,
                    "status": job.get("status"),
                    "terminal": True
                })
                return True
            elif canon_curr_status in (JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_REJECTED):
                if reported_st == JOB_STATE_TIMED_OUT and canon_curr_status == JOB_STATE_FAILED:
                    pass  # Allow worker's truthful timeout execution result to refine swept failure state
                else:
                    handler.send_json(200, {
                        "status": job.get("status"),
                        "message": f"Job '{job_id}' has already reached terminal state '{job.get('status')}'. Duplicate failure report ignored.",
                        "job_id": job_id,
                        "duplicate": True
                    })
                    return True

        # 2. In-flight lease protection: reject results if active lease was revoked or in retry wait
        if canon_curr_status in (JOB_STATE_RETRY_WAIT, JOB_STATE_RECOVERING):
            controller_logger.warning(
                "worker.recovering_lease_expired",
                f"Rejected result for job '{job_id}' in '{job.get('status')}' state: active lease was revoked or worker disconnected",
                correlation_id=corr_id,
                job_id=job_id,
                node_id=target_node,
                current_attempt=current_attempt
            )
            handler.send_json(409, {
                "error": f"Cannot submit result for job '{job_id}' in '{job.get('status')}' state: active lease expired or worker disconnected.",
                "job_id": job_id,
                "current_attempt": current_attempt,
                "lease_expired": True
            })
            return True

        # 3. Fencing token: reject superseded lease generations (J1/J3 mandatory HTTP 412)
        reported_gen = body.get("lease_generation")
        if reported_gen is None and body.get("fencing_token") is not None:
            reported_gen = body.get("fencing_token")
        current_gen = int(job.get("lease_generation", 1))
        if reported_gen is not None and int(reported_gen) < current_gen:
            controller_logger.warning(
                "worker.stale_generation",
                f"Superseded lease generation result for job '{job_id}': reported generation {reported_gen} < current generation {current_gen}",
                correlation_id=corr_id,
                job_id=job_id,
                node_id=target_node,
                reported_generation=reported_gen,
                current_generation=current_gen
            )
            handler.send_json(412, {
                "error": f"Precondition Failed: Superseded lease generation {reported_gen} is older than current generation {current_gen}.",
                "job_id": job_id,
                "current_generation": current_gen,
                "fencing_token": current_gen,
                "superseded": True
            })
            return True

        # 4. Stale worker protection: check attempt number if provided (or default to 1 for unversioned reports)
        reported_attempt = body.get("attempt")
        if reported_attempt is not None:
            eff_reported_attempt = int(reported_attempt)
        elif reported_gen is not None and int(reported_gen) == current_gen:
            eff_reported_attempt = current_attempt
        else:
            eff_reported_attempt = 1 if current_attempt > 1 else current_attempt

        if eff_reported_attempt < current_attempt:
            controller_logger.warning(
                "worker.stale_attempt",
                f"Stale worker result for job '{job_id}': reported attempt {eff_reported_attempt} < current attempt {current_attempt}",
                correlation_id=corr_id,
                job_id=job_id,
                node_id=target_node,
                reported_attempt=eff_reported_attempt,
                current_attempt=current_attempt
            )
            handler.send_json(409, {
                "error": f"Stale worker result: reported attempt {eff_reported_attempt} is older than current attempt {current_attempt}.",
                "job_id": job_id,
                "current_attempt": current_attempt,
                "stale_attempt": True
            })
            return True

        job_status = body.get("status", JOB_STATE_FAILED)
        now_iso = get_current_iso_timestamp()
        now_epoch = time.time()

        # 5. Controller-authoritative lease validity check:
        # If the controller clock indicates the lease has expired, reject the result.
        lease_exp = job.get("lease_expires_at")
        if (
            lease_exp is not None
            and now_epoch > float(lease_exp)
            and canon_curr_status in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING)
        ):
            controller_logger.warning(
                "worker.lease_expired_on_submission",
                f"Rejected result for job '{job_id}': controller clock indicates lease expired at {lease_exp} (now: {now_epoch})",
                correlation_id=corr_id,
                job_id=job_id,
                node_id=target_node,
                current_attempt=current_attempt,
                lease_expires_at=lease_exp,
            )
            sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)
            handler.send_json(409, {
                "error": f"Cannot submit result for job '{job_id}': active lease expired at {lease_exp} (controller time: {now_epoch}).",
                "job_id": job_id,
                "current_attempt": current_attempt,
                "lease_expired": True
            })
            return True

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
            # Advance fencing token on retry (J1/J3)
            job["lease_generation"] = int(job.get("lease_generation", 0)) + 1
            try:
                from controller.stores.reservations import release_reservation
                release_reservation(jobs_db, job_id, attempt=attempt, force=True)
            except Exception:
                pass

            # Compute backoff
            base_backoff = float(job.get("backoff_sec") if job.get("backoff_sec") is not None else getattr(handler, "default_backoff_sec", 0.0))
            backoff_sec = min(60.0, base_backoff * (2 ** (attempt - 1)))
            job["retry_after"] = now_epoch + backoff_sec
            job["retry_after_iso"] = datetime.fromtimestamp(now_epoch + backoff_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            target_mode = job.get("target", "auto")
            if target_mode == "auto":
                reqs = job.get("requirements", {})
                decision = ResourceScheduler.select_node(reqs, nodes_db, handler.heartbeat_timeout)
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
            notify_job_failure(job, f"Attempt {attempt} failed: {job_status}", is_terminal=False)

        else:
            # Terminal Success / Final Failure
            job["status"] = job_status
            job["state"] = job_status
            job["finished_at"] = now_iso
            job["lease_expires_at"] = None
            try:
                from controller.stores.reservations import release_reservation
                release_reservation(jobs_db, job_id, attempt=attempt, force=True)
            except Exception:
                pass
            if not is_retryable and job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                job["retry_reason"] = f"Non-retryable failure on attempt {attempt}: {body.get('stderr') or job_status}"
            job["result"] = {
                "status": job_status,
                "exit_code": body.get("exit_code", 0),
                "stdout": stdout_bounded,
                "stderr": stderr_bounded,
                "duration_ms": body.get("duration_ms", 0),
                "declared_outputs": body.get("declared_outputs"),
                "outputs": body.get("outputs") or body.get("declared_outputs"),
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
                notify_job_failure(job, job.get("retry_reason") or f"Job permanently failed with {job_status}", is_terminal=True)
            else:
                controller_logger.info(
                    "job.succeeded",
                    f"Job {job_id} SUCCEEDED on node {target_node} in {body.get('duration_ms', 0)}ms",
                    correlation_id=corr_id,
                    job_id=job_id,
                    node_id=target_node,
                    duration_ms=body.get("duration_ms", 0)
                )

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

                        # Architecture compatibility check and worker scheduling
                        nodes_db = load_nodes_db()
                        reqs = {
                            "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"],
                            "supported_architectures": [img_arch]
                        }
                        decision = ResourceScheduler.select_node(reqs, nodes_db, handler.heartbeat_timeout)
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

                elif job.get("type") == "app-deploy":
                    dep_id = job.get("deployment_id") or job.get("parameters", {}).get("deployment_id")
                    from controller.deployment_state_machine import DeploymentStateMachine, DEP_STATE_STARTING, DEP_STATE_HEALTH_CHECK, DEP_STATE_ACTIVATING, DEP_STATE_SUCCEEDED, DEP_STATE_FAILED
                    if job_status == JOB_STATE_SUCCEEDED:
                        out_meta = body.get("declared_outputs") or body.get("outputs") or {}
                        port = out_meta.get("port") or job.get("parameters", {}).get("port") or app.get("port", 8090)
                        assigned_node = job.get("assigned_node") or job.get("target_node")

                        try:
                            curr_dep = DeploymentStateMachine.get_deployment(app_id, dep_id, paths=handler.paths)
                            if curr_dep and curr_dep.get("status") == DEP_STATE_STARTING:
                                DeploymentStateMachine.transition(
                                    app_id=app_id,
                                    deployment_id=dep_id,
                                    next_state=DEP_STATE_HEALTH_CHECK,
                                    reason="Worker health verification succeeded",
                                    metadata={"pid": out_meta.get("pid"), "port": port},
                                    paths=handler.paths
                                )
                            DeploymentStateMachine.transition(
                                app_id=app_id,
                                deployment_id=dep_id,
                                next_state=DEP_STATE_ACTIVATING,
                                reason="Health verification succeeded",
                                paths=handler.paths
                            )
                            DeploymentStateMachine.transition(
                                app_id=app_id,
                                deployment_id=dep_id,
                                next_state=DEP_STATE_SUCCEEDED,
                                reason="Release activated",
                                metadata={
                                    "node": assigned_node,
                                    "port": port,
                                    "address": f"http://{assigned_node}:{port}",
                                    "pid": out_meta.get("pid")
                                },
                                paths=handler.paths
                            )
                        except Exception as tr_err:
                            print(f"[CONTROLLER-DEPLOY] State transition error: {tr_err}", flush=True)

                        app["status"] = APP_STATE_RUNNING
                        app["selected_node"] = assigned_node
                        app["node"] = assigned_node
                        app["assigned_node"] = assigned_node
                        app["port"] = port
                        app["host_port"] = port
                        app["active_deployment_id"] = dep_id
                        app["error"] = None
                        app["failure_reason"] = None
                        app["updated_at"] = now_iso
                    else:
                        err_text = body.get("stderr") or body.get("diagnostics", {}).get("failure_reason") or "Application deployment failed"
                        try:
                            DeploymentStateMachine.transition(
                                app_id=app_id,
                                deployment_id=dep_id,
                                next_state=DEP_STATE_FAILED,
                                error=err_text,
                                reason="Worker execution or health check failed",
                                paths=handler.paths
                            )
                        except Exception:
                            pass

                        if app.get("active_deployment_id") and app.get("active_deployment_id") != dep_id:
                            app["status"] = APP_STATE_RUNNING
                            app["error"] = f"Deployment {dep_id} failed: {err_text}"
                        else:
                            app["status"] = APP_STATE_FAILED
                            app["error"] = err_text
                            app["failure_reason"] = err_text
                        app["updated_at"] = now_iso

                elif job.get("type") == "docker-stop" and job_status == JOB_STATE_SUCCEEDED:
                    app["status"] = APP_STATE_STOPPED
                    app["updated_at"] = now_iso
                elif job.get("type") == "docker-restart" and job_status == JOB_STATE_SUCCEEDED:
                    app["status"] = APP_STATE_RUNNING
                    app["updated_at"] = now_iso
                save_apps_db(apps_db)

        reported_artifacts = body.get("artifacts")
        if reported_artifacts and isinstance(reported_artifacts, list):
            job["artifacts"] = reported_artifacts
            try:
                artifacts_db = load_artifacts_db()
                for art in reported_artifacts:
                    if isinstance(art, dict) and art.get("artifact_id"):
                        art["job_id"] = job_id
                        if not art.get("producing_node"):
                            art["producing_node"] = target_node
                        artifacts_db.setdefault("artifacts", {})[art["artifact_id"]] = art
                save_artifacts_db(artifacts_db)
            except Exception as e:
                print(f"[CONTROLLER-ARTIFACTS] Error storing reported artifacts: {e}", file=sys.stderr)

        # Progress any parent workflow if job belongs to a workflow
        wf_id = job.get("workflow_id")
        if wf_id:
            try:
                workflows_db = load_workflows_db()
                if progress_workflows(workflows_db, jobs_db):
                    save_workflows_db(workflows_db)
            except Exception as e:
                print(f"[CONTROLLER-WORKFLOW] Error progressing workflow '{wf_id}': {e}", file=sys.stderr)

        save_jobs_db(jobs_db)

        handler.send_json(200, {
            "status": "ok",
            "ack": True,
            "job_id": job_id,
            "job_status": job["status"]
        })
        return True

    # POST /jobs/<job_id>/cancel - Cancel Job
    if path.startswith("/jobs/") and path.endswith("/cancel"):
        job_id = path[6:-7].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to cancel job"})
            return True

        jobs_db = load_jobs_db()
        job = jobs_db.get("jobs", {}).get(job_id)
        if not job:
            handler.send_json(404, {"error": f"Job '{job_id}' not found"})
            return True

        current_st = job.get("status")

        # Idempotent repeat cancellation check
        if current_st == JOB_STATE_CANCELLED:
            if (body and body.get("idempotent")) or query.get("idempotent", [False])[0]:
                handler.send_json(200, {
                    "status": JOB_STATE_CANCELLED,
                    "job_id": job_id,
                    "duplicate": True,
                    "message": f"Job '{job_id}' is already cancelled.",
                    "job": job
                })
                return True
            handler.send_json(400, {
                "error": f"Invalid state transition: Cannot cancel job '{job_id}' in terminal state '{current_st}'.",
                "status": current_st
            })
            return True

        # Terminal states: SUCCEEDED, FAILED, REJECTED cannot be cancelled
        if current_st in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_REJECTED) or not validate_job_transition(current_st, JOB_STATE_CANCELLED):
            handler.send_json(400, {
                "error": f"Invalid state transition: Cannot cancel job '{job_id}' in terminal state '{current_st}'.",
                "status": current_st
            })
            return True

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

        # Progress any parent workflow if job belongs to a workflow
        wf_id = job.get("workflow_id")
        if wf_id:
            try:
                workflows_db = load_workflows_db()
                if progress_workflows(workflows_db, jobs_db):
                    save_workflows_db(workflows_db)
                    save_jobs_db(jobs_db)
            except Exception as e:
                print(f"[CONTROLLER-WORKFLOW] Error progressing workflow '{wf_id}' after cancellation: {e}", file=sys.stderr)

        print(f"[CONTROLLER] Job {job_id} CANCELLED: {reason}")
        handler.send_json(200, {
            "status": JOB_STATE_CANCELLED,
            "job_id": job_id,
            "message": f"Job '{job_id}' successfully cancelled.",
            "termination": termination_info,
            "job": job
        })
        return True

    # POST /jobs/<job_id>/lease/renew - Worker lease renewal (heartbeat extension)
    if path.startswith("/jobs/") and path.endswith("/lease/renew"):
        job_id = path[6:-12].strip()
        jobs_db = load_jobs_db()
        job = jobs_db.get("jobs", {}).get(job_id)
        if not job:
            handler.send_json(404, {"error": f"Job '{job_id}' not found"})
            return True

        target_node = job.get("target_node")
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(target_node, {})
        expected_auth = node_entry.get("auth_token")
        enrollment_token = get_or_create_enrollment_token()
        if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
            handler.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
            return True

        req_gen = body.get("lease_generation") if body else None
        if req_gen is None and body and body.get("fencing_token") is not None:
            req_gen = body.get("fencing_token")
        curr_gen = int(job.get("lease_generation", 1))
        if req_gen is not None and int(req_gen) < curr_gen:
            handler.send_json(412, {
                "error": f"Precondition Failed: Cannot renew superseded lease generation {req_gen} (current is {curr_gen})",
                "lease_generation": curr_gen,
                "fencing_token": curr_gen
            })
            return True

        curr_st = canonicalize_job_state(job.get("status"))
        if curr_st not in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
            handler.send_json(400, {"error": f"Cannot renew lease for job in '{curr_st}' state"})
            return True

        now_epoch = time.time()
        lease_exp = job.get("lease_expires_at")
        if lease_exp is not None and now_epoch > float(lease_exp):
            sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)
            handler.send_json(409, {
                "error": f"Cannot renew expired lease for job '{job_id}': lease expired at {lease_exp} (controller time: {now_epoch}).",
                "job_id": job_id,
                "lease_expired": True
            })
            return True

        extension_sec = min(300, max(10, int((body or {}).get("extension_sec", job.get("timeout", 60) + DEFAULT_LEASE_GRACE_SEC))))
        now_epoch = time.time()
        job["lease_expires_at"] = now_epoch + extension_sec
        save_jobs_db(jobs_db)

        handler.send_json(200, {
            "status": "ok",
            "job_id": job_id,
            "lease_expires_at": job["lease_expires_at"],
            "lease_generation": curr_gen
        })
        return True

    # POST /jobs/<job_id>/status - Update Job Status (e.g. CLAIMED -> RUNNING)
    if path.startswith("/jobs/") and path.endswith("/status"):
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True

        job_id = path[6:-7].strip()
        jobs_db = load_jobs_db()
        job = jobs_db.get("jobs", {}).get(job_id)
        if not job:
            handler.send_json(404, {"error": f"Job '{job_id}' not found"})
            return True

        target_node = job.get("target_node")
        nodes_db = load_nodes_db()
        node_entry = nodes_db.get("nodes", {}).get(target_node, {})
        expected_auth = node_entry.get("auth_token")
        enrollment_token = get_or_create_enrollment_token()
        if not token or (not constant_time_compare(token, expected_auth) and not constant_time_compare(token, enrollment_token)):
            handler.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
            return True

        req_gen = body.get("lease_generation") if body else None
        if req_gen is None and body and body.get("fencing_token") is not None:
            req_gen = body.get("fencing_token")
        curr_gen = int(job.get("lease_generation", 1))
        if req_gen is not None and int(req_gen) < curr_gen:
            handler.send_json(412, {
                "error": f"Precondition Failed: Superseded lease generation {req_gen} (current is {curr_gen})",
                "lease_generation": curr_gen,
                "fencing_token": curr_gen
            })
            return True

        new_status = (body.get("status") or "").upper()
        current_st = job.get("status")
        now_epoch = time.time()
        lease_exp = job.get("lease_expires_at")
        if lease_exp is not None and now_epoch > float(lease_exp) and current_st in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
            sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)
            handler.send_json(409, {
                "error": f"Cannot update status for job '{job_id}': active lease expired at {lease_exp} (controller time: {now_epoch}).",
                "job_id": job_id,
                "lease_expired": True
            })
            return True
        if not validate_job_transition(current_st, new_status):
            handler.send_json(400, {
                "error": f"Invalid state transition from '{current_st}' to '{new_status}'.",
                "current_status": current_st,
                "target_status": new_status
            })
            return True

        now_iso = get_current_iso_timestamp()
        job["status"] = new_status
        job["state"] = new_status
        if new_status == JOB_STATE_RUNNING and not job.get("started_at"):
            job["started_at"] = now_iso
            # Activate reservation in authoritative ledger (J3.3)
            try:
                from controller.stores.reservations import activate_reservation
                activate_reservation(jobs_db, job_id, int(job.get("attempt", 1)), curr_gen)
            except Exception:
                pass
        elif new_status in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
            try:
                from controller.stores.reservations import release_reservation
                release_reservation(jobs_db, job_id, int(job.get("attempt", 1)), curr_gen)
            except Exception:
                pass
        save_jobs_db(jobs_db)

        handler.send_json(200, {
            "status": "ok",
            "job_id": job_id,
            "current_status": new_status
        })
        return True

    # Delegated Workflow Routes
    if path == "/workflows" or path.startswith("/workflows/"):
        return handle_workflow_post(handler, path, body, token, query)

    # Delegated Artifact Routes
    if path == "/artifacts/upload" or path == "/artifacts/upload/raw" or (path.startswith("/artifacts/") and path.endswith("/verify")):
        return handle_artifact_post(handler, path, body, token, query)

    return False


def handle_job_delete(handler: Any, path: str, token: str) -> bool:
    """
    Handles DELETE requests for removing jobs from execution history.
    Returns True if handled, False otherwise.
    """
    if not path.startswith("/jobs/"):
        return False

    job_id = path[6:].strip()
    # Reject sub-paths if any
    if not job_id or "/" in job_id:
        return False

    enrollment_token = get_or_create_enrollment_token()
    if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
        handler.send_json(401, {"error": "Unauthorized: Authentication required to delete jobs"})
        return True

    jobs_db = load_jobs_db()
    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        handler.send_json(404, {"error": f"Job '{job_id}' not found"})
        return True

    # Guard: Active jobs and jobs belonging to active workflows cannot be deleted
    status = (job.get("status") or job.get("state") or "").upper()
    canon_st = canonicalize_job_state(status)
    if canon_st in (JOB_STATE_RUNNING, JOB_STATE_ASSIGNED, JOB_STATE_QUEUED, JOB_STATE_RETRY_WAIT, JOB_STATE_WAITING_FOR_DEPENDENCY, JOB_STATE_NEEDS_ATTENTION):
        handler.send_json(409, {
            "error": f"Cannot delete job '{job_id}' while in active state '{canon_st}'. Cancel the job first.",
            "status": canon_st,
            "job_id": job_id
        })
        return True

    # Check if job belongs to an active DAG workflow
    wf_id = job.get("workflow_id")
    if wf_id:
        workflows_db = load_workflows_db()
        wf = workflows_db.get("workflows", {}).get(wf_id)
        if wf and wf.get("status") not in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
            handler.send_json(409, {
                "error": f"Cannot delete job '{job_id}': step of active workflow '{wf_id}'. Cancel or delete workflow first.",
                "workflow_id": wf_id,
                "job_id": job_id
            })
            return True

    # Delete job from database
    del jobs_db["jobs"][job_id]
    save_jobs_db(jobs_db)

    corr_id = job.get("correlation_id") or getattr(handler, "current_correlation_id", None) or generate_correlation_id()
    controller_logger.info(
        "job.deleted",
        f"Job '{job_id}' ({job.get('type')}) deleted successfully from history.",
        correlation_id=corr_id,
        job_id=job_id,
        status=status
    )

    handler.send_json(200, {
        "status": "DELETED",
        "job_id": job_id,
        "message": f"Job '{job_id}' deleted successfully."
    })
    return True


__all__ = [
    "handle_job_get",
    "handle_job_query_get",
    "handle_job_post",
    "handle_job_delete",
]
