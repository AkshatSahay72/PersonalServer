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
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_REJECTED,
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
        raise WorkflowValidationError("Workflow must contain at least one step or job.")

    step_ids = set(steps.keys())
    adj: Dict[str, List[str]] = {s: [] for s in step_ids}
    in_degree: Dict[str, int] = {s: 0 for s in step_ids}

    for step_id, spec in steps.items():
        if not isinstance(spec, dict):
            raise WorkflowValidationError(f"Step '{step_id}' definition must be a dictionary.")
        depends_on = spec.get("depends_on") or spec.get("dependencies") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on] if depends_on.strip() else []
        elif not isinstance(depends_on, (list, tuple, set)):
            raise WorkflowValidationError(f"Step '{step_id}' dependencies must be a list of step/job IDs.")

        for dep in depends_on:
            if not isinstance(dep, str):
                continue
            if dep not in step_ids:
                raise WorkflowValidationError(f"Step '{step_id}' depends on undefined step/job '{dep}'.")
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
        unresolved = sorted([s for s, deg in in_degree.items() if deg > 0])
        raise WorkflowValidationError(f"Dependency cycle detected among steps: {unresolved}")

    return sorted_steps


def instantiate_workflow(
    workflow_spec: Dict[str, Any],
    jobs_db: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Instantiates a workflow specification into a workflow record and corresponding
    child jobs, setting independent steps to QUEUED and dependent steps to WAITING_FOR_DEPENDENCY.
    Validates DAG integrity, unique IDs, and parameter consistency prior to job creation.
    
    Returns (workflow_record, created_jobs_list).
    """
    name = workflow_spec.get("name") or "unnamed-workflow"
    description = workflow_spec.get("description", "")
    raw_steps = workflow_spec.get("steps") or workflow_spec.get("jobs") or {}

    if isinstance(raw_steps, list):
        # Validate unique step IDs when list is provided
        steps_dict = {}
        seen_ids = set()
        for idx, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                raise WorkflowValidationError(f"Step at index {idx} must be a dictionary.")
            sid = s.get("step_id") or s.get("id") or s.get("job_id")
            if not sid:
                raise WorkflowValidationError(f"Step at index {idx} is missing required 'id', 'step_id', or 'job_id'.")
            if sid in seen_ids:
                raise WorkflowValidationError(f"Duplicate step/job ID '{sid}' in workflow specification.")
            seen_ids.add(sid)
            steps_dict[sid] = s
        raw_steps = steps_dict
    elif isinstance(raw_steps, dict):
        for sid, s in raw_steps.items():
            if not isinstance(s, dict):
                raise WorkflowValidationError(f"Step '{sid}' definition must be a dictionary.")
    else:
        raise WorkflowValidationError("Workflow 'steps' or 'jobs' must be a dictionary or list.")

    # Validate graph structure & cycle detection before mutating jobs_db
    validate_workflow_dag(raw_steps)

    wf_id = f"wf-{secrets.token_hex(6)}"
    now = get_current_iso_timestamp()

    # Create jobs for each step, preserving explicit IDs if unique
    step_to_job_id = {}
    for step_id, spec in raw_steps.items():
        custom_id = spec.get("job_id") or spec.get("id")
        if custom_id and custom_id not in jobs_db.get("jobs", {}) and not any(custom_id == v for v in step_to_job_id.values()):
            step_to_job_id[step_id] = custom_id
        else:
            step_to_job_id[step_id] = f"job-{secrets.token_hex(6)}"

    created_jobs = []
    instantiated_steps = {}
    failure_policy = workflow_spec.get("failure_policy", "fail_fast")

    for step_id, spec in raw_steps.items():
        job_id = step_to_job_id[step_id]
        depends_on_steps = spec.get("depends_on") or spec.get("dependencies") or []
        if isinstance(depends_on_steps, str):
            depends_on_steps = [depends_on_steps] if depends_on_steps.strip() else []

        depends_on_job_ids = [step_to_job_id[d] for d in depends_on_steps if d in step_to_job_id]

        # Initial status: QUEUED if 0 dependencies, else WAITING_FOR_DEPENDENCY
        initial_status = JOB_STATE_QUEUED if not depends_on_steps else JOB_STATE_WAITING_FOR_DEPENDENCY
        waiting_reason = "Waiting for dependencies to complete" if depends_on_steps else None

        target_val = spec.get("target") or spec.get("target_node") or "auto"
        target_str = str(target_val).strip()
        is_cap_target = target_str.lower().startswith(("capability:", "cap:", "role:"))
        if is_cap_target:
            if target_str.lower().startswith(("capability:", "cap:")):
                c_name = target_str.split(":", 1)[1].strip()
                if c_name:
                    rc = spec.setdefault("requirements", {}).setdefault("capabilities", [])
                    if isinstance(rc, list):
                        if c_name not in rc:
                            rc.append(c_name)
                    elif isinstance(rc, str):
                        spec["requirements"]["capabilities"] = [rc, c_name] if rc != c_name else [c_name]
            elif target_str.lower().startswith("role:"):
                r_name = target_str.split(":", 1)[1].strip()
                if r_name:
                    spec.setdefault("requirements", {})["role"] = r_name

        is_dynamic = (target_str.lower() == "auto" or is_cap_target)
        explicit_target_node = None if is_dynamic else (spec.get("target_node") or spec.get("target"))

        scheduler_meta = None
        if is_dynamic:
            scheduler_meta = {
                "mode": "capability-pending" if is_cap_target else "resource-aware-pending",
                "selected_node": None,
                "reason": "Awaiting eligible worker" if not depends_on_steps else "Awaiting dependencies before scheduling"
            }

        job_record = {
            "id": job_id,
            "job_id": job_id,
            "workflow_id": wf_id,
            "workflow_step_id": step_id,
            "name": f"{name} :: {spec.get('name', step_id)}",
            "type": spec.get("type", "echo"),
            "parameters": spec.get("parameters", {}) or {},
            "requirements": spec.get("requirements", {}) or {},
            "target": target_val,
            "target_node": explicit_target_node,
            "assigned_node": None,
            "timeout": int(spec.get("timeout", 60)),
            "max_attempts": int(spec.get("max_attempts", 3)),
            "attempt": 1,
            "lease_generation": 0,
            "priority": spec.get("priority", "normal"),
            "depends_on_steps": depends_on_steps,
            "depends_on_jobs": depends_on_job_ids,
            "dependencies": depends_on_job_ids,
            "waiting_reason": waiting_reason,
            "blocked_reason": None,
            "scheduler": scheduler_meta,
            "status": initial_status,
            "state": initial_status,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "inputs": spec.get("inputs") or spec.get("parameters", {}).get("inputs") or [],
            "outputs": spec.get("outputs") or spec.get("parameters", {}).get("outputs") or spec.get("parameters", {}).get("artifacts") or []
        }

        created_jobs.append(job_record)
        jobs_db.setdefault("jobs", {})[job_id] = job_record

        instantiated_steps[step_id] = {
            "step_id": step_id,
            "name": spec.get("name", step_id),
            "job_id": job_id,
            "type": spec.get("type", "echo"),
            "depends_on": depends_on_steps,
            "dependencies": depends_on_steps,
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
    jobs_db: Dict[str, Any],
    artifacts_db: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Evaluates step dependencies for a workflow and unblocks ready steps.
    Transitions jobs from WAITING_FOR_DEPENDENCY to QUEUED when parents SUCCEED.
    Transitions dependent jobs to FAILED or CANCELLED if any required parent fails.
    Cascades transitions across multiple dependency levels in a single evaluation.
    Respects workflow failure_policy ('fail_fast' vs 'continue_on_failure').
    Resolves artifact outputs from parent jobs and cluster artifacts store.
    Returns True if any job or workflow state was modified.
    """
    if workflow.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_CANCELLED, JOB_STATE_FAILED):
        return False

    changed = False
    all_jobs = jobs_db.get("jobs", {})
    steps = workflow.get("steps", {})

    # Load artifacts store if not passed in
    if artifacts_db is None:
        try:
            from controller.stores.artifacts_store import load_artifacts_db
            artifacts_db = load_artifacts_db()
        except Exception:
            artifacts_db = {"artifacts": {}}

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
                parent_step_ids = step.get("depends_on") or step.get("dependencies") or []
                parent_jobs = [all_jobs.get(steps[p]["job_id"]) for p in parent_step_ids if p in steps]

                # Check if any parent permanently failed or was cancelled
                failed_parent = next((
                    p for p in parent_jobs
                    if p and canonicalize_job_state(p.get("status")) in (
                        JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_CANCELLED, JOB_STATE_REJECTED
                    )
                ), None)

                if failed_parent:
                    parent_st = canonicalize_job_state(failed_parent.get("status"))
                    now_iso = get_current_iso_timestamp()
                    parent_step_label = failed_parent.get("workflow_step_id") or failed_parent.get("job_id")

                    if parent_st == JOB_STATE_CANCELLED:
                        job["status"] = JOB_STATE_CANCELLED
                        job["state"] = JOB_STATE_CANCELLED
                        job["cancel_reason"] = f"Upstream dependency cancelled: step '{parent_step_label}' was cancelled"
                        job["blocked_reason"] = f"Blocked: upstream dependency '{parent_step_label}' was cancelled"
                        step["status"] = JOB_STATE_CANCELLED
                    else:
                        job["status"] = JOB_STATE_FAILED
                        job["state"] = JOB_STATE_FAILED
                        job["retry_reason"] = f"Upstream dependency failed: step '{parent_step_label}' had status {parent_st}"
                        job["blocked_reason"] = f"Blocked: upstream dependency '{parent_step_label}' failed with {parent_st}"
                        step["status"] = JOB_STATE_FAILED

                    job["finished_at"] = now_iso
                    job["waiting_reason"] = None
                    changed = True
                    iteration_changed = True
                    continue

                # Check if all parents succeeded
                all_parents_succeeded = parent_jobs and len(parent_jobs) == len(parent_step_ids) and all(
                    p and canonicalize_job_state(p.get("status")) == JOB_STATE_SUCCEEDED
                    for p in parent_jobs
                )
                if all_parents_succeeded:
                    # Resolve input artifacts from successful parent steps
                    declared_inputs = job.get("inputs") or job.get("parameters", {}).get("inputs") or []
                    resolved_artifacts = []
                    missing_artifact_err = None

                    for inp in declared_inputs:
                        if isinstance(inp, str):
                            if "/" in inp:
                                p_name, a_name = inp.split("/", 1)
                            else:
                                p_name, a_name = None, inp
                            loc_name = a_name
                        elif isinstance(inp, dict):
                            p_name = inp.get("step")
                            a_name = inp.get("artifact") or inp.get("name") or inp.get("filename")
                            loc_name = inp.get("name") or a_name
                        else:
                            continue

                        cand_parent_steps = [p_name] if p_name else parent_step_ids
                        found = None

                        for cand in cand_parent_steps:
                            if cand in steps:
                                p_jid = steps[cand]["job_id"]
                                pj = all_jobs.get(p_jid)
                                # 1. Check parent job record
                                if pj and pj.get("artifacts"):
                                    for art in pj["artifacts"]:
                                        if art.get("name") == a_name or art.get("filename") == a_name or art.get("artifact_id") == a_name:
                                            found = art
                                            break
                                # 2. Check artifacts store if not in pj
                                if not found and artifacts_db:
                                    for art in artifacts_db.get("artifacts", {}).values():
                                        if art.get("job_id") == p_jid and (art.get("name") == a_name or art.get("filename") == a_name or art.get("artifact_id") == a_name):
                                            found = art
                                            break
                            if found:
                                break

                        if not found:
                            missing_artifact_err = f"Required input artifact '{a_name}' from parent step '{p_name or cand_parent_steps}' was not produced."
                            break

                        resolved_artifacts.append({
                            "artifact_id": found["artifact_id"],
                            "name": loc_name,
                            "filename": found.get("filename") or loc_name,
                            "sha256": found.get("sha256"),
                            "size_bytes": found.get("size_bytes"),
                            "source_step": p_name or (cand_parent_steps[0] if cand_parent_steps else None),
                            "source_job_id": found.get("job_id")
                        })

                    if missing_artifact_err:
                        now_iso = get_current_iso_timestamp()
                        job["status"] = JOB_STATE_FAILED
                        job["state"] = JOB_STATE_FAILED
                        job["finished_at"] = now_iso
                        job["retry_reason"] = missing_artifact_err
                        job["blocked_reason"] = missing_artifact_err
                        job["waiting_reason"] = None
                        step["status"] = JOB_STATE_FAILED
                        changed = True
                        iteration_changed = True
                        continue

                    # Unblock job to QUEUED
                    job["input_artifacts"] = resolved_artifacts
                    job["status"] = JOB_STATE_QUEUED
                    job["state"] = JOB_STATE_QUEUED
                    job["waiting_reason"] = None
                    job["blocked_reason"] = None
                    t_mode = str(job.get("target", "auto")).lower()
                    if t_mode == "auto" or t_mode.startswith(("capability:", "cap:", "role:")):
                        job["scheduler"] = {
                            "mode": "capability-pending" if t_mode.startswith(("capability:", "cap:", "role:")) else "resource-aware-pending",
                            "selected_node": None,
                            "reason": "Awaiting eligible worker"
                        }
                    step["status"] = JOB_STATE_QUEUED
                    changed = True
                    iteration_changed = True

        if not iteration_changed:
            break

    total = len(steps)
    completed_count = sum(1 for s in steps.values() if s.get("status") == JOB_STATE_SUCCEEDED)
    failed_count = sum(1 for s in steps.values() if s.get("status") in (JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_REJECTED))
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
                    j["blocked_reason"] = "Cancelled due to sibling step failure (fail_fast policy)"
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

    elif completed_count > 0 or any(s.get("status") in (JOB_STATE_RUNNING, JOB_STATE_ASSIGNED, JOB_STATE_CLAIMED) for s in steps.values()):
        if workflow.get("status") == JOB_STATE_QUEUED:
            workflow["status"] = "RUNNING"
            workflow["started_at"] = workflow.get("started_at") or get_current_iso_timestamp()
            changed = True

    return changed


def progress_workflows(
    workflows_db: Dict[str, Any],
    jobs_db: Dict[str, Any],
    artifacts_db: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Evaluates all active workflows in workflows_db against current jobs in jobs_db.
    Returns True if any workflow or job was modified.
    """
    changed = False
    for wf in workflows_db.get("workflows", {}).values():
        if wf.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED):
            continue
        if evaluate_workflow_progress(wf, jobs_db, artifacts_db=artifacts_db):
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
