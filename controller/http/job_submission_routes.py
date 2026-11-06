#!/usr/bin/env python3
"""
Controller Job Submission Route Handlers
========================================
HTTP request handling for job submission (POST /jobs), payload validation,
parameter and artifact normalization, resource-aware/capability-aware scheduling,
explicit node targeting, and job record persistence.
"""

import secrets
from typing import Any, Dict, Optional

from common.crypto_utils import constant_time_compare
from common.time_utils import get_current_iso_timestamp
from config.structured_logger import (
    controller_logger,
    generate_correlation_id,
)
from controller.constants import (
    ALLOWLISTED_WORKLOADS,
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_REJECTED,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_OFFLINE,
    STATE_REMOVED,
)
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import (
    load_artifacts_db,
    load_jobs_db,
    load_nodes_db,
    save_jobs_db,
)
from scheduler.scheduler import (
    ResourceScheduler,
    compute_node_liveness,
)


def handle_job_submission_post(
    handler: Any,
    path: str,
    body: Optional[Dict[str, Any]],
    token: str,
    query: Dict[str, list],
) -> bool:
    """
    Handles POST requests for job submission (POST /jobs).
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
        target_str = str(target).strip()
        is_cap_target = False
        target_lower = target_str.lower()

        job_type = body.get("type")
        job_name = body.get("name", job_type or "job")
        timeout_sec = int(body.get("timeout", 60))
        max_attempts = int(body.get("max_attempts", 3))
        parameters = body.get("parameters") or body.get("params") or {}
        if not isinstance(parameters, dict):
            parameters = {}
        else:
            parameters = dict(parameters)

        # Normalize top-level code, script, and args without overwriting explicitly passed parameters
        if "code" in body and "code" not in parameters:
            parameters["code"] = body["code"]
        if "script" in body and "script" not in parameters:
            parameters["script"] = body["script"]
        if "args" in body and "args" not in parameters:
            parameters["args"] = body["args"]

        # Normalize output declarations into canonical list of dicts: {"name": ..., "filename": ..., "path": ...}
        raw_outputs = body.get("outputs") or body.get("expected_outputs") or parameters.get("outputs") or parameters.get("artifacts") or []
        if isinstance(raw_outputs, str):
            raw_outputs = [raw_outputs]
        elif not isinstance(raw_outputs, (list, tuple)):
            raw_outputs = []

        normalized_outputs = []
        for item in raw_outputs:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    normalized_outputs.append({"name": s, "filename": s, "path": s})
            elif isinstance(item, dict):
                out_name = item.get("name") or item.get("filename") or item.get("path")
                out_filename = item.get("filename") or item.get("path") or item.get("name")
                out_path = item.get("path") or item.get("filename") or item.get("name")
                if out_name:
                    out_copy = dict(item)
                    out_copy["name"] = str(out_name).strip()
                    out_copy["filename"] = str(out_filename).strip()
                    out_copy["path"] = str(out_path).strip()
                    normalized_outputs.append(out_copy)

        # Resolve and validate input artifacts against the cluster artifacts database
        raw_inputs = body.get("inputs") or parameters.get("inputs") or body.get("input_artifacts") or []
        if isinstance(raw_inputs, str):
            raw_inputs = [raw_inputs]
        elif not isinstance(raw_inputs, (list, tuple)):
            raw_inputs = []

        resolved_input_artifacts = []
        if raw_inputs:
            artifacts_db = load_artifacts_db()
            known_artifacts = artifacts_db.get("artifacts", {})

            for item in raw_inputs:
                art_id = None
                dest_name = None

                if isinstance(item, str):
                    art_id = item.strip()
                elif isinstance(item, dict):
                    art_id = str(item.get("artifact_id") or item.get("id") or item.get("art_id") or "").strip()
                    dest_name = item.get("name") or item.get("filename")
                else:
                    handler.send_json(400, {
                        "error": f"Invalid input specification '{item}'. Must be an artifact ID string or dictionary.",
                        "status": JOB_STATE_REJECTED
                    })
                    return True

                if not art_id:
                    handler.send_json(400, {
                        "error": "Input artifact specification missing required 'artifact_id'.",
                        "status": JOB_STATE_REJECTED
                    })
                    return True

                art_entry = known_artifacts.get(art_id)
                if not art_entry:
                    handler.send_json(400, {
                        "error": f"Input artifact '{art_id}' not found in cluster artifact database.",
                        "status": JOB_STATE_REJECTED
                    })
                    return True

                final_dest_name = str(dest_name or art_entry.get("name") or art_entry.get("filename") or art_id).strip()
                resolved_input_artifacts.append({
                    "artifact_id": art_id,
                    "name": final_dest_name,
                    "filename": art_entry.get("filename") or final_dest_name,
                    "sha256": art_entry.get("sha256"),
                    "size_bytes": art_entry.get("size_bytes", 0),
                    "source_job_id": art_entry.get("job_id")
                })

        requirements = body.get("requirements", {}) or {}

        if target_lower.startswith(("capability:", "cap:")):
            is_cap_target = True
            cap_names = [c.strip() for c in target_str.split(":", 1)[1].split(",") if c.strip()]
            for cap_name in cap_names:
                req_caps = requirements.setdefault("capabilities", [])
                if isinstance(req_caps, list):
                    if cap_name not in req_caps:
                        req_caps.append(cap_name)
                elif isinstance(req_caps, str):
                    requirements["capabilities"] = [req_caps, cap_name] if req_caps != cap_name else [cap_name]
        elif target_lower.startswith("role:"):
            is_cap_target = True
            role_name = target_str.split(":", 1)[1].strip()
            if role_name:
                requirements["role"] = role_name

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

        # Path A: Automatic / Capability-Based Resource-Aware Scheduling
        jobs_db = load_jobs_db()
        waiting_reason = None
        initial_status = JOB_STATE_QUEUED

        if target_lower == "auto" or is_cap_target:
            decision = ResourceScheduler.select_node(
                requirements,
                nodes_db,
                handler.heartbeat_timeout,
                cluster_jobs=jobs_db.get("jobs", {})
            )
            mode_prefix = "capability-aware" if is_cap_target else "resource-aware"
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
                    "mode": f"{mode_prefix}-pending",
                    "selected_node": None,
                    "score": 0,
                    "reason": decision["reason"],
                    "rejected": decision.get("rejected", {}),
                    "candidates_count": len(decision.get("candidates", []))
                }
                waiting_reason = f"Awaiting eligible worker: {decision['reason']}"
            else:
                target_node = decision["selected_node"]
                scheduler_info = {
                    "mode": mode_prefix,
                    "selected_node": target_node,
                    "score": decision["score"],
                    "reason": decision["reason"],
                    "candidates_count": len(decision["candidates"])
                }
                print(f"[CONTROLLER-SCHEDULER] {mode_prefix.capitalize()}-selected node '{target_node}': {decision['reason']}")

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

            if requirements and live_status not in (STATE_OFFLINE, STATE_DRAINING, STATE_DEBOARDING):
                is_eligible, ineligibility_reason = ResourceScheduler.is_node_eligible(
                    node_entry, requirements, handler.heartbeat_timeout
                )
                if not is_eligible:
                    if is_pinned or body.get("wait_if_insufficient", False):
                        waiting_reason = f"Pinned node '{target_node}' does not satisfy requirements: {ineligibility_reason}"
                        initial_status = JOB_STATE_NEEDS_ATTENTION
                        scheduler_info = {
                            "mode": "explicit-pinned",
                            "target_node": target_node,
                            "reason": waiting_reason
                        }
                    else:
                        handler.send_json(400, {
                            "error": f"Target node '{target_node}' does not satisfy job requirements: {ineligibility_reason}",
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
            "inputs": raw_inputs,
            "input_artifacts": resolved_input_artifacts,
            "outputs": normalized_outputs,
            "expected_outputs": normalized_outputs,
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
            "waiting_reason": waiting_reason,
            "scheduler": scheduler_info,
            "job": job_record
        })
        return True

    return False
