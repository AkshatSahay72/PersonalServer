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

# Centralized Job States (V2.0 Extended Lifecycle)
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
JOB_STATE_ASSIGNED = "ASSIGNED"
JOB_STATE_CLAIMED = "CLAIMED"              # Compatible alias for ASSIGNED
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_RETRY_WAIT = "RETRY_WAIT"
JOB_STATE_RECOVERING = "RECOVERING"        # Compatible alias for RETRY_WAIT
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMED_OUT = "TIMED_OUT"
JOB_STATE_TIMEOUT = "TIMEOUT"              # Compatible alias for TIMED_OUT
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_NEEDS_ATTENTION = "NEEDS_ATTENTION"
JOB_STATE_REJECTED = "REJECTED"

# State Aliases for transparent compatibility
JOB_STATE_ALIASES: Dict[str, str] = {
    JOB_STATE_CLAIMED: JOB_STATE_ASSIGNED,
    JOB_STATE_RECOVERING: JOB_STATE_RETRY_WAIT,
    JOB_STATE_TIMEOUT: JOB_STATE_TIMED_OUT,
}

def canonicalize_job_state(state: str) -> str:
    """Returns canonical state identifier for aliases."""
    if not state:
        return ""
    st = state.strip().upper()
    return JOB_STATE_ALIASES.get(st, st)

# Formal Job State Transitions Lifecycle (defined on canonical states)
CANONICAL_JOB_TRANSITIONS: Dict[str, Set[str]] = {
    JOB_STATE_QUEUED: {JOB_STATE_ASSIGNED, JOB_STATE_WAITING_FOR_DEPENDENCY, JOB_STATE_CANCELLED, JOB_STATE_REJECTED, JOB_STATE_NEEDS_ATTENTION},
    JOB_STATE_WAITING_FOR_DEPENDENCY: {JOB_STATE_QUEUED, JOB_STATE_FAILED, JOB_STATE_CANCELLED, JOB_STATE_NEEDS_ATTENTION},
    JOB_STATE_ASSIGNED: {JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_CANCELLED, JOB_STATE_RETRY_WAIT, JOB_STATE_NEEDS_ATTENTION, JOB_STATE_QUEUED},
    JOB_STATE_RUNNING: {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMED_OUT, JOB_STATE_CANCELLED, JOB_STATE_RETRY_WAIT, JOB_STATE_NEEDS_ATTENTION},
    JOB_STATE_RETRY_WAIT: {JOB_STATE_QUEUED, JOB_STATE_ASSIGNED, JOB_STATE_FAILED, JOB_STATE_CANCELLED, JOB_STATE_NEEDS_ATTENTION},
    JOB_STATE_NEEDS_ATTENTION: {JOB_STATE_QUEUED, JOB_STATE_ASSIGNED, JOB_STATE_FAILED, JOB_STATE_CANCELLED},
    JOB_STATE_SUCCEEDED: set(),   # Terminal
    JOB_STATE_FAILED: set(),      # Terminal
    JOB_STATE_TIMED_OUT: set(),   # Terminal if max_attempts reached
    JOB_STATE_CANCELLED: set(),   # Terminal
    JOB_STATE_REJECTED: set(),    # Terminal
}

# Compatibility dictionary: includes both canonical and legacy alias keys/values
VALID_JOB_TRANSITIONS: Dict[str, Set[str]] = {}
for src, dsts in CANONICAL_JOB_TRANSITIONS.items():
    expanded_dsts = set(dsts)
    if JOB_STATE_ASSIGNED in dsts:
        expanded_dsts.add(JOB_STATE_CLAIMED)
    if JOB_STATE_RETRY_WAIT in dsts:
        expanded_dsts.add(JOB_STATE_RECOVERING)
    if JOB_STATE_TIMED_OUT in dsts:
        expanded_dsts.add(JOB_STATE_TIMEOUT)

    VALID_JOB_TRANSITIONS[src] = expanded_dsts
    # Populate legacy aliases as sources too
    if src == JOB_STATE_ASSIGNED:
        VALID_JOB_TRANSITIONS[JOB_STATE_CLAIMED] = expanded_dsts
    elif src == JOB_STATE_RETRY_WAIT:
        VALID_JOB_TRANSITIONS[JOB_STATE_RECOVERING] = expanded_dsts
    elif src == JOB_STATE_TIMED_OUT:
        VALID_JOB_TRANSITIONS[JOB_STATE_TIMEOUT] = expanded_dsts


def validate_job_transition(current_state: str, new_state: str) -> bool:
    """
    Validates whether transitioning from current_state to new_state is allowed by the lifecycle state machine.
    Transparently normalizes aliases (e.g. CLAIMED <-> ASSIGNED, TIMEOUT <-> TIMED_OUT).
    """
    curr_c = canonicalize_job_state(current_state)
    new_c = canonicalize_job_state(new_state)
    if curr_c == new_c:
        return True  # Idempotent re-affirmation
    allowed = CANONICAL_JOB_TRANSITIONS.get(curr_c, set())
    return new_c in allowed


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
    "ping-check": "Run safe ICMP or network ping probe",
    "http-check": "Run safe HTTP/HTTPS endpoint probe",
    "dns-check": "Run DNS resolution and connectivity diagnostic",
    "python-script": "Execute safe inline python script or function",
    "shell-script": "Execute approved structured shell script on compatible hosts",
    "echo": "Echo back test message",
    "failing-test": "Controlled error-handling test workload",
    "timeout-test": "Controlled timeout test workload",
    "image-build": "Build, tag, and compute immutable metadata for container image from source",
    "image-tag": "Tag image with versioned tag and compute immutable digest",
    "image-inspect": "Inspect local/registry image metadata and architecture compatibility",
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
    "JOB_STATE_WAITING_FOR_DEPENDENCY",
    "JOB_STATE_ASSIGNED",
    "JOB_STATE_CLAIMED",
    "JOB_STATE_RUNNING",
    "JOB_STATE_RETRY_WAIT",
    "JOB_STATE_RECOVERING",
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_TIMED_OUT",
    "JOB_STATE_TIMEOUT",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_NEEDS_ATTENTION",
    "JOB_STATE_REJECTED",
    "JOB_STATE_ALIASES",
    "canonicalize_job_state",
    "VALID_JOB_TRANSITIONS",
    "CANONICAL_JOB_TRANSITIONS",
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
