#!/usr/bin/env python3
"""
PersonalServer Application Deployment Manager
=============================================
Manages end-to-end reliable deployment of self-hosted Python web services and multi-component applications:
1. Manifest validation (schema, route conflicts, zero-secret check)
2. Artifact checksum and integrity verification
3. Resource-aware capability scheduling (matching python3 / web_service capabilities)
4. Port conflict detection and working directory isolation
5. Process execution via SafeProcessLifecycleManager
6. Strict health check probing before advertising success
7. Graceful rollback to prior release when health check or startup fails
"""

import os
import sys
import json
import time
import shutil
import zipfile
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.time_utils import get_current_iso_timestamp
from common.artifact_manager import compute_file_sha256
from common.app_manifest import (
    ApplicationManifest,
    ManifestRegistry,
    check_for_secrets,
    ManifestValidationError,
    RouteConflictError,
    sanitize_and_normalize_path
)
from common.process_manager import (
    SafeProcessLifecycleManager,
    ProcessIdentityRecord,
    ProcessSecurityError
)
from config.platform_config import get_controller_paths
from controller.stores import (
    load_apps_db,
    save_apps_db,
    load_nodes_db,
    load_jobs_db,
    save_jobs_db,
    load_artifacts_db,
    save_artifacts_db,
)
from scheduler.scheduler import ResourceScheduler
from controller.deployment_state_machine import (
    DeploymentStateMachine,
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
)
from controller.artifact_packager import package_source_to_artifact
from controller.deployment_job_contract import (
    build_canonical_deployment_job,
    generate_deployment_id,
    generate_deployment_job_id,
    DEPLOYMENT_JOB_TYPE,
)
from agent.job_executor import JobExecutor, JOB_STATE_SUCCEEDED

STORAGE_APPS_DIR = BASE_DIR / "storage" / "applications"
STORAGE_ARTIFACTS_DIR = BASE_DIR / "storage" / "artifacts"
RUNTIME_APPS_DIR = BASE_DIR / "runtime" / "apps"


class DeploymentError(RuntimeError):
    """Raised when application deployment fails."""
    pass


class DeploymentConcurrencyError(DeploymentError):
    """Raised when concurrent deployment requests conflict for an application."""
    pass


class FencingError(DeploymentError):
    """Raised when a stale worker or outdated deployment generation attempts state activation."""
    pass


