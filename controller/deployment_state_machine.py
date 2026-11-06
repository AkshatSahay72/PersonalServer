#!/usr/bin/env python3
"""
PersonalServer Canonical Deployment State Machine
=================================================
Manages legal state transitions, monotonic history tracking, and durable persistence
for application deployments across the cluster.

Lifecycle:
QUEUED -> FETCHING -> DETECTING -> SCHEDULING -> BUILDING -> PUBLISHING
       -> STARTING -> HEALTH_CHECK -> ACTIVATING -> SUCCEEDED

Terminal / Exceptional States:
FAILED, CANCELLED, RETRYING
"""

import time
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime, timezone

from common.time_utils import get_current_iso_timestamp
from controller.stores import APPS_LOCK, load_apps_db, save_apps_db

# Canonical Deployment States
DEP_STATE_QUEUED = "QUEUED"
DEP_STATE_FETCHING = "FETCHING"
DEP_STATE_DETECTING = "DETECTING"
DEP_STATE_SCHEDULING = "SCHEDULING"
DEP_STATE_BUILDING = "BUILDING"
DEP_STATE_PUBLISHING = "PUBLISHING"
DEP_STATE_STARTING = "STARTING"
DEP_STATE_HEALTH_CHECK = "HEALTH_CHECK"
DEP_STATE_ACTIVATING = "ACTIVATING"
DEP_STATE_SUCCEEDED = "SUCCEEDED"

# Exceptional & Terminal States
DEP_STATE_FAILED = "FAILED"
DEP_STATE_CANCELLED = "CANCELLED"
DEP_STATE_RETRYING = "RETRYING"

ALL_DEPLOYMENT_STATES: Set[str] = {
    DEP_STATE_QUEUED,
    DEP_STATE_FETCHING,
    DEP_STATE_DETECTING,
    DEP_STATE_SCHEDULING,
    DEP_STATE_BUILDING,
    DEP_STATE_PUBLISHING,
    DEP_STATE_STARTING,
    DEP_STATE_HEALTH_CHECK,
    DEP_STATE_ACTIVATING,
    DEP_STATE_SUCCEEDED,
    DEP_STATE_FAILED,
    DEP_STATE_CANCELLED,
    DEP_STATE_RETRYING,
}

TERMINAL_STATES: Set[str] = {
    DEP_STATE_SUCCEEDED,
    DEP_STATE_FAILED,
    DEP_STATE_CANCELLED,
}

# Legal state transition graph
LEGAL_TRANSITIONS: Dict[str, Set[str]] = {
    DEP_STATE_QUEUED: {
        DEP_STATE_FETCHING,
        DEP_STATE_DETECTING,
        DEP_STATE_SCHEDULING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
    },
    DEP_STATE_FETCHING: {
        DEP_STATE_DETECTING,
        DEP_STATE_SCHEDULING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_DETECTING: {
        DEP_STATE_SCHEDULING,
        DEP_STATE_BUILDING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_SCHEDULING: {
        DEP_STATE_BUILDING,
        DEP_STATE_STARTING,      # If pre-built or script runtime
        DEP_STATE_PUBLISHING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_BUILDING: {
        DEP_STATE_PUBLISHING,
        DEP_STATE_STARTING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_PUBLISHING: {
        DEP_STATE_STARTING,
        DEP_STATE_SCHEDULING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_STARTING: {
        DEP_STATE_HEALTH_CHECK,
        DEP_STATE_ACTIVATING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_HEALTH_CHECK: {
        DEP_STATE_ACTIVATING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
        DEP_STATE_RETRYING,
    },
    DEP_STATE_ACTIVATING: {
        DEP_STATE_SUCCEEDED,
        DEP_STATE_FAILED,
    },
    DEP_STATE_RETRYING: {
        DEP_STATE_QUEUED,
        DEP_STATE_FETCHING,
        DEP_STATE_SCHEDULING,
        DEP_STATE_STARTING,
        DEP_STATE_FAILED,
        DEP_STATE_CANCELLED,
    },
    DEP_STATE_SUCCEEDED: set(),  # Terminal
    DEP_STATE_FAILED: set(),     # Terminal
    DEP_STATE_CANCELLED: set(),  # Terminal
}


class DeploymentTransitionError(ValueError):
    """Raised when an illegal or conflicting deployment state transition is attempted."""
    pass


class DeploymentStateMachine:
    """
    Validates and applies state transitions for application deployments.
    Guarantees monotonic history, transition validation, and safe crash recovery.
    """

    @staticmethod
    def is_legal_transition(current_state: str, next_state: str) -> bool:
        """Checks if moving from current_state to next_state is valid."""
        c = str(current_state).upper() if current_state else DEP_STATE_QUEUED
        n = str(next_state).upper() if next_state else ""
        if c == n:
            return True  # Idempotent no-op
        allowed = LEGAL_TRANSITIONS.get(c, set())
        return n in allowed

    @staticmethod
    def transition(
        app_id: str,
        deployment_id: str,
        next_state: str,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Atomically transitions a deployment record to next_state and records history.
        Enforces APPS_LOCK for data consistency.
        """
        norm_next = str(next_state).upper()
        if norm_next not in ALL_DEPLOYMENT_STATES:
            raise DeploymentTransitionError(f"Unknown deployment state: '{next_state}'")

        now_iso = get_current_iso_timestamp()

        with APPS_LOCK:
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id)
            if not app:
                raise DeploymentTransitionError(f"Application '{app_id}' not found.")

            deployments = app.setdefault("deployments", [])
            target_dep = None
            for d in deployments:
                if d.get("deployment_id") == deployment_id:
                    target_dep = d
                    break

            if not target_dep:
                raise DeploymentTransitionError(
                    f"Deployment '{deployment_id}' not found in application '{app_id}'."
                )

            current_state = str(target_dep.get("status", DEP_STATE_QUEUED)).upper()

            # Idempotent no-op
            if current_state == norm_next:
                if error and not target_dep.get("error"):
                    target_dep["error"] = error
                if metadata:
                    for k, v in metadata.items():
                        target_dep[k] = v
                save_apps_db(db, paths=paths)
                return target_dep

            # Validate legal transition
            if not DeploymentStateMachine.is_legal_transition(current_state, norm_next):
                raise DeploymentTransitionError(
                    f"Illegal deployment transition from '{current_state}' to '{norm_next}' "
                    f"for deployment '{deployment_id}'."
                )

            # Record history entry
            history = target_dep.setdefault("history", [])
            history.append({
                "from_state": current_state,
                "to_state": norm_next,
                "timestamp": now_iso,
                "reason": reason or "",
                "error": error
            })

            target_dep["status"] = norm_next
            target_dep["updated_at"] = now_iso

            if error:
                target_dep["error"] = error
            if reason:
                target_dep["status_reason"] = reason

            if norm_next in TERMINAL_STATES:
                target_dep["finished_at"] = now_iso
                start_iso = target_dep.get("started_at")
                if start_iso:
                    try:
                        t0 = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()
                        t1 = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).timestamp()
                        target_dep["duration_ms"] = int((t1 - t0) * 1000)
                    except Exception:
                        pass

                # Automatically release application deployment lock on terminal transition
                dep_lock = app.get("deployment_lock")
                if isinstance(dep_lock, dict) and dep_lock.get("deployment_id") == deployment_id:
                    app["deployment_lock"] = None

            if metadata:
                for k, v in metadata.items():
                    target_dep[k] = v

            save_apps_db(db, paths=paths)
            return target_dep

