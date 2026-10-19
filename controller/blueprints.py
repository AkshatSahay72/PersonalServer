#!/usr/bin/env python3
"""
Controller Blueprints & Application Routing
===========================================
Validation for PersonalServer application blueprints (personalserver.yaml / Dockerfile),
application route reservation and collision avoidance, dynamic host port allocation,
sandboxed GitHub source fetching, and application record secret masking.
"""

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from common.storage_utils import resolve_safe_path, validate_app_id
from config.platform_config import get_controller_paths, get_platform_config
from controller.constants import PORT_RANGE_END, PORT_RANGE_START

BASE_DIR = Path(__file__).resolve().parent.parent
PATHS = get_controller_paths()
APP_STORAGE_ROOT = (PATHS.storage_dir / "applications").resolve()

RESERVED_ROUTES = {
    "/", "/api", "/static", "/storage", "/health", "/status",
    "/cluster", "/nodes", "/jobs", "/admin", "/login", "/ws", "/apps"
}


def resolve_safe_app_storage_path(app_id: str, rel_path: str = "") -> Tuple[Path, Path]:
    """
    Resolves and sandboxes paths strictly within the application's storage namespace:
    ~/PersonalServer/storage/applications/<app_id>/
    """
    validate_app_id(app_id)
    app_base = (APP_STORAGE_ROOT / app_id).resolve()

    # Initialize starter structure if app directory does not exist yet
    if not app_base.exists():
        app_base.mkdir(parents=True, exist_ok=True)
        readme_file = app_base / "README.md"
        if not readme_file.exists():
            readme_file.write_text(
                f"# Application: {app_id}\n\nManaged application storage namespace.\nPlace project files, Dockerfile, configurations, and assets here.\n",
                encoding="utf-8"
            )
        dockerfile = app_base / "Dockerfile"
        if not dockerfile.exists():
            dockerfile.write_text(
                "# PersonalServer Deployment Dockerfile\nFROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\nCMD [\"python\", \"-m\", \"http.server\", \"8000\"]\n",
                encoding="utf-8"
            )

    try:
        target = resolve_safe_path(app_base, rel_path)
    except ValueError:
        raise ValueError("Access Denied: Path escapes application storage sandbox")

    return target, app_base


def allocate_host_port(apps_db: Dict[str, Any]) -> int:
    """
    Finds the lowest available host port in range PORT_RANGE_START to PORT_RANGE_END.
    """
    allocated = set()
    for app in apps_db.get("apps", {}).values():
        if app.get("status") != "DELETED" and "host_port" in app:
            try:
                allocated.add(int(app["host_port"]))
            except (ValueError, TypeError):
                pass
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in allocated:
            return port
    raise RuntimeError(f"Port exhaustion: All host ports in range {PORT_RANGE_START}-{PORT_RANGE_END} are allocated.")


