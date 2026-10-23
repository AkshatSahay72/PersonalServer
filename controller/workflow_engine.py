#!/usr/bin/env python3
"""
Controller Workflow Engine (DAG Task Decomposition)
===================================================
Validates, instantiates, and manages multi-step acyclic workflow graphs (DAGs),
tracking step progression, dependency fulfillment, and failure propagation.
"""

import secrets
from typing import Dict, Any, List, Set, Optional, Tuple

from common.time_utils import get_current_iso_timestamp
from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CANCELLED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMED_OUT,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
    canonicalize_job_state,
)


class WorkflowValidationError(ValueError):
    """Raised when a workflow graph contains cycles, invalid steps, or missing dependencies."""
    pass


def validate_workflow_dag(steps: Dict[str, Dict[str, Any]]) -> List[str]:
    """
    Validates that the steps mapping defines a valid, non-empty, Directed Acyclic Graph (DAG).
    Returns a topologically sorted list of step_ids.
    Raises WorkflowValidationError if a cycle or missing dependency is found.
    """
    if not steps or not isinstance(steps, dict):
        raise WorkflowValidationError("Workflow must contain at least one step.")

    step_ids = set(steps.keys())
    adj: Dict[str, List[str]] = {s: [] for s in step_ids}
    in_degree: Dict[str, int] = {s: 0 for s in step_ids}

    for step_id, spec in steps.items():
        depends_on = spec.get("depends_on") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        for dep in depends_on:
            if dep not in step_ids:
                raise WorkflowValidationError(f"Step '{step_id}' depends on undefined step '{dep}'.")
            if dep == step_id:
                raise WorkflowValidationError(f"Self-dependency cycle detected on step '{step_id}'.")
            adj[dep].append(step_id)
            in_degree[step_id] += 1

    # Kahn's algorithm for topological sorting
    queue = [s for s in step_ids if in_degree[s] == 0]
    sorted_steps = []

    while queue:
        u = queue.pop(0)
        sorted_steps.append(u)
        for v in adj[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    if len(sorted_steps) != len(step_ids):
        unresolved = [s for s, deg in in_degree.items() if deg > 0]
        raise WorkflowValidationError(f"Dependency cycle detected among steps: {unresolved}")

    return sorted_steps


def instantiate_workflow(
    workflow_spec: Dict[str, Any],
    jobs_db: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Instantiates a workflow specification into a workflow record and corresponding
    child jobs, setting independent steps to QUEUED and dependent steps to WAITING_FOR_DEPENDENCY.
    
    Returns (workflow_record, created_jobs_list).
    """
    name = workflow_spec.get("name") or "unnamed-workflow"
    description = workflow_spec.get("description", "")
    raw_steps = workflow_spec.get("steps", {})
    if isinstance(raw_steps, list):
        # Allow passing steps as list of dicts with 'id' or 'step_id'
        steps_dict = {}
        for s in raw_steps:
            sid = s.get("step_id") or s.get("id") or f"step-{len(steps_dict)+1}"
            steps_dict[sid] = s
        raw_steps = steps_dict

    # Validate graph
    validate_workflow_dag(raw_steps)

    wf_id = f"wf-{secrets.token_hex(6)}"
    now = get_current_iso_timestamp()

    # Create jobs for each step
    step_to_job_id = {}
    for step_id in raw_steps.keys():
        step_to_job_id[step_id] = f"job-{secrets.token_hex(6)}"

    created_jobs = []
    instantiated_steps = {}
    failure_policy = workflow_spec.get("failure_policy", "fail_fast")

    for step_id, spec in raw_steps.items():
        job_id = step_to_job_id[step_id]
        depends_on_steps = spec.get("depends_on") or []
        if isinstance(depends_on_steps, str):
            depends_on_steps = [depends_on_steps]

        depends_on_job_ids = [step_to_job_id[d] for d in depends_on_steps]

        # Initial status: QUEUED if 0 dependencies, else WAITING_FOR_DEPENDENCY
        initial_status = JOB_STATE_QUEUED if not depends_on_steps else JOB_STATE_WAITING_FOR_DEPENDENCY
        waiting_reason = "Waiting for dependencies to complete" if depends_on_steps else None

        job_record = {
            "id": job_id,
            "job_id": job_id,
            "workflow_id": wf_id,
            "workflow_step_id": step_id,
            "name": f"{name} :: {spec.get('name', step_id)}",
            "type": spec.get("type", "echo"),
            "parameters": spec.get("parameters", {}) or {},
            "requirements": spec.get("requirements", {}) or {},
            "target": spec.get("target") or spec.get("target_node") or "auto",
            "target_node": spec.get("target_node") or (spec.get("target") if spec.get("target") != "auto" else None),
            "assigned_node": None,
            "timeout": int(spec.get("timeout", 60)),
            "max_attempts": int(spec.get("max_attempts", 3)),
            "attempt": 1,
            "lease_generation": 0,
            "priority": spec.get("priority", "normal"),
            "depends_on_steps": depends_on_steps,
            "depends_on_jobs": depends_on_job_ids,
            "waiting_reason": waiting_reason,
            "status": initial_status,
            "state": initial_status,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "inputs": spec.get("inputs", []),
            "outputs": spec.get("outputs", [])
        }

        created_jobs.append(job_record)
        jobs_db.setdefault("jobs", {})[job_id] = job_record

        instantiated_steps[step_id] = {
            "step_id": step_id,
            "name": spec.get("name", step_id),
            "job_id": job_id,
            "type": spec.get("type"),
            "depends_on": depends_on_steps,
            "status": initial_status
        }

    workflow_record = {
        "workflow_id": wf_id,
        "id": wf_id,
        "name": name,
        "description": description,
        "status": JOB_STATE_QUEUED,
        "failure_policy": failure_policy,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "total_steps": len(instantiated_steps),
        "completed_steps": 0,
        "progress_percent": 0.0,
        "steps": instantiated_steps,
        "step_to_job_id": step_to_job_id
    }

    return workflow_record, created_jobs


def evaluate_workflow_progress(
    workflow: Dict[str, Any],
    jobs_db: Dict[str, Any]
) -> bool:
    """
    Evaluates step dependencies for a workflow and unblocks ready steps.
    Transitions jobs from WAITING_FOR_DEPENDENCY to QUEUED when parents SUCCEED.
    Transitions dependent jobs to FAILED if any required parent fails.
    Cascades transitions across multiple dependency levels in a single evaluation.
    Respects workflow failure_policy ('fail_fast' vs 'continue_on_failure').
    Returns True if any job or workflow state was modified.
    """
    if workflow.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED, JOB_STATE_FAILED):
        return False

    changed = False
    all_jobs = jobs_db.get("jobs", {})
    steps = workflow.get("steps", {})

    # Iteratively cascade dependency changes across arbitrary DAG depths
    while True:
        iteration_changed = False
        for step_id, step in steps.items():
            job_id = step.get("job_id")
            job = all_jobs.get(job_id)
            if not job:
                continue

            raw_status = job.get("status")
            canon_status = canonicalize_job_state(raw_status)
            step["status"] = canon_status

            # Check if this step is waiting for dependencies
            if canon_status == JOB_STATE_WAITING_FOR_DEPENDENCY:
                parent_step_ids = step.get("depends_on", [])
                parent_jobs = [all_jobs.get(steps[p]["job_id"]) for p in parent_step_ids if p in steps]

                # Check if any parent permanently failed or was cancelled
                failed_parent = next((p for p in parent_jobs if p and canonicalize_job_state(p.get("status")) in (JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_CANCELLED)), None)
                if failed_parent:
                    parent_st = canonicalize_job_state(failed_parent.get("status"))
                    job["status"] = JOB_STATE_FAILED
                    job["state"] = JOB_STATE_FAILED
                    job["finished_at"] = get_current_iso_timestamp()
                    job["retry_reason"] = f"Upstream dependency failed: step '{failed_parent.get('workflow_step_id')}' had status {parent_st}"
                    job["waiting_reason"] = None
                    step["status"] = JOB_STATE_FAILED
                    changed = True
                    iteration_changed = True
                    continue

                # Check if all parents succeeded
                all_parents_succeeded = parent_jobs and all(
                    p and canonicalize_job_state(p.get("status")) == JOB_STATE_SUCCEEDED
                    for p in parent_jobs
                )
                if all_parents_succeeded:
                    job["status"] = JOB_STATE_QUEUED
                    job["state"] = JOB_STATE_QUEUED
                    job["waiting_reason"] = None
                    step["status"] = JOB_STATE_QUEUED
                    changed = True
                    iteration_changed = True

        if not iteration_changed:
            break

    total = len(steps)
    completed_count = sum(1 for s in steps.values() if s.get("status") == JOB_STATE_SUCCEEDED)
    failed_count = sum(1 for s in steps.values() if s.get("status") in (JOB_STATE_FAILED, JOB_STATE_TIMED_OUT))
    cancelled_count = sum(1 for s in steps.values() if s.get("status") == JOB_STATE_CANCELLED)
    terminal_count = completed_count + failed_count + cancelled_count

    workflow["completed_steps"] = completed_count
    workflow["progress_percent"] = round((completed_count / total * 100), 1) if total > 0 else 100.0

    policy = workflow.get("failure_policy", "fail_fast")
    if failed_count > 0 and policy == "fail_fast":
        # Fail-fast: Cancel all remaining non-terminal steps so no sibling is stranded
        now_iso = get_current_iso_timestamp()
        for s in steps.values():
            if s.get("status") not in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
                j = all_jobs.get(s.get("job_id"))
                if j and canonicalize_job_state(j.get("status")) not in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
                    j["status"] = JOB_STATE_CANCELLED
                    j["state"] = JOB_STATE_CANCELLED
                    j["cancel_reason"] = "Cancelled due to workflow fail-fast policy"
                    j["finished_at"] = now_iso
                s["status"] = JOB_STATE_CANCELLED
                changed = True
        workflow["status"] = JOB_STATE_FAILED
        workflow["finished_at"] = now_iso
        changed = True

    elif terminal_count == total:
        if completed_count == total:
            workflow["status"] = JOB_STATE_SUCCEEDED
        elif failed_count > 0:
            workflow["status"] = JOB_STATE_FAILED
        else:
            workflow["status"] = JOB_STATE_CANCELLED
        workflow["finished_at"] = get_current_iso_timestamp()
        changed = True

    elif completed_count > 0 or any(s.get("status") in (JOB_STATE_RUNNING, JOB_STATE_ASSIGNED) for s in steps.values()):
        if workflow.get("status") == JOB_STATE_QUEUED:
            workflow["status"] = "RUNNING"
            workflow["started_at"] = workflow.get("started_at") or get_current_iso_timestamp()
            changed = True

    return changed


def progress_workflows(
    workflows_db: Dict[str, Any],
    jobs_db: Dict[str, Any]
) -> bool:
    """
    Evaluates all active workflows in workflows_db against current jobs in jobs_db.
    Returns True if any workflow or job was modified.
    """
    changed = False
    for wf in workflows_db.get("workflows", {}).values():
        if wf.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
            continue
        if evaluate_workflow_progress(wf, jobs_db):
            changed = True
    return changed


def cancel_workflow(
    workflow_id: str,
    workflows_db: Dict[str, Any],
    jobs_db: Dict[str, Any],
    reason: str = "Cancelled by operator"
) -> Optional[Dict[str, Any]]:
    """
    Cancels a workflow and all its non-terminal child jobs.
    Returns the updated workflow record, or None if not found.
    """
    workflow = workflows_db.get("workflows", {}).get(workflow_id)
    if not workflow:
        return None

    now_iso = get_current_iso_timestamp()
    workflow["status"] = JOB_STATE_CANCELLED
    workflow["finished_at"] = now_iso
    workflow["cancel_reason"] = reason

    all_jobs = jobs_db.get("jobs", {})
    for step in workflow.get("steps", {}).values():
        job_id = step.get("job_id")
        job = all_jobs.get(job_id)
        if job:
            curr_st = canonicalize_job_state(job.get("status"))
            if curr_st not in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
                job["status"] = JOB_STATE_CANCELLED
                job["state"] = JOB_STATE_CANCELLED
                job["finished_at"] = now_iso
                job["cancel_reason"] = reason
                step["status"] = JOB_STATE_CANCELLED

    return workflow