class DeploymentManager:
    """Orchestrates reliable application registration, deployment, rollback, and lifecycle."""

    @staticmethod
    def acquire_deployment_lock(
        app_id: str,
        deployment_id: str,
        owner: str = "deployment",
        ttl_seconds: int = 180,
        paths: Optional[Any] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Acquires a persistent, recoverable application-level deployment lock.
        Prevents concurrent deployments targeting the same application from interfering
        or activating conflicting releases. Automatically breaks expired locks.
        """
        from controller.stores import APPS_LOCK
        now_dt = datetime.now(timezone.utc)
        now_iso = get_current_iso_timestamp()

        with APPS_LOCK:
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id)
            if not app:
                return False, f"Application '{app_id}' not found."

            lock = app.get("deployment_lock")
            if isinstance(lock, dict):
                cur_dep = lock.get("deployment_id")
                if cur_dep == deployment_id:
                    # Idempotent re-acquisition by same deployment
                    return True, None

                exp_str = lock.get("expires_at")
                if exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                        if now_dt <= exp_dt:
                            return False, (
                                f"Concurrent deployment conflict: Application '{app_id}' is currently "
                                f"being deployed by deployment '{cur_dep}' (lock expires at {exp_str})."
                            )
                    except Exception:
                        pass

            # Acquire lock
            expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
            app["deployment_lock"] = {
                "deployment_id": deployment_id,
                "owner": owner,
                "locked_at": now_iso,
                "expires_at": expires_at
            }
            save_apps_db(db, paths=paths)
            return True, None

    @staticmethod
    def release_deployment_lock(
        app_id: str,
        deployment_id: str,
        paths: Optional[Any] = None
    ) -> bool:
        """
        Releases the application-level deployment lock if held by this deployment.
        """
        from controller.stores import APPS_LOCK
        with APPS_LOCK:
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id)
            if not app:
                return False

            lock = app.get("deployment_lock")
            if isinstance(lock, dict) and lock.get("deployment_id") == deployment_id:
                app["deployment_lock"] = None
                save_apps_db(db, paths=paths)
                return True
            return False

    @staticmethod
    def reconcile_in_flight_deployments(paths: Optional[Any] = None) -> Dict[str, Any]:
        """
        Hardened controller recovery:
        Reconciles in-flight application deployments following a controller restart or worker disconnection:
        1. Identifies deployments stuck in non-terminal states.
        2. Inspects corresponding canonical jobs in jobs_db and worker node liveness.
        3. If worker finished job successfully, probes health:
           - If healthy: promotes release atomically to SUCCEEDED.
           - If unhealthy: transitions to FAILED and preserves prior healthy release.
        4. If worker is dead or lease expired: marks deployment FAILED and preserves prior healthy release.
        5. If job is still validly executing on live worker: preserves active in-flight lease without duplicate dispatch.
        6. Sweeps expired application deployment locks.
        """
        from controller.deployment_state_machine import TERMINAL_STATES, DEP_STATE_SUCCEEDED, DEP_STATE_FAILED
        from scheduler.scheduler import compute_node_liveness, STATE_OFFLINE, STATE_REMOVED
        from controller.stores import APPS_LOCK

        now_dt = datetime.now(timezone.utc)
        now_iso = get_current_iso_timestamp()

        with APPS_LOCK:
            db = load_apps_db(paths=paths)
            jobs_db = load_jobs_db(paths=paths)
            nodes_db = load_nodes_db(paths=paths)

            reconciled = {
                "recovered_success": 0,
                "recovered_failed": 0,
                "in_flight_preserved": 0,
                "stale_locks_cleared": 0
            }

            for app_id, app in db.get("apps", {}).items():
                # 1. Sweep expired deployment locks
                dep_lock = app.get("deployment_lock")
                if isinstance(dep_lock, dict):
                    exp_str = dep_lock.get("expires_at")
                    if exp_str:
                        try:
                            exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                            if now_dt > exp_dt:
                                app["deployment_lock"] = None
                                reconciled["stale_locks_cleared"] += 1
                        except Exception:
                            app["deployment_lock"] = None
                            reconciled["stale_locks_cleared"] += 1

                deployments = app.get("deployments", [])
                for dep in deployments:
                    st = str(dep.get("status", "")).upper()
                    lst = str(dep.get("lifecycle_state", "")).upper()
                    if st in TERMINAL_STATES or st in ("SUCCESS", "SUCCEEDED", "FAILED", "CANCELLED") or lst in TERMINAL_STATES:
                        continue

                    dep_id = dep.get("deployment_id")
                    job_id = dep.get("job_id")
                    prior_active = app.get("active_deployment_id")

                    job = jobs_db.get("jobs", {}).get(job_id) if job_id else None
                    assigned_node = dep.get("node") or (job.get("assigned_node") if job else None)
                    node_entry = nodes_db.get("nodes", {}).get(assigned_node, {}) if assigned_node else {}
                    node_live = compute_node_liveness(node_entry) if node_entry else STATE_OFFLINE

                    if job and job.get("status") == "SUCCEEDED":
                        port = dep.get("port") or (job.get("declared_outputs", {}) or {}).get("port")
                        is_static = str(app.get("runtime", "")).lower() in ("static", "static-web", "html", "web")
                        healthy = False
                        if is_static:
                            healthy = True
                        elif port and assigned_node:
                            target_ip = node_entry.get("ip") or "127.0.0.1"
                            hc_path = (app.get("health_check") or {}).get("path", "/health")
                            clean_hc = hc_path if hc_path.startswith("/") else f"/{hc_path}"
                            try:
                                req = urllib.request.Request(
                                    f"http://{target_ip}:{port}{clean_hc}",
                                    headers={"User-Agent": "PersonalServer-Recovery/1.0"}
                                )
                                with urllib.request.urlopen(req, timeout=2.0) as resp:
                                    if 200 <= resp.status < 400:
                                        healthy = True
                            except Exception:
                                healthy = False

                        if healthy:
                            # Advance state machine through legal stages to SUCCEEDED
                            cur_state = dep.get("lifecycle_state") or dep.get("status")
                            if cur_state == "PUBLISHING":
                                DeploymentStateMachine.transition(app_id, dep_id, "STARTING", reason="Advancing recovered candidate to starting", paths=paths)
                                cur_state = "STARTING"
                            if cur_state == "STARTING":
                                DeploymentStateMachine.transition(app_id, dep_id, "HEALTH_CHECK", reason="Candidate health verified on worker", paths=paths)
                                cur_state = "HEALTH_CHECK"
                            if cur_state == "HEALTH_CHECK":
                                DeploymentStateMachine.transition(app_id, dep_id, "ACTIVATING", reason="Promoting candidate to active release", paths=paths)
                                cur_state = "ACTIVATING"
                            if cur_state == "ACTIVATING":
                                DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SUCCEEDED, reason="Recovered candidate verified healthy", paths=paths)
                            app["status"] = "RUNNING"
                            app["active_deployment_id"] = dep_id
                            app["selected_node"] = assigned_node
                            app["port"] = port
                            dep["status"] = "SUCCESS"
                            dep["finished_at"] = now_iso
                            reconciled["recovered_success"] += 1
                        else:
                            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error="Candidate health could not be established during controller recovery", paths=paths)
                            dep["status"] = "FAILED"
                            dep["finished_at"] = now_iso
                            if prior_active:
                                app["status"] = "RUNNING"
                                app["active_deployment_id"] = prior_active
                            else:
                                app["status"] = "FAILED"
                            reconciled["recovered_failed"] += 1

                    elif job and job.get("status") in ("ASSIGNED", "RUNNING"):
                        l_exp = job.get("lease_expires_at")
                        lease_valid = (l_exp is not None and time.time() <= float(l_exp))
                        if lease_valid and node_live not in (STATE_OFFLINE, STATE_REMOVED):
                            reconciled["in_flight_preserved"] += 1
                        else:
                            fail_reason = f"Worker node '{assigned_node}' lease expired or worker disconnected"
                            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=fail_reason, paths=paths)
                            dep["status"] = "FAILED"
                            dep["error"] = fail_reason
                            dep["finished_at"] = now_iso
                            if prior_active:
                                app["status"] = "RUNNING"
                                app["active_deployment_id"] = prior_active
                            else:
                                app["status"] = "FAILED"
                            reconciled["recovered_failed"] += 1

                    else:
                        fail_reason = "Deployment interrupted or orphaned before job dispatch"
                        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=fail_reason, paths=paths)
                        dep["status"] = "FAILED"
                        dep["error"] = fail_reason
                        dep["finished_at"] = now_iso
                        if prior_active:
                            app["status"] = "RUNNING"
                            app["active_deployment_id"] = prior_active
                        else:
                            app["status"] = "FAILED"
                        reconciled["recovered_failed"] += 1

                    # Clear deployment lock if held by this deployment
                    if isinstance(app.get("deployment_lock"), dict) and app["deployment_lock"].get("deployment_id") == dep_id:
                        app["deployment_lock"] = None

            save_apps_db(db, paths=paths)
            return reconciled

    @staticmethod
    def register_manifest(
        manifest_data: Dict[str, Any],
        apps_db: Optional[Dict[str, Any]] = None,
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Validates and registers an application manifest into the apps database.
        Strictly enforces:
        - Zero secret exposure
        - Schema compliance
        - Route conflict check against active applications
        """
        # 1. Zero-secret verification
        check_for_secrets(manifest_data)

        # 2. Parse and validate manifest schema
        app_id = manifest_data.get("app_id") or f"app-{manifest_data.get('name', 'unnamed').lower().replace(' ', '-')}"
        name = manifest_data.get("name") or app_id
        public_path = manifest_data.get("public_path") or f"/{name}"
        clean_path = sanitize_and_normalize_path(public_path)

        # 3. Conflict detection against registered active applications
        db = apps_db if apps_db is not None else load_apps_db()
        for existing_id, existing_app in db.get("apps", {}).items():
            if existing_id != app_id and existing_app.get("status") not in ("DELETED", "deleted"):
                exist_path = existing_app.get("public_path") or f"/{existing_app.get('name', '')}"
                try:
                    norm_exist = sanitize_and_normalize_path(exist_path)
                    if norm_exist.lower() == clean_path.lower():
                        raise RouteConflictError(
                            f"Public path '{clean_path}' conflicts with existing application '{existing_id}'."
                        )
                except ManifestValidationError:
                    pass

        # 4. Create or update registered app record
        existing_rec = db.get("apps", {}).get(app_id, {})
        deployments = existing_rec.get("deployments", [])

        app_record = {
            "app_id": app_id,
            "name": name,
            "display_name": manifest_data.get("display_name", name),
            "public_path": clean_path,
            "status": existing_rec.get("status", "CREATED"),
            "runtime": manifest_data.get("runtime", "python"),
            "port": int(manifest_data.get("port") or manifest_data.get("container_port") or manifest_data.get("host_port") or (8088 if manifest_data.get("runtime") in ("static", "static-web", "html", "web") else 8090)),
            "host_port": int(manifest_data.get("port") or manifest_data.get("container_port") or manifest_data.get("host_port") or (8088 if manifest_data.get("runtime") in ("static", "static-web", "html", "web") else 8090)),
            "static_root": manifest_data.get("static_root") or manifest_data.get("root_dir") or existing_rec.get("static_root", ""),
            "spa_fallback": bool(manifest_data.get("spa_fallback", existing_rec.get("spa_fallback", False))),
            "source": manifest_data.get("source", existing_rec.get("source", {"type": "manual"})),
            "webhook": manifest_data.get("webhook", existing_rec.get("webhook", {})),
            "components": manifest_data.get("components", {}),
            "health_check": manifest_data.get("health_check", {
                "path": "/health",
                "timeout_seconds": 5,
                "interval_seconds": 0.5,
                "expected_status": 200
            }),
            "release": manifest_data.get("release", {}),
            "requirements": manifest_data.get("requirements", existing_rec.get("requirements", {})),
            "deployments": deployments,
            "active_deployment_id": existing_rec.get("active_deployment_id"),
            "env_vars": existing_rec.get("env_vars", {}),
            "env": existing_rec.get("env", {}),
            "created_at": existing_rec.get("created_at", get_current_iso_timestamp()),
            "updated_at": get_current_iso_timestamp()
        }

        db.setdefault("apps", {})[app_id] = app_record
        save_apps_db(db, paths=paths)
        return app_record

    @staticmethod
    def verify_artifact_checksum(artifact_path: Path, expected_sha256: str) -> None:
        """Verifies that an artifact file matches its declared SHA-256 digest."""
        if not artifact_path.exists() or not artifact_path.is_file():
            raise DeploymentError(f"Artifact file not found at: {artifact_path}")

        computed_sha256, _ = compute_file_sha256(artifact_path)
        if computed_sha256.lower() != expected_sha256.lower():
            raise DeploymentError(
                f"Artifact checksum verification failed for {artifact_path.name}. "
                f"Expected: {expected_sha256}, Computed: {computed_sha256}"
            )

    @staticmethod
    def select_deployment_node(requirements: Dict[str, Any], heartbeat_timeout: int = 15) -> str:
        """Schedules deployment to an eligible, online cluster node."""
        nodes_db = load_nodes_db()
        decision = ResourceScheduler.select_node(requirements, nodes_db, heartbeat_timeout)
        selected_node = decision.get("selected_node")
        if not selected_node:
            rejection_reasons = "; ".join(decision.get("rejected", {}).values())
            raise DeploymentError(
                f"No eligible worker node available for requirements: {rejection_reasons or decision.get('reason')}"
            )
        return selected_node

    @staticmethod
    def read_bounded_log_tail(log_path: Path, max_lines: int = 100, max_bytes: int = 64 * 1024) -> str:
        """
        Reads a bounded, sanitized tail of an application log file.
        Limits memory consumption to max_bytes and bounds output to max_lines.
        """
        if not log_path or not log_path.exists() or not log_path.is_file():
            return ""
        try:
            file_size = log_path.stat().st_size
            if file_size == 0:
                return ""
            read_bytes = min(file_size, max_bytes)
            with open(log_path, "rb") as f:
                if file_size > max_bytes:
                    f.seek(file_size - max_bytes)
                raw = f.read(read_bytes)
            decoded = raw.decode("utf-8", errors="replace")
            lines = decoded.splitlines()
            if file_size > max_bytes and len(lines) > 1:
                # Discard possible leading partial line
                lines = lines[1:]
            tail = "\n".join(lines[-max_lines:]).strip()
            from config.structured_logger import redact_sensitive_text
            return redact_sensitive_text(tail)
        except Exception as e:
            return f"[Error reading log tail: {e}]"

    @staticmethod
    def probe_http_health(
        port: int,
        health_path: str = "/health",
        timeout_sec: float = 5.0,
        interval_sec: float = 0.5,
        expected_status: int = 200,
        host: str = "127.0.0.1",
        pid: Optional[int] = None,
        log_path: Optional[Path] = None
    ) -> Tuple[bool, str]:
        """
        Polls the HTTP health endpoint until HTTP 200 or timeout.
        If a PID is provided, continuously checks that the child process is still alive.
        If the process exits before becoming healthy, aborts polling immediately.
        Returns (True, 'Healthy') or (False, error_details_with_log_tail).
        """
        clean_path = health_path if health_path.startswith("/") else f"/{health_path}"
        target_url = f"http://{host}:{port}{clean_path}"

        t0 = time.time()
        last_error = ""

        while time.time() - t0 < timeout_sec:
            # Check if child process terminated prematurely
            if pid is not None and pid > 0:
                if not SafeProcessLifecycleManager.is_pid_active(pid):
                    last_error = f"Process (PID {pid}) exited unexpectedly before becoming healthy"
                    break

            try:
                req = urllib.request.Request(
                    target_url,
                    headers={"User-Agent": "PersonalServer-DeploymentHealthCheck/1.0"}
                )
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == expected_status or (expected_status == 200 and 200 <= resp.status < 400):
                        return True, f"Health check passed (HTTP {resp.status}) at {target_url}"
                    last_error = f"HTTP {resp.status}"
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
            except Exception as e:
                last_error = str(e)

            time.sleep(interval_sec)

        elapsed = round(time.time() - t0, 2)
        diag = f"Health check failed on {target_url} after {elapsed}s: {last_error}"

        if log_path:
            tail = DeploymentManager.read_bounded_log_tail(log_path, max_lines=20, max_bytes=16 * 1024)
            if tail:
                diag += f"\n--- Process Log Tail (last 20 lines) ---\n{tail}"

        return False, diag

    @staticmethod
    def submit_deployment(
        app_id: str,
        source: Optional[Dict[str, Any]] = None,
        commit: Optional[str] = None,
        branch: Optional[str] = None,
        trigger: str = "manual",
        node_override: Optional[str] = None,
        port_override: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        env_overrides: Optional[Dict[str, str]] = None,
        artifact_path: Optional[Path] = None,
        artifact_sha256: Optional[str] = None,
        start_cmd_override: Optional[List[str]] = None,
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Canonical deployment submission interface for Dashboard, CLI, REST API, and GitHub Webhooks.
        Creates a versioned deployment record, initializes the deployment state machine,
        evaluates node eligibility via ResourceScheduler, and dispatches a durable job into jobs_db.
        """
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found in registry.")

        # 1. Idempotency Check
        if idempotency_key:
            for d in app.get("deployments", []):
                if d.get("idempotency_key") == idempotency_key:
                    if d.get("status") not in (DEP_STATE_FAILED, DEP_STATE_CANCELLED):
                        return {
                            "deployment_id": d.get("deployment_id"),
                            "job_id": d.get("job_id"),
                            "app_id": app_id,
                            "status": d.get("status"),
                            "assigned_node": d.get("node"),
                            "idempotent": True
                        }

        # 2. Acquire Application Deployment Lock (Concurrency Protection)
        dep_id = generate_deployment_id()
        lock_ok, lock_err = DeploymentManager.acquire_deployment_lock(
            app_id=app_id,
            deployment_id=dep_id,
            owner=trigger,
            ttl_seconds=180,
            paths=paths
        )
        if not lock_ok:
            raise DeploymentConcurrencyError(lock_err)

        # 3. Artifact Checksum Verification if provided
        if artifact_path and artifact_sha256:
            try:
                DeploymentManager.verify_artifact_checksum(artifact_path, artifact_sha256)
            except Exception:
                DeploymentManager.release_deployment_lock(app_id, dep_id, paths=paths)
                raise

        port = port_override or int(app.get("port") or app.get("host_port") or 8090)

        # 4. Create Job Identifier and Monotonic Deployment Generation
        job_id = generate_deployment_job_id()
        now_iso = get_current_iso_timestamp()
        generation = int(app.get("deployment_generation", 0)) + 1
        app["deployment_generation"] = generation
        dep_number = len(app.get("deployments", [])) + 1
        effective_commit = commit or "latest"
        effective_branch = branch or "main"
        eff_idempotency_key = idempotency_key or f"{app_id}:{dep_id}"

        # 4. Initialize Deployment Record in QUEUED state
        dep_record = {
            "deployment_id": dep_id,
            "job_id": job_id,
            "number": dep_number,
            "generation": generation,
            "commit": effective_commit,
            "branch": effective_branch,
            "trigger": trigger,
            "status": DEP_STATE_QUEUED,
            "error": None,
            "node": node_override,
            "port": port,
            "host_port": port,
            "artifact_sha256": artifact_sha256,
            "artifact_reference": artifact_path.name if artifact_path else None,
            "started_at": now_iso,
            "updated_at": now_iso,
            "idempotency_key": eff_idempotency_key,
            "history": [{
                "from_state": "NONE",
                "to_state": DEP_STATE_QUEUED,
                "timestamp": now_iso,
                "reason": f"Deployment submitted via {trigger}"
            }]
        }
        app.setdefault("deployments", []).append(dep_record)
        app["status"] = "DEPLOYING"
        app["updated_at"] = now_iso
        save_apps_db(db, paths=paths)

        # 5. Fetch / Detect States
        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FETCHING, reason="Resolving source", paths=paths)
        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_DETECTING, reason="Detecting runtime requirements", paths=paths)

        # 6. Scheduling State
        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SCHEDULING, reason="Evaluating cluster node placement", paths=paths)

        runtime = (app.get("runtime") or "").strip().lower()
        if not runtime or runtime in ("auto", "detect"):
            # Auto-detect from source
            src_check = STORAGE_APPS_DIR / app_id / "source"
            if (src_check / "package.json").exists():
                runtime = "nodejs"
            elif (src_check / "Dockerfile").exists() or (src_check / "dockerfile").exists():
                runtime = "docker"
            elif (src_check / "requirements.txt").exists() or (src_check / "app.py").exists():
                runtime = "python"
            elif any((src_check / c / "index.html").exists() for c in ("dist", "build", "public")) or (src_check / "index.html").exists():
                runtime = "static"
            else:
                runtime = "python"

        if runtime in ("nodejs", "node", "javascript", "js"):
            req_caps = ["nodejs"]
        elif runtime in ("python", "python3", "self-hosted"):
            req_caps = ["python3"]
        elif runtime in ("static", "static-web", "html", "web"):
            req_caps = ["compute"]
        elif runtime in ("docker", "container", "containerd", "dockerfile"):
            req_caps = ["container_runtime:docker"]
        else:
            req_caps = ["compute"]

        # Preserve and union explicitly declared manifest capabilities
        app_req_caps = (app.get("requirements") or {}).get("capabilities")
        if app_req_caps and isinstance(app_req_caps, list):
            req_caps = list(dict.fromkeys(req_caps + app_req_caps))

        nodes_db = load_nodes_db(paths=paths)
        jobs_db = load_jobs_db(paths=paths)

        if node_override:
            if node_override in nodes_db.get("nodes", {}):
                selected_node = node_override
            else:
                rejection = f"Selected node '{node_override}' is not registered in the cluster."
                DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=rejection, paths=paths)
                raise DeploymentError(rejection)
        else:
            req_res = app.get("requirements") or app.get("resource_requirements") or {}
            reqs = {
                "capabilities": req_caps
            }
            if "min_memory_mb" in req_res:
                reqs["min_memory_mb"] = req_res["min_memory_mb"]
            if "min_cpu_cores" in req_res:
                reqs["min_cpu_cores"] = req_res["min_cpu_cores"]
            if "architecture" in req_res:
                reqs["architecture"] = req_res["architecture"]

            decision = ResourceScheduler.select_node(reqs, nodes_db, cluster_jobs=jobs_db.get("jobs", {}))
            selected_node = decision.get("selected_node")
            if not selected_node:
                rejection = f"No eligible worker node available for requirements: {decision.get('reason') or list(decision.get('rejected', {}).values())}"
                DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=rejection, paths=paths)
                raise DeploymentError(rejection)

        # 7. Building & Packaging State
        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_BUILDING, reason="Packaging release artifact", paths=paths)

        eff_art_id = None
        eff_art_name = None
        eff_art_sha = artifact_sha256

        # Package or register artifact
        try:
            if artifact_path and Path(artifact_path).is_file():
                art_p = Path(artifact_path)
                with open(art_p, "rb") as af:
                    content_bytes = af.read()
                from common.artifact_manager import generate_artifact_id, store_artifact_bytes
                import hashlib
                sha256 = hashlib.sha256(content_bytes).hexdigest()
                eff_art_id = generate_artifact_id()
                eff_art_name = art_p.name
                art_storage = getattr(paths, "artifacts_dir", STORAGE_ARTIFACTS_DIR) if paths else STORAGE_ARTIFACTS_DIR
                art_db = load_artifacts_db(paths=paths)

                # Check canonical deduplication by SHA-256
                canonical_art = None
                canonical_id = None
                for exist_id, exist_entry in art_db.get("artifacts", {}).items():
                    if exist_entry.get("sha256", "").lower() == sha256.lower():
                        canonical_art = exist_entry
                        canonical_id = exist_entry.get("canonical_artifact_id") or exist_id
                        break

                if canonical_art:
                    art_rec = {
                        "artifact_id": eff_art_id,
                        "canonical_artifact_id": canonical_id,
                        "is_alias": True,
                        "filename": eff_art_name,
                        "sha256": sha256,
                        "size_bytes": len(content_bytes),
                        "path": canonical_art.get("path"),
                        "content_type": "application/zip",
                        "created_at": get_current_iso_timestamp(),
                        "authorized_nodes": list(dict.fromkeys((canonical_art.get("authorized_nodes") or []) + [selected_node])),
                        "metadata": {
                            "app_id": app_id,
                            "deployment_id": dep_id,
                            "commit_sha": effective_commit,
                            "alias_of": canonical_id,
                            "deduplicated": True
                        }
                    }
                    canonical_art.setdefault("aliases", [])
                    if eff_art_id not in canonical_art["aliases"]:
                        canonical_art["aliases"].append(eff_art_id)
                    canonical_art.setdefault("authorized_nodes", [])
                    if selected_node not in canonical_art["authorized_nodes"]:
                        canonical_art["authorized_nodes"].append(selected_node)
                else:
                    art_rec = store_artifact_bytes(
                        storage_root=art_storage,
                        artifact_id=eff_art_id,
                        filename=eff_art_name,
                        content_bytes=content_bytes,
                        content_type="application/zip",
                        metadata={
                            "app_id": app_id,
                            "deployment_id": dep_id,
                            "commit_sha": effective_commit,
                        }
                    )
                    art_rec["canonical_artifact_id"] = eff_art_id
                    art_rec["is_alias"] = False
                    art_rec["aliases"] = []
                    art_rec["authorized_nodes"] = [selected_node]

                eff_art_sha = art_rec["sha256"]
                art_db.setdefault("artifacts", {})[eff_art_id] = art_rec
                save_artifacts_db(art_db, paths=paths)

            elif artifact_path and Path(artifact_path).is_dir():
                art_rec = package_source_to_artifact(
                    source_dir=Path(artifact_path),
                    app_id=app_id,
                    deployment_id=dep_id,
                    commit_sha=effective_commit,
                    paths=paths,
                    authorized_nodes=[selected_node]
                )
                eff_art_id = art_rec["artifact_id"]
                eff_art_name = art_rec["filename"]
                eff_art_sha = art_rec["sha256"]

            else:
                # Check application source directory
                src_dir = STORAGE_APPS_DIR / app_id / "source"
                if src_dir.exists() and src_dir.is_dir():
                    art_rec = package_source_to_artifact(
                        source_dir=src_dir,
                        app_id=app_id,
                        deployment_id=dep_id,
                        commit_sha=effective_commit,
                        paths=paths,
                        authorized_nodes=[selected_node]
                    )
                    eff_art_id = art_rec["artifact_id"]
                    eff_art_name = art_rec["filename"]
                    eff_art_sha = art_rec["sha256"]
        except Exception as pkg_err:
            fail_msg = f"Artifact packaging failed: {pkg_err}"
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=fail_msg, paths=paths)
            raise DeploymentError(fail_msg)

        # 8. Publishing State
        DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_PUBLISHING, reason="Publishing artifact to registry", paths=paths)

        # 9. Build and persist canonical deployment job in jobs_db
        combined_env = dict(app.get("env", {}))
        if env_overrides:
            combined_env.update(env_overrides)

        canonical_job = build_canonical_deployment_job(
            app_id=app_id,
            deployment_id=dep_id,
            job_id=job_id,
            project_type=runtime,
            source=source or app.get("source"),
            source_revision=effective_commit,
            artifact_reference=eff_art_name or (str(artifact_path.name) if artifact_path else None),
            artifact_sha256=eff_art_sha,
            artifact_id=eff_art_id,
            required_capabilities=req_caps,
            runtime_requirements={
                "port": port,
                "env": combined_env,
                "start_command": start_cmd_override,
                "static_root": app.get("static_root") or "",
                "spa_fallback": bool(app.get("spa_fallback", False))
            },
            health_check=app.get("health_check"),
            requested_by=trigger,
            idempotency_key=eff_idempotency_key,
            assigned_node=selected_node,
            target_node=selected_node,
            fencing_token=generation
        )

        jobs_db = load_jobs_db(paths=paths)
        jobs_db.setdefault("jobs", {})[job_id] = canonical_job
        save_jobs_db(jobs_db, paths=paths)

        # Update deployment record in apps_db with artifact metadata
        db = load_apps_db(paths=paths)
        if app_id in db.get("apps", {}):
            for d in db["apps"][app_id].get("deployments", []):
                if d.get("deployment_id") == dep_id:
                    d["artifact_id"] = eff_art_id
                    d["artifact_reference"] = eff_art_name
                    d["artifact_sha256"] = eff_art_sha
                    d["node"] = selected_node
                    break
            save_apps_db(db, paths=paths)

        # 10. Transition deployment to STARTING
        DeploymentStateMachine.transition(
            app_id=app_id,
            deployment_id=dep_id,
            next_state=DEP_STATE_STARTING,
            reason=f"Scheduled onto worker node {selected_node}",
            metadata={"node": selected_node, "job_id": job_id, "artifact_id": eff_art_id},
            paths=paths
        )

        return {
            "deployment_id": dep_id,
            "job_id": job_id,
            "app_id": app_id,
            "status": DEP_STATE_STARTING,
            "assigned_node": selected_node,
            "node": selected_node,
            "port": port,
            "commit": effective_commit,
            "branch": effective_branch,
            "trigger": trigger,
            "artifact_sha256": artifact_sha256
        }

    @staticmethod
    def deploy_application(
        app_id: str,
        artifact_path: Optional[Path] = None,
        artifact_sha256: Optional[str] = None,
        port_override: Optional[int] = None,
        start_cmd_override: Optional[List[str]] = None,
        auto_rollback_on_failure: bool = True,
        node_override: Optional[str] = None,
        commit: Optional[str] = None,
        branch: Optional[str] = None,
        trigger: str = "manual",
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Executes a controlled deployment of an application:
        - Submits deployment through canonical scheduler pipeline
        - Executes via Node Agent JobExecutor contract
        - Performs health check verification
        - Transitions state machine to SUCCEEDED or FAILED
        - Executes automatic rollback on failure if requested
        """
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found in registry.")

        prior_dep_id = app.get("active_deployment_id")

        # 1. Canonical submission through ResourceScheduler
        sub_res = DeploymentManager.submit_deployment(
            app_id=app_id,
            source=app.get("source"),
            commit=commit,
            branch=branch,
            trigger=trigger,
            node_override=node_override,
            port_override=port_override,
            artifact_path=artifact_path,
            artifact_sha256=artifact_sha256,
            start_cmd_override=start_cmd_override,
            paths=paths
        )

        dep_id = sub_res["deployment_id"]
        job_id = sub_res["job_id"]
        selected_node = sub_res["assigned_node"]
        port = sub_res["port"]

        # 2. Execute workload via Node Agent JobExecutor contract
        jobs_db = load_jobs_db(paths=paths)
        canonical_job = jobs_db.get("jobs", {}).get(job_id)
        if not canonical_job:
            raise DeploymentError(f"Canonical job '{job_id}' missing from jobs store.")

        # Ensure artifact is available in app sandbox if artifact_path given
        if artifact_path and artifact_path.exists():
            art_dir = STORAGE_APPS_DIR / app_id / "artifacts"
            art_dir.mkdir(parents=True, exist_ok=True)
            target_art = art_dir / artifact_path.name
            if target_art != artifact_path:
                shutil.copy2(artifact_path, target_art)

        exec_res = JobExecutor.execute(canonical_job)

        now_iso = get_current_iso_timestamp()

        # 3. Process Execution Result
        if exec_res.get("status") == JOB_STATE_SUCCEEDED:
            out_meta = exec_res.get("declared_outputs") or exec_res.get("outputs") or {}
            actual_port = out_meta.get("port") or port
            pid = out_meta.get("pid")

            # Fencing Token Check: Ensure candidate generation is not superseded by a newer deployment
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id, {})
            current_gen = int(app.get("deployment_generation", 1))
            cand_dep = next((d for d in app.get("deployments", []) if d.get("deployment_id") == dep_id), {})
            cand_gen = int(cand_dep.get("generation", current_gen))
            if cand_gen < current_gen:
                err_fencing = f"Stale worker activation rejected: candidate generation {cand_gen} is superseded by current generation {current_gen}."
                DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=err_fencing, reason="Fencing token mismatch", paths=paths)
                raise FencingError(err_fencing)

            # State Machine: HEALTH_CHECK -> ACTIVATING -> SUCCEEDED
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_HEALTH_CHECK, reason="Health probe verified", metadata={"port": actual_port, "pid": pid}, paths=paths)
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_ACTIVATING, reason="Promoting release", metadata={"port": actual_port, "pid": pid}, paths=paths)
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_SUCCEEDED, reason="Release active and healthy", metadata={"port": actual_port, "pid": pid, "address": f"http://127.0.0.1:{actual_port}"}, paths=paths)

            # Update application registry with rollback safety
            try:
                db = load_apps_db(paths=paths)
                app = db.get("apps", {}).get(app_id, {})
                app["status"] = "RUNNING"
                app["selected_node"] = selected_node
                app["assigned_node"] = selected_node
                app["port"] = actual_port
                app["host_port"] = actual_port
                app["active_deployment_id"] = dep_id
                app["failure_reason"] = None
                if out_meta.get("static_root") is not None:
                    app["static_root"] = out_meta.get("static_root")
                if out_meta.get("spa_fallback") is not None:
                    app["spa_fallback"] = out_meta.get("spa_fallback")
                app["updated_at"] = now_iso

                # Update deployment record inside deployments list
                for d in app.get("deployments", []):
                    if d.get("deployment_id") == dep_id:
                        d["status"] = "SUCCESS"
                        d["port"] = actual_port
                        d["pid"] = pid
                        d["address"] = f"http://127.0.0.1:{actual_port}"
                        d["release_dir"] = str(STORAGE_APPS_DIR / app_id / "releases" / dep_id)
                        d["finished_at"] = now_iso
                        break
                save_apps_db(db, paths=paths)
            except Exception as act_err:
                db = load_apps_db(paths=paths)
                app = db.get("apps", {}).get(app_id, {})
                if prior_dep_id:
                    app["active_deployment_id"] = prior_dep_id
                    app["status"] = "RUNNING"
                else:
                    app["status"] = "FAILED"
                save_apps_db(db, paths=paths)
                raise DeploymentError(f"Release activation failed: {act_err}")

            return {
                "status": "SUCCESS",
                "app_id": app_id,
                "deployment_id": dep_id,
                "job_id": job_id,
                "node": selected_node,
                "port": actual_port,
                "pid": pid,
                "commit": commit or "latest",
                "branch": branch or "main",
                "trigger": trigger,
                "artifact_sha256": artifact_sha256,
                "health_probe": "Health check passed",
                "address": f"http://127.0.0.1:{actual_port}"
            }
        else:
            err_msg = exec_res.get("stderr") or "Application health check or execution failed"
            DeploymentStateMachine.transition(app_id, dep_id, DEP_STATE_FAILED, error=err_msg, reason="Execution failed", paths=paths)

            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id, {})

            # Update deployment record
            for d in app.get("deployments", []):
                if d.get("deployment_id") == dep_id:
                    d["status"] = "FAILED"
                    d["error"] = err_msg
                    d["finished_at"] = now_iso
                    break

            if auto_rollback_on_failure and prior_dep_id:
                try:
                    rollback_res = DeploymentManager.rollback_application(app_id, target_deployment_id=prior_dep_id)
                    app["status"] = "RUNNING"
                    app["active_deployment_id"] = prior_dep_id
                    app["failure_reason"] = f"Deployment {dep_id} failed. Automatically rolled back to {prior_dep_id}: {err_msg}"
                    save_apps_db(db, paths=paths)
                    return {
                        "status": "ROLLED_BACK",
                        "error": err_msg,
                        "active_deployment_id": prior_dep_id,
                        "rollback_details": rollback_res
                    }
                except Exception as rb_err:
                    app["status"] = "FAILED"
                    app["failure_reason"] = f"Deployment {dep_id} failed ({err_msg}) and rollback failed ({rb_err})."
                    save_apps_db(db, paths=paths)
                    raise DeploymentError(app["failure_reason"])
            else:
                app["status"] = "FAILED"
                app["failure_reason"] = err_msg
                save_apps_db(db, paths=paths)
                raise DeploymentError(f"Deployment rejected: {err_msg}")

    @staticmethod
    def rollback_application(app_id: str, target_deployment_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Restores a previously successful release:
        - If prior release exists: safely switches and restarts release
        - If NO prior release exists: reports actionable error without altering state
        """
        db = load_apps_db()
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found.")

        deployments = app.get("deployments", [])
        target_dep = None

        if target_deployment_id:
            for d in deployments:
                if d.get("deployment_id") == target_deployment_id and d.get("status") == "SUCCESS":
                    target_dep = d
                    break
            if not target_dep:
                raise DeploymentError(f"Specified successful deployment '{target_deployment_id}' not found for rollback.")
        else:
            curr_dep = app.get("active_deployment_id")
            candidates = [d for d in deployments if d.get("status") == "SUCCESS" and d.get("deployment_id") != curr_dep]
            if not candidates:
                raise DeploymentError("No previous successful release available for rollback.")
            target_dep = candidates[-1]

        # Stop currently running process
        SafeProcessLifecycleManager.stop_process(app_id)

        target_dep_id = target_dep["deployment_id"]
        target_port = target_dep.get("host_port") or target_dep.get("port") or app.get("host_port") or app.get("port", 8090)

        # Restore and run prior deployment
        app_sandbox = STORAGE_APPS_DIR / app_id
        target_release_dir = Path(target_dep.get("release_dir") or (app_sandbox / "releases" / target_dep_id))
        exec_dir = target_release_dir if target_release_dir.exists() and any(target_release_dir.iterdir()) else app_sandbox

        if (exec_dir / "app.py").exists():
            cmd = [sys.executable, "-u", str(exec_dir / "app.py")]
        elif (exec_dir / "main.py").exists():
            cmd = [sys.executable, "-u", str(exec_dir / "main.py")]
        elif (app_sandbox / "app.py").exists():
            cmd = [sys.executable, "-u", str(app_sandbox / "app.py")]
        else:
            cmd = [sys.executable, "-u", str(exec_dir / "_server.py")]

        env = os.environ.copy()
        env["PORT"] = str(target_port)
        env["HOST"] = "127.0.0.1"
        env["APPLICATION_ID"] = app_id
        env["PYTHONUNBUFFERED"] = "1"
        for k, v in app.get("env", {}).items():
            env[str(k)] = str(v)

        record = SafeProcessLifecycleManager.start_process(
            app_id=app_id,
            deployment_id=target_dep_id,
            cmd=cmd,
            cwd=exec_dir,
            port=target_port,
            env=env
        )

        # Verify restored health
        hc = app.get("health_check", {})
        target_log = Path(record.log_file) if record and record.log_file else (BASE_DIR / "logs" / f"app_{app_id}.log")
        healthy, hc_msg = DeploymentManager.probe_http_health(
            port=target_port,
            health_path=hc.get("path", "/health"),
            timeout_sec=hc.get("timeout_seconds", 5.0),
            pid=record.pid,
            log_path=target_log
        )
        if not healthy:
            SafeProcessLifecycleManager.stop_process(app_id)
            app["status"] = "FAILED"
            app["failure_reason"] = f"Rollback health check failed: {hc_msg}"
            save_apps_db(db)
            raise DeploymentError(app["failure_reason"])

        app["status"] = "RUNNING"
        app["active_deployment_id"] = target_dep_id
        app["port"] = target_port
        app["host_port"] = target_port
        app["updated_at"] = get_current_iso_timestamp()
        save_apps_db(db)

        return {
            "status": "ROLLED_BACK",
            "app_id": app_id,
            "restored_deployment_id": target_dep_id,
            "port": target_port,
            "pid": record.pid,
            "health_probe": hc_msg
        }

    @staticmethod
    def stop_application(app_id: str) -> Dict[str, Any]:
        """Safely stops an active application process."""
        db = load_apps_db()
        app = db.get("apps", {}).get(app_id)
        if not app:
            raise DeploymentError(f"Application '{app_id}' not found.")

        ok, msg = SafeProcessLifecycleManager.stop_process(app_id)
        app["status"] = "STOPPED"
        app["updated_at"] = get_current_iso_timestamp()
        save_apps_db(db)
        return {"status": "STOPPED", "app_id": app_id, "message": msg}

    @staticmethod
    def restart_application(app_id: str) -> Dict[str, Any]:
        """Safely restarts an active application using its current deployment configuration."""
        db = load_apps_db()
        app = db.get("apps", {}).get(app_id)
        if not app:
            raise DeploymentError(f"Application '{app_id}' not found.")

        SafeProcessLifecycleManager.stop_process(app_id)
        return DeploymentManager.deploy_application(app_id)

    @staticmethod
    def get_application_status(app_id: str, paths: Optional[Any] = None) -> Dict[str, Any]:
        """Inspects runtime status, process liveness, and configuration."""
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app:
            raise DeploymentError(f"Application '{app_id}' not found.")

        record = SafeProcessLifecycleManager.read_record(app_id)
        is_running = False
        if record and SafeProcessLifecycleManager.is_pid_active(record.pid):
            start_time = getattr(record, "start_time", None)
            if start_time is not None:
                curr_start = SafeProcessLifecycleManager.get_process_start_time(record.pid)
                is_running = (curr_start is not None and str(curr_start) == str(start_time))
            else:
                is_running = True

        port = record.port if record else (app.get("port") or app.get("host_port"))

        return {
            "app_id": app_id,
            "name": app.get("name"),
            "status": "RUNNING" if is_running else app.get("status", "STOPPED"),
            "active_deployment_id": app.get("active_deployment_id"),
            "selected_node": app.get("selected_node"),
            "port": port,
            "host_port": port,
            "pid": record.pid if record else None,
            "address": f"http://127.0.0.1:{port}" if port else None,
            "deployments_count": len(app.get("deployments", [])),
            "updated_at": app.get("updated_at")
        }

    @staticmethod
    def cancel_deployment(
        app_id: str,
        deployment_id: str,
        reason: str = "Deployment cancelled by user operator",
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Safely cancels an active or queued application deployment:
        - Rejects terminal state cancellation (idempotent 200 or no-op)
        - Cancels associated background scheduler job in jobs_db
        - Transitions state machine to CANCELLED
        - Releases deployment lock
        - Preserves previous active release in running state
        """
        from controller.stores import APPS_LOCK
        from controller.deployment_state_machine import TERMINAL_STATES, DEP_STATE_CANCELLED
        now_iso = get_current_iso_timestamp()

        with APPS_LOCK:
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id)
            if not app or app.get("status") in ("DELETED", "deleted"):
                raise DeploymentError(f"Application '{app_id}' not found.")

            target_dep = None
            for d in app.get("deployments", []):
                if d.get("deployment_id") == deployment_id:
                    target_dep = d
                    break

            if not target_dep:
                raise DeploymentError(f"Deployment '{deployment_id}' not found for application '{app_id}'.")

            cur_status = str(target_dep.get("status", "")).upper()
            if cur_status in TERMINAL_STATES or cur_status in ("SUCCESS", "SUCCEEDED", "FAILED", "CANCELLED"):
                return {
                    "status": cur_status,
                    "app_id": app_id,
                    "deployment_id": deployment_id,
                    "message": f"Deployment '{deployment_id}' is already in terminal state '{cur_status}'.",
                    "duplicate": True
                }

            # 1. Transition state machine
            DeploymentStateMachine.transition(
                app_id,
                deployment_id,
                DEP_STATE_CANCELLED,
                reason=reason,
                paths=paths
            )

            # 2. Release lock
            DeploymentManager.release_deployment_lock(app_id, deployment_id, paths=paths)

            # 3. Cancel associated scheduler job
            job_id = target_dep.get("job_id")
            if job_id:
                jobs_db = load_jobs_db(paths=paths)
                job = jobs_db.get("jobs", {}).get(job_id)
                if job and job.get("status") not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    job["status"] = "CANCELLED"
                    job["state"] = "CANCELLED"
                    job["cancellation_requested"] = True
                    job["cancelled_at"] = now_iso
                    job["finished_at"] = now_iso
                    job["lease_expires_at"] = None
                    save_jobs_db(jobs_db, paths=paths)

            # 4. Restore application status
            db = load_apps_db(paths=paths)
            app = db.get("apps", {}).get(app_id, {})
            prior_active = app.get("active_deployment_id")
            if prior_active:
                app["status"] = "RUNNING"
            else:
                app["status"] = "STOPPED"
            app["updated_at"] = now_iso

            for d in app.get("deployments", []):
                if d.get("deployment_id") == deployment_id:
                    d["status"] = "CANCELLED"
                    d["error"] = reason
                    d["finished_at"] = now_iso
                    break
            save_apps_db(db, paths=paths)

            return {
                "status": "CANCELLED",
                "app_id": app_id,
                "deployment_id": deployment_id,
                "reason": reason,
                "cancelled_at": now_iso
            }

    @staticmethod
    def retry_deployment(
        app_id: str,
        deployment_id: str,
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Retries an eligible failed or cancelled deployment using identical configuration.
        """
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found.")

        target_dep = None
        for d in app.get("deployments", []):
            if d.get("deployment_id") == deployment_id:
                target_dep = d
                break

        if not target_dep:
            raise DeploymentError(f"Deployment '{deployment_id}' not found for application '{app_id}'.")

        cur_status = target_dep.get("status")
        if cur_status not in ("FAILED", "CANCELLED"):
            raise DeploymentError(f"Deployment '{deployment_id}' in state '{cur_status}' is not eligible for retry; must be FAILED or CANCELLED.")

        return DeploymentManager.deploy_application(
            app_id=app_id,
            commit=target_dep.get("commit"),
            branch=target_dep.get("branch"),
            trigger=f"retry:{deployment_id}",
            node_override=target_dep.get("node"),
            port_override=target_dep.get("port") or target_dep.get("host_port"),
            artifact_sha256=target_dep.get("artifact_sha256"),
            paths=paths
        )

    @staticmethod
    def list_deployments(
        app_id: str,
        page: int = 1,
        limit: int = 20,
        status_filter: Optional[str] = None,
        paths: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Returns paginated deployment history for an application.
        """
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found.")

        deployments = list(app.get("deployments", []))
        if status_filter:
            sf = str(status_filter).upper()
            deployments = [d for d in deployments if str(d.get("status", "")).upper() == sf]

        # Reverse order so most recent is first
        deployments.reverse()

        total_count = len(deployments)
        p = max(1, int(page))
        l = max(1, min(100, int(limit)))
        start_idx = (p - 1) * l
        end_idx = start_idx + l
        page_items = deployments[start_idx:end_idx]

        active_dep_id = app.get("active_deployment_id")
        for item in page_items:
            item["is_active"] = (item.get("deployment_id") == active_dep_id)

        total_pages = (total_count + l - 1) // l if total_count > 0 else 1

        return {
            "app_id": app_id,
            "deployments": page_items,
            "total_count": total_count,
            "page": p,
            "limit": l,
            "pages": total_pages,
            "active_deployment_id": active_dep_id
        }

    @staticmethod
    def get_deployment_record(
        app_id: str,
        deployment_id: str,
        paths: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves detailed record for a specific deployment.
        """
        db = load_apps_db(paths=paths)
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            return None

        for d in app.get("deployments", []):
            if d.get("deployment_id") == deployment_id:
                rec = dict(d)
                rec["is_active"] = (deployment_id == app.get("active_deployment_id"))
                return rec
        return None

    @staticmethod
    def get_deployment_logs(
        app_id: str,
        deployment_id: Optional[str] = None,
        max_lines: int = 100,
        paths: Optional[Any] = None
    ) -> str:
        """
        Retrieves bounded, redacted logs for an application or specific deployment.
        """
        from common.process_manager import LOGS_DIR
        clean_app = "".join(c for c in app_id if c.isalnum() or c in ("-", "_"))
        candidate_paths = []
        if deployment_id:
            clean_dep = "".join(c for c in deployment_id if c.isalnum() or c in ("-", "_"))
            candidate_paths.extend([
                LOGS_DIR / f"app_{clean_app}_{clean_dep}.log",
                STORAGE_APPS_DIR / app_id / "releases" / clean_dep / "app.log",
                STORAGE_APPS_DIR / app_id / "releases" / f"{clean_dep}.failed" / "app.log",
            ])
        candidate_paths.extend([
            LOGS_DIR / f"app_{clean_app}.log",
            LOGS_DIR / f"{clean_app}.log",
        ])

        for c_p in candidate_paths:
            if c_p.exists() and c_p.is_file():
                return DeploymentManager.read_bounded_log_tail(c_p, max_lines=max_lines)

        return f"No log output available for application '{app_id}'."