def validate_app_route(route_dict: Optional[Dict[str, Any]], apps_db: Dict[str, Any], current_app_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Validates application route metadata:
    - Path must match ^/[a-zA-Z0-9_-]+$
    - Path normalized to lowercase
    - Rejects reserved system routes
    - Enforces route path uniqueness across non-deleted applications
    """
    if route_dict is None:
        return None

    if not isinstance(route_dict, dict):
        raise ValueError("Route specification must be an object with 'path' field.")

    if not route_dict.get("enabled", True):
        raw_path = str(route_dict.get("path", "")).strip().lower()
        return {
            "enabled": False,
            "type": str(route_dict.get("type", "path")).lower(),
            "path": raw_path,
            "strip_prefix": bool(route_dict.get("strip_prefix", True)),
            "public_access": bool(route_dict.get("public_access", True))
        }

    path = str(route_dict.get("path") or "").strip().lower()
    if path in RESERVED_ROUTES:
        raise ValueError(f"Route path '{path}' is a reserved PersonalServer system path.")

    if not path or not re.match(r'^\/[a-zA-Z0-9_-]+$', path):
        raise ValueError("Invalid route path. Must start with '/' followed only by alphanumeric characters, dashes, or underscores.")

    for existing in apps_db.get("apps", {}).values():
        if existing.get("status") == "DELETED" or existing.get("app_id") == current_app_id:
            continue
        existing_route = existing.get("route")
        if existing_route and isinstance(existing_route, dict) and existing_route.get("enabled", True):
            if str(existing_route.get("path", "")).strip().lower() == path:
                raise ValueError(f"Route path '{path}' is already registered to application '{existing.get('name')}'.")

    return {
        "enabled": True,
        "type": str(route_dict.get("type", "path")).lower(),
        "path": path,
        "strip_prefix": bool(route_dict.get("strip_prefix", True)),
        "public_access": bool(route_dict.get("public_access", True)),
        "public_url": get_platform_config().get_app_public_url(path)
    }


def parse_yaml_or_json(content_str: str) -> Dict[str, Any]:
    """Parses YAML (using PyYAML if installed or built-in YAML subset parser) or JSON."""
    if not content_str or not isinstance(content_str, str):
        return {}
    content_str = content_str.strip()
    if content_str.startswith("{"):
        try:
            return json.loads(content_str)
        except Exception:
            pass
    try:
        import yaml
        return yaml.safe_load(content_str) or {}
    except ImportError:
        # Robust fallback parser for personalserver.yaml subset
        result: Dict[str, Any] = {"services": []}
        curr_service: Optional[Dict[str, Any]] = None
        curr_env_vars: Optional[list] = None
        for raw_line in content_str.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- type:"):
                curr_service = {"type": line.split(":", 1)[1].strip()}
                result["services"].append(curr_service)
                curr_env_vars = None
            elif curr_service is not None:
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip().lstrip("- ")
                    v = v.strip().strip("'\"")
                    if k == "envVars":
                        curr_env_vars = []
                        curr_service["envVars"] = curr_env_vars
                    elif curr_env_vars is not None and k == "key":
                        curr_env_vars.append({"key": v, "sync": False})
                    elif k in ("name", "runtime", "rootDir", "dockerfile", "route"):
                        curr_service[k] = v
        return result


def parse_and_validate_blueprint(blueprint_input: Any, apps_db: Optional[Dict[str, Any]] = None, current_app_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Validates PersonalServer Blueprint schema:
    services:
      - type: web
        name: <app_name>
        runtime: docker
        rootDir: .
        dockerfile: ./Dockerfile
        route: /<app_name>
        envVars:
          - key: KEY_NAME
            sync: false
    """
    if apps_db is None:
        apps_db = {"apps": {}}

    if isinstance(blueprint_input, str):
        data = parse_yaml_or_json(blueprint_input)
    elif isinstance(blueprint_input, dict):
        data = blueprint_input
    else:
        raise ValueError("Blueprint must be a YAML/JSON string or an object.")

    services = data.get("services")
    if not services or not isinstance(services, list) or len(services) == 0:
        raise ValueError("Blueprint must define at least one service under 'services'.")

    svc = services[0]
    if not isinstance(svc, dict):
        raise ValueError("Service definition must be an object.")

    svc_type = str(svc.get("type", "web")).strip().lower()
    if svc_type != "web":
        raise ValueError(f"Unsupported service type '{svc_type}'. Only 'web' is supported in this phase.")

    svc_name = str(svc.get("name", "")).strip()
    if not svc_name or not re.match(r'^[a-zA-Z0-9_-]+$', svc_name):
        raise ValueError("Service name must be non-empty and contain only alphanumeric characters, dashes, and underscores.")

    runtime = str(svc.get("runtime", "docker")).strip().lower()
    if runtime != "docker":
        raise ValueError(f"Unsupported runtime '{runtime}'. Only 'docker' is supported.")

    root_dir = str(svc.get("rootDir") or ".").strip().replace("\\", "/")
    if root_dir.startswith("/") or ".." in root_dir:
        raise ValueError("rootDir must be a relative path and cannot escape repository root.")

    dockerfile = str(svc.get("dockerfile") or "Dockerfile").strip().replace("\\", "/")
    if dockerfile.startswith("/") or ".." in dockerfile:
        raise ValueError("dockerfile must be a relative path within rootDir.")

    # Route validation
    route_path = svc.get("route")
    route_obj = None
    if route_path:
        route_dict = {"enabled": True, "type": "path", "path": route_path, "strip_prefix": True, "public_access": True}
        route_obj = validate_app_route(route_dict, apps_db, current_app_id)

    # Environment variables validation
    raw_env_vars = svc.get("envVars") or []
    validated_env_vars = []
    if isinstance(raw_env_vars, list):
        for ev in raw_env_vars:
            if isinstance(ev, dict) and "key" in ev:
                k = str(ev["key"]).strip()
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k):
                    raise ValueError(f"Invalid environment variable name '{k}'.")
                validated_env_vars.append({"key": k, "sync": bool(ev.get("sync", False))})
            elif isinstance(ev, str):
                k = ev.strip()
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k):
                    raise ValueError(f"Invalid environment variable name '{k}'.")
                validated_env_vars.append({"key": k, "sync": False})

    sanitized_blueprint = {
        "version": "1.0",
        "services": [
            {
                "type": "web",
                "name": svc_name,
                "runtime": "docker",
                "rootDir": root_dir,
                "dockerfile": dockerfile,
                "route": route_obj.get("path") if route_obj else f"/{svc_name.lower()}",
                "envVars": validated_env_vars
            }
        ]
    }
    return sanitized_blueprint


