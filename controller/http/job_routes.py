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
    Handles GET requests for job querying, metrics, workflows, artifacts,
    and worker job lease claims.
    Returns True if handled, False otherwise.
    """
    # GET /jobs/metrics - Distributed scheduler and queue observability
    if path == "/jobs/metrics":
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        workflows_db = load_workflows_db()
        artifacts_db = load_artifacts_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

        all_jobs = list(jobs_db.get("jobs", {}).values())
        status_counts: Dict[str, int] = {}
        total_duration_ms = 0
        dur_count = 0
        active_leases = 0
        now_ts = time.time()

        for j in all_jobs:
            st = j.get("status", "UNKNOWN")
            status_counts[st] = status_counts.get(st, 0) + 1
            if j.get("lease_expires_at") and j.get("lease_expires_at") > now_ts and st in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING):
                active_leases += 1
            dur = (j.get("result") or {}).get("duration_ms")
            if dur is not None and isinstance(dur, (int, float)) and dur > 0:
                total_duration_ms += dur
                dur_count += 1

        avg_duration_ms = round(total_duration_ms / dur_count, 1) if dur_count > 0 else 0
        queue_depth = sum(
            status_counts.get(s, 0)
            for s in (
                JOB_STATE_QUEUED,
                JOB_STATE_ASSIGNED,
                JOB_STATE_RETRY_WAIT,
                JOB_STATE_WAITING_FOR_DEPENDENCY,
                JOB_STATE_RECOVERING,
                JOB_STATE_NEEDS_ATTENTION,
            )
        )

        handler.send_json(200, {
            "queue_depth": queue_depth,
            "total_jobs": len(all_jobs),
            "status_counts": status_counts,
            "active_leases": active_leases,
            "avg_duration_ms": avg_duration_ms,
            "total_workflows": len(workflows_db.get("workflows", {})),
            "total_artifacts": len(artifacts_db.get("artifacts", {})),
            "heartbeat_timeout_sec": handler.heartbeat_timeout,
        })
        return True

    # GET /workflows - List DAG workflows
    if path == "/workflows":
        workflows_db = load_workflows_db()
        jobs_db = load_jobs_db()
        if progress_workflows(workflows_db, jobs_db):
            save_workflows_db(workflows_db)
            save_jobs_db(jobs_db)
        wfs = list(workflows_db.get("workflows", {}).values())
        handler.send_json(200, {
            "workflows": wfs,
            "count": len(wfs)
        })
        return True

    # GET /workflows/<workflow_id> - Workflow details & progress
    if path.startswith("/workflows/"):
        wf_id = path[11:].strip()
        workflows_db = load_workflows_db()
        jobs_db = load_jobs_db()
        if progress_workflows(workflows_db, jobs_db):
            save_workflows_db(workflows_db)
            save_jobs_db(jobs_db)
        wf = workflows_db.get("workflows", {}).get(wf_id)
        if not wf:
            handler.send_json(404, {"error": f"Workflow '{wf_id}' not found"})
            return True
        handler.send_json(200, {"workflow": wf})
        return True

    # GET /artifacts - List cluster artifacts
    if path == "/artifacts":
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to list artifacts"})
            return True
        artifacts_db = load_artifacts_db()
        arts = list(artifacts_db.get("artifacts", {}).values())
        handler.send_json(200, {
            "artifacts": arts,
            "count": len(arts)
        })
        return True

    # GET /artifacts/<artifact_id>/metadata - Artifact metadata & checksum status
    if path.startswith("/artifacts/") and path.endswith("/metadata"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to access artifact metadata"})
            return True
        art_id = path[11:-9].strip()
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True

        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        if not is_admin:
            caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
            if not caller_node_id:
                handler.send_json(401, {"error": "Unauthorized: Node credentials invalid"})
                return True
            art_wf_id = art.get("workflow_id")
            authorized_nodes = art.get("authorized_nodes", [])
            producing_node = art.get("producing_node")
            if not producing_node and art.get("job_id"):
                jobs_db = load_jobs_db()
                j = jobs_db.get("jobs", {}).get(art["job_id"], {})
                producing_node = j.get("assigned_node") or j.get("target_node")

            if art_wf_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == art_wf_id]
                node_in_wf = any(j.get("assigned_node") == caller_node_id or j.get("target_node") == caller_node_id for j in wf_jobs)
                if not node_in_wf and caller_node_id not in authorized_nodes and producing_node != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifacts for workflow '{art_wf_id}'"})
                    return True
            else:
                # Ad-hoc artifact without workflow: restrict to producing node or explicitly authorized nodes
                if caller_node_id != producing_node and caller_node_id not in authorized_nodes:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifact '{art_id}' (not producer or authorized node)"})
                    return True

        is_valid = verify_artifact_integrity(art)
        handler.send_json(200, {"artifact": art, "integrity_verified": is_valid})
        return True

    # GET /artifacts/<artifact_id>/download or GET /artifacts/<artifact_id>
    if path.startswith("/artifacts/"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to download artifacts"})
            return True
        art_id = path[11:].rstrip("/")
        if art_id.endswith("/download"):
            art_id = art_id[:-9]
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True

        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        if not is_admin:
            caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
            if not caller_node_id:
                handler.send_json(401, {"error": "Unauthorized: Node credentials invalid"})
                return True
            art_wf_id = art.get("workflow_id")
            authorized_nodes = art.get("authorized_nodes", [])
            producing_node = art.get("producing_node")
            if not producing_node and art.get("job_id"):
                jobs_db = load_jobs_db()
                j = jobs_db.get("jobs", {}).get(art["job_id"], {})
                producing_node = j.get("assigned_node") or j.get("target_node")

            if art_wf_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == art_wf_id]
                node_in_wf = any(j.get("assigned_node") == caller_node_id or j.get("target_node") == caller_node_id for j in wf_jobs)
                if not node_in_wf and caller_node_id not in authorized_nodes and producing_node != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifacts for workflow '{art_wf_id}'"})
                    return True
            else:
                # Ad-hoc artifact without workflow: restrict to producing node or explicitly authorized nodes
                if caller_node_id != producing_node and caller_node_id not in authorized_nodes:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifact '{art_id}' (not producer or authorized node)"})
                    return True


        file_path = art.get("path")
        if not file_path or not os.path.exists(file_path):
            handler.send_json(404, {"error": f"Artifact file on disk missing for '{art_id}'"})
            return True
        file_size = os.path.getsize(file_path)
        handler.send_response(200)
        handler.send_header("Content-Type", art.get("content_type", "application/octet-stream"))
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("X-Artifact-Sha256", art.get("sha256", ""))
        handler.send_header("Content-Disposition", f'attachment; filename="{art.get("filename", art_id)}"')
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                handler.wfile.write(chunk)
        return True

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
                    and is_node_eligible(node, j.get("requirements", {}))
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
            f"Job {next_job['job_id']} ({next_job['type']}) CLAIMED by node '{node_id}' (Attempt {next_job.get('attempt', 1)}, Lease {lease_duration}s)",
            correlation_id=corr_id,
            job_id=next_job["job_id"],
            node_id=node_id,
            attempt=next_job.get("attempt", 1),
            lease_duration_sec=lease_duration
        )
        handler.send_json(200, {"job": next_job})
        return True

    # GET /jobs/<job_id>/artifacts
    if path.startswith("/jobs/") and path.endswith("/artifacts"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to access job artifacts"})
            return True
        job_id = path[6:-10].strip()
        artifacts_db = load_artifacts_db()
        matching = [
            a for a in artifacts_db.get("artifacts", {}).values()
            if a.get("job_id") == job_id
        ]
        handler.send_json(200, {
            "job_id": job_id,
            "artifacts": matching,
            "count": len(matching)
        })
        return True

    # GET /jobs
    if path == "/jobs":
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

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

            handler.send_json(200, {
                "jobs": paginated_jobs,
                "count": len(paginated_jobs),
                "total_count": total_count,
                "page": page,
                "page_size": limit,
                "total_pages": total_pages
            })
            return True

        handler.send_json(200, {
            "jobs": all_jobs,
            "count": len(all_jobs),
            "total_count": total_count
        })
        return True

    # GET /jobs/<job_id>
    if path.startswith("/jobs/"):
        job_id = path[6:].strip()
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        sweep_expired_leases(jobs_db, nodes_db, handler.heartbeat_timeout)

        job = jobs_db.get("jobs", {}).get(job_id)
        if not job:
            handler.send_json(404, {"error": f"Job '{job_id}' not found"})
            return True
        diagnostics = {
            "attempts_total": len(job.get("attempts_history", [])),
            "current_attempt": job.get("attempt", 1),
            "is_active": job.get("status") in (JOB_STATE_QUEUED, JOB_STATE_CLAIMED, JOB_STATE_RUNNING, JOB_STATE_RECOVERING),
            "last_failure_reason": job.get("retry_reason"),
            "correlation_id": job.get("correlation_id"),
            "lease_active": bool(job.get("lease_expires_at") and time.time() < job.get("lease_expires_at")),
        }
        handler.send_json(200, {"job": job, "diagnostics": diagnostics})
        return True

    return False


def handle_job_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: str, query: Dict[str, list]) -> bool:
    """
    Handles POST requests for job submission, result submission, retry scheduling,
    and cancellation.
    Returns True if handled, False otherwise.
    """
    # POST /jobs - Job Submission
    if path == "/jobs":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True

        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to submit jobs"})
            return True

        target = body.get("target") or body.get("target_node") or "auto"
        job_type = body.get("type")
        job_name = body.get("name", job_type or "job")
        timeout_sec = int(body.get("timeout", 60))
        max_attempts = int(body.get("max_attempts", 3))
        parameters = body.get("parameters", {}) or {}
        requirements = body.get("requirements", {}) or {}

        # Privileged capability check: allow_internal and privileged requirements require admin credentials
        is_admin = constant_time_compare(token, enrollment_token)
        if parameters.get("allow_internal") and not is_admin:
            handler.send_json(403, {
                "error": "Forbidden: 'allow_internal' parameter requires administrator credentials.",
                "status": JOB_STATE_REJECTED
            })
            return True

        if requirements.get("privileged") and not is_admin:
            handler.send_json(403, {
                "error": "Forbidden: Privileged workloads require administrator credentials.",
                "status": JOB_STATE_REJECTED
            })
            return True

        # Validation: Safe allowlisted workload type rejection
        if job_type not in ALLOWLISTED_WORKLOADS:
            handler.send_json(400, {
                "error": f"Job type '{job_type}' is not in the safe allowlist. Allowed: {list(ALLOWLISTED_WORKLOADS.keys())}",
                "status": JOB_STATE_REJECTED
            })
            return True

        nodes_db = load_nodes_db()
        target_node = None
        scheduler_info = None

        # Path A: Automatic Resource-Aware Scheduling
        jobs_db = load_jobs_db()
        waiting_reason = None
        initial_status = JOB_STATE_QUEUED

        if target.lower() == "auto":
            decision = ResourceScheduler.select_node(
                requirements,
                nodes_db,
                handler.heartbeat_timeout,
                cluster_jobs=jobs_db.get("jobs", {})
            )
            if not decision["selected_node"]:
                if body.get("fail_if_no_nodes", False):
                    handler.send_json(400, {
                        "error": f"Scheduling Failed: {decision['reason']}",
                        "status": JOB_STATE_REJECTED,
                        "scheduler": decision
                    })
                    return True
                target_node = None
                scheduler_info = {
                    "mode": "resource-aware-pending",
                    "selected_node": None,
                    "score": 0,
                    "reason": decision["reason"],
                    "candidates_count": len(decision.get("candidates", []))
                }
                waiting_reason = f"Awaiting eligible worker: {decision['reason']}"
            else:
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
            is_pinned = bool(body.get("pinned", False) or body.get("pin_node"))
            target_node = target
            node_entry = nodes_db.get("nodes", {}).get(target_node)

            if not node_entry:
                handler.send_json(400, {
                    "error": f"Target node '{target_node}' does not exist in cluster.",
                    "status": JOB_STATE_REJECTED
                })
                return True

            if node_entry.get("status") == STATE_REMOVED:
                handler.send_json(400, {
                    "error": f"Target node '{target_node}' was removed from the cluster.",
                    "status": JOB_STATE_REJECTED
                })
                return True

            live_status = compute_node_liveness(node_entry, handler.heartbeat_timeout)
            if live_status == STATE_OFFLINE:
                if is_pinned or body.get("wait_if_offline", False):
                    waiting_reason = f"Pinned node '{target_node}' is currently OFFLINE; awaiting reconnection."
                    initial_status = JOB_STATE_NEEDS_ATTENTION
                    scheduler_info = {
                        "mode": "explicit-pinned",
                        "target_node": target_node,
                        "reason": waiting_reason
                    }
                else:
                    handler.send_json(400, {
                        "error": f"Target node '{target_node}' is currently OFFLINE (heartbeat timeout). Cannot submit job.",
                        "status": JOB_STATE_REJECTED
                    })
                    return True

            if live_status in (STATE_DRAINING, STATE_DEBOARDING) or node_entry.get("status") in (STATE_DRAINING, STATE_DEBOARDING):
                handler.send_json(400, {
                    "error": f"Target node '{target_node}' is currently {live_status or node_entry.get('status')} (no new workloads accepted). Cannot submit job.",
                    "status": JOB_STATE_REJECTED
                })
                return True

            if not scheduler_info:
                scheduler_info = {
                    "mode": "explicit-pinned" if is_pinned else "explicit",
                    "target_node": target_node,
                    "reason": "Pinned node specified by client" if is_pinned else "Explicit node specified by client"
                }

        timeout_sec = max(5, min(timeout_sec, 300))
        job_id = f"job-{secrets.token_hex(6)}"
        now = get_current_iso_timestamp()

        corr_id = getattr(handler, "current_correlation_id", None) or generate_correlation_id()
        job_record = {
            "id": job_id,
            "job_id": job_id,
            "correlation_id": corr_id,
            "name": job_name,
            "type": job_type,
            "parameters": parameters,
            "requirements": requirements,
            "target": target,
            "target_node": target_node,
            "assigned_node": target_node,
            "pinned": bool(body.get("pinned", False) or body.get("pin_node")),
            "priority": body.get("priority", "normal"),
            "waiting_reason": waiting_reason,
            "inputs": body.get("inputs", []),
            "expected_outputs": body.get("expected_outputs", []),
            "scheduler": scheduler_info,
            "timeout": timeout_sec,
            "max_attempts": max_attempts,
            "attempt": 1,
            "lease_generation": 0,
            "claimed_at": None,
            "lease_expires_at": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "status": initial_status,
            "state": initial_status,
            "retryable": bool(body.get("retryable", True)),
            "backoff_sec": body.get("backoff_sec"),
            "retry_reason": None,
            "attempts_history": [],
            "result": None
        }

        jobs_db.setdefault("jobs", {})[job_id] = job_record
        save_jobs_db(jobs_db)

        controller_logger.info(
            "job.scheduled",
            f"Queued job {job_id} ({job_type}) for target node {target_node} [mode={scheduler_info['mode']}]",
            correlation_id=corr_id,
            job_id=job_id,
            node_id=target_node,
            job_type=job_type,
            mode=scheduler_info["mode"],
            score=scheduler_info.get("score"),
            reason=scheduler_info.get("reason")
        )
        handler.send_json(200, {
            "status": initial_status,
            "job_id": job_id,
            "id": job_id,
            "target_node": target_node,
            "scheduler": scheduler_info,
            "job": job_record
        })
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

        # Stale worker protection: check attempt number if provided
        reported_attempt = body.get("attempt")
        current_attempt = job.get("attempt", 1)
        corr_id = job.get("correlation_id") or getattr(handler, "current_correlation_id", None) or generate_correlation_id()
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
            handler.send_json(409, {
                "error": f"Stale worker result: reported attempt {reported_attempt} is older than current attempt {current_attempt}.",
                "job_id": job_id,
                "current_attempt": current_attempt,
                "stale_attempt": True
            })
            return True

        # Fencing token: reject superseded lease generations
        reported_gen = body.get("lease_generation")
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
            handler.send_json(409, {
                "error": f"Superseded lease generation: reported generation {reported_gen} is older than current generation {current_gen}.",
                "job_id": job_id,
                "current_generation": current_gen,
                "superseded": True
            })
            return True

        # Idempotency / Stale report protection: Ignore duplicate reports if job is already terminal
        if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED) or (job.get("status") == JOB_STATE_FAILED and job.get("result") is not None and current_attempt >= job.get("max_attempts", 3)):
            handler.send_json(200, {
                "status": job.get("status"),
                "message": f"Job '{job_id}' has already reached terminal state '{job.get('status')}'. Duplicate report ignored.",
                "job_id": job_id,
                "duplicate": True
            })
            return True

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
        curr_gen = int(job.get("lease_generation", 1))
        if req_gen is not None and int(req_gen) < curr_gen:
            handler.send_json(409, {
                "error": f"Cannot renew superseded lease generation {req_gen} (current is {curr_gen})",
                "lease_generation": curr_gen
            })
            return True

        curr_st = canonicalize_job_state(job.get("status"))
        if curr_st not in (JOB_STATE_ASSIGNED, JOB_STATE_RUNNING):
            handler.send_json(400, {"error": f"Cannot renew lease for job in '{curr_st}' state"})
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

        new_status = (body.get("status") or "").upper()
        current_st = job.get("status")
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
        save_jobs_db(jobs_db)

        handler.send_json(200, {
            "status": "ok",
            "job_id": job_id,
            "current_status": new_status
        })
        return True

    # POST /workflows - Create and instantiate multi-step DAG workflow
    if path == "/workflows":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to create workflows"})
            return True

        jobs_db = load_jobs_db()
        workflows_db = load_workflows_db()
        try:
            workflow_record, created_jobs = instantiate_workflow(body, jobs_db)
        except WorkflowValidationError as e:
            handler.send_json(400, {"error": f"Invalid workflow specification: {e}"})
            return True
        except Exception as e:
            handler.send_json(400, {"error": f"Failed to instantiate workflow: {e}"})
            return True

        wf_id = workflow_record["workflow_id"]
        workflows_db.setdefault("workflows", {})[wf_id] = workflow_record
        save_workflows_db(workflows_db)
        save_jobs_db(jobs_db)

        corr_id = getattr(handler, "current_correlation_id", None) or generate_correlation_id()
        controller_logger.info(
            "workflow.created",
            f"Created workflow '{wf_id}' ({workflow_record['name']}) with {len(created_jobs)} steps",
            correlation_id=corr_id,
            workflow_id=wf_id,
            total_steps=len(created_jobs)
        )
        handler.send_json(201, {
            "status": "CREATED",
            "workflow": workflow_record,
            "jobs_count": len(created_jobs)
        })
        return True

    # POST /workflows/<workflow_id>/cancel - Cancel DAG workflow
    if path.startswith("/workflows/") and path.endswith("/cancel"):
        wf_id = path[11:-7].strip()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to cancel workflow"})
            return True

        workflows_db = load_workflows_db()
        jobs_db = load_jobs_db()
        reason = (body.get("reason") if body else "") or "Cancelled by operator"
        cancelled_wf = cancel_workflow(wf_id, workflows_db, jobs_db, reason=reason)
        if not cancelled_wf:
            handler.send_json(404, {"error": f"Workflow '{wf_id}' not found"})
            return True

        save_workflows_db(workflows_db)
        save_jobs_db(jobs_db)
        handler.send_json(200, {
            "status": "CANCELLED",
            "workflow_id": wf_id,
            "workflow": cancelled_wf,
            "message": f"Workflow '{wf_id}' and all pending/active steps cancelled."
        })
        return True

    # POST /artifacts/upload - Secure atomic artifact upload with SHA-256
    if path == "/artifacts/upload":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to upload artifacts"})
            return True

        filename = body.get("filename") or body.get("name")
        content_b64 = body.get("content_base64")
        if not filename or content_b64 is None:
            handler.send_json(400, {"error": "Missing required 'filename' or 'content_base64'"})
            return True

        # Bound request payload memory size: 64 MB base64 limit
        if len(content_b64) > 64 * 1024 * 1024:
            handler.send_json(413, {"error": "Payload Too Large: Base64 artifact exceeds 64MB limit. Use streaming upload."})
            return True

        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception as e:
            handler.send_json(400, {"error": f"Invalid base64 encoding: {e}"})
            return True

        expected_sha256 = body.get("sha256")
        job_id = body.get("job_id")
        workflow_id = body.get("workflow_id")
        content_type = body.get("content_type", "application/octet-stream")
        metadata = body.get("metadata", {})

        storage_root = get_controller_paths().artifacts_dir
        art_id = generate_artifact_id()

        try:
            art_record = store_artifact_bytes(
                storage_root=storage_root,
                artifact_id=art_id,
                filename=filename,
                content_bytes=content_bytes,
                expected_sha256=expected_sha256,
                job_id=job_id,
                workflow_id=workflow_id,
                content_type=content_type,
                metadata=metadata
            )
        except ValueError as e:
            handler.send_json(400, {"error": str(e)})
            return True
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to store artifact: {e}"})
            return True

        caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
        if isinstance(caller_node_id, str):
            art_record["producing_node"] = caller_node_id
        elif job_id:
            jobs_db = load_jobs_db()
            j = jobs_db.get("jobs", {}).get(job_id, {})
            if j.get("assigned_node"):
                art_record["producing_node"] = j.get("assigned_node")

        auth_nodes = body.get("authorized_nodes") or metadata.get("authorized_nodes")
        if auth_nodes and isinstance(auth_nodes, list):
            art_record["authorized_nodes"] = [str(n) for n in auth_nodes]

        artifacts_db = load_artifacts_db()
        artifacts_db.setdefault("artifacts", {})[art_id] = art_record
        save_artifacts_db(artifacts_db)

        handler.send_json(201, {
            "status": "UPLOADED",
            "artifact": art_record
        })
        return True

    # POST /artifacts/upload/raw - Streaming raw binary artifact upload
    if path == "/artifacts/upload/raw":
        enrollment_token = get_or_create_enrollment_token()
        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        caller_node_id = None
        if hasattr(handler, "get_authenticated_node_id"):
            raw_node_id = handler.get_authenticated_node_id()
            if isinstance(raw_node_id, str):
                caller_node_id = raw_node_id

        if not is_admin and not caller_node_id and not handler.is_authenticated_admin_or_node():
            handler.send_json(401, {"error": "Unauthorized: Authentication required to upload artifacts"})
            return True

        filename = query.get("filename", [None])[0] or handler.headers.get("X-Artifact-Filename")
        if not filename:
            handler.send_json(400, {"error": "Missing 'filename' query parameter or X-Artifact-Filename header"})
            return True

        try:
            content_length = int(handler.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0

        # Maximum artifact limit: 64 MB
        MAX_ARTIFACT_LIMIT = 64 * 1024 * 1024
        if content_length > MAX_ARTIFACT_LIMIT:
            handler.send_json(413, {"error": f"Payload Too Large: Artifact exceeds {MAX_ARTIFACT_LIMIT // (1024*1024)}MB limit."})
            return True

        expected_sha256 = query.get("sha256", [None])[0] or handler.headers.get("X-Artifact-Sha256")
        job_id = query.get("job_id", [None])[0] or handler.headers.get("X-Job-Id")
        workflow_id = query.get("workflow_id", [None])[0] or handler.headers.get("X-Workflow-Id")
        name = query.get("name", [None])[0] or handler.headers.get("X-Artifact-Name") or filename
        step_id = query.get("step_id", [None])[0] or handler.headers.get("X-Workflow-Step-Id")
        content_type = handler.headers.get("Content-Type", "application/octet-stream")

        # Worker assignment authorization
        if job_id:
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if job:
                if not is_admin and caller_node_id and job.get("assigned_node") and job.get("assigned_node") != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not assigned to job '{job_id}'"})
                    return True
                if not workflow_id and job.get("workflow_id"):
                    workflow_id = job.get("workflow_id")
                if not step_id and job.get("workflow_step_id"):
                    step_id = job.get("workflow_step_id")

        # Check for existing artifact on retry (idempotent duplicate avoidance)
        artifacts_db = load_artifacts_db()
        existing_art = next(
            (a for a in artifacts_db.get("artifacts", {}).values()
             if a.get("job_id") == job_id and (a.get("filename") == filename or a.get("name") == name)),
            None
        )
        if existing_art and expected_sha256 and existing_art.get("sha256", "").lower() == expected_sha256.lower():
            if content_length > 0:
                # Drain stream cleanly
                try:
                    _ = handler.rfile.read(content_length)
                except Exception:
                    pass
            if verify_artifact_integrity(existing_art):
                handler.send_json(200, {
                    "status": "EXISTS",
                    "artifact": existing_art,
                    "message": "Artifact already exists with identical checksum."
                })
                return True

        storage_root = get_controller_paths().artifacts_dir
        art_id = generate_artifact_id()

        try:
            art_record = store_artifact_stream(
                storage_root=storage_root,
                artifact_id=art_id,
                filename=filename,
                stream=handler.rfile,
                total_bytes_expected=content_length,
                expected_sha256=expected_sha256,
                job_id=job_id,
                workflow_id=workflow_id,
                content_type=content_type,
                max_bytes=MAX_ARTIFACT_LIMIT
            )
            art_record["name"] = name
            art_record["workflow_step_id"] = step_id
            art_record["producing_node"] = caller_node_id if isinstance(caller_node_id, str) else None
            if workflow_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == workflow_id]
                auth_nodes = [j.get("assigned_node") for j in wf_jobs if isinstance(j.get("assigned_node"), str)]
                if isinstance(caller_node_id, str):
                    auth_nodes.append(caller_node_id)
                art_record["authorized_nodes"] = list(set(auth_nodes))
        except ValueError as e:
            handler.send_json(400, {"error": str(e)})
            return True
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to store streaming artifact: {e}"})
            return True

        artifacts_db = load_artifacts_db()
        artifacts_db.setdefault("artifacts", {})[art_id] = art_record
        save_artifacts_db(artifacts_db)

        handler.send_json(201, {
            "status": "UPLOADED",
            "artifact": art_record
        })
        return True

    # POST /artifacts/<artifact_id>/verify - Verify integrity of artifact on disk
    if path.startswith("/artifacts/") and path.endswith("/verify"):
        art_id = path[11:-7].strip()
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True
        is_valid = verify_artifact_integrity(art)
        handler.send_json(200, {
            "artifact_id": art_id,
            "integrity_verified": is_valid,
            "artifact": art
        })
        return True

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
    "handle_job_post",
    "handle_job_delete",
]
