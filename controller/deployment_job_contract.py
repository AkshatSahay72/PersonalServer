#!/usr/bin/env python3
"""
PersonalServer Canonical Deployment Job Contract
================================================
Defines the standard, versioned contract for deployment jobs dispatched across
the cluster, scheduled by ResourceScheduler, and executed by Node Agents.

Features:
- Immutable identifiers (job_id, deployment_id, app_id)
- Zero-leakage portable source and artifact references (no controller-local absolute paths)
- Capability and architecture requirements for node selection
- Health check specification
- Idempotency key tracking
"""

import secrets
from typing import Dict, Any, List, Optional
from common.time_utils import get_current_iso_timestamp

DEPLOYMENT_JOB_TYPE = "app-deploy"
CANONICAL_JOB_SCHEMA_VERSION = "1.0"


def generate_deployment_id() -> str:
    """Generates an immutable deployment ID e.g. dep-a1b2c3d4."""
    return f"dep-{secrets.token_hex(4)}"


def generate_deployment_job_id() -> str:
    """Generates an immutable scheduler job ID for a deployment e.g. job-dep-a1b2c3d4."""
    return f"job-dep-{secrets.token_hex(4)}"


def build_canonical_deployment_job(
    app_id: str,
    deployment_id: str,
    job_id: Optional[str] = None,
    project_type: str = "python",
    source: Optional[Dict[str, Any]] = None,
    source_revision: Optional[str] = None,
    artifact_reference: Optional[str] = None,
    artifact_sha256: Optional[str] = None,
    artifact_id: Optional[str] = None,
    input_artifacts: Optional[List[Dict[str, Any]]] = None,
    required_capabilities: Optional[List[str]] = None,
    target_architecture: Optional[str] = None,
    resource_requirements: Optional[Dict[str, Any]] = None,
    runtime_requirements: Optional[Dict[str, Any]] = None,
    health_check: Optional[Dict[str, Any]] = None,
    requested_by: str = "manual",
    idempotency_key: Optional[str] = None,
    assigned_node: Optional[str] = None,
    target_node: Optional[str] = None,
    fencing_token: Optional[int] = None,
    timeout_sec: int = 120,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """
    Constructs a validated, canonical deployment job record adhering to the D2 contract.
    Ensures that no local filesystem paths leak across node boundaries.
    """
    jid = job_id or generate_deployment_job_id()
    created_iso = get_current_iso_timestamp()

    # Determine default capabilities based on project type
    caps = list(required_capabilities or [])
    if not caps:
        if project_type in ("python", "python3"):
            caps = ["python3"]
        elif project_type in ("node", "nodejs", "javascript", "js"):
            caps = ["nodejs"]
        elif project_type in ("static", "static-web", "html", "web"):
            caps = ["compute"]
        elif project_type in ("docker", "container", "containerd", "dockerfile"):
            caps = ["container_runtime:docker"]

    # Normalize resource requirements
    res_reqs = dict(resource_requirements or {})
    res_reqs.setdefault("min_memory_mb", 128)
    res_reqs.setdefault("min_cpu_cores", 1)

    # Standard requirements block evaluated by ResourceScheduler
    requirements: Dict[str, Any] = {
        "capabilities": caps,
        "min_memory_mb": res_reqs["min_memory_mb"],
        "min_cpu_cores": res_reqs["min_cpu_cores"],
    }
    if target_architecture and target_architecture != "any":
        requirements["architecture"] = target_architecture

    # Normalized health check block
    hc = dict(health_check or {})
    hc.setdefault("path", "/health")
    hc.setdefault("timeout_seconds", 30)
    hc.setdefault("interval_seconds", 0.5)
    hc.setdefault("expected_status", 200)

    # Runtime parameters needed by Node Agent's JobExecutor
    rt_reqs = dict(runtime_requirements or {})

    # Portable artifact reference (strictly relative name or artifact ID, never absolute local path)
    clean_art_ref = None
    if artifact_reference:
        import os
        clean_art_ref = os.path.basename(str(artifact_reference).strip())

    parameters: Dict[str, Any] = {
        "app_id": app_id,
        "deployment_id": deployment_id,
        "project_type": project_type,
        "source": source or {"type": "manual"},
        "source_revision": source_revision or "latest",
        "artifact_reference": clean_art_ref,
        "artifact_sha256": artifact_sha256,
        "artifact_id": artifact_id,
        "runtime_requirements": rt_reqs,
        "health_check": hc,
        "port": rt_reqs.get("port") or 8090,
        "env": rt_reqs.get("env") or {},
        "start_command": rt_reqs.get("start_command"),
        "fencing_token": fencing_token,
    }

    # Normalized input artifacts for cross-node download
    resolved_inputs = list(input_artifacts or [])
    if not resolved_inputs and artifact_id:
        resolved_inputs.append({
            "artifact_id": artifact_id,
            "name": clean_art_ref or f"{artifact_id}.zip",
            "sha256": artifact_sha256
        })

    job_record: Dict[str, Any] = {
        "job_id": jid,
        "deployment_id": deployment_id,
        "app_id": app_id,
        "type": DEPLOYMENT_JOB_TYPE,
        "name": f"deploy-{app_id}",
        "schema_version": CANONICAL_JOB_SCHEMA_VERSION,
        "status": "QUEUED",
        "target_node": target_node or "auto",
        "assigned_node": assigned_node,
        "requirements": requirements,
        "parameters": parameters,
        "input_artifacts": resolved_inputs,
        "created_at": created_iso,
        "requested_by": requested_by,
        "idempotency_key": idempotency_key or f"{app_id}:{deployment_id}",
        "timeout": timeout_sec,
        "attempt": 1,
        "max_attempts": max_attempts,
        "fencing_token": fencing_token,
        "lease_token": None,
        "lease_expires_at": None,
    }

    return job_record

