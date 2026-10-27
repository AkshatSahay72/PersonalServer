#!/usr/bin/env python3
"""
Controller Command-Line Interface (CLI)
=======================================
Administrative CLI dispatch and command implementations for cluster monitoring,
node management, workload job submission/cancellation, and server lifecycle.
"""

import argparse
import json
import os
import secrets
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Optional, List, Dict


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths, get_controller_runtime_config
from controller.constants import (
    ALLOWLISTED_WORKLOADS,
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_REJECTED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMEOUT,
    STATE_OFFLINE,
    STATE_REMOVED,
)
from controller.ha_helper import get_ha_manager, resolve_controller_node_id
from controller.lease_sweeper import (
    reconcile_startup_recovery,
    start_background_lease_sweeper,
    sweep_expired_leases,
)
from controller.node_manager import get_cluster_summary, sanitize_node_record
from controller.onboarding import (
    DEFAULT_ONBOARDING_TTL_SEC,
    generate_onboarding_code,
    get_or_create_enrollment_token,
)
from controller.stores import (
    load_jobs_db,
    load_nodes_db,
    load_workflows_db,
    save_jobs_db,
    save_nodes_db,
    save_workflows_db,
)
from controller.workflow_engine import (
    WorkflowValidationError,
    cancel_workflow,
    instantiate_workflow,
    progress_workflows,
)
from scheduler.scheduler import ResourceScheduler, compute_node_liveness

PATHS = get_controller_paths()
ENROLLMENT_TOKEN_FILE = PATHS.enrollment_token_file
NODES_FILE = PATHS.nodes_file
JOBS_FILE = PATHS.jobs_file

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HOST = RUNTIME_CFG.host
DEFAULT_PORT = RUNTIME_CFG.port
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout


def start_server(host: Optional[str] = None, port: Optional[int] = None, timeout: Optional[int] = None, role: Optional[str] = None, handler_class: Any = None) -> None:
    """Starts the cluster controller HTTP service."""
    paths = get_controller_paths()
    paths.validate()
    cfg = get_controller_runtime_config()
    host = host if host is not None else cfg.host
    port = port if port is not None else cfg.port
    timeout = timeout if timeout is not None else cfg.heartbeat_timeout

    paths.ensure_directories()
    get_or_create_enrollment_token()

    if handler_class is None:
        from controller.controller import ControllerHandler
        handler_class = ControllerHandler

    handler_class.heartbeat_timeout = timeout

    # Recover orphaned jobs from previous shutdown
    jobs_db = load_jobs_db(paths=paths)
    nodes_db = load_nodes_db(paths=paths)
    recovery_summary = reconcile_startup_recovery(jobs_db, nodes_db, timeout, paths=paths)
    if recovery_summary.get("active_leases_preserved") or recovery_summary.get("expired_leases_swept") or recovery_summary.get("steps_unblocked"):
        print(f"Startup Recovery: {recovery_summary['active_leases_preserved']} active lease(s) preserved, "
              f"{recovery_summary['expired_leases_swept']} expired lease(s) recovered, "
              f"{recovery_summary['steps_unblocked']} workflow step(s) unblocked.")

    # Start background lease sweeper thread
    start_background_lease_sweeper(timeout, paths=paths)

    # Initialize High Availability (HA) Manager
    ha_role = role or os.environ.get("PERSONALSERVER_HA_ROLE", "auto")
    ha_node_id = resolve_controller_node_id(node_id=None, paths=paths)
    ha_node_url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    ha_mgr = get_ha_manager(paths=paths, role=ha_role, node_id=ha_node_id, node_url=ha_node_url, force_reload=True)
    ha_mgr.initialize()
    ha_mgr.start_monitor()

    print("==========================================")
    print(" PersonalServer Cluster Controller & Scheduler (v1.0)")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Heartbeat timeout:     {timeout} seconds")
    print(f"HA Failover Role:      {ha_mgr.state.upper()} (Term {ha_mgr.current_term})")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print(f"Cluster database:      {NODES_FILE}")
    print(f"Jobs database:         {JOBS_FILE}")
    print("==========================================")

    server = ThreadedHTTPServer((host, port), handler_class)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
    finally:
        ha_mgr.stop_monitor()
        server.server_close()


