#!/usr/bin/env python3
"""
Controller Constants & Lifecycle States
=======================================
Standardized node, job, and application lifecycle states, transition validation,
allowlisted workloads, and networking constraints for PersonalServer controller.
"""

from typing import Dict, Set

# Centralized Node State Constants
STATE_REGISTERING = "REGISTERING"
STATE_ONLINE = "ONLINE"
STATE_DRAINING = "DRAINING"
STATE_DEBOARDING = "DEBOARDING"
STATE_OFFLINE = "OFFLINE"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_UNKNOWN = "UNKNOWN"
STATE_REMOVED = "REMOVED"

VALID_ROLES = ["compute", "storage", "gateway", "controller", "hybrid"]

# Centralized Job States (V1.0 Lifecycle)
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_CLAIMED = "CLAIMED"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMEOUT = "TIMEOUT"
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_RECOVERING = "RECOVERING"
JOB_STATE_REJECTED = "REJECTED"

# Formal Job State Transitions Lifecycle
VALID_JOB_TRANSITIONS: Dict[str, Set[str]] = {
    JOB_STATE_QUEUED: {JOB_STATE_CLAIMED, JOB_STATE_CANCELLED, JOB_STATE_REJECTED},
    JOB_STATE_CLAIMED: {JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED, JOB_STATE_RECOVERING},
    JOB_STATE_RUNNING: {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED, JOB_STATE_RECOVERING},
    JOB_STATE_RECOVERING: {JOB_STATE_CLAIMED, JOB_STATE_FAILED, JOB_STATE_CANCELLED},
    JOB_STATE_SUCCEEDED: set(),   # Terminal
    JOB_STATE_FAILED: set(),      # Terminal
    JOB_STATE_TIMEOUT: set(),     # Terminal if max_attempts reached
    JOB_STATE_CANCELLED: set(),   # Terminal
    JOB_STATE_REJECTED: set(),    # Terminal
}


def validate_job_transition(current_state: str, new_state: str) -> bool:
    """
    Validates whether transitioning from current_state to new_state is allowed by the lifecycle state machine.
    """
    if current_state == new_state:
        return True  # Idempotent re-affirmation
    allowed = VALID_JOB_TRANSITIONS.get(current_state, set())
    return new_state in allowed


# Centralized Application States (Phase 11-13 Lifecycle)
APP_STATE_CREATED = "CREATED"
APP_STATE_FETCHING_SOURCE = "FETCHING_SOURCE"
APP_STATE_CONFIGURING = "CONFIGURING"
APP_STATE_BUILDING = "BUILDING"
APP_STATE_SCHEDULING = "SCHEDULING"
APP_STATE_DEPLOYING = "DEPLOYING"
APP_STATE_RUNNING = "RUNNING"
APP_STATE_STOPPED = "STOPPED"
APP_STATE_FAILED = "FAILED"
APP_STATE_REMOVING = "REMOVING"

# Dynamic port allocation range for containerized/proxied apps
PORT_RANGE_START = 18000
PORT_RANGE_END = 18999

# Allowlisted Workload Types
ALLOWLISTED_WORKLOADS = {
    "system-info": "Gather system hardware and OS metrics",
    "health-check": "Run node health check script",
    "node-status": "Run node status check script",
    "python-script": "Execute safe inline python script",
    "echo": "Echo back test message",
    "failing-test": "Controlled error-handling test workload",
    "timeout-test": "Controlled timeout test workload",
    "docker-deploy": "Pull image and safely launch containerized application",
    "docker-build-deploy": "Build image from source context and launch containerized application",
    "docker-stop": "Stop running containerized application",
    "docker-restart": "Restart containerized application",
    "docker-remove": "Remove containerized application",
    "docker-logs": "Fetch bounded container logs"
}

__all__ = [
    "STATE_REGISTERING",
    "STATE_ONLINE",
    "STATE_DRAINING",
    "STATE_DEBOARDING",
    "STATE_OFFLINE",
    "STATE_UNHEALTHY",
    "STATE_UNKNOWN",
    "STATE_REMOVED",
    "VALID_ROLES",
    "JOB_STATE_QUEUED",
    "JOB_STATE_CLAIMED",
    "JOB_STATE_RUNNING",
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_TIMEOUT",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_RECOVERING",
    "JOB_STATE_REJECTED",
    "VALID_JOB_TRANSITIONS",
    "validate_job_transition",
    "APP_STATE_CREATED",
    "APP_STATE_FETCHING_SOURCE",
    "APP_STATE_CONFIGURING",
    "APP_STATE_BUILDING",
    "APP_STATE_SCHEDULING",
    "APP_STATE_DEPLOYING",
    "APP_STATE_RUNNING",
    "APP_STATE_STOPPED",
    "APP_STATE_FAILED",
    "APP_STATE_REMOVING",
    "PORT_RANGE_START",
    "PORT_RANGE_END",
    "ALLOWLISTED_WORKLOADS",
]
