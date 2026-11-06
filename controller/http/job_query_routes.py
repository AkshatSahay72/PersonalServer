#!/usr/bin/env python3
"""
Controller Job Query Route Handlers
===================================
HTTP request handling for read-only job listing, filtering, search, sorting,
server-side pagination, metrics observability, and individual job inspection.
"""

import math
import time
from typing import Any, Dict, Optional

from controller.constants import (
    JOB_STATE_ASSIGNED,
    JOB_STATE_CLAIMED,
    JOB_STATE_NEEDS_ATTENTION,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RETRY_WAIT,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_FOR_DEPENDENCY,
)
from controller.lease_sweeper import sweep_expired_leases
from controller.stores import (
    load_artifacts_db,
    load_jobs_db,
    load_nodes_db,
    load_workflows_db,
)


def handle_job_query_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles read-only GET requests for job queries, metrics, search, and detail inspection.
    Returns True if handled, False otherwise.
    """
    # 1. GET /jobs/metrics - Distributed scheduler and queue observability
    if path == "/jobs/metrics":
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        workflows_db = load_workflows_db()
        artifacts_db = load_artifacts_db()
        heartbeat_timeout = getattr(handler, "heartbeat_timeout", 60)
        sweep_expired_leases(jobs_db, nodes_db, heartbeat_timeout)

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
            "heartbeat_timeout_sec": heartbeat_timeout,
        })
        return True

    # 2. GET /jobs - List, filter, search, sort, and paginate jobs
    if path == "/jobs":
        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        heartbeat_timeout = getattr(handler, "heartbeat_timeout", 60)
        sweep_expired_leases(jobs_db, nodes_db, heartbeat_timeout)

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
            try:
                limit = max(1, min(100, int(limit_str) if limit_str else 25))
            except (ValueError, TypeError):
                limit = 25

            if offset_str is not None:
                try:
                    offset = max(0, int(offset_str))
                except (ValueError, TypeError):
                    offset = 0
                page = (offset // limit) + 1
            elif page_str is not None:
                try:
                    page = max(1, int(page_str))
                except (ValueError, TypeError):
                    page = 1
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

    # 3. GET /jobs/<job_id> - Single job inspection
    if path.startswith("/jobs/"):
        # Exclude sub-resource paths like /jobs/<job_id>/artifacts
        if path.endswith("/artifacts"):
            return False

        job_id = path[6:].strip()
        if not job_id or "/" in job_id:
            return False

        jobs_db = load_jobs_db()
        nodes_db = load_nodes_db()
        heartbeat_timeout = getattr(handler, "heartbeat_timeout", 60)
        sweep_expired_leases(jobs_db, nodes_db, heartbeat_timeout)

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


__all__ = ["handle_job_query_get"]