def schedule_test(requirements_json: Optional[str] = None) -> int:
    """Evaluates scheduler node selection against current cluster nodes without submitting a job."""
    nodes_db = load_nodes_db()
    reqs = {}
    if requirements_json:
        try:
            reqs = json.loads(requirements_json)
        except Exception as e:
            print(f"Error parsing requirements JSON: {e}", file=sys.stderr)
            return 1

    print("==========================================")
    print(" PersonalServer Scheduler Evaluation")
    print("==========================================")
    print(f"Requirements: {json.dumps(reqs, indent=2) if reqs else 'None (Default Resource-Aware)'}")
    print()

    decision = ResourceScheduler.select_node(reqs, nodes_db)

    print(f"Selected Node: {decision['selected_node'] or 'NONE'}")
    print(f"Score:         {decision['score']}/100")
    print(f"Reason:        {decision['reason']}")
    print()

    print("Candidates:")
    for c in decision["candidates"]:
        print(f"  * {c['node_id']} ({c['name']}) -> Score: {c['score']}/100 [{c['reason']}]")
    if not decision["candidates"]:
        print("  (None)")
    print()

    print("Rejected Nodes:")
    for nid, r in decision["rejected"].items():
        print(f"  * {nid}: {r}")
    if not decision["rejected"]:
        print("  (None)")
    print("==========================================")
    return 0


def submit_job(
    target_or_auto: Optional[str],
    job_type: str,
    name: Optional[str] = None,
    timeout: int = 60,
    params_json: Optional[str] = None,
    reqs_json: Optional[str] = None,
    max_attempts: int = 3,
    code: Optional[str] = None,
    args: Optional[List[str]] = None,
    inputs: Optional[Any] = None,
    outputs: Optional[Any] = None
) -> int:
    """Submits a workload job to an explicit node or 'auto' for resource-aware scheduling."""
    nodes_db = load_nodes_db()

    if job_type not in ALLOWLISTED_WORKLOADS:
        print(f"Error: Job type '{job_type}' is not allowlisted.", file=sys.stderr)
        print(f"Allowed types: {list(ALLOWLISTED_WORKLOADS.keys())}")
        return 1

    params = {}
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception as e:
            print(f"Error parsing params JSON: {e}", file=sys.stderr)
            return 1
    if not isinstance(params, dict):
        params = {}

    if code and "code" not in params:
        params["code"] = code
    if args and "args" not in params:
        params["args"] = args

    # Parse and normalize outputs
    raw_outputs = []
    if outputs:
        if isinstance(outputs, str):
            try:
                parsed_out = json.loads(outputs)
                raw_outputs = parsed_out if isinstance(parsed_out, list) else [str(parsed_out)]
            except Exception:
                raw_outputs = [o.strip() for o in outputs.split(",") if o.strip()]
        elif isinstance(outputs, (list, tuple)):
            raw_outputs = list(outputs)
    elif "outputs" in params or "expected_outputs" in params:
        raw_outputs = params.get("outputs") or params.get("expected_outputs") or []

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

    # Parse and resolve inputs against artifacts database
    raw_inputs = []
    if inputs:
        if isinstance(inputs, str):
            try:
                parsed_in = json.loads(inputs)
                raw_inputs = parsed_in if isinstance(parsed_in, list) else [parsed_in]
            except Exception:
                raw_inputs = [i.strip() for i in inputs.split(",") if i.strip()]
        elif isinstance(inputs, (list, tuple)):
            raw_inputs = list(inputs)
    elif "inputs" in params:
        raw_inputs = params.get("inputs") or []

    resolved_input_artifacts = []
    if raw_inputs:
        from controller.stores import load_artifacts_db
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
                print(f"Error: Invalid input specification '{item}'.", file=sys.stderr)
                return 1

            if not art_id or art_id not in known_artifacts:
                print(f"Error: Input artifact '{art_id}' not found in cluster database.", file=sys.stderr)
                return 1

            art_entry = known_artifacts[art_id]
            final_dest_name = str(dest_name or art_entry.get("name") or art_entry.get("filename") or art_id).strip()
            resolved_input_artifacts.append({
                "artifact_id": art_id,
                "name": final_dest_name,
                "filename": art_entry.get("filename") or final_dest_name,
                "sha256": art_entry.get("sha256"),
                "size_bytes": art_entry.get("size_bytes", 0),
                "source_job_id": art_entry.get("job_id")
            })

    reqs = {}
    if reqs_json:
        try:
            reqs = json.loads(reqs_json)
        except Exception as e:
            print(f"Error parsing requirements JSON: {e}", file=sys.stderr)
            return 1

    target_node = None
    scheduler_info = None
    waiting_reason = None

    if not target_or_auto or target_or_auto.lower() == "auto":
        decision = ResourceScheduler.select_node(reqs, nodes_db)
        if decision.get("selected_node"):
            target_node = decision["selected_node"]
            scheduler_info = {
                "mode": "resource-aware",
                "selected_node": target_node,
                "score": decision["score"],
                "reason": decision["reason"]
            }
        else:
            target_node = None
            waiting_reason = f"Awaiting eligible worker: {decision['reason']}"
            scheduler_info = {
                "mode": "resource-aware-pending",
                "reason": decision["reason"],
                "rejected": decision.get("rejected", {})
            }
    else:
        target_node = target_or_auto
        node = nodes_db.get("nodes", {}).get(target_node)
        if not node:
            print(f"Error: Node '{target_node}' not found in cluster.", file=sys.stderr)
            return 1
        if node.get("status") == STATE_REMOVED:
            print(f"Error: Node '{target_node}' is removed from cluster.", file=sys.stderr)
            return 1
        live_status = compute_node_liveness(node)
        if live_status == STATE_OFFLINE:
            print(f"Error: Node '{target_node}' is OFFLINE. Cannot submit job.", file=sys.stderr)
            return 1
        if reqs:
            eligible, err_reason = ResourceScheduler.is_node_eligible(node, reqs)
            if not eligible:
                print(f"Error: Node '{target_node}' does not satisfy job requirements: {err_reason}", file=sys.stderr)
                return 1
        scheduler_info = {"mode": "explicit", "target_node": target_node, "reason": "Explicit node"}

    job_id = f"job-{secrets.token_hex(6)}"
    now = get_current_iso_timestamp()

    job_record = {
        "id": job_id,
        "job_id": job_id,
        "name": name or job_type,
        "type": job_type,
        "parameters": params,
        "requirements": reqs,
        "target": target_or_auto or "auto",
        "target_node": target_node,
        "assigned_node": target_node,
        "scheduler": scheduler_info,
        "waiting_reason": waiting_reason,
        "inputs": raw_inputs,
        "input_artifacts": resolved_input_artifacts,
        "outputs": normalized_outputs,
        "expected_outputs": normalized_outputs,
        "timeout": int(timeout),
        "max_attempts": int(max_attempts),
        "attempt": 1,
        "claimed_at": None,
        "lease_expires_at": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "status": JOB_STATE_QUEUED,
        "state": JOB_STATE_QUEUED,
        "retry_reason": None,
        "attempts_history": [],
        "result": None
    }

    jobs_db = load_jobs_db()
    jobs_db.setdefault("jobs", {})[job_id] = job_record
    save_jobs_db(jobs_db)

    print("==========================================")
    print(" PersonalServer Job Submitted")
    print("==========================================")
    print(f"Job ID:          {job_id}")
    print(f"Type:            {job_type}")
    print(f"Selected Target: {target_node}")
    print(f"Schedule Mode:   {scheduler_info['mode']}")
    print(f"Reason:          {scheduler_info['reason']}")
    print(f"Status:          {JOB_STATE_QUEUED}")
    print(f"Max Attempts:    {max_attempts}")
    print(f"Timeout:         {timeout}s")
    print("==========================================")
    return 0


