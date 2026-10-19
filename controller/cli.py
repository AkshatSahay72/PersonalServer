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
from typing import Any, Optional

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
from controller.lease_sweeper import start_background_lease_sweeper, sweep_expired_leases
from controller.node_manager import get_cluster_summary, sanitize_node_record
from controller.onboarding import (
    DEFAULT_ONBOARDING_TTL_SEC,
    generate_onboarding_code,
    get_or_create_enrollment_token,
)
from controller.stores import load_jobs_db, load_nodes_db, save_jobs_db, save_nodes_db
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
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db, timeout)

    # Start background lease sweeper thread
    start_background_lease_sweeper(timeout)

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

    server = HTTPServer((host, port), handler_class)
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
    max_attempts: int = 3
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

    reqs = {}
    if reqs_json:
        try:
            reqs = json.loads(reqs_json)
        except Exception as e:
            print(f"Error parsing requirements JSON: {e}", file=sys.stderr)
            return 1

    target_node = None
    scheduler_info = None

    if not target_or_auto or target_or_auto.lower() == "auto":
        decision = ResourceScheduler.select_node(reqs, nodes_db)
        if not decision["selected_node"]:
            print(f"Scheduling Error: {decision['reason']}", file=sys.stderr)
            return 1
        target_node = decision["selected_node"]
        scheduler_info = {
            "mode": "resource-aware",
            "selected_node": target_node,
            "score": decision["score"],
            "reason": decision["reason"]
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

    # token command
    subparsers.add_parser("token", help="Display enrollment token file path")

    # onboard-code command
    p_onboard = subparsers.add_parser("onboard-code", help="Generate a short-lived single-use node onboarding code")
    p_onboard.add_argument("--ttl", type=int, default=DEFAULT_ONBOARDING_TTL_SEC, help="Time to live in seconds (default: 900 / 15m)")

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
        sys.exit(submit_job(args.node, args.type, args.name, args.timeout, args.params, args.requirements, getattr(args, "max_attempts", 3)))
    elif args.command == "jobs":
        list_jobs(getattr(args, "node", None), getattr(args, "status", None))
    elif args.command == "job":
        sys.exit(show_job_details(args.job_id))
    elif args.command == "cancel":
        sys.exit(cancel_job(args.job_id))
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


__all__ = [
    "start_server",
    "schedule_test",
    "submit_job",
    "list_jobs",
    "show_job_details",
    "cancel_job",
    "list_nodes",
    "show_node_details",
    "show_cluster",
    "remove_node",
    "main",
]