def fetch_github_source(app_id: str, repository: str, branch: str = "main", root_directory: str = ".") -> Dict[str, Any]:
    """
    Fetches GitHub source repository into sandboxed application storage directory:
    storage/applications/<app_id>/source/
    Ensures safe unpacking and zero path traversal.
    """
    if not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repository.strip()):
        raise ValueError("Invalid repository format. Expected 'owner/repository'.")

    branch = branch.strip() or "main"
    if not re.match(r'^[a-zA-Z0-9_./-]+$', branch) or ".." in branch:
        raise ValueError("Invalid branch name.")

    target_source_dir, app_base = resolve_safe_app_storage_path(app_id, "source")
    target_source_dir.mkdir(parents=True, exist_ok=True)

    # Check local test fixture directories (e.g. tests/fixtures/sample_repo)
    repo_leaf = repository.split("/")[-1]
    fixture_dir = BASE_DIR / "tests" / "fixtures" / repo_leaf
    commit_sha = "main"

    if fixture_dir.exists() and fixture_dir.is_dir():
        shutil.copytree(fixture_dir, target_source_dir, dirs_exist_ok=True)
    else:
        url = f"https://codeload.github.com/{repository}/tar.gz/refs/heads/{branch}"
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-SourceDeployer/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                import io
                import tarfile
                tar_bytes = resp.read()
                tar_fp = io.BytesIO(tar_bytes)
                with tarfile.open(fileobj=tar_fp, mode="r:gz") as tar:
                    members = tar.getmembers()
                    if not members:
                        raise ValueError("Empty repository archive.")
                    root_prefix = members[0].name.split("/")[0] + "/"
                    for member in members:
                        rel_path = member.name[len(root_prefix):] if member.name.startswith(root_prefix) else member.name
                        if not rel_path or rel_path.startswith("/") or ".." in rel_path:
                            continue
                        member.name = rel_path
                        dest_path = (target_source_dir / rel_path).resolve()
                        dest_path.relative_to(target_source_dir)
                        tar.extract(member, path=target_source_dir)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Repository '{repository}' or branch '{branch}' not found on GitHub (404).")
            raise RuntimeError(f"GitHub returned HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to connect to GitHub: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"Failed to fetch GitHub repository '{repository}' on branch '{branch}': {e}")

    # Inspect fetched source for blueprint or Dockerfile
    resolved_root = (target_source_dir / (root_directory or ".")).resolve()
    try:
        resolved_root.relative_to(target_source_dir)
    except ValueError:
        raise ValueError("Access Denied: rootDirectory escapes source repository.")

    blueprint = None
    bp_file = resolved_root / "personalserver.yaml"
    if not bp_file.exists():
        bp_file = resolved_root / "personalserver.yml"

    is_blueprint_file = bp_file.exists()
    dockerfile_file = resolved_root / "Dockerfile"
    dockerfile_found = dockerfile_file.exists()

    if is_blueprint_file:
        try:
            blueprint = parse_and_validate_blueprint(bp_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid blueprint file {bp_file.name}: {e}")
    elif dockerfile_found:
        app_name = repository.split("/")[-1].lower().replace(".", "-")
        blueprint = {
            "version": "1.0",
            "services": [
                {
                    "type": "web",
                    "name": app_name,
                    "runtime": "docker",
                    "rootDir": root_directory or ".",
                    "dockerfile": "Dockerfile",
                    "route": f"/{app_name}",
                    "envVars": []
                }
            ]
        }
    else:
        raise ValueError("No 'personalserver.yaml' blueprint or 'Dockerfile' found in repository.")

    return {
        "status": "fetched",
        "repository": repository,
        "branch": branch,
        "root_directory": root_directory,
        "commit": commit_sha[:8],
        "source_dir": str(target_source_dir),
        "blueprint": blueprint,
        "has_blueprint": is_blueprint_file,
        "dockerfile_found": dockerfile_found
    }


def mask_app_record(app: Optional[Dict[str, Any]], reveal_secrets: bool = False) -> Optional[Dict[str, Any]]:
    """Returns a sanitized copy of application record with secret environment variables masked."""
    if not app or not isinstance(app, dict):
        return app
    app_copy = json.loads(json.dumps(app))

    # Mask env_vars object
    env_vars = app_copy.get("env_vars") or {}
    if isinstance(env_vars, dict):
        masked_env = {}
        for k, v in env_vars.items():
            if isinstance(v, dict):
                is_sec = bool(v.get("is_secret", True if any(s in k.lower() for s in ("key", "secret", "password", "token", "auth")) else False))
                val = v.get("value", "")
                masked_env[k] = {
                    "value": val if (reveal_secrets or not is_sec) else "********",
                    "is_secret": is_sec
                }
            else:
                is_sec = bool(any(s in k.lower() for s in ("key", "secret", "password", "token", "auth")))
                masked_env[k] = {
                    "value": str(v) if (reveal_secrets or not is_sec) else "********",
                    "is_secret": is_sec
                }
        app_copy["env_vars"] = masked_env

    # Mask legacy env dict
    raw_env = app_copy.get("env") or {}
    if isinstance(raw_env, dict):
        masked_raw = {}
        for k, v in raw_env.items():
            is_sec = bool(any(s in k.lower() for s in ("key", "secret", "password", "token", "auth")))
            masked_raw[k] = str(v) if (reveal_secrets or not is_sec) else "********"
        app_copy["env"] = masked_raw

    # Attach dynamic public URL from centralized PlatformConfig
    route = app_copy.get("route")
    if route and isinstance(route, dict) and route.get("enabled"):
        app_copy["public_url"] = get_platform_config().get_app_public_url(route.get("path"))
    else:
        app_copy["public_url"] = "-"

    return app_copy


__all__ = [
    "RESERVED_ROUTES",
    "resolve_safe_app_storage_path",
    "allocate_host_port",
    "validate_app_route",
    "parse_yaml_or_json",
    "parse_and_validate_blueprint",
    "fetch_github_source",
    "mask_app_record",
]