def list_jobs(filter_node: Optional[str] = None, filter_status: Optional[str] = None) -> None:
    """Lists submitted cluster jobs with optional filtering."""
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db)

    jobs = list(jobs_db.get("jobs", {}).values())

    if filter_node:
        jobs = [j for j in jobs if j.get("target_node") == filter_node]
    if filter_status:
        jobs = [j for j in jobs if j.get("status") == filter_status.upper()]

    print("==========================================")
    print(f" PersonalServer Jobs ({len(jobs)})")
    print("==========================================")
    if not jobs:
        print("No jobs found.")
    else:
        for j in reversed(jobs):
            res_summary = ""
            if j.get("result"):
                res_summary = f"| Exit: {j['result'].get('exit_code')} | Duration: {j['result'].get('duration_ms')}ms"
            mode = j.get("scheduler", {}).get("mode", "explicit")
            attempt_str = f"Attempt {j.get('attempt', 1)}/{j.get('max_attempts', 3)}"
            print(f"Job ID:      {j.get('job_id')}")
            print(f"  Type:      {j.get('type')} ({attempt_str})")
            print(f"  Target:    {j.get('target_node')} ({mode})")
            print(f"  Status:    {j.get('status')} {res_summary}")
            if j.get("retry_reason"):
                print(f"  Retry Info:{j.get('retry_reason')}")
            print(f"  Created:   {j.get('created_at')}")
            print()
    print("==========================================")


def show_job_details(job_id: str) -> int:
    """Displays detailed JSON payload for a single job."""
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db)

    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        print(f"Error: Job '{job_id}' not found.", file=sys.stderr)
        return 1

    print("==========================================")
    print(f" Job Details: {job_id}")
    print("==========================================")
    print(json.dumps(job, indent=2))
    print("==========================================")
    return 0


def cancel_job(job_id: str) -> int:
    """Cancels a queued or running job."""
    jobs_db = load_jobs_db()
    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        print(f"Error: Job '{job_id}' not found.", file=sys.stderr)
        return 1

    if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED):
        print(f"Error: Job '{job_id}' is already in finished state '{job.get('status')}'.", file=sys.stderr)
        return 1

    job["status"] = JOB_STATE_CANCELLED
    job["state"] = JOB_STATE_CANCELLED
    job["finished_at"] = get_current_iso_timestamp()
    job["lease_expires_at"] = None
    save_jobs_db(jobs_db)
    print(f"Job '{job_id}' has been marked as CANCELLED.")
    return 0


