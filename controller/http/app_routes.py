#!/usr/bin/env python3
"""
Controller Application Route Handlers
=====================================
HTTP request handling for application lifecycle management, container deployment,
GitHub source inspection, environment variable configuration, rollback orchestration,
and application storage file management.
"""

import os
import re
import secrets
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from common.time_utils import get_current_iso_timestamp
from controller.blueprints import (
    APP_STORAGE_ROOT,
    allocate_host_port,
    fetch_github_source,
    mask_app_record,
    parse_and_validate_blueprint,
    resolve_safe_app_storage_path,
    validate_app_route,
)
from controller.constants import (
    APP_STATE_BUILDING,
    APP_STATE_CONFIGURING,
    APP_STATE_CREATED,
    APP_STATE_DEPLOYING,
    APP_STATE_FAILED,
    APP_STATE_FETCHING_SOURCE,
    APP_STATE_RUNNING,
    APP_STATE_STOPPED,
    JOB_STATE_QUEUED,
)
from controller.env_manager import parse_env_file_content
from controller.image_inspector import (
    detect_application_port,
    detect_image_architectures,
    validate_image_reference,
)
from controller.stores import (
    APPS_LOCK,
    load_apps_db,
    load_jobs_db,
    load_nodes_db,
    save_apps_db,
    save_jobs_db,
)
from scheduler.scheduler import ResourceScheduler


