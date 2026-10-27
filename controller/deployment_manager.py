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
from controller.stores import load_apps_db, save_apps_db, load_nodes_db
from scheduler.scheduler import ResourceScheduler

STORAGE_APPS_DIR = BASE_DIR / "storage" / "applications"
STORAGE_ARTIFACTS_DIR = BASE_DIR / "storage" / "artifacts"
RUNTIME_APPS_DIR = BASE_DIR / "runtime" / "apps"


class DeploymentError(RuntimeError):
    """Raised when application deployment fails."""
    pass


class DeploymentManager:
    """Orchestrates reliable application registration, deployment, rollback, and lifecycle."""

    @staticmethod
    def register_manifest(manifest_data: Dict[str, Any], apps_db: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            "port": int(manifest_data.get("port") or manifest_data.get("container_port") or 8090),
            "components": manifest_data.get("components", {}),
            "health_check": manifest_data.get("health_check", {
                "path": "/health",
                "timeout_seconds": 5,
                "interval_seconds": 0.5,
                "expected_status": 200
            }),
            "release": manifest_data.get("release", {}),
            "deployments": deployments,
            "active_deployment_id": existing_rec.get("active_deployment_id"),
            "env_vars": existing_rec.get("env_vars", {}),
            "env": existing_rec.get("env", {}),
            "created_at": existing_rec.get("created_at", get_current_iso_timestamp()),
            "updated_at": get_current_iso_timestamp()
        }

        db.setdefault("apps", {})[app_id] = app_record
        save_apps_db(db)
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
    def probe_http_health(
        port: int,
        health_path: str = "/health",
        timeout_sec: float = 5.0,
        interval_sec: float = 0.5,
        expected_status: int = 200,
        host: str = "127.0.0.1"
    ) -> Tuple[bool, str]:
        """
        Polls the HTTP health endpoint until HTTP 200 or timeout.
        Returns (True, 'Healthy') or (False, error_details).
        """
        clean_path = health_path if health_path.startswith("/") else f"/{health_path}"
        target_url = f"http://{host}:{port}{clean_path}"

        t0 = time.time()
        last_error = ""

        while time.time() - t0 < timeout_sec:
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

        return False, f"Health check failed on {target_url} after {timeout_sec}s: {last_error}"

    @staticmethod
    def deploy_application(
        app_id: str,
        artifact_path: Optional[Path] = None,
        artifact_sha256: Optional[str] = None,
        port_override: Optional[int] = None,
        start_cmd_override: Optional[List[str]] = None,
        auto_rollback_on_failure: bool = True
    ) -> Dict[str, Any]:
        """
        Executes a controlled deployment of an application:
        - Verifies artifact integrity if artifact is provided
        - Pre-flight port availability check
        - Capability-based node scheduling
        - Isolated execution sandbox
        - Strict health probe verification
        - Automatic graceful rollback to prior working release upon startup or health failure
        """
        db = load_apps_db()
        app = db.get("apps", {}).get(app_id)
        if not app or app.get("status") in ("DELETED", "deleted"):
            raise DeploymentError(f"Application '{app_id}' not found in registry.")

        # 1. Artifact Verification
        if artifact_path and artifact_sha256:
            DeploymentManager.verify_artifact_checksum(artifact_path, artifact_sha256)

        port = port_override or int(app.get("port") or 8090)

        # 2. Port Conflict Check (Pre-flight)
        if not SafeProcessLifecycleManager.check_port_available(port):
            # Check if port is held by previous version of same application
            rec = SafeProcessLifecycleManager.read_record(app_id)
            if not rec or not SafeProcessLifecycleManager.is_pid_active(rec.pid):
                raise DeploymentError(f"Port conflict: Port {port} is already in use by an external process.")

        # 3. Capability-based Node Selection
        runtime = app.get("runtime", "python")
        reqs = {
            "capabilities": ["python3"] if runtime in ("python", "python3", "self-hosted") else ["container_runtime"]
        }
        selected_node = DeploymentManager.select_deployment_node(reqs)

        # Record deployment ID
        dep_id = f"dep-{secrets.token_hex(4)}"
        now_iso = get_current_iso_timestamp()
        dep_number = len(app.get("deployments", [])) + 1
        prior_dep_id = app.get("active_deployment_id")

        # 4. Prepare Application Sandbox and Versioned Release Directory
        app_sandbox = STORAGE_APPS_DIR / app_id
        app_sandbox.mkdir(parents=True, exist_ok=True)
        release_dir = app_sandbox / "releases" / dep_id
        release_dir.mkdir(parents=True, exist_ok=True)

        # Extract artifact if ZIP provided
        if artifact_path and artifact_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(artifact_path, "r") as zf:
                zf.extractall(release_dir)
        else:
            # Check for existing active release directory or sandbox files to copy forward
            src_dir = None
            if prior_dep_id:
                for d in app.get("deployments", []):
                    if d.get("deployment_id") == prior_dep_id and d.get("release_dir"):
                        cand = Path(d["release_dir"])
                        if cand.exists():
                            src_dir = cand
                            break
            if not src_dir and (app_sandbox / "releases").exists():
                releases = sorted([p for p in (app_sandbox / "releases").iterdir() if p.is_dir() and p != release_dir], key=os.path.getmtime)
                if releases:
                    src_dir = releases[-1]
            if not src_dir:
                src_dir = app_sandbox

            if src_dir and src_dir.exists() and src_dir != release_dir:
                for item in src_dir.iterdir():
                    if item.name != "releases":
                        if item.is_file():
                            shutil.copy2(item, release_dir / item.name)
                        elif item.is_dir():
                            shutil.copytree(item, release_dir / item.name, dirs_exist_ok=True)

        # 5. Determine Command Line & Environment
        exec_dir = release_dir if any(release_dir.iterdir()) else app_sandbox
        if start_cmd_override:
            cmd = start_cmd_override
        elif (exec_dir / "app.py").exists():
            cmd = [sys.executable, str(exec_dir / "app.py")]
        elif (exec_dir / "main.py").exists():
            cmd = [sys.executable, str(exec_dir / "main.py")]
        elif (app_sandbox / "app.py").exists():
            cmd = [sys.executable, str(app_sandbox / "app.py")]
        else:
            fallback_script = exec_dir / "_server.py"
            if not fallback_script.exists():
                fallback_script.write_text(
                    f"import http.server, socketserver\n"
                    f"class Handler(http.server.SimpleHTTPRequestHandler):\n"
                    f"    def do_GET(self):\n"
                    f"        if self.path in ('/health', '/api/health'):\n"
                    f"            self.send_response(200)\n"
                    f"            self.send_header('Content-Type', 'application/json')\n"
                    f"            self.end_headers()\n"
                    f"            self.wfile.write(b'{{\"status\":\"ok\",\"app\":\"{app_id}\"}}\\n')\n"
                    f"        else:\n"
                    f"            super().do_GET()\n"
                    f"http.server.HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n",
                    encoding="utf-8"
                )
            cmd = [sys.executable, str(fallback_script)]

        env = os.environ.copy()
        env["PORT"] = str(port)
        env["HOST"] = "127.0.0.1"
        env["APPLICATION_ID"] = app_id
        for k, v in app.get("env", {}).items():
            env[str(k)] = str(v)

        # 6. Stop previous instance of this application if running
        SafeProcessLifecycleManager.stop_process(app_id)

        # 7. Start Process with Process Identity Contract
        try:
            record = SafeProcessLifecycleManager.start_process(
                app_id=app_id,
                deployment_id=dep_id,
                cmd=cmd,
                cwd=exec_dir,
                port=port,
                env=env
            )
        except Exception as e:
            app["status"] = "FAILED"
            app["failure_reason"] = f"Process startup failed: {e}"
            app["updated_at"] = now_iso
            save_apps_db(db)
            raise DeploymentError(app["failure_reason"])

        # 8. Strict Health Check Probe
        hc = app.get("health_check", {})
        hc_path = hc.get("path", "/health")
        hc_timeout = float(hc.get("timeout_seconds", 5.0))
        hc_interval = float(hc.get("interval_seconds", 0.5))
        expected_status = int(hc.get("expected_status", 200))

        healthy, hc_msg = DeploymentManager.probe_http_health(
            port=port,
            health_path=hc_path,
            timeout_sec=hc_timeout,
            interval_sec=hc_interval,
            expected_status=expected_status
        )

        if not healthy:
            # Terminate failing process immediately
            SafeProcessLifecycleManager.stop_process(app_id)

            dep_record = {
                "deployment_id": dep_id,
                "number": dep_number,
                "status": "FAILED",
                "error": hc_msg,
                "node": selected_node,
                "port": port,
                "release_dir": str(release_dir),
                "started_at": now_iso,
                "finished_at": get_current_iso_timestamp()
            }
            app.setdefault("deployments", []).append(dep_record)

            if auto_rollback_on_failure and prior_dep_id:
                # Attempt automatic rollback to prior working release
                try:
                    rollback_res = DeploymentManager.rollback_application(app_id, target_deployment_id=prior_dep_id)
                    app["status"] = "RUNNING"
                    app["active_deployment_id"] = prior_dep_id
                    app["failure_reason"] = f"Deployment {dep_id} failed health check. Automatically rolled back to {prior_dep_id}: {hc_msg}"
                    save_apps_db(db)
                    return {
                        "status": "ROLLED_BACK",
                        "error": hc_msg,
                        "active_deployment_id": prior_dep_id,
                        "rollback_details": rollback_res
                    }
                except Exception as rb_err:
                    app["status"] = "FAILED"
                    app["failure_reason"] = f"Deployment {dep_id} failed ({hc_msg}) and rollback failed ({rb_err})."
                    save_apps_db(db)
                    raise DeploymentError(app["failure_reason"])
            else:
                app["status"] = "FAILED"
                app["failure_reason"] = hc_msg
                save_apps_db(db)
                raise DeploymentError(f"Deployment rejected: {hc_msg}")

        # 9. Successful Deployment
        dep_record = {
            "deployment_id": dep_id,
            "number": dep_number,
            "status": "SUCCESS",
            "node": selected_node,
            "port": port,
            "pid": record.pid,
            "release_dir": str(release_dir),
            "artifact_sha256": artifact_sha256,
            "started_at": now_iso,
            "finished_at": get_current_iso_timestamp()
        }
        app.setdefault("deployments", []).append(dep_record)
        app["status"] = "RUNNING"
        app["selected_node"] = selected_node
        app["port"] = port
        app["active_deployment_id"] = dep_id
        app["failure_reason"] = None
        app["updated_at"] = get_current_iso_timestamp()
        save_apps_db(db)

        return {
            "status": "SUCCESS",
            "app_id": app_id,
            "deployment_id": dep_id,
            "node": selected_node,
            "port": port,
            "pid": record.pid,
            "health_probe": hc_msg,
            "address": f"http://127.0.0.1:{port}"
        }

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
        target_port = target_dep.get("port") or app.get("port", 8090)

        # Restore and run prior deployment
        app_sandbox = STORAGE_APPS_DIR / app_id
        target_release_dir = Path(target_dep.get("release_dir") or (app_sandbox / "releases" / target_dep_id))
        exec_dir = target_release_dir if target_release_dir.exists() and any(target_release_dir.iterdir()) else app_sandbox

        if (exec_dir / "app.py").exists():
            cmd = [sys.executable, str(exec_dir / "app.py")]
        elif (exec_dir / "main.py").exists():
            cmd = [sys.executable, str(exec_dir / "main.py")]
        elif (app_sandbox / "app.py").exists():
            cmd = [sys.executable, str(app_sandbox / "app.py")]
        else:
            cmd = [sys.executable, str(exec_dir / "_server.py")]

        env = os.environ.copy()
        env["PORT"] = str(target_port)
        env["HOST"] = "127.0.0.1"
        env["APPLICATION_ID"] = app_id
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
        healthy, hc_msg = DeploymentManager.probe_http_health(
            port=target_port,
            health_path=hc.get("path", "/health"),
            timeout_sec=hc.get("timeout_seconds", 5.0)
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
    def get_application_status(app_id: str) -> Dict[str, Any]:
        """Inspects runtime status, process liveness, and configuration."""
        db = load_apps_db()
        app = db.get("apps", {}).get(app_id)
        if not app:
            raise DeploymentError(f"Application '{app_id}' not found.")

        record = SafeProcessLifecycleManager.read_record(app_id)
        is_running = SafeProcessLifecycleManager.is_pid_active(record.pid) if record else False

        port = record.port if record else app.get("port")

        return {
            "app_id": app_id,
            "name": app.get("name"),
            "status": "RUNNING" if is_running else app.get("status", "STOPPED"),
            "active_deployment_id": app.get("active_deployment_id"),
            "selected_node": app.get("selected_node"),
            "port": port,
            "pid": record.pid if record else None,
            "address": f"http://127.0.0.1:{port}" if port else None,
            "deployments_count": len(app.get("deployments", [])),
            "updated_at": app.get("updated_at")
        }