def delete_job(job_id: str, force: bool = False) -> int:
    """Deletes a job from execution history."""
    jobs_db = load_jobs_db()
    job = jobs_db.get("jobs", {}).get(job_id)
    if not job:
        print(f"Error: Job '{job_id}' not found.", file=sys.stderr)
        return 1

    status = (job.get("status") or job.get("state") or "").upper()
    if status in (JOB_STATE_RUNNING, JOB_STATE_CLAIMED) and not force:
        print(f"Error: Job '{job_id}' is currently in active state '{status}'. Cancel it first or pass force=True.", file=sys.stderr)
        return 1

    del jobs_db["jobs"][job_id]
    save_jobs_db(jobs_db)
    print(f"Job '{job_id}' deleted successfully from history.")
    return 0


def list_nodes(filter_status: Optional[str] = None) -> None:
    """Lists registered cluster nodes."""
    nodes_db = load_nodes_db()
    summary = get_cluster_summary(nodes_db)
    nodes = summary["nodes"]

    if filter_status and filter_status.lower() != "all":
        nodes = [n for n in nodes if n["status"] == filter_status.upper()]
    elif not filter_status or filter_status.lower() != "all":
        nodes = [n for n in nodes if n["status"] != STATE_REMOVED]

    print("==========================================")
    print(f" PersonalServer Cluster Nodes ({len(nodes)})")
    print("==========================================")
    if not nodes:
        print("No matching nodes.")
    else:
        for node in nodes:
            nid = node.get("node_id")
            name = node.get("name")
            role = node.get("role", "compute")
            platform_str = f"{node.get('platform')} ({node.get('architecture')})"
            status = node.get("status")
            last_seen = node.get("last_seen")
            caps = ", ".join([k for k, v in node.get("capabilities", {}).items() if v]) or "none"
            res = node.get("resources", {})
            last_hb = node.get("last_heartbeat", {}).get("system", {})
            mem_info = last_hb.get("memory", f"{res.get('ram_mb', 'N/A')} MB")

            print(f"Node ID:      {nid}")
            print(f"  Name:       {name}")
            print(f"  Role:       {role}")
            print(f"  Status:     {status}")
            print(f"  Platform:   {platform_str}")
            print(f"  Resources:  {res.get('cpu_cores', 'N/A')} cores | RAM: {mem_info}")
            print(f"  Caps:       {caps}")
            print(f"  Last Seen:  {last_seen}")
            print()
    print("==========================================")


def show_node_details(node_id: str) -> int:
    """Displays detailed JSON payload for a single node."""
    nodes_db = load_nodes_db()
    node = nodes_db.get("nodes", {}).get(node_id)
    if not node:
        print(f"Error: Node '{node_id}' not found.", file=sys.stderr)
        return 1

    s_node = sanitize_node_record(node)
    print("==========================================")
    print(f" Node Details: {node_id}")
    print("==========================================")
    print(json.dumps(s_node, indent=2))
    print("==========================================")
    return 0


def show_cluster() -> None:
    """Displays an overview of the cluster and active nodes."""
    nodes_db = load_nodes_db()
    summary = get_cluster_summary(nodes_db)
    print("==========================================")
    print(" PersonalServer Cluster Summary")
    print("==========================================")
    c = summary["cluster"]
    print(f"Cluster:        {c['name']}")
    print(f"Active Nodes:   {c['total_nodes']}")
    print(f"  ONLINE:       {c['online']}")
    print(f"  OFFLINE:      {c['offline']}")
    print(f"  UNHEALTHY:    {c['unhealthy']}")
    print(f"  REMOVED:      {c['removed']}")
    print(f"Timestamp:      {c['timestamp']}")
    print("==========================================")


def remove_node(node_id: str) -> int:
    """Removes a node from active cluster membership."""
    nodes_db = load_nodes_db()
    node = nodes_db.get("nodes", {}).get(node_id)
    if not node:
        print(f"Error: Node '{node_id}' not found.", file=sys.stderr)
        return 1

    node["status"] = STATE_REMOVED
    node["auth_token"] = None
    node["removed_at"] = get_current_iso_timestamp()
    save_nodes_db(nodes_db)

    jobs_db = load_jobs_db()
    sweep_expired_leases(jobs_db, nodes_db)

    print(f"Node '{node_id}' has been removed from active cluster membership.")
    return 0


