#!/usr/bin/env python3
"""
Controller Workflow Route Handlers
==================================
HTTP request handling for DAG workflow creation, querying, progress inspection,
and cancellation.
"""

from typing import Any, Dict, Optional

from common.crypto_utils import constant_time_compare
from config.structured_logger import (
    controller_logger,
    generate_correlation_id,
)
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import (
    load_jobs_db,
    load_workflows_db,
    save_jobs_db,
    save_workflows_db,
)
from controller.workflow_engine import (
    WorkflowValidationError,
    cancel_workflow,
    instantiate_workflow,
    progress_workflows,
)


def handle_workflow_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles GET requests for DAG workflows and workflow status.
    Returns True if handled, False otherwise.
    """
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
        wf_jobs = {}
        for step_id, step in wf.get("steps", {}).items():
            jid = step.get("job_id")
            if jid and jid in jobs_db.get("jobs", {}):
                wf_jobs[step_id] = jobs_db["jobs"][jid]
        handler.send_json(200, {
            "workflow": wf,
            "jobs": wf_jobs,
            "jobs_list": list(wf_jobs.values())
        })
        return True

    return False


def handle_workflow_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: Optional[str], query: Dict[str, list]) -> bool:
    """
    Handles POST requests for creating and cancelling DAG workflows.
    Returns True if handled, False otherwise.
    """
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

    return False


__all__ = [
    "handle_workflow_get",
    "handle_workflow_post",
]