def handle_app_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles GET requests for applications, deployments, env vars, logs, and files.
    Returns True if handled, False otherwise.
    """
    if not (path == "/apps" or path.startswith("/apps/")):
        return False

    if not handler.is_authenticated_admin_or_node():
        handler.send_json(401, {"error": "Unauthorized: Authentication required"})
        return True

    # GET /apps
    if path == "/apps":
        apps_db = load_apps_db()
        apps_list = [mask_app_record(app) for app in apps_db.get("apps", {}).values() if app.get("status") != "DELETED"]
        handler.send_json(200, {
            "apps": apps_list,
            "count": len(apps_list)
        })
        return True

    # GET /apps/<app_id>/deployments
    if path.startswith("/apps/") and path.endswith("/deployments"):
        app_id = path[6:-12].strip()
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app or app.get("status") == "DELETED":
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True
        deployments = app.get("deployments", [])
        handler.send_json(200, {
            "app_id": app_id,
            "deployments": deployments,
            "count": len(deployments)
        })
        return True

    # GET /apps/<app_id>/env
    if path.startswith("/apps/") and path.endswith("/env"):
        app_id = path[6:-4].strip()
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app or app.get("status") == "DELETED":
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True
        masked_app = mask_app_record(app)
        handler.send_json(200, {
            "app_id": app_id,
            "env_vars": masked_app.get("env_vars", {}),
            "env": masked_app.get("env", {})
        })
        return True

    # GET /apps/<app_id>/logs
    if path.startswith("/apps/") and path.endswith("/logs"):
        app_id = path[6:-5].strip()
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        jobs_db = load_jobs_db()
        container_id = app.get("container_id") or f"ps-{app.get('name')}"

        # Look up recent job output for this app
        app_jobs = [
            j for j in jobs_db.get("jobs", {}).values()
            if j.get("parameters", {}).get("app_id") == app_id or j.get("parameters", {}).get("container_name") == container_id
        ]
        app_jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)

        log_output = "No logs recorded yet."
        if app_jobs:
            latest_job = app_jobs[0]
            res = latest_job.get("result", {})
            log_output = res.get("stdout") or res.get("stderr") or f"Job {latest_job.get('job_id')} status: {latest_job.get('status')}"

        handler.send_json(200, {
            "app_id": app_id,
            "name": app.get("name"),
            "status": app.get("status"),
            "node": app.get("selected_node"),
            "logs": log_output
        })
        return True

    # GET /apps/<app_id>/files or /apps/<app_id>/files/download
    if path.startswith("/apps/") and "/files" in path:
        is_download = path.endswith("/files/download")
        parts = path.split("/")
        app_id = parts[2] if len(parts) > 2 else ""

        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        req_path = query.get("path", [""])[0]
        try:
            target_path, app_base = resolve_safe_app_storage_path(app_id, req_path)
        except ValueError as e:
            handler.send_json(403, {"error": str(e)})
            return True

        if not target_path.exists():
            handler.send_json(404, {"error": f"Path not found: {req_path}"})
            return True

        if is_download:
            if not target_path.is_file():
                handler.send_json(400, {"error": "Target is not a file"})
                return True
            file_size = target_path.stat().st_size
            filename = target_path.name
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(file_size))
            handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            handler.end_headers()
            with open(target_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
            return True

        if not target_path.is_dir():
            handler.send_json(400, {"error": f"Path is a file, not a directory: {req_path}"})
            return True

        items = []
        try:
            with os.scandir(target_path) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                        items.append({
                            "name": entry.name,
                            "is_dir": is_dir,
                            "size_bytes": stat.st_size if not is_dir else 0,
                            "modified": stat.st_mtime,
                            "extension": Path(entry.name).suffix.lstrip(".") if not is_dir else None
                        })
                    except Exception:
                        continue
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to list directory: {e}"})
            return True

        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        rel_display = str(target_path.relative_to(app_base)).replace("\\", "/")
        if rel_display == ".":
            rel_display = ""

        handler.send_json(200, {
            "status": "ok",
            "app_id": app_id,
            "name": app.get("name"),
            "path": rel_display,
            "items": items,
            "count": len(items)
        })
        return True

    # GET /apps/<app_id>
    if path.startswith("/apps/"):
        app_id = path[6:].strip()
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app or app.get("status") == "DELETED":
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True
        handler.send_json(200, {
            "app": mask_app_record(app)
        })
        return True

    return False


def handle_app_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: str, query: Dict[str, list]) -> bool:
    """
    Handles POST requests for application creation, build/deploy, rollback, lifecycle,
    and storage operations.
    Returns True if handled, False otherwise.
    """
    if not (path == "/apps" or path.startswith("/apps/")):
        return False

    if not handler.is_authenticated_admin_or_node():
        handler.send_json(401, {"error": "Unauthorized: Authentication required"})
        return True

    # POST /apps/github/inspect
    if path == "/apps/github/inspect":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        repo = (body.get("repository") or "").strip()
        branch = (body.get("branch") or "main").strip()
        root_dir = (body.get("root_directory") or ".").strip()
        if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
            handler.send_json(400, {"error": "Invalid repository format. Must be 'owner/repository'."})
            return True
        try:
            inspect_id = f"inspect-{secrets.token_hex(4)}"
            res = fetch_github_source(inspect_id, repo, branch, root_dir)
            temp_dir = APP_STORAGE_ROOT / inspect_id
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            bp = res.get("blueprint")
            has_bp = res.get("has_blueprint", False)
            handler.send_json(200, {
                "status": "ok",
                "repository": repo,
                "branch": branch,
                "root_directory": root_dir,
                "commit": res.get("commit", "main"),
                "blueprint": bp,
                "has_blueprint": has_bp,
                "dockerfile_found": res.get("dockerfile_found", False),
                "env_vars_needed": bp.get("services", [{}])[0].get("envVars", []) if bp else []
            })
        except Exception as e:
            handler.send_json(400, {"error": f"Inspection failed: {e}"})
        return True

    # POST /apps - Create Application
    if path == "/apps":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True

        name = (body.get("name") or "").strip()
        source = body.get("source") or {"type": "manual"}
        blueprint_input = body.get("blueprint")
        image = (body.get("image") or "").strip()
        port = int(body.get("container_port") or body.get("port") or 8000)
        target = (body.get("target") or "auto").strip()
        raw_env_vars = body.get("env_vars") or body.get("env") or {}
        cpu_limit = str(body.get("cpu_limit", "0.5"))
        memory_limit = str(body.get("memory_limit", "256m"))

        if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
            handler.send_json(400, {"error": "Invalid application name. Must contain only alphanumeric characters, dashes, and underscores."})
            return True

        if not (1 <= port <= 65535):
            handler.send_json(400, {"error": "Invalid container port. Must be between 1 and 65535."})
            return True

        # Normalize env_vars
        env_vars = {}
        flat_env = {}
        if isinstance(raw_env_vars, dict):
            for k, v in raw_env_vars.items():
                k_str = str(k).strip()
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k_str):
                    continue
                if isinstance(v, dict):
                    is_sec = bool(v.get("is_secret", True if any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")) else False))
                    val_str = str(v.get("value", ""))
                    env_vars[k_str] = {"value": val_str, "is_secret": is_sec}
                    flat_env[k_str] = val_str
                else:
                    is_sec = bool(any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")))
                    env_vars[k_str] = {"value": str(v), "is_secret": is_sec}
                    flat_env[k_str] = str(v)
        elif isinstance(raw_env_vars, list):
            for item in raw_env_vars:
                if isinstance(item, dict) and "key" in item:
                    k_str = str(item["key"]).strip()
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k_str):
                        continue
                    val_str = str(item.get("value", ""))
                    is_sec = bool(item.get("is_secret", True if any(s in k_str.lower() for s in ("key", "secret", "password", "token", "auth")) else False))
                    env_vars[k_str] = {"value": val_str, "is_secret": is_sec}
                    flat_env[k_str] = val_str

        with APPS_LOCK:
            apps_db = load_apps_db()
            for existing in apps_db.get("apps", {}).values():
                if existing.get("name") == name and existing.get("status") != "DELETED":
                    handler.send_json(409, {"error": f"Application with name '{name}' already exists."})
                    return True

            app_id = f"app-{secrets.token_hex(6)}"
            now_iso = get_current_iso_timestamp()

            source_type = str(source.get("type", "manual")).lower()
            validated_blueprint = None

            if source_type == "github":
                repo = str(source.get("repository", "")).strip()
                branch = str(source.get("branch", "main")).strip()
                root_dir = str(source.get("root_directory", ".")).strip()
                if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
                    handler.send_json(400, {"error": "Invalid GitHub repository format. Must be 'owner/repository'."})
                    return True
                try:
                    fetch_res = fetch_github_source(app_id, repo, branch, root_dir)
                    validated_blueprint = fetch_res.get("blueprint")
                    if blueprint_input:
                        validated_blueprint = parse_and_validate_blueprint(blueprint_input, apps_db, app_id)
                except Exception as e:
                    handler.send_json(400, {"error": f"GitHub source error: {e}"})
                    return True
                source = {
                    "type": "github",
                    "repository": repo,
                    "branch": branch,
                    "root_directory": root_dir
                }
            else:
                # Container Image Deployment
                raw_img = (image or source.get("image") or "").strip()
                if not raw_img:
                    handler.send_json(400, {"error": "Missing container image reference."})
                    return True
                try:
                    parsed_img = validate_image_reference(raw_img)
                    image = parsed_img["raw"]
                except ValueError as e:
                    handler.send_json(400, {"error": str(e)})
                    return True

                supp_archs = detect_image_architectures(image)
                detected_port = detect_application_port(image, explicit_port=body.get("container_port"))
                port = detected_port

                source = {
                    "type": "image",
                    "image": image,
                    "supported_architectures": supp_archs
                }

            route_data = body.get("route")
            if not route_data and validated_blueprint:
                bp_route = validated_blueprint.get("services", [{}])[0].get("route")
                if bp_route:
                    route_data = {"enabled": True, "type": "path", "path": bp_route, "strip_prefix": True, "public_access": True}
            elif not route_data:
                route_data = {"enabled": True, "type": "path", "path": f"/{name.lower()}", "strip_prefix": True, "public_access": True}

            try:
                validated_route = validate_app_route(route_data, apps_db)
            except ValueError as e:
                handler.send_json(400, {"error": str(e)})
                return True

            try:
                host_port = allocate_host_port(apps_db)
            except Exception as e:
                handler.send_json(500, {"error": f"Failed to allocate host port: {e}"})
                return True

            app_record = {
                "app_id": app_id,
                "id": app_id,
                "name": name,
                "source": source,
                "blueprint": validated_blueprint,
                "image": image or f"personalserver/{name}:latest",
                "port": host_port,
                "container_port": port,
                "host_port": host_port,
                "target": target,
                "status": APP_STATE_CREATED,
                "selected_node": None,
                "container_id": f"ps-{name}",
                "route": validated_route,
                "env_vars": env_vars,
                "env": flat_env,
                "deployments": [],
                "cpu_limit": cpu_limit,
                "memory_limit": memory_limit,
                "created_at": now_iso,
                "updated_at": now_iso,
                "failure_reason": None,
                "error": None
            }

            apps_db.setdefault("apps", {})[app_id] = app_record
            save_apps_db(apps_db)

        print(f"[CONTROLLER] Created application '{name}' ({app_id}) on host port {host_port}")
        handler.send_json(201, {
            "status": "created",
            "app_id": app_id,
            "app": mask_app_record(app_record)
        })
        return True

    # POST /apps/env/parse - Parse .env file server-side without saving
    if path == "/apps/env/parse":
        content_str = (body.get("content") or "") if body else ""
        parsed = parse_env_file_content(content_str)
        handler.send_json(200, {
            "status": "ok",
            "env_vars": parsed,
            "count": len(parsed)
        })
        return True

    # POST /apps/<app_id>/env/import - Import .env content into existing app
    if path.startswith("/apps/") and path.endswith("/env/import"):
        app_id = path[6:-11].strip()
        content_str = (body.get("content") or "") if body else ""
        parsed = parse_env_file_content(content_str)
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True
            app.setdefault("env_vars", {}).update(parsed)
            for k, v in parsed.items():
                app.setdefault("env", {})[k] = v.get("value", "")
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)
        handler.send_json(200, {
            "status": "imported",
            "count": len(parsed),
            "app": mask_app_record(app)
        })
        return True

    # POST /apps/<app_id>/env
    if path.startswith("/apps/") and path.endswith("/env"):
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        app_id = path[6:-4].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            key = (body.get("key") or "").strip()
            val = str(body.get("value") or "")
            is_sec = bool(body.get("is_secret", True if any(s in key.lower() for s in ("key", "secret", "password", "token", "auth")) else False))

            if not key or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', key):
                handler.send_json(400, {"error": "Invalid environment variable name. Must start with letter/underscore."})
                return True

            app.setdefault("env_vars", {})[key] = {"value": val, "is_secret": is_sec}
            app.setdefault("env", {})[key] = val
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)

        masked_app = mask_app_record(app)
        handler.send_json(200, {
            "status": "updated",
            "app_id": app_id,
            "env_vars": masked_app.get("env_vars", {})
        })
        return True

    # POST /apps/<app_id>/env/reveal
    if path.startswith("/apps/") and path.endswith("/env/reveal"):
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        app_id = path[6:-11].strip()
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app or app.get("status") == "DELETED":
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        key = (body.get("key") or "").strip()
        env_vars = app.get("env_vars", {})
        if key not in env_vars:
            handler.send_json(404, {"error": f"Variable '{key}' not found"})
            return True

        entry = env_vars[key]
        val = entry.get("value") if isinstance(entry, dict) else str(entry)
        is_sec = entry.get("is_secret", False) if isinstance(entry, dict) else False
        handler.send_json(200, {
            "key": key,
            "value": val,
            "is_secret": is_sec
        })
        return True

    # POST /apps/<app_id>/route
    if path.startswith("/apps/") and path.endswith("/route"):
        app_id = path[6:-6].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            route_data = body.get("route") if body else None
            try:
                validated_route = validate_app_route(route_data, apps_db, current_app_id=app_id)
            except ValueError as e:
                handler.send_json(400, {"error": str(e)})
                return True

            app["route"] = validated_route
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)

        handler.send_json(200, {
            "status": "updated",
            "app_id": app_id,
            "route": validated_route,
            "app": mask_app_record(app)
        })
        return True

    # POST /apps/<app_id>/deploy or redeploy
    if path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/redeploy")):
        is_redeploy = path.endswith("/redeploy")
        app_id = path[6:-9].strip() if is_redeploy else path[6:-7].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            source = app.get("source") or {"type": "manual"}
            source_type = source.get("type", "manual")
            now_iso = get_current_iso_timestamp()
            commit_sha = "latest"

            # If GitHub source app, fetch latest source
            if source_type == "github":
                app["status"] = APP_STATE_FETCHING_SOURCE
                save_apps_db(apps_db)
                repo = source.get("repository")
                branch = source.get("branch", "main")
                root_dir = source.get("root_directory", ".")
                try:
                    fetch_res = fetch_github_source(app_id, repo, branch, root_dir)
                    if fetch_res.get("blueprint"):
                        app["blueprint"] = fetch_res["blueprint"]
                    commit_sha = fetch_res.get("commit", "latest")
                    app["status"] = APP_STATE_CONFIGURING
                    save_apps_db(apps_db)
                except Exception as e:
                    app["status"] = APP_STATE_FAILED
                    app["failure_reason"] = f"Failed to fetch GitHub source: {e}"
                    app["error"] = app["failure_reason"]
                    app["updated_at"] = now_iso
                    save_apps_db(apps_db)
                    handler.send_json(400, {
                        "error": app["failure_reason"],
                        "status": APP_STATE_FAILED,
                        "app": mask_app_record(app)
                    })
                    return True

            # Schedule onto container-capable node (Docker or udocker) with architecture matching
            t_sched_start = time.time()
            nodes_db = load_nodes_db()
            reqs = {
                "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"]
            }
            if source_type == "image" or app.get("image"):
                img_to_check = app.get("image") or source.get("image", "")
                supp_archs = source.get("supported_architectures") or detect_image_architectures(img_to_check)
                if supp_archs:
                    reqs["supported_architectures"] = supp_archs

            decision = ResourceScheduler.select_node(reqs, nodes_db, handler.heartbeat_timeout)
            sched_ms = int((time.time() - t_sched_start) * 1000)

            if not decision.get("selected_node"):
                rejections = list(decision.get("rejected", {}).values())
                if any("Image does not support this node architecture" in r for r in rejections):
                    reason = "Image does not support this node architecture."
                else:
                    reason = f"No container-capable node is currently available in the cluster: {decision.get('reason', '')}".strip()
                app["status"] = APP_STATE_FAILED
                app["failure_reason"] = reason
                app["error"] = reason
                app["updated_at"] = now_iso
                save_apps_db(apps_db)
                print(f"[CONTROLLER-APP] Deployment of '{app['name']}' ({app_id}) failed: {reason}")
                handler.send_json(400, {
                    "error": reason,
                    "status": APP_STATE_FAILED,
                    "app": mask_app_record(app),
                    "scheduler": decision
                })
                return True

            selected_node = decision["selected_node"]
            app["status"] = APP_STATE_DEPLOYING
            app["selected_node"] = selected_node
            app["updated_at"] = now_iso
            app["error"] = None

            # Record deployment entry in history
            dep_num = len(app.get("deployments", [])) + 1
            dep_id = f"dep-{secrets.token_hex(4)}"
            dep_record = {
                "deployment_id": dep_id,
                "number": dep_num,
                "commit": commit_sha,
                "branch": source.get("branch", "main") if source_type == "github" else "manual",
                "trigger": "redeploy" if is_redeploy else (source_type),
                "status": "BUILDING" if source_type == "github" else "DEPLOYING",
                "started_at": now_iso,
                "finished_at": None,
                "duration_ms": None,
                "stage_timings": {
                    "scheduling_ms": sched_ms,
                    "fetch_source_ms": 0,
                    "build_ms": 0,
                    "pull_image_ms": 0,
                    "start_container_ms": 0,
                    "health_check_ms": 0,
                    "total_ms": sched_ms
                }
            }
            app.setdefault("deployments", []).append(dep_record)
            save_apps_db(apps_db)

            # Prepare environment variables unmasked for Docker container
            container_env = dict(app.get("env", {}))
            for k, v in (app.get("env_vars") or {}).items():
                if isinstance(v, dict):
                    container_env[k] = v.get("value", "")
                else:
                    container_env[k] = str(v)

            job_id = f"job-{secrets.token_hex(6)}"

            if source_type == "github":
                # Build and deploy from source context
                src_context, _ = resolve_safe_app_storage_path(app_id, f"source/{source.get('root_directory', '.')}".rstrip("/."))
                dockerfile_name = "Dockerfile"
                if app.get("blueprint"):
                    dockerfile_name = app["blueprint"].get("services", [{}])[0].get("dockerfile", "Dockerfile")

                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"build-{app['name']}",
                    "type": "image-build",
                    "parameters": {
                        "app_id": app_id,
                        "app_name": app["name"],
                        "source_dir": str(src_context),
                        "dockerfile": dockerfile_name,
                        "image_tag": f"personalserver/{app['name']}:v{dep_num}"
                    },
                    "requirements": reqs,
                    "target": selected_node,
                    "target_node": selected_node,
                    "assigned_node": selected_node,
                    "scheduler": {
                        "mode": "resource-aware-docker",
                        "selected_node": selected_node,
                        "score": decision["score"],
                        "reason": decision["reason"]
                    },
                    "timeout": 180,
                    "max_attempts": 1,
                    "attempt": 1,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "created_at": now_iso,
                    "started_at": None,
                    "finished_at": None,
                    "status": JOB_STATE_QUEUED,
                    "state": JOB_STATE_QUEUED,
                    "retry_reason": None,
                    "attempts_history": [],
                    "result": None
                }
            else:
                # Direct image deployment
                parsed_img = validate_image_reference(app["image"])
                img_ref = parsed_img["raw"].split("@")[0]
                img_digest = parsed_img.get("digest")
                dep_record["image"] = img_ref
                if img_digest:
                    dep_record["digest"] = img_digest
                dep_record["architecture"] = app.get("architecture") or "amd64"

                job_record = {
                    "id": job_id,
                    "job_id": job_id,
                    "name": f"deploy-{app['name']}",
                    "type": "docker-deploy",
                    "parameters": {
                        "app_id": app_id,
                        "image": img_ref,
                        "image_digest": img_digest,
                        "architecture": app.get("architecture"),
                        "container_name": app.get("container_id") or f"ps-{app['name']}",
                        "host_port": app["host_port"],
                        "container_port": app.get("container_port", 8000),
                        "env": container_env,
                        "cpu_limit": app.get("cpu_limit", "0.5"),
                        "memory_limit": app.get("memory_limit", "256m")
                    },
                    "requirements": reqs,
                    "target": selected_node,
                    "target_node": selected_node,
                    "assigned_node": selected_node,
                    "scheduler": {
                        "mode": "resource-aware-docker",
                        "selected_node": selected_node,
                        "score": decision["score"],
                        "reason": decision["reason"]
                    },
                    "timeout": 120,
                    "max_attempts": 2,
                    "attempt": 1,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "created_at": now_iso,
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

        print(f"[CONTROLLER] Queued {job_record['type']} job {job_id} for app '{app['name']}' to node '{selected_node}'")
        handler.send_json(200, {
            "status": APP_STATE_DEPLOYING,
            "app": mask_app_record(app),
            "job_id": job_id
        })
        return True

    # POST /apps/<app_id>/stop
    if path.startswith("/apps/") and path.endswith("/stop"):
        app_id = path[6:-5].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            target_node = app.get("selected_node")
            if not target_node:
                app["status"] = APP_STATE_STOPPED
                save_apps_db(apps_db)
                handler.send_json(200, {"status": "STOPPED", "app": app})
                return True

            app["status"] = APP_STATE_STOPPED
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)

            # Queue docker-stop job
            job_id = f"job-{secrets.token_hex(6)}"
            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": f"stop-{app['name']}",
                "type": "docker-stop",
                "parameters": {
                    "app_id": app_id,
                    "container_name": app.get("container_id") or f"ps-{app['name']}"
                },
                "target": target_node,
                "target_node": target_node,
                "assigned_node": target_node,
                "timeout": 30,
                "max_attempts": 1,
                "attempt": 1,
                "created_at": get_current_iso_timestamp(),
                "status": JOB_STATE_QUEUED
            }
            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

        handler.send_json(200, {"status": "STOPPED", "app": app, "job_id": job_id})
        return True

    # POST /apps/<app_id>/restart
    if path.startswith("/apps/") and path.endswith("/restart"):
        app_id = path[6:-8].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            target_node = app.get("selected_node")
            if not target_node:
                handler.send_json(400, {"error": "Application is not deployed on any node."})
                return True

            app["status"] = APP_STATE_RUNNING
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)

            job_id = f"job-{secrets.token_hex(6)}"
            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": f"restart-{app['name']}",
                "type": "docker-restart",
                "parameters": {
                    "app_id": app_id,
                    "container_name": app.get("container_id") or f"ps-{app['name']}"
                },
                "target": target_node,
                "target_node": target_node,
                "assigned_node": target_node,
                "timeout": 30,
                "max_attempts": 1,
                "attempt": 1,
                "created_at": get_current_iso_timestamp(),
                "status": JOB_STATE_QUEUED
            }
            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

        handler.send_json(200, {"status": "RUNNING", "app": app, "job_id": job_id})
        return True

    # POST /apps/<app_id>/rollback
    if path.startswith("/apps/") and path.endswith("/rollback"):
        app_id = path[6:-9].strip()
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            deployments = app.get("deployments", [])
            target_dep_id = (body.get("deployment_id") or body.get("target_deployment_id") if body else None)
            target_dep = None

            if target_dep_id:
                for d in deployments:
                    if d.get("deployment_id") == target_dep_id:
                        target_dep = d
                        break
                if not target_dep:
                    handler.send_json(404, {"error": f"Specified deployment '{target_dep_id}' not found for application '{app_id}'"})
                    return True
            else:
                current_dep_id = app.get("active_deployment_id")
                candidate_deps = [
                    d for d in deployments
                    if d.get("status") == "SUCCESS" and d.get("deployment_id") != current_dep_id
                ]
                if not candidate_deps:
                    candidate_deps = [d for d in deployments if d.get("status") == "SUCCESS"]

                if not candidate_deps:
                    handler.send_json(400, {
                        "error": "No previous successful deployment found to roll back to.",
                        "app_id": app_id,
                        "deployments_count": len(deployments)
                    })
                    return True
                target_dep = candidate_deps[-1]

            now_iso = get_current_iso_timestamp()
            rollback_img = target_dep.get("image") or app.get("image")
            rollback_digest = target_dep.get("digest") or app.get("image_digest")
            rollback_arch = target_dep.get("architecture") or app.get("architecture") or "amd64"

            # Schedule onto container-capable node matching architecture
            nodes_db = load_nodes_db()
            reqs = {
                "any_capabilities": ["container_runtime:docker", "container_runtime:udocker", "container_runtime"],
                "supported_architectures": [rollback_arch] if rollback_arch else ["amd64", "arm64"]
            }
            decision = ResourceScheduler.select_node(reqs, nodes_db, handler.heartbeat_timeout)
            if not decision.get("selected_node"):
                reason = f"No container-capable node available for rollback: {decision.get('reason', '')}".strip()
                handler.send_json(400, {
                    "error": reason,
                    "status": APP_STATE_FAILED,
                    "app": mask_app_record(app)
                })
                return True

            selected_node = decision["selected_node"]
            dep_num = len(deployments) + 1
            dep_id = f"dep-{secrets.token_hex(4)}"

            rollback_record = {
                "deployment_id": dep_id,
                "number": dep_num,
                "commit": target_dep.get("commit", "rollback"),
                "branch": target_dep.get("branch", "rollback"),
                "trigger": "rollback",
                "status": "DEPLOYING",
                "rollback_target": target_dep.get("deployment_id"),
                "image": rollback_img,
                "digest": rollback_digest,
                "architecture": rollback_arch,
                "started_at": now_iso,
                "finished_at": None,
                "duration_ms": None,
                "stage_timings": {
                    "scheduling_ms": 0,
                    "pull_image_ms": 0,
                    "start_container_ms": 0,
                    "health_check_ms": 0,
                    "total_ms": 0
                }
            }
            app.setdefault("deployments", []).append(rollback_record)
            app["status"] = APP_STATE_DEPLOYING
            app["selected_node"] = selected_node
            app["updated_at"] = now_iso
            app["error"] = None
            app["image"] = rollback_img
            if rollback_digest:
                app["image_digest"] = rollback_digest
            app["architecture"] = rollback_arch
            save_apps_db(apps_db)

            # Prepare environment variables
            container_env = dict(app.get("env", {}))
            for k, v in (app.get("env_vars") or {}).items():
                if isinstance(v, dict):
                    container_env[k] = v.get("value", "")
                else:
                    container_env[k] = str(v)

            job_id = f"job-{secrets.token_hex(6)}"
            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": f"rollback-{app['name']}",
                "type": "docker-deploy",
                "parameters": {
                    "app_id": app_id,
                    "image": rollback_img,
                    "image_digest": rollback_digest,
                    "architecture": rollback_arch,
                    "container_name": app.get("container_id") or f"ps-{app['name']}",
                    "host_port": app["host_port"],
                    "container_port": app.get("container_port", 8000),
                    "env": container_env,
                    "cpu_limit": app.get("cpu_limit", "0.5"),
                    "memory_limit": app.get("memory_limit", "256m")
                },
                "requirements": reqs,
                "target": selected_node,
                "target_node": selected_node,
                "assigned_node": selected_node,
                "scheduler": {
                    "mode": "resource-aware-docker",
                    "selected_node": selected_node,
                    "score": decision["score"],
                    "reason": f"Rollback to deployment {target_dep.get('deployment_id')}"
                },
                "timeout": 120,
                "max_attempts": 2,
                "attempt": 1,
                "claimed_at": None,
                "lease_expires_at": None,
                "created_at": now_iso,
                "status": JOB_STATE_QUEUED,
                "state": JOB_STATE_QUEUED
            }

            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

        print(f"[CONTROLLER] Queued rollback deployment {dep_id} for app '{app['name']}' restoring deployment {target_dep.get('deployment_id')}")
        handler.send_json(200, {
            "status": APP_STATE_DEPLOYING,
            "app": mask_app_record(app),
            "job_id": job_id,
            "rollback_target": target_dep.get("deployment_id")
        })
        return True

    # POST /apps/<app_id>/files/mkdir
    if path.startswith("/apps/") and path.endswith("/files/mkdir"):
        app_id = path.split("/")[2]
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        folder_name = ((body.get("name") if body else "") or "").strip()
        parent_rel = (body.get("path") if body else "") or ""

        if not folder_name or re.search(r'[\\/:\*\?"<>\|\x00]', folder_name) or ".." in folder_name:
            handler.send_json(400, {"error": "Invalid folder name"})
            return True

        try:
            target_dir, app_base = resolve_safe_app_storage_path(app_id, os.path.join(parent_rel, folder_name))
        except ValueError as e:
            handler.send_json(403, {"error": str(e)})
            return True

        if target_dir.exists():
            handler.send_json(409, {"error": "Directory already exists"})
            return True

        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            handler.send_json(200, {
                "status": "created",
                "path": str(target_dir.relative_to(app_base)).replace("\\", "/")
            })
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to create directory: {e}"})
        return True

    # POST /apps/<app_id>/files/upload
    if path.startswith("/apps/") and path.endswith("/files/upload"):
        app_id = path.split("/")[2]
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        target_rel = query.get("path", [""])[0]
        try:
            target_dir, app_base = resolve_safe_app_storage_path(app_id, target_rel)
        except ValueError as e:
            handler.send_json(403, {"error": str(e)})
            return True

        if not target_dir.exists() or not target_dir.is_dir():
            handler.send_json(400, {"error": "Upload target directory does not exist"})
            return True

        content_len = int(handler.headers.get("Content-Length", 0))
        max_upload = int(os.environ.get("MAX_UPLOAD_BYTES")) if os.environ.get("MAX_UPLOAD_BYTES") else None
        if max_upload is not None and content_len > max_upload:
            handler.send_json(413, {"error": f"Upload exceeds {max_upload // (1024 * 1024)} MB limit"})
            return True

        content_type = handler.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            boundary = content_type.split("boundary=")[-1].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            raw_data = handler.rfile.read(content_len)
            boundary_bytes = boundary.encode("utf-8")
            parts = raw_data.split(b"--" + boundary_bytes)
            saved_files = []
            for part in parts:
                if b"Content-Disposition" in part and b'filename="' in part:
                    header_part, file_data = part.split(b"\r\n\r\n", 1)
                    file_data = file_data.rstrip(b"\r\n--")
                    match = re.search(rb'filename="([^"]+)"', header_part)
                    if match:
                        raw_filename = match.group(1).decode("utf-8", errors="ignore")
                        safe_name = os.path.basename(raw_filename)
                        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
                        if not safe_name or safe_name in (".", ".."):
                            safe_name = "uploaded_file"
                        dest_path = target_dir / safe_name
                        with open(dest_path, "wb") as f:
                            f.write(file_data)
                        saved_files.append(safe_name)
            handler.send_json(200, {
                "status": "uploaded",
                "files": saved_files,
                "target_dir": str(target_dir.relative_to(app_base)).replace("\\", "/")
            })
            return True
        else:
            filename = query.get("filename", ["upload.dat"])[0]
            safe_name = os.path.basename(filename)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
            dest_path = target_dir / safe_name
            with open(dest_path, "wb") as f:
                remaining = content_len
                while remaining > 0:
                    chunk_size = min(remaining, 65536)
                    chunk = handler.rfile.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            handler.send_json(200, {
                "status": "uploaded",
                "file": safe_name,
                "bytes": content_len
            })
            return True

    return False


def handle_app_delete(handler: Any, path: str, token: str, query: Dict[str, list]) -> bool:
    """
    Handles DELETE requests for application files, environment variables, and applications.
    Returns True if handled, False otherwise.
    """
    if not path.startswith("/apps/"):
        return False

    if not handler.is_authenticated_admin_or_node():
        handler.send_json(401, {"error": "Unauthorized: Authentication required"})
        return True

    # DELETE /apps/<app_id>/files?path=<rel_path>
    if "/files" in path:
        app_id = path.split("/")[2]
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        req_path = query.get("path", [""])[0]
        if not req_path:
            handler.send_json(400, {"error": "Missing 'path' parameter"})
            return True

        try:
            target_item, app_base = resolve_safe_app_storage_path(app_id, req_path)
        except ValueError as e:
            handler.send_json(403, {"error": str(e)})
            return True

        if target_item == app_base:
            handler.send_json(403, {"error": "Cannot delete application storage root"})
            return True

        if not target_item.exists():
            handler.send_json(404, {"error": "Item not found"})
            return True

        try:
            if target_item.is_dir():
                shutil.rmtree(target_item)
            else:
                target_item.unlink()
            handler.send_json(200, {
                "status": "deleted",
                "path": req_path
            })
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to delete item: {e}"})
        return True

    # DELETE /apps/<app_id>/env/<key>
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) == 4 and parts[0] == "apps" and parts[2] == "env":
        app_id = parts[1]
        env_key = parts[3]
        with APPS_LOCK:
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                handler.send_json(404, {"error": f"Application '{app_id}' not found"})
                return True

            if env_key in app.get("env_vars", {}):
                del app["env_vars"][env_key]
            if env_key in app.get("env", {}):
                del app["env"][env_key]
            app["updated_at"] = get_current_iso_timestamp()
            save_apps_db(apps_db)

        handler.send_json(200, {
            "status": "deleted",
            "key": env_key,
            "app_id": app_id,
            "env_vars": mask_app_record(app).get("env_vars", {})
        })
        return True

    # DELETE /apps/<app_id>
    app_id = path[6:].strip()
    with APPS_LOCK:
        apps_db = load_apps_db()
        app = apps_db.get("apps", {}).get(app_id)
        if not app:
            handler.send_json(404, {"error": f"Application '{app_id}' not found"})
            return True

        target_node = app.get("selected_node")
        if target_node:
            job_id = f"job-{secrets.token_hex(6)}"
            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": f"remove-{app['name']}",
                "type": "docker-remove",
                "parameters": {
                    "container_name": app.get("container_id") or f"ps-{app['name']}"
                },
                "target": target_node,
                "target_node": target_node,
                "assigned_node": target_node,
                "timeout": 30,
                "max_attempts": 1,
                "attempt": 1,
                "created_at": get_current_iso_timestamp(),
                "status": JOB_STATE_QUEUED
            }
            jobs_db = load_jobs_db()
            jobs_db.setdefault("jobs", {})[job_id] = job_record
            save_jobs_db(jobs_db)

        del apps_db["apps"][app_id]
        save_apps_db(apps_db)

    print(f"[CONTROLLER] Deleted application '{app['name']}' ({app_id}). Released host port {app.get('host_port')}")
    handler.send_json(200, {
        "status": "deleted",
        "app_id": app_id,
        "message": f"Application '{app['name']}' deleted."
    })
    return True


__all__ = [
    "handle_app_get",
    "handle_app_post",
    "handle_app_delete",
]