def list_workflows() -> None:
    """Lists all DAG workflows with overall progress and state."""
    workflows_db = load_workflows_db()
    jobs_db = load_jobs_db()
    if progress_workflows(workflows_db, jobs_db):
        save_workflows_db(workflows_db)
        save_jobs_db(jobs_db)

    wfs = list(workflows_db.get("workflows", {}).values())
    if not wfs:
        print("No workflows found.")
        return

    print("==========================================================================================")
    print(" WORKFLOW ID       NAME                  STATUS      PROGRESS   TOTAL   COMPLETED   CREATED")
    print("==========================================================================================")
    for wf in wfs:
        wid = (wf.get("workflow_id") or wf.get("id", ""))[:16].ljust(16)
        name = (wf.get("name") or "unnamed")[:20].ljust(20)
        status = (wf.get("status") or "UNKNOWN")[:10].ljust(10)
        pct = f"{wf.get('progress_percent', 0.0):.1f}%".rjust(8)
        tot = str(wf.get("total_steps", 0)).rjust(6)
        cmp_cnt = str(wf.get("completed_steps", 0)).rjust(10)
        created = (wf.get("created_at") or "")[:19]
        print(f" {wid}  {name}  {status}  {pct}  {tot}  {cmp_cnt}   {created}")
    print("==========================================================================================")


def show_workflow_details(workflow_id: str) -> int:
    """Displays detailed per-job workflow state and dependency graph."""
    workflows_db = load_workflows_db()
    jobs_db = load_jobs_db()
    if progress_workflows(workflows_db, jobs_db):
        save_workflows_db(workflows_db)
        save_jobs_db(jobs_db)

    wf = workflows_db.get("workflows", {}).get(workflow_id)
    if not wf:
        print(f"Error: Workflow '{workflow_id}' not found.", file=sys.stderr)
        return 1

    print("==========================================")
    print(f" Workflow: {wf.get('name')} ({wf.get('workflow_id')})")
    print("==========================================")
    print(f"Status:          {wf.get('status')}")
    print(f"Progress:        {wf.get('progress_percent', 0.0):.1f}% ({wf.get('completed_steps')}/{wf.get('total_steps')} steps)")
    print(f"Failure Policy:  {wf.get('failure_policy', 'fail_fast')}")
    print(f"Created At:      {wf.get('created_at')}")
    print(f"Started At:      {wf.get('started_at') or 'N/A'}")
    print(f"Finished At:     {wf.get('finished_at') or 'N/A'}")
    if wf.get("cancel_reason"):
        print(f"Cancel Reason:   {wf.get('cancel_reason')}")

    print("\n--- Steps & Child Jobs ---")
    all_jobs = jobs_db.get("jobs", {})
    for step_id, step in wf.get("steps", {}).items():
        jid = step.get("job_id")
        job = all_jobs.get(jid, {})
        deps = step.get("depends_on") or []
        deps_str = ", ".join(deps) if deps else "None (root)"
        assigned = job.get("assigned_node") or job.get("target_node") or "auto"
        print(f"\n  • Step '{step_id}' [{step.get('type')}]:")
        print(f"      Job ID:        {jid}")
        print(f"      Status:        {job.get('status', step.get('status'))}")
        print(f"      Dependencies:  {deps_str}")
        print(f"      Assigned Node: {assigned}")
        if job.get("waiting_reason"):
            print(f"      Waiting:       {job.get('waiting_reason')}")
        if job.get("blocked_reason"):
            print(f"      Blocked:       {job.get('blocked_reason')}")
        if job.get("retry_reason"):
            print(f"      Retry/Error:   {job.get('retry_reason')}")
        if job.get("cancel_reason"):
            print(f"      Cancel Reason: {job.get('cancel_reason')}")
        if job.get("input_artifacts"):
            print(f"      Inputs:        {len(job['input_artifacts'])} resolved artifact(s)")
            for a in job["input_artifacts"]:
                print(f"        - {a.get('name')} (id={a.get('artifact_id')}, sha256={str(a.get('sha256'))[:12]}...)")
        if job.get("artifacts"):
            print(f"      Outputs:       {len(job['artifacts'])} artifact(s)")
            for a in job["artifacts"]:
                print(f"        - {a.get('name') or a.get('filename')} (id={a.get('artifact_id')}, sha256={str(a.get('sha256'))[:12]}...)")
    print("==========================================")
    return 0


def submit_workflow_cmd(spec_arg: str) -> int:
    """Instantiates and submits a DAG workflow from JSON string or file."""
    if os.path.exists(spec_arg):
        try:
            with open(spec_arg, "r", encoding="utf-8") as f:
                spec = json.load(f)
        except Exception as e:
            print(f"Error reading workflow spec file '{spec_arg}': {e}", file=sys.stderr)
            return 1
    else:
        try:
            spec = json.loads(spec_arg)
        except Exception as e:
            print(f"Error parsing workflow JSON: {e}", file=sys.stderr)
            return 1

    jobs_db = load_jobs_db()
    workflows_db = load_workflows_db()
    try:
        wf_rec, created_jobs = instantiate_workflow(spec, jobs_db)
    except WorkflowValidationError as e:
        print(f"Workflow Validation Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Failed to instantiate workflow: {e}", file=sys.stderr)
        return 1

    wf_id = wf_rec["workflow_id"]
    workflows_db.setdefault("workflows", {})[wf_id] = wf_rec
    save_workflows_db(workflows_db)
    save_jobs_db(jobs_db)

    print("==========================================")
    print(f" Workflow Created: {wf_rec['name']}")
    print("==========================================")
    print(f"Workflow ID:  {wf_id}")
    print(f"Total Steps:  {len(created_jobs)}")
    for j in created_jobs:
        print(f"  • {j['workflow_step_id']}: job={j['job_id']} status={j['status']}")
    print("==========================================")
    return 0


def cancel_workflow_cmd(workflow_id: str, reason: str = "Cancelled by operator") -> int:
    """Cancels an active workflow and its non-terminal child jobs."""
    workflows_db = load_workflows_db()
    jobs_db = load_jobs_db()
    cancelled = cancel_workflow(workflow_id, workflows_db, jobs_db, reason=reason)
    if not cancelled:
        print(f"Error: Workflow '{workflow_id}' not found.", file=sys.stderr)
        return 1
    save_workflows_db(workflows_db)
    save_jobs_db(jobs_db)
    print(f"Workflow '{workflow_id}' and all active/pending steps cancelled.")
    return 0


def handle_app_cli(args) -> int:
    """Handles CLI application management actions."""
    from controller.deployment_manager import DeploymentManager
    from controller.stores import load_apps_db
    from pathlib import Path

    action = getattr(args, "app_action", None)
    if not action or action == "list":
        db = load_apps_db()
        apps = db.get("apps", {})
        if not apps:
            print("No applications registered.")
            return 0
        print(f"{'APP ID':<22} {'NAME':<20} {'STATUS':<12} {'PORT':<8} {'PUBLIC PATH'}")
        print("-" * 75)
        for app in apps.values():
            if app.get("status") not in ("DELETED", "deleted"):
                print(f"{app.get('app_id', '-'):<22} {app.get('name', '-'):<20} {app.get('status', '-'):<12} {str(app.get('port', '-')):<8} {app.get('public_path', '-')}")
        return 0

    elif action == "register":
        spec_path = Path(args.manifest)
        if not spec_path.exists():
            print(f"Error: Manifest file not found: {args.manifest}", file=sys.stderr)
            return 1
        with open(spec_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        try:
            rec = DeploymentManager.register_manifest(manifest_data)
            print(f"Application registered successfully: {rec['app_id']} ({rec['name']}) at {rec['public_path']}")
            return 0
        except Exception as e:
            print(f"Error registering application: {e}", file=sys.stderr)
            return 1

    elif action == "deploy":
        app_id = args.app_id
        artifact_path = Path(args.artifact) if getattr(args, "artifact", None) else None
        artifact_sha256 = getattr(args, "sha256", None)
        port = getattr(args, "port", None)
        try:
            res = DeploymentManager.deploy_application(
                app_id=app_id,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
                port_override=port
            )
            print(f"Deployment succeeded: {res['deployment_id']} for '{app_id}' on {res['address']}")
            print(f"Health check: {res.get('health_probe')}")
            return 0
        except Exception as e:
            print(f"Deployment failed: {e}", file=sys.stderr)
            return 1

    elif action == "status":
        app_id = args.app_id
        try:
            st = DeploymentManager.get_application_status(app_id)
            print(f"App ID:         {st['app_id']}")
            print(f"Name:           {st['name']}")
            print(f"Status:         {st['status']}")
            print(f"Active Release: {st.get('active_deployment_id') or '-'}")
            print(f"Address:        {st.get('address') or '-'}")
            print(f"PID:            {st.get('pid') or '-'}")
            print(f"Deployments:    {st.get('deployments_count', 0)}")
            return 0
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    elif action == "stop":
        app_id = args.app_id
        try:
            res = DeploymentManager.stop_application(app_id)
            print(f"Stopped: {res.get('message')}")
            return 0
        except Exception as e:
            print(f"Error stopping application: {e}", file=sys.stderr)
            return 1

    elif action == "restart":
        app_id = args.app_id
        try:
            res = DeploymentManager.restart_application(app_id)
            print(f"Restarted: {res.get('deployment_id')} on {res.get('address')}")
            return 0
        except Exception as e:
            print(f"Error restarting application: {e}", file=sys.stderr)
            return 1

    elif action == "rollback":
        app_id = args.app_id
        target_dep = getattr(args, "deployment_id", None)
        try:
            res = DeploymentManager.rollback_application(app_id, target_dep)
            print(f"Rolled back to release {res.get('restored_deployment_id')} on port {res.get('port')}")
            print(f"Health probe: {res.get('health_probe')}")
            return 0
        except Exception as e:
            print(f"Rollback failed: {e}", file=sys.stderr)
            return 1

    return 1


def main(args_list: Optional[list] = None) -> None:
    """Entry point for controller command-line invocation."""
    parser = argparse.ArgumentParser(
        prog="controller",
        description="PersonalServer Cluster Controller & Resource Scheduler (v1.0)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Controller commands")

    # start command
    p_start = subparsers.add_parser("start", help="Start the cluster controller HTTP service")
    p_start.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    p_start.add_argument("--timeout", type=int, default=DEFAULT_HEARTBEAT_TIMEOUT, help=f"Heartbeat timeout in seconds (default: {DEFAULT_HEARTBEAT_TIMEOUT})")
    p_start.add_argument("--role", choices=["auto", "leader", "standby"], default=None, help="HA failover role (default: auto)")

    # nodes command
    p_nodes = subparsers.add_parser("nodes", help="List cluster nodes")
    p_nodes.add_argument("--status", choices=["online", "offline", "unhealthy", "removed", "all"], help="Filter by node status")

    # node command
    p_node = subparsers.add_parser("node", help="Inspect single node details")
    p_node.add_argument("node_id", help="ID of node to inspect")

    # cluster command
    subparsers.add_parser("cluster", help="Display cluster overview")

    # remove command
    p_remove = subparsers.add_parser("remove", help="Remove a node from active cluster")
    p_remove.add_argument("node_id", help="ID of node to remove")

    # submit command
    p_submit = subparsers.add_parser("submit", help="Submit a workload job to an explicit node or 'auto'")
    p_submit.add_argument("--node", default="auto", help="Target Node ID or 'auto' for scheduler (default: auto)")
    p_submit.add_argument("--type", required=True, choices=list(ALLOWLISTED_WORKLOADS.keys()), help="Allowlisted workload type")
    p_submit.add_argument("--name", help="Optional friendly name for the job")
    p_submit.add_argument("--timeout", type=int, default=60, help="Timeout in seconds (default 60)")
    p_submit.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts (default 3)")
    p_submit.add_argument("--params", help="JSON string of parameters")
    p_submit.add_argument("--requirements", help="JSON string of scheduler requirements")
    p_submit.add_argument("--code", help="Inline Python code string for python-script workload")
    p_submit.add_argument("--args", nargs="*", help="Positional argument strings passed to script")
    p_submit.add_argument("--inputs", help="JSON string or comma-separated list of input artifact IDs")
    p_submit.add_argument("--outputs", help="JSON string or comma-separated list of declared output filenames")

    # schedule-test command
    p_sched = subparsers.add_parser("schedule-test", help="Test scheduler evaluation against current cluster")
    p_sched.add_argument("--requirements", help="JSON string of scheduler requirements")

    # jobs command
    p_jobs = subparsers.add_parser("jobs", help="List jobs")
    p_jobs.add_argument("--node", help="Filter jobs by target node ID")
    p_jobs.add_argument("--status", choices=[JOB_STATE_QUEUED, JOB_STATE_CLAIMED, JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED, JOB_STATE_RECOVERING, JOB_STATE_REJECTED], help="Filter jobs by status")

    # job command
    p_job = subparsers.add_parser("job", help="Inspect single job details")
    p_job.add_argument("job_id", help="Job ID to inspect")

    # cancel command
    p_cancel = subparsers.add_parser("cancel", help="Cancel a queued or running job")
    p_cancel.add_argument("job_id", help="Job ID to cancel")

    # delete-job command
    p_del_job = subparsers.add_parser("delete-job", help="Delete a job from execution history")
    p_del_job.add_argument("job_id", help="Job ID to delete")
    p_del_job.add_argument("--force", action="store_true", help="Force deletion even if active")

    # token command
    subparsers.add_parser("token", help="Display enrollment token file path")

    # onboard-code command
    p_onboard = subparsers.add_parser("onboard-code", help="Generate a short-lived single-use node onboarding code")
    p_onboard.add_argument("--ttl", type=int, default=DEFAULT_ONBOARDING_TTL_SEC, help="Time to live in seconds (default: 900 / 15m)")

    # workflows command
    subparsers.add_parser("workflows", help="List all DAG workflows")

    # workflow command
    p_wf = subparsers.add_parser("workflow", help="Inspect single workflow details and child jobs")
    p_wf.add_argument("workflow_id", help="Workflow ID to inspect")

    # submit-workflow command
    p_sub_wf = subparsers.add_parser("submit-workflow", help="Submit a DAG workflow from JSON string or file path")
    p_sub_wf.add_argument("spec", help="Path to workflow JSON file or JSON string")

    # cancel-workflow command
    p_canc_wf = subparsers.add_parser("cancel-workflow", help="Cancel an active DAG workflow")
    p_canc_wf.add_argument("workflow_id", help="Workflow ID to cancel")
    p_canc_wf.add_argument("--reason", default="Cancelled by operator", help="Cancellation reason")

    # app command group
    p_app = subparsers.add_parser("app", help="Manage application deployments and lifecycle")
    app_sub = p_app.add_subparsers(dest="app_action", help="Application action")
    
    # app list
    app_sub.add_parser("list", help="List registered applications")

    # app register
    p_app_reg = app_sub.add_parser("register", help="Register an application manifest")
    p_app_reg.add_argument("manifest", help="Path to manifest JSON file")

    # app deploy
    p_app_dep = app_sub.add_parser("deploy", help="Deploy an application")
    p_app_dep.add_argument("app_id", help="Application ID to deploy")
    p_app_dep.add_argument("--artifact", help="Optional path to artifact file (e.g. zip bundle)")
    p_app_dep.add_argument("--sha256", help="Optional artifact SHA-256 checksum for verification")
    p_app_dep.add_argument("--port", type=int, help="Optional port override")

    # app status
    p_app_st = app_sub.add_parser("status", help="Inspect application runtime status and health")
    p_app_st.add_argument("app_id", help="Application ID")

    # app stop
    p_app_stop = app_sub.add_parser("stop", help="Safely stop an active application")
    p_app_stop.add_argument("app_id", help="Application ID")

    # app restart
    p_app_rst = app_sub.add_parser("restart", help="Safely restart an active application")
    p_app_rst.add_argument("app_id", help="Application ID")

    # app rollback
    p_app_rb = app_sub.add_parser("rollback", help="Roll back application to a previous successful release")
    p_app_rb.add_argument("app_id", help="Application ID")
    p_app_rb.add_argument("--deployment-id", help="Optional specific prior deployment ID to restore")

    args = parser.parse_args(args_list)

    if not args.command or args.command == "start":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        timeout = getattr(args, "timeout", DEFAULT_HEARTBEAT_TIMEOUT)
        role = getattr(args, "role", None)
        start_server(host, port, timeout, role)
    elif args.command == "nodes":
        list_nodes(getattr(args, "status", None))
    elif args.command == "node":
        sys.exit(show_node_details(args.node_id))
    elif args.command == "cluster":
        show_cluster()
    elif args.command == "remove":
        sys.exit(remove_node(args.node_id))
    elif args.command == "schedule-test":
        sys.exit(schedule_test(args.requirements))
    elif args.command == "submit":
        sys.exit(submit_job(
            args.node,
            args.type,
            args.name,
            args.timeout,
            args.params,
            args.requirements,
            getattr(args, "max_attempts", 3),
            getattr(args, "code", None),
            getattr(args, "args", None),
            getattr(args, "inputs", None),
            getattr(args, "outputs", None)
        ))
    elif args.command == "jobs":
        list_jobs(getattr(args, "node", None), getattr(args, "status", None))
    elif args.command == "job":
        sys.exit(show_job_details(args.job_id))
    elif args.command == "cancel":
        sys.exit(cancel_job(args.job_id))
    elif args.command == "delete-job":
        sys.exit(delete_job(args.job_id, getattr(args, "force", False)))
    elif args.command == "token":
        get_or_create_enrollment_token()
        print(f"Enrollment token stored at: {ENROLLMENT_TOKEN_FILE}")
    elif args.command == "onboard-code":
        ttl = getattr(args, "ttl", DEFAULT_ONBOARDING_TTL_SEC)
        code, ttl_sec = generate_onboarding_code(ttl)
        minutes = ttl_sec // 60
        print("==========================================")
        print(f"Onboarding code: {code}")
        print(f"Expires in:      {minutes} minutes")
        print("==========================================")
    elif args.command == "workflows":
        list_workflows()
    elif args.command == "workflow":
        sys.exit(show_workflow_details(args.workflow_id))
    elif args.command == "submit-workflow":
        sys.exit(submit_workflow_cmd(args.spec))
    elif args.command == "cancel-workflow":
        sys.exit(cancel_workflow_cmd(args.workflow_id, getattr(args, "reason", "Cancelled by operator")))
    elif args.command == "app":
        from controller.cli import handle_app_cli
        sys.exit(handle_app_cli(args))


__all__ = [
    "start_server",
    "schedule_test",
    "submit_job",
    "list_jobs",
    "show_job_details",
    "cancel_job",
    "delete_job",
    "list_nodes",
    "show_node_details",
    "show_cluster",
    "remove_node",
    "handle_app_cli",
    "main",
]

if __name__ == "__main__":
    main()

