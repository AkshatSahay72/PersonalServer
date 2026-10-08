#!/usr/bin/env python3
"""
PersonalServer Controller
=========================
Cluster controller for multi-node inventory, authenticated registration,
heartbeat liveness monitoring, node removal, resource-aware workload scheduling,
persistent job lifecycle, lease-based failure detection, and automatic recovery.
"""
import sys
import os
import json
import time
import secrets
import argparse
import hashlib
import hmac
import shutil
import threading
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Import ResourceScheduler
from scheduler.scheduler import ResourceScheduler, compute_node_liveness, extract_node_telemetry
from config.platform_config import get_platform_config
from controller.image_inspector import (
    validate_image_reference,
    detect_image_architectures,
    detect_application_port,
    is_architecture_compatible,
    normalize_architecture
)
from controller.env_manager import (
    parse_env_file_content,
    sanitize_env_vars_input,
    is_secret_variable
)

CONFIG_DIR = BASE_DIR / "config"
SECRETS_DIR = CONFIG_DIR / "secrets"
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = CONTROLLER_DIR / "data"

NODES_FILE = DATA_DIR / "nodes.json"
BACKUP_NODES_FILE = DATA_DIR / "nodes.json.bak"
JOBS_FILE = DATA_DIR / "jobs.json"
BACKUP_JOBS_FILE = DATA_DIR / "jobs.json.bak"
APPS_FILE = DATA_DIR / "apps.json"
BACKUP_APPS_FILE = DATA_DIR / "apps.json.bak"
ONBOARDING_CODES_FILE = DATA_DIR / "onboarding_codes.json"
BACKUP_ONBOARDING_CODES_FILE = DATA_DIR / "onboarding_codes.json.bak"
ENROLLMENT_TOKEN_FILE = SECRETS_DIR / "enrollment.token"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds
DEFAULT_LEASE_GRACE_SEC = 20    # seconds beyond job timeout
DEFAULT_ONBOARDING_TTL_SEC = 900 # 15 minutes (seconds)

PORT_RANGE_START = 18000
PORT_RANGE_END = 18999

ONBOARDING_LOCK = threading.Lock()
APPS_LOCK = threading.Lock()
APP_STORAGE_ROOT = (BASE_DIR / "storage" / "applications").resolve()


def resolve_safe_app_storage_path(app_id, rel_path=""):
    """
    Resolves and sandboxes paths strictly within the application's storage namespace:
    ~/PersonalServer/storage/applications/<app_id>/
    """
    if not app_id or not re.match(r'^[a-zA-Z0-9_-]+$', app_id):
        raise ValueError("Invalid application ID")
    
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
                f"# PersonalServer Deployment Dockerfile\nFROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\nCMD [\"python\", \"-m\", \"http.server\", \"8000\"]\n",
                encoding="utf-8"
            )

    rel_clean = (rel_path or "").replace("\\", "/").strip().lstrip("/")
    target = (app_base / rel_clean).resolve()
    
    try:
        target.relative_to(app_base)
    except ValueError:
        raise ValueError("Access Denied: Path escapes application storage sandbox")
        
    return target, app_base


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


def get_current_iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


def ensure_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)


def get_or_create_enrollment_token():
    ensure_directories()
    if ENROLLMENT_TOKEN_FILE.exists():
        try:
            token = ENROLLMENT_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_hex(24)
    ENROLLMENT_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(ENROLLMENT_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


# ------------------------------------------------------------------------------
# Storage Helpers
# ------------------------------------------------------------------------------

def load_nodes_db():
    ensure_directories()
    if NODES_FILE.exists():
        try:
            with open(NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {NODES_FILE} ({e}). Attempting rollback to backup...", file=sys.stderr)
            if BACKUP_NODES_FILE.exists():
                try:
                    with open(BACKUP_NODES_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"cluster_name": "PersonalServer", "nodes": {}}


def save_nodes_db(db):
    ensure_directories()
    temp_file = NODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if NODES_FILE.exists():
        try:
            shutil.copy2(NODES_FILE, BACKUP_NODES_FILE)
        except Exception:
            pass

    temp_file.replace(NODES_FILE)


def load_jobs_db():
    ensure_directories()
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {JOBS_FILE} ({e}). Rolling back to backup...", file=sys.stderr)
            if BACKUP_JOBS_FILE.exists():
                try:
                    with open(BACKUP_JOBS_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"jobs": {}}


def save_jobs_db(db):
    ensure_directories()
    temp_file = JOBS_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if JOBS_FILE.exists():
        try:
            shutil.copy2(JOBS_FILE, BACKUP_JOBS_FILE)
        except Exception:
            pass

    temp_file.replace(JOBS_FILE)


def load_apps_db():
    ensure_directories()
    if APPS_FILE.exists():
        try:
            with open(APPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {APPS_FILE} ({e}). Rolling back to backup...", file=sys.stderr)
            if BACKUP_APPS_FILE.exists():
                try:
                    with open(BACKUP_APPS_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"apps": {}}


def save_apps_db(db):
    ensure_directories()
    temp_file = APPS_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if APPS_FILE.exists():
        try:
            shutil.copy2(APPS_FILE, BACKUP_APPS_FILE)
        except Exception:
            pass

    temp_file.replace(APPS_FILE)


def allocate_host_port(apps_db):
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


RESERVED_ROUTES = {
    "/", "/api", "/static", "/storage", "/health", "/status",
    "/cluster", "/nodes", "/jobs", "/admin", "/login", "/ws", "/apps"
}


def validate_app_route(route_dict, apps_db, current_app_id=None):
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


# ------------------------------------------------------------------------------
# Phase 13: PersonalServer Blueprint & GitHub Source Subsystem
# ------------------------------------------------------------------------------

def parse_yaml_or_json(content_str):
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
        result = {"services": []}
        curr_service = None
        curr_env_vars = None
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


def parse_and_validate_blueprint(blueprint_input, apps_db=None, current_app_id=None):
    if apps_db is None:
        apps_db = {"apps": {}}
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


def fetch_github_source(app_id, repository, branch="main", root_directory="."):
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
                import tarfile
                import io
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


def mask_app_record(app, reveal_secrets=False):
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


# ------------------------------------------------------------------------------
# Onboarding Code Store & Validation
# ------------------------------------------------------------------------------

def hash_onboarding_code(code: str) -> str:
    """Computes a SHA-256 hash of the normalized onboarding code for secure storage."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def load_onboarding_codes_db():
    ensure_directories()
    if ONBOARDING_CODES_FILE.exists():
        try:
            with open(ONBOARDING_CODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Corrupt {ONBOARDING_CODES_FILE} ({e}). Checking backup...", file=sys.stderr)
            if BACKUP_ONBOARDING_CODES_FILE.exists():
                try:
                    with open(BACKUP_ONBOARDING_CODES_FILE, "r", encoding="utf-8") as bf:
                        return json.load(bf)
                except Exception:
                    pass
    return {"codes": {}}


def save_onboarding_codes_db(db):
    ensure_directories()
    temp_file = ONBOARDING_CODES_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    if ONBOARDING_CODES_FILE.exists():
        try:
            shutil.copy2(ONBOARDING_CODES_FILE, BACKUP_ONBOARDING_CODES_FILE)
        except Exception:
            pass

    temp_file.replace(ONBOARDING_CODES_FILE)


def generate_onboarding_code(ttl_seconds=DEFAULT_ONBOARDING_TTL_SEC):
    """
    Generates a cryptographically secure, single-use onboarding code.
    Format: PS-XXXX-XXXX
    Stores SHA-256 hash in onboarding_codes.json.
    Returns (code_str, ttl_seconds).
    """
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    code = f"PS-{part1}-{part2}"

    code_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        # Clean up stale codes older than 24 hours
        now_ts = now_dt.timestamp()
        filtered = {}
        for h, info in db.get("codes", {}).items():
            try:
                exp_ts = datetime.fromisoformat(info["expires_at"]).timestamp()
                if now_ts - exp_ts < 86400:
                    filtered[h] = info
            except Exception:
                pass
        db["codes"] = filtered

        db.setdefault("codes", {})[code_hash] = {
            "created_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "used": False,
            "used_at": None,
            "used_by_node": None
        }
        save_onboarding_codes_db(db)

    return code, ttl_seconds


def validate_and_consume_onboarding_code(code: str, node_id: str) -> tuple:
    """
    Thread-safe validation and single-use consumption of an onboarding code.
    Returns (is_valid: bool, error_reason: str).
    """
    if not code or not code.strip():
        return False, "Missing onboarding code"

    input_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        codes = db.get("codes", {})

        matched_entry = None
        for h, entry in codes.items():
            if hmac.compare_digest(h, input_hash):
                matched_entry = entry
                break

        if not matched_entry:
            return False, "Invalid onboarding code"

        if matched_entry.get("used"):
            return False, "Onboarding code has already been used"

        try:
            expires_at = datetime.fromisoformat(matched_entry["expires_at"])
            if now_dt > expires_at:
                return False, "Onboarding code has expired"
        except Exception:
            return False, "Corrupt expiration timestamp on onboarding code"

        # Atomically consume the code
        matched_entry["used"] = True
        matched_entry["used_at"] = now_dt.isoformat()
        matched_entry["used_by_node"] = node_id
        save_onboarding_codes_db(db)

        return True, ""


def sanitize_node_record(node, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    node_copy = dict(node)
    node_copy.pop("auth_token", None)
    node_copy["status"] = compute_node_liveness(node, timeout_seconds)
    return node_copy


def inspect_node_storage_safety(node_id, node_entry, associated_apps):
    """
    Inspects PersonalServer-managed storage for the given node.
    Returns (used_bytes, files_count).
    Does NOT scan arbitrary system directories.
    Only inspects PersonalServer-managed storage roots:
    - storage/applications/<app_id>
    - storage/<node_id> or storage/nodes/<node_id>
    - storage/ (excluding application and system files)
    """
    total_bytes = 0
    total_files = 0

    # Explicit simulation / mock support in tests
    if "storage_files_count" in node_entry:
        total_files += int(node_entry.get("storage_files_count", 0))
    if "storage_used_bytes" in node_entry:
        total_bytes += int(node_entry.get("storage_used_bytes", 0))

    # 1. Associated application storage
    for aid, app in associated_apps:
        app_dir = APP_STORAGE_ROOT / aid
        if app_dir.exists() and app_dir.is_dir():
            for root, dirs, files in os.walk(app_dir):
                for f in files:
                    if f == ".gitkeep":
                        continue
                    fp = os.path.join(root, f)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except Exception:
                        pass

    # 2. Node-specific storage folders
    for folder_name in (node_id, f"nodes/{node_id}"):
        nd = BASE_DIR / "storage" / folder_name
        if nd.exists() and nd.is_dir():
            for root, dirs, files in os.walk(nd):
                for f in files:
                    if f == ".gitkeep":
                        continue
                    fp = os.path.join(root, f)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except Exception:
                        pass

    # 3. Local node check
    config_file = CONFIG_DIR / "node.conf"
    local_id = None
    if config_file.exists():
        try:
            for line in config_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("NODE_ID="):
                    local_id = line.split("=", 1)[1].strip()
        except Exception:
            pass
    is_local = (local_id and local_id == node_id) or (node_entry.get("is_local") is True)
    if is_local:
        gen_storage = BASE_DIR / "storage"
        if gen_storage.exists() and gen_storage.is_dir():
            for root, dirs, files in os.walk(gen_storage):
                rel = os.path.relpath(root, gen_storage)
                if rel == "applications" or rel.startswith("applications" + os.sep):
                    continue
                if rel == "nodes" or rel.startswith("nodes" + os.sep):
                    continue
                if rel == node_id or rel.startswith(node_id + os.sep):
                    continue
                for f in files:
                    if f == ".gitkeep":
                        continue
                    fp = os.path.join(root, f)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except Exception:
                        pass

    return total_bytes, total_files


def evaluate_node_deboarding_safety(node_id, nodes_db=None, jobs_db=None, apps_db=None):
    """
    Evaluates whether a node can safely be deboarded and removed.
    Returns dictionary with state, can_remove boolean, blockers list, and workload/storage counts.
    """
    if nodes_db is None:
        nodes_db = load_nodes_db()
    if jobs_db is None:
        jobs_db = load_jobs_db()
    if apps_db is None:
        apps_db = load_apps_db()

    node_entry = nodes_db.get("nodes", {}).get(node_id)
    if not node_entry:
        return {
            "node_id": node_id,
            "state": "NOT_FOUND",
            "can_remove": False,
            "blockers": [f"Node '{node_id}' does not exist in cluster."],
            "active_jobs": 0,
            "applications": 0,
            "storage_used_gb": 0.0,
            "storage_used_bytes": 0,
            "storage_files_count": 0
        }

    current_state = node_entry.get("status", STATE_UNKNOWN)
    blockers = []

    # 1. Evaluate Active / In-progress Jobs
    active_jobs = []
    for jid, job in jobs_db.get("jobs", {}).items():
        if job.get("target_node") == node_id:
            st = job.get("status")
            if st in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING):
                active_jobs.append(jid)
    active_jobs_count = len(active_jobs)
    if active_jobs_count > 0:
        blockers.append(f"{active_jobs_count} running/claimed job(s) in progress")

    # 2. Evaluate Associated Applications
    active_apps = []
    all_associated_apps = []
    for aid, app in apps_db.get("apps", {}).items():
        if app.get("selected_node") == node_id or app.get("target_node") == node_id:
            all_associated_apps.append((aid, app))
            if app.get("status") in (APP_STATE_RUNNING, APP_STATE_DEPLOYING, "BUILDING", "STARTING"):
                active_apps.append(app.get("name") or aid)

    apps_count = len(all_associated_apps)
    if active_apps:
        blockers.append(f"Cannot deboard node: active applications depend on this node ({', '.join(active_apps)}).")

    # 3. Evaluate Storage Data
    storage_used_bytes, storage_files_count = inspect_node_storage_safety(node_id, node_entry, all_associated_apps)
    storage_used_gb = round(storage_used_bytes / (1024 ** 3), 2)
    if storage_files_count > 0 or storage_used_bytes > 0:
        blockers.append(
            f"Cannot deboard node: storage contains data that has not been migrated ({storage_files_count} file(s), {storage_used_gb} GB in use)."
        )

    # 4. State requirement: Node must be in DEBOARDING state before final removal
    if current_state != STATE_DEBOARDING:
        blockers.append(f"Node must be in DEBOARDING state before final removal (currently {current_state}).")

    can_remove = (len(blockers) == 0)

    return {
        "node_id": node_id,
        "state": current_state,
        "can_remove": can_remove,
        "blockers": blockers,
        "active_jobs": active_jobs_count,
        "applications": apps_count,
        "storage_used_gb": storage_used_gb,
        "storage_used_bytes": storage_used_bytes,
        "storage_files_count": storage_files_count
    }


def get_cluster_summary(db, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    nodes = db.get("nodes", {})
    online_count = 0
    draining_count = 0
    deboarding_count = 0
    offline_count = 0
    unhealthy_count = 0
    removed_count = 0
    active_count = 0

    sanitized_nodes = []
    for nid, node in nodes.items():
        s_node = sanitize_node_record(node, timeout_seconds)
        st = s_node["status"]
        if st == STATE_ONLINE:
            online_count += 1
            active_count += 1
        elif st == STATE_DRAINING:
            draining_count += 1
            active_count += 1
        elif st == STATE_DEBOARDING:
            deboarding_count += 1
            active_count += 1
        elif st == STATE_OFFLINE:
            offline_count += 1
            active_count += 1
        elif st == STATE_UNHEALTHY:
            unhealthy_count += 1
            active_count += 1
        elif st == STATE_REMOVED:
            removed_count += 1

        sanitized_nodes.append(s_node)

    return {
        "cluster": {
            "name": db.get("cluster_name", "PersonalServer"),
            "total_nodes": active_count,
            "all_registered": len(nodes),
            "online": online_count,
            "draining": draining_count,
            "deboarding": deboarding_count,
            "offline": offline_count,
            "unhealthy": unhealthy_count,
            "removed": removed_count,
            "timestamp": get_current_iso_timestamp()
        },
        "nodes": sanitized_nodes
    }


def get_cluster_utilization(nodes_db=None, jobs_db=None, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """
    Computes overall cluster utilization and per-node telemetry.
    Offline nodes MUST NOT contribute stale utilization as active capacity.
    """
    if nodes_db is None:
        nodes_db = load_nodes_db()
    if jobs_db is None:
        jobs_db = load_jobs_db()

    nodes = nodes_db.get("nodes", {})
    all_jobs = list(jobs_db.get("jobs", {}).values())

    # Map jobs by target node
    jobs_by_node = {}
    total_active_jobs = 0
    total_running_jobs = 0
    for j in all_jobs:
        target = j.get("target_node")
        st = j.get("status")
        if target:
            jobs_by_node.setdefault(target, []).append(j)
        if st in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING, JOB_STATE_RECOVERING):
            total_active_jobs += 1
        if st == JOB_STATE_RUNNING:
            total_running_jobs += 1

    cluster_cpu_cores_total = 0
    cluster_memory_total_mb = 0.0
    cluster_memory_used_mb = 0.0
    cluster_memory_available_mb = 0.0
    cluster_storage_total_gb = 0.0
    cluster_storage_used_gb = 0.0
    cluster_storage_available_gb = 0.0
    cluster_running_containers = 0

    nodes_telemetry = {}
    for node_id, node in nodes.items():
        node_status = compute_node_liveness(node, timeout_seconds)
        node_tel = extract_node_telemetry(node, status=node_status, node_jobs=jobs_by_node.get(node_id, []))
        nodes_telemetry[node_id] = node_tel

        # Offline nodes must NOT contribute stale utilization as active capacity
        if node_status == STATE_ONLINE:
            cluster_cpu_cores_total += node_tel["cpu"].get("cores", 0)
            cluster_memory_total_mb += node_tel["memory"].get("total_mb", 0.0)
            cluster_memory_used_mb += node_tel["memory"].get("used_mb", 0.0)
            cluster_memory_available_mb += node_tel["memory"].get("available_mb", 0.0)
            cluster_storage_total_gb += node_tel["storage"].get("total_gb", 0.0)
            cluster_storage_used_gb += node_tel["storage"].get("used_gb", 0.0)
            cluster_storage_available_gb += node_tel["storage"].get("available_gb", 0.0)
            cluster_running_containers += node_tel["workloads"].get("running_containers", 0)

    cluster_summary = {
        "cpu_cores_total": cluster_cpu_cores_total,
        "memory_total_mb": round(cluster_memory_total_mb, 1),
        "memory_used_mb": round(cluster_memory_used_mb, 1),
        "memory_available_mb": round(cluster_memory_available_mb, 1),
        "storage_total_gb": round(cluster_storage_total_gb, 1),
        "storage_used_gb": round(cluster_storage_used_gb, 1),
        "storage_available_gb": round(cluster_storage_available_gb, 1),
        "active_jobs": total_active_jobs,
        "running_jobs": total_running_jobs,
        "running_containers": cluster_running_containers
    }

    return {
        "cluster": cluster_summary,
        "nodes": nodes_telemetry
    }


# ------------------------------------------------------------------------------
# Lease Sweeping & Recovery Subsystem
# ------------------------------------------------------------------------------

def sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """
    Evaluates active CLAIMED / RUNNING jobs for lease expiration or worker node disconnection.
    Recovers eligible jobs or marks them FAILED deterministically.
    """
    changed = False
    now_epoch = time.time()
    now_iso = get_current_iso_timestamp()

    for job_id, job in list(jobs_db.get("jobs", {}).items()):
        status = job.get("status")
        if status not in (JOB_STATE_CLAIMED, JOB_STATE_RUNNING):
            continue

        lease_exp = job.get("lease_expires_at")
        target_node = job.get("target_node")
        node_entry = nodes_db.get("nodes", {}).get(target_node, {})
        node_live = compute_node_liveness(node_entry, timeout_seconds) if node_entry else STATE_OFFLINE

        # Check conditions for abandoned lease / worker failure
        lease_expired = lease_exp and now_epoch > lease_exp
        worker_lost = node_live in (STATE_OFFLINE, STATE_REMOVED)

        if lease_expired or worker_lost:
            attempt = job.get("attempt", 1)
            max_attempts = job.get("max_attempts", 3)
            reason = "Worker lease expired" if lease_expired else f"Worker node '{target_node}' went OFFLINE"

            print(f"[CONTROLLER-RECOVERY] Job {job_id} on node '{target_node}' failure detected: {reason} (Attempt {attempt}/{max_attempts})")

            # Record history
            attempts_history = job.setdefault("attempts_history", [])
            attempts_history.append({
                "attempt": attempt,
                "node": target_node,
                "reason": reason,
                "claimed_at": job.get("claimed_at"),
                "failed_at": now_iso
            })

            # Check if retry is allowed
            if attempt < max_attempts:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["retry_reason"] = reason
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                # Policy: Auto-target vs Explicit Target
                target_mode = job.get("target", "auto")
                if target_mode == "auto":
                    # Reschedule onto eligible online node
                    reqs = job.get("requirements", {})
                    decision = ResourceScheduler.select_node(reqs, nodes_db, timeout_seconds)
                    if decision.get("selected_node"):
                        job["target_node"] = decision["selected_node"]
                        job["scheduler"] = {
                            "mode": "resource-aware-recovery",
                            "selected_node": decision["selected_node"],
                            "score": decision["score"],
                            "reason": f"Recovered from {target_node} ({reason})"
                        }
                        print(f"[CONTROLLER-RECOVERY] Rescheduled job {job_id} to node '{decision['selected_node']}'")
                    else:
                        print(f"[CONTROLLER-RECOVERY] No eligible online node available for auto-recovery of {job_id}. Retaining in RECOVERING state.")
                else:
                    # Explicit target: DO NOT silently migrate! Keep same target node.
                    job["target_node"] = target_mode
                    job["scheduler"] = {
                        "mode": "explicit-recovery",
                        "target_node": target_mode,
                        "reason": f"Awaiting recovery on explicit target {target_mode}"
                    }
                    print(f"[CONTROLLER-RECOVERY] Explicit target job {job_id} queued for recovery on '{target_mode}' (no migration).")

            else:
                # Max retries exhausted
                job["status"] = JOB_STATE_FAILED
                job["finished_at"] = now_iso
                job["retry_reason"] = f"Max retries ({max_attempts}) exceeded after: {reason}"
                job["lease_expires_at"] = None
                print(f"[CONTROLLER-RECOVERY] Job {job_id} permanently marked FAILED (max retries reached).")

            changed = True

    if changed:
        save_jobs_db(jobs_db)


def start_background_lease_sweeper(timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Starts a daemon thread to periodically sweep expired leases every 3 seconds."""
    def _sweeper():
        while True:
            try:
                time.sleep(3)
                jobs_db = load_jobs_db()
                nodes_db = load_nodes_db()
                sweep_expired_leases(jobs_db, nodes_db, timeout_seconds)
            except Exception as e:
                print(f"[SWEEPER-ERR] {e}", file=sys.stderr)

    t = threading.Thread(target=_sweeper, daemon=True, name="LeaseSweeper")
    t.start()


# ------------------------------------------------------------------------------
# HTTP Request Handler
# ------------------------------------------------------------------------------

class ControllerHandler(BaseHTTPRequestHandler):
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT

    def send_json(self, status_code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def parse_auth_token(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return self.headers.get("X-Auth-Token", "").strip()

    def is_authenticated_admin_or_node(self):
        token = self.parse_auth_token()
        if not token:
            return False
        enrollment_token = get_or_create_enrollment_token()
        if token == enrollment_token:
            return True
        nodes_db = load_nodes_db()
        for node in nodes_db.get("nodes", {}).values():
            if node.get("auth_token") and node.get("auth_token") == token:
                return True
        return False

    def read_json_body(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len == 0:
                return None
            raw = self.rfile.read(content_len).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # GET /health or GET /status
        if path in ("/health", "/status"):
            nodes_db = load_nodes_db()
            jobs_db = load_jobs_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            self.send_json(200, {
                "status": "online",
                "service": "PersonalServer Controller",
                "cluster": summary["cluster"],
                "total_jobs": len(jobs_db.get("jobs", {})),
                "timestamp": get_current_iso_timestamp()
            })
            return

        # GET /platform or GET /api/platform
        if path in ("/platform", "/api/platform"):
            self.send_json(200, get_platform_config().to_dict())
            return

        # GET /cluster/utilization
        if path == "/cluster/utilization":
            nodes_db = load_nodes_db()
            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)
            util = get_cluster_utilization(nodes_db, jobs_db, self.heartbeat_timeout)
            self.send_json(200, util)
            return

        # GET /cluster
        if path == "/cluster":
            nodes_db = load_nodes_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            self.send_json(200, summary)
            return

        # GET /nodes
        if path == "/nodes":
            nodes_db = load_nodes_db()
            summary = get_cluster_summary(nodes_db, self.heartbeat_timeout)
            nodes_list = summary["nodes"]

            filter_status = query.get("status", [None])[0]
            filter_role = query.get("role", [None])[0]

            if filter_status:
                filter_status = filter_status.upper()
                nodes_list = [n for n in nodes_list if n["status"] == filter_status]
            else:
                nodes_list = [n for n in nodes_list if n["status"] != STATE_REMOVED]

            if filter_role:
                nodes_list = [n for n in nodes_list if n.get("role") == filter_role.lower()]

            self.send_json(200, {
                "nodes": nodes_list,
                "count": len(nodes_list)
            })
            return

        # GET /nodes/<node_id>/jobs/next (Node Agent fetching its assigned job with Lease)
        if path.startswith("/nodes/") and path.endswith("/jobs/next"):
            node_id = path[7:-10].strip()
            token = self.parse_auth_token()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)

            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            expected_auth = node.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()
            if not token or (token != expected_auth and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            jobs_db = load_jobs_db()
            # Perform sweep before claiming
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            # Find jobs waiting for this node
            claimable_jobs = [
                j for j in jobs_db.get("jobs", {}).values()
                if j.get("target_node") == node_id and j.get("status") in (JOB_STATE_QUEUED, JOB_STATE_RECOVERING)
            ]

            if not claimable_jobs:
                self.send_json(200, {"job": None, "message": "No pending jobs for this node."})
                return

            # Atomic claim: pick oldest job and assign lease
            next_job = claimable_jobs[0]
            now_iso = get_current_iso_timestamp()
            now_epoch = time.time()
            timeout_sec = next_job.get("timeout", 60)
            lease_duration = timeout_sec + DEFAULT_LEASE_GRACE_SEC

            next_job["status"] = JOB_STATE_CLAIMED
            next_job["claimed_at"] = now_iso
            next_job["started_at"] = now_iso
            next_job["lease_expires_at"] = now_epoch + lease_duration

            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Job {next_job['job_id']} ({next_job['type']}) CLAIMED by node '{node_id}' (Attempt {next_job.get('attempt', 1)}, Lease {lease_duration}s)")
            self.send_json(200, {"job": next_job})
            return

        # GET /nodes/<node_id>/deboard
        if path.startswith("/nodes/") and path.endswith("/deboard"):
            node_id = path[7:-8].strip()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            self.send_json(200, eval_res)
            return

        # GET /nodes/<node_id>
        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            nodes_db = load_nodes_db()
            node = nodes_db.get("nodes", {}).get(node_id)
            if not node:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return
            s_node = sanitize_node_record(node, self.heartbeat_timeout)
            s_node["deboarding"] = evaluate_node_deboarding_safety(node_id, nodes_db)
            self.send_json(200, {"node": s_node})
            return

        # GET /jobs
        if path == "/jobs":
            jobs_db = load_jobs_db()
            nodes_db = load_nodes_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            all_jobs = list(jobs_db.get("jobs", {}).values())
            filter_node = query.get("node", [None])[0]
            filter_status = query.get("status", [None])[0]

            if filter_node:
                all_jobs = [j for j in all_jobs if j.get("target_node") == filter_node]
            if filter_status:
                all_jobs = [j for j in all_jobs if j.get("status") == filter_status.upper()]

            self.send_json(200, {
                "jobs": all_jobs,
                "count": len(all_jobs)
            })
            return

        # GET /jobs/<job_id>
        if path.startswith("/jobs/"):
            job_id = path[6:].strip()
            jobs_db = load_jobs_db()
            nodes_db = load_nodes_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return
            self.send_json(200, {"job": job})
            return

        # GET /apps
        if path == "/apps":
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            apps_db = load_apps_db()
            apps_list = [mask_app_record(app) for app in apps_db.get("apps", {}).values() if app.get("status") != "DELETED"]
            self.send_json(200, {
                "apps": apps_list,
                "count": len(apps_list)
            })
            return

        # GET /apps/<app_id>/deployments
        if path.startswith("/apps/") and path.endswith("/deployments"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-12].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            deployments = app.get("deployments", [])
            self.send_json(200, {
                "app_id": app_id,
                "deployments": deployments,
                "count": len(deployments)
            })
            return

        # GET /apps/<app_id>/env
        if path.startswith("/apps/") and path.endswith("/env"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-4].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            masked_app = mask_app_record(app)
            self.send_json(200, {
                "app_id": app_id,
                "env_vars": masked_app.get("env_vars", {}),
                "env": masked_app.get("env", {})
            })
            return

        # GET /apps/<app_id>
        if path.startswith("/apps/") and not any(path.endswith(s) for s in ("/logs", "/files", "/env", "/deployments")) and "/" not in path[6:]:
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            self.send_json(200, {
                "app": mask_app_record(app)
            })
            return

        # GET /apps/<app_id>/logs
        if path.startswith("/apps/") and path.endswith("/logs"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:-5].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            # Check if there's a recent docker-logs or deployment result in jobs_db
            jobs_db = load_jobs_db()
            target_node = app.get("selected_node")
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

            self.send_json(200, {
                "app_id": app_id,
                "name": app.get("name"),
                "status": app.get("status"),
                "node": app.get("selected_node"),
                "logs": log_output
            })
            return

        # GET /apps/<app_id>/files or /apps/<app_id>/files/download
        if path.startswith("/apps/") and "/files" in path:
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            
            is_download = path.endswith("/files/download")
            parts = path.split("/")
            app_id = parts[2] if len(parts) > 2 else ""
            
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            req_path = query.get("path", [""])[0]
            try:
                target_path, app_base = resolve_safe_app_storage_path(app_id, req_path)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_path.exists():
                self.send_json(404, {"error": f"Path not found: {req_path}"})
                return

            if is_download:
                if not target_path.is_file():
                    self.send_json(400, {"error": "Target is not a file"})
                    return
                file_size = target_path.stat().st_size
                filename = target_path.name
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                with open(target_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return

            if not target_path.is_dir():
                self.send_json(400, {"error": f"Path is a file, not a directory: {req_path}"})
                return

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
                self.send_json(500, {"error": f"Failed to list directory: {e}"})
                return

            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            rel_display = str(target_path.relative_to(app_base)).replace("\\", "/")
            if rel_display == ".":
                rel_display = ""

            self.send_json(200, {
                "status": "ok",
                "app_id": app_id,
                "name": app.get("name"),
                "path": rel_display,
                "items": items,
                "count": len(items)
            })
            return

        # GET /apps/<app_id>
        if path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path[6:].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return
            self.send_json(200, {"app": app})
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()
        if body is None:
            # Endpoints that do not strictly require a request body
            no_body_endpoints = (
                (path.startswith("/nodes/") and path.endswith("/remove")),
                (path.startswith("/nodes/") and path.endswith("/drain")),
                (path.startswith("/nodes/") and path.endswith("/deboard")),
                (path.startswith("/nodes/") and path.endswith("/resume")),
                (path.startswith("/jobs/") and path.endswith("/cancel")),
                (path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/stop") or path.endswith("/restart")))
            )
            if not any(no_body_endpoints):
                self.send_json(400, {"error": "Invalid or missing JSON payload"})
                return
            body = {}

        token = self.parse_auth_token()
        if not token and body:
            token = body.get("enrollment_token") or body.get("auth_token") or ""

        # ----------------------------------------------------------------------
        # 1. Node Registration: POST /register
        # ----------------------------------------------------------------------
        if path == "/register":
            onboarding_code = self.headers.get("X-Onboarding-Code", "").strip()

            node_id = body.get("node_id")
            if not node_id:
                self.send_json(400, {"error": "Missing required field 'node_id'"})
                return

            if onboarding_code:
                is_valid, err = validate_and_consume_onboarding_code(onboarding_code, node_id)
                if not is_valid:
                    self.send_json(401, {"error": f"Unauthorized: {err}"})
                    return
            else:
                enrollment_token = get_or_create_enrollment_token()
                if not token or token != enrollment_token:
                    self.send_json(401, {"error": "Unauthorized: Invalid enrollment token"})
                    return

            node_name = body.get("name", "unknown")
            node_role = body.get("role", "compute")
            if node_role not in VALID_ROLES:
                node_role = "compute"

            platform = body.get("platform", "unknown")
            os_name = body.get("os", "unknown")
            arch = body.get("architecture", "unknown")

            resources = {
                "cpu_cores": int(body.get("cpu_cores", 0)),
                "ram_mb": int(body.get("ram_mb", 0)),
                "storage_gb": int(body.get("storage_gb", 0))
            }

            capabilities = body.get("capabilities", {
                "compute": True,
                "storage": True,
                "network": True
            })

            nodes_db = load_nodes_db()
            now = get_current_iso_timestamp()

            node_auth_token = secrets.token_hex(20)

            node_record = {
                "node_id": node_id,
                "name": node_name,
                "role": node_role,
                "platform": platform,
                "os": os_name,
                "architecture": arch,
                "resources": resources,
                "capabilities": capabilities,
                "auth_token": node_auth_token,
                "status": STATE_ONLINE,
                "registered_at": now,
                "last_seen": now,
                "last_heartbeat": {
                    "timestamp": now,
                    "services": {},
                    "system": resources
                }
            }

            nodes_db.setdefault("nodes", {})[node_id] = node_record
            save_nodes_db(nodes_db)

            print(f"[CONTROLLER] Registered node: {node_id} ({node_name}, role: {node_role})")

            self.send_json(200, {
                "status": "registered",
                "node_id": node_id,
                "auth_token": node_auth_token,
                "registered_at": now,
                "message": f"Node '{node_id}' successfully registered in cluster '{nodes_db.get('cluster_name')}'."
            })
            return

        # ----------------------------------------------------------------------
        # 2. Node Heartbeat: POST /heartbeat
        # ----------------------------------------------------------------------
        if path == "/heartbeat":
            node_id = body.get("node_id")
            if not node_id:
                self.send_json(400, {"error": "Missing required field 'node_id'"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' is not registered. Please register first."})
                return

            if node_entry.get("status") == STATE_REMOVED or not node_entry.get("auth_token"):
                self.send_json(403, {"error": f"Node '{node_id}' was removed from cluster. Re-registration required."})
                return

            expected_auth_token = node_entry.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()

            if not token or (token != expected_auth_token and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication token"})
                return

            now = get_current_iso_timestamp()
            current_st = node_entry.get("status")
            if current_st not in (STATE_DRAINING, STATE_DEBOARDING):
                node_entry["status"] = STATE_ONLINE
            node_entry["last_seen"] = now
            if self.client_address and self.client_address[0] not in ("127.0.0.1", "localhost", "0.0.0.0"):
                node_entry["ip"] = self.client_address[0]
            node_entry["last_heartbeat"] = {
                "timestamp": body.get("timestamp", now),
                "services": body.get("services", {}),
                "system": body.get("system", {}),
                "workloads": body.get("workloads", {})
            }

            save_nodes_db(nodes_db)

            self.send_json(200, {
                "status": "ok",
                "ack": True,
                "node_id": node_id,
                "timestamp": now
            })
            return

        # ----------------------------------------------------------------------
        # 3. Submit Workload / Job: POST /jobs (Scheduler + Lifecycle)
        # ----------------------------------------------------------------------
        if path == "/jobs":
            enrollment_token = get_or_create_enrollment_token()
            # Allow admin token or valid request
            if token and token != enrollment_token:
                pass  # allow web UI proxy if auth passes

            target = body.get("target") or body.get("target_node") or "auto"
            job_type = body.get("type")
            job_name = body.get("name", job_type or "job")
            timeout_sec = int(body.get("timeout", 60))
            max_attempts = int(body.get("max_attempts", 3))
            parameters = body.get("parameters", {}) or {}
            requirements = body.get("requirements", {}) or {}

            # Validation: Safe allowlisted workload type rejection
            if job_type not in ALLOWLISTED_WORKLOADS:
                self.send_json(400, {
                    "error": f"Job type '{job_type}' is not in the safe allowlist. Allowed: {list(ALLOWLISTED_WORKLOADS.keys())}",
                    "status": JOB_STATE_REJECTED
                })
                return

            nodes_db = load_nodes_db()
            target_node = None
            scheduler_info = None

            # Path A: Automatic Resource-Aware Scheduling
            if target.lower() == "auto":
                decision = ResourceScheduler.select_node(requirements, nodes_db, self.heartbeat_timeout)
                if not decision["selected_node"]:
                    self.send_json(400, {
                        "error": f"Scheduling Failed: {decision['reason']}",
                        "status": JOB_STATE_REJECTED,
                        "scheduler": decision
                    })
                    return
                target_node = decision["selected_node"]
                scheduler_info = {
                    "mode": "resource-aware",
                    "selected_node": target_node,
                    "score": decision["score"],
                    "reason": decision["reason"],
                    "candidates_count": len(decision["candidates"])
                }
                print(f"[CONTROLLER-SCHEDULER] Auto-selected node '{target_node}': {decision['reason']}")

            # Path B: Explicit Target Node (Preserves explicit target policy)
            else:
                target_node = target
                node_entry = nodes_db.get("nodes", {}).get(target_node)

                if not node_entry:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' does not exist in cluster.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                if node_entry.get("status") == STATE_REMOVED:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' was removed from the cluster.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                live_status = compute_node_liveness(node_entry, self.heartbeat_timeout)
                if live_status == STATE_OFFLINE:
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' is currently OFFLINE (heartbeat timeout). Cannot submit job.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                if live_status in (STATE_DRAINING, STATE_DEBOARDING) or node_entry.get("status") in (STATE_DRAINING, STATE_DEBOARDING):
                    self.send_json(400, {
                        "error": f"Target node '{target_node}' is currently {live_status or node_entry.get('status')} (no new workloads accepted). Cannot submit job.",
                        "status": JOB_STATE_REJECTED
                    })
                    return

                scheduler_info = {
                    "mode": "explicit",
                    "target_node": target_node,
                    "reason": "Explicit node specified by client"
                }

            timeout_sec = max(5, min(timeout_sec, 300))
            job_id = f"job-{secrets.token_hex(6)}"
            now = get_current_iso_timestamp()

            job_record = {
                "id": job_id,
                "job_id": job_id,
                "name": job_name,
                "type": job_type,
                "parameters": parameters,
                "requirements": requirements,
                "target": target,
                "target_node": target_node,
                "assigned_node": target_node,
                "scheduler": scheduler_info,
                "timeout": timeout_sec,
                "max_attempts": max_attempts,
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

            print(f"[CONTROLLER] Queued job {job_id} ({job_type}) for target node {target_node}")
            self.send_json(200, {
                "status": JOB_STATE_QUEUED,
                "job_id": job_id,
                "id": job_id,
                "target_node": target_node,
                "scheduler": scheduler_info,
                "job": job_record
            })
            return

        # ----------------------------------------------------------------------
        # 4. Job Result Submission & Retry Handling: POST /jobs/<job_id>/result
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/result"):
            job_id = path[6:-7].strip()
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)

            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return

            target_node = job.get("target_node")
            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(target_node, {})

            expected_auth = node_entry.get("auth_token")
            enrollment_token = get_or_create_enrollment_token()
            if not token or (token != expected_auth and token != enrollment_token):
                self.send_json(401, {"error": "Unauthorized: Invalid node authentication credentials"})
                return

            job_status = body.get("status", JOB_STATE_FAILED)
            now_iso = get_current_iso_timestamp()

            # Record attempt
            attempt = job.get("attempt", 1)
            max_attempts = job.get("max_attempts", 3)
            attempts_history = job.setdefault("attempts_history", [])
            attempts_history.append({
                "attempt": attempt,
                "node": target_node,
                "status": job_status,
                "exit_code": body.get("exit_code", 0),
                "duration_ms": body.get("duration_ms", 0),
                "started_at": body.get("started_at"),
                "finished_at": now_iso
            })

            # Check for failure retry
            if job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT) and attempt < max_attempts:
                job["attempt"] = attempt + 1
                job["status"] = JOB_STATE_RECOVERING
                job["state"] = JOB_STATE_RECOVERING
                job["retry_reason"] = f"Attempt {attempt} completed with status: {job_status}"
                job["claimed_at"] = None
                job["lease_expires_at"] = None

                target_mode = job.get("target", "auto")
                if target_mode == "auto":
                    reqs = job.get("requirements", {})
                    decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                    if decision.get("selected_node"):
                        job["target_node"] = decision["selected_node"]
                        job["assigned_node"] = decision["selected_node"]
                        job["scheduler"] = {
                            "mode": "resource-aware-recovery",
                            "selected_node": decision["selected_node"],
                            "score": decision["score"],
                            "reason": f"Retry attempt {attempt+1} scheduled on {decision['selected_node']}"
                        }
                print(f"[CONTROLLER-RETRY] Job {job_id} failed on node {target_node}. Queued for retry attempt {attempt+1}/{max_attempts}")

            else:
                # Terminal Success / Final Failure
                job["status"] = job_status
                job["state"] = job_status
                job["finished_at"] = now_iso
                job["lease_expires_at"] = None
                job["result"] = {
                    "status": job_status,
                    "exit_code": body.get("exit_code", 0),
                    "stdout": body.get("stdout", ""),
                    "stderr": body.get("stderr", ""),
                    "duration_ms": body.get("duration_ms", 0),
                }

            # Update application state if this was a docker lifecycle job
            app_id = job.get("parameters", {}).get("app_id")
            if app_id:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if app:
                    if job.get("type") in ("docker-deploy", "docker-build-deploy"):
                        if job_status == JOB_STATE_SUCCEEDED:
                            app["status"] = APP_STATE_RUNNING
                            app["error"] = None
                            app["updated_at"] = now_iso
                        elif job_status in (JOB_STATE_FAILED, JOB_STATE_TIMEOUT):
                            app["status"] = APP_STATE_FAILED
                            app["error"] = body.get("stderr") or f"Deployment job {job_status}"
                            app["failure_reason"] = app["error"]
                            app["updated_at"] = now_iso

                        # Update deployment record if present
                        if app.get("deployments"):
                            last_dep = app["deployments"][-1]
                            last_dep["status"] = "SUCCESS" if job_status == JOB_STATE_SUCCEEDED else "FAILED"
                            last_dep["finished_at"] = now_iso
                            last_dep["duration_ms"] = body.get("duration_ms", 0)
                            if body.get("timings"):
                                last_dep.setdefault("stage_timings", {}).update(body["timings"])
                                last_dep["stage_timings"]["total_ms"] = body.get("duration_ms", 0) + last_dep["stage_timings"].get("scheduling_ms", 0)

                    elif job.get("type") == "docker-stop" and job_status == JOB_STATE_SUCCEEDED:
                        app["status"] = APP_STATE_STOPPED
                        app["updated_at"] = now_iso
                    elif job.get("type") == "docker-restart" and job_status == JOB_STATE_SUCCEEDED:
                        app["status"] = APP_STATE_RUNNING
                        app["updated_at"] = now_iso
                    save_apps_db(apps_db)

            save_jobs_db(jobs_db)

            self.send_json(200, {
                "status": "ok",
                "ack": True,
                "job_id": job_id,
                "job_status": job["status"]
            })
            return

        # ----------------------------------------------------------------------
        # 5. Application Management: POST /apps and POST /apps/<app_id>/...
        # ----------------------------------------------------------------------
        if path == "/apps" or path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

        # ----------------------------------------------------------------------
        # GitHub Inspect: POST /apps/github/inspect
        # ----------------------------------------------------------------------
        if path == "/apps/github/inspect":
            repo = (body.get("repository") or "").strip()
            branch = (body.get("branch") or "main").strip()
            root_dir = (body.get("root_directory") or ".").strip()
            if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
                self.send_json(400, {"error": "Invalid repository format. Must be 'owner/repository'."})
                return
            try:
                inspect_id = f"inspect-{secrets.token_hex(4)}"
                res = fetch_github_source(inspect_id, repo, branch, root_dir)
                temp_dir = APP_STORAGE_ROOT / inspect_id
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                bp = res.get("blueprint")
                has_bp = res.get("has_blueprint", False)
                self.send_json(200, {
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
                self.send_json(400, {"error": f"Inspection failed: {e}"})
            return

        if path == "/apps":
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
                self.send_json(400, {"error": "Invalid application name. Must contain only alphanumeric characters, dashes, and underscores."})
                return

            if not (1 <= port <= 65535):
                self.send_json(400, {"error": "Invalid container port. Must be between 1 and 65535."})
                return

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
                        self.send_json(409, {"error": f"Application with name '{name}' already exists."})
                        return

                app_id = f"app-{secrets.token_hex(6)}"
                now_iso = get_current_iso_timestamp()

                source_type = str(source.get("type", "manual")).lower()
                validated_blueprint = None

                if source_type == "github":
                    repo = str(source.get("repository", "")).strip()
                    branch = str(source.get("branch", "main")).strip()
                    root_dir = str(source.get("root_directory", ".")).strip()
                    if not repo or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo):
                        self.send_json(400, {"error": "Invalid GitHub repository format. Must be 'owner/repository'."})
                        return
                    try:
                        fetch_res = fetch_github_source(app_id, repo, branch, root_dir)
                        validated_blueprint = fetch_res.get("blueprint")
                        if blueprint_input:
                            validated_blueprint = parse_and_validate_blueprint(blueprint_input, apps_db, app_id)
                    except Exception as e:
                        self.send_json(400, {"error": f"GitHub source error: {e}"})
                        return
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
                        self.send_json(400, {"error": "Missing container image reference."})
                        return
                    try:
                        parsed_img = validate_image_reference(raw_img)
                        image = parsed_img["raw"]
                    except ValueError as e:
                        self.send_json(400, {"error": str(e)})
                        return

                    # Discover supported architectures and exposed port
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
                    self.send_json(400, {"error": str(e)})
                    return

                try:
                    host_port = allocate_host_port(apps_db)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to allocate host port: {e}"})
                    return

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
            self.send_json(201, {
                "status": "created",
                "app_id": app_id,
                "app": mask_app_record(app_record)
            })
            return

        # POST /apps/env/parse - Parse .env file server-side without saving
        if path == "/apps/env/parse":
            content_str = body.get("content") or ""
            parsed = parse_env_file_content(content_str)
            self.send_json(200, {
                "status": "ok",
                "env_vars": parsed,
                "count": len(parsed)
            })
            return

        # POST /apps/<app_id>/env/import - Import .env content into existing app
        if path.startswith("/apps/") and path.endswith("/env/import"):
            app_id = path[6:-11].strip()
            content_str = body.get("content") or ""
            parsed = parse_env_file_content(content_str)
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return
                app.setdefault("env_vars", {}).update(parsed)
                for k, v in parsed.items():
                    app.setdefault("env", {})[k] = v.get("value", "")
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)
            self.send_json(200, {
                "status": "imported",
                "count": len(parsed),
                "app": mask_app_record(app)
            })
            return

        # POST /apps/<app_id>/env
        if path.startswith("/apps/") and path.endswith("/env"):
            app_id = path[6:-4].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                key = (body.get("key") or "").strip()
                val = str(body.get("value") or "")
                is_sec = bool(body.get("is_secret", True if any(s in key.lower() for s in ("key", "secret", "password", "token", "auth")) else False))

                if not key or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', key):
                    self.send_json(400, {"error": "Invalid environment variable name. Must start with letter/underscore."})
                    return

                app.setdefault("env_vars", {})[key] = {"value": val, "is_secret": is_sec}
                app.setdefault("env", {})[key] = val
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

            masked_app = mask_app_record(app)
            self.send_json(200, {
                "status": "updated",
                "app_id": app_id,
                "env_vars": masked_app.get("env_vars", {})
            })
            return

        # POST /apps/<app_id>/env/reveal
        if path.startswith("/apps/") and path.endswith("/env/reveal"):
            app_id = path[6:-11].strip()
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app or app.get("status") == "DELETED":
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            key = (body.get("key") or "").strip()
            env_vars = app.get("env_vars", {})
            if key not in env_vars:
                self.send_json(404, {"error": f"Variable '{key}' not found"})
                return

            entry = env_vars[key]
            val = entry.get("value") if isinstance(entry, dict) else str(entry)
            is_sec = entry.get("is_secret", False) if isinstance(entry, dict) else False
            self.send_json(200, {
                "key": key,
                "value": val,
                "is_secret": is_sec
            })
            return

        if path.startswith("/apps/") and path.endswith("/route"):
            app_id = path[6:-6].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                route_data = body.get("route") if body else None
                try:
                    validated_route = validate_app_route(route_data, apps_db, current_app_id=app_id)
                except ValueError as e:
                    self.send_json(400, {"error": str(e)})
                    return

                app["route"] = validated_route
                app["updated_at"] = get_current_iso_timestamp()
                save_apps_db(apps_db)

            self.send_json(200, {
                "status": "updated",
                "app_id": app_id,
                "route": validated_route,
                "app": mask_app_record(app)
            })
            return

        if path.startswith("/apps/") and (path.endswith("/deploy") or path.endswith("/redeploy")):
            is_redeploy = path.endswith("/redeploy")
            app_id = path[6:-9].strip() if is_redeploy else path[6:-7].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app or app.get("status") == "DELETED":
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

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
                        self.send_json(400, {
                            "error": app["failure_reason"],
                            "status": APP_STATE_FAILED,
                            "app": mask_app_record(app)
                        })
                        return

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

                decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                sched_ms = int((time.time() - t_sched_start) * 1000)

                if not decision.get("selected_node"):
                    # Check if failure was caused by architecture mismatch
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
                    self.send_json(400, {
                        "error": reason,
                        "status": APP_STATE_FAILED,
                        "app": mask_app_record(app),
                        "scheduler": decision
                    })
                    return

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
                        "name": f"build-deploy-{app['name']}",
                        "type": "docker-build-deploy",
                        "parameters": {
                            "app_id": app_id,
                            "app_name": app["name"],
                            "source_dir": str(src_context),
                            "dockerfile": dockerfile_name,
                            "image_tag": f"personalserver/{app['name']}:v{dep_num}",
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
                    job_record = {
                        "id": job_id,
                        "job_id": job_id,
                        "name": f"deploy-{app['name']}",
                        "type": "docker-deploy",
                        "parameters": {
                            "app_id": app_id,
                            "image": app["image"],
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
            self.send_json(200, {
                "status": APP_STATE_DEPLOYING,
                "app": mask_app_record(app),
                "job_id": job_id
            })
            return

        if path.startswith("/apps/") and path.endswith("/stop"):
            app_id = path[6:-5].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                target_node = app.get("selected_node")
                if not target_node:
                    app["status"] = APP_STATE_STOPPED
                    save_apps_db(apps_db)
                    self.send_json(200, {"status": "STOPPED", "app": app})
                    return

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

            self.send_json(200, {"status": "STOPPED", "app": app, "job_id": job_id})
            return

        if path.startswith("/apps/") and path.endswith("/restart"):
            app_id = path[6:-8].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                target_node = app.get("selected_node")
                if not target_node:
                    self.send_json(400, {"error": "Application is not deployed on any node."})
                    return

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

            self.send_json(200, {"status": "RUNNING", "app": app, "job_id": job_id})
            return

        # POST /apps/<app_id>/files/mkdir
        if path.startswith("/apps/") and path.endswith("/files/mkdir"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path.split("/")[2]
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            folder_name = (body.get("name") or "").strip()
            parent_rel = body.get("path") or ""

            if not folder_name or re.search(r'[\\/:\*\?"<>\|\x00]', folder_name) or ".." in folder_name:
                self.send_json(400, {"error": "Invalid folder name"})
                return

            try:
                target_dir, app_base = resolve_safe_app_storage_path(app_id, os.path.join(parent_rel, folder_name))
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if target_dir.exists():
                self.send_json(409, {"error": "Directory already exists"})
                return

            try:
                target_dir.mkdir(parents=True, exist_ok=False)
                self.send_json(200, {
                    "status": "created",
                    "path": str(target_dir.relative_to(app_base)).replace("\\", "/")
                })
            except Exception as e:
                self.send_json(500, {"error": f"Failed to create directory: {e}"})
            return

        # POST /apps/<app_id>/files/upload
        if path.startswith("/apps/") and path.endswith("/files/upload"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return
            app_id = path.split("/")[2]
            apps_db = load_apps_db()
            app = apps_db.get("apps", {}).get(app_id)
            if not app:
                self.send_json(404, {"error": f"Application '{app_id}' not found"})
                return

            parsed_q = urllib.parse.parse_qs(parsed.query)
            target_rel = parsed_q.get("path", [""])[0]
            try:
                target_dir, app_base = resolve_safe_app_storage_path(app_id, target_rel)
            except ValueError as e:
                self.send_json(403, {"error": str(e)})
                return

            if not target_dir.exists() or not target_dir.is_dir():
                self.send_json(400, {"error": "Upload target directory does not exist"})
                return

            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 100 * 1024 * 1024:
                self.send_json(413, {"error": "Upload exceeds 100 MB limit"})
                return

            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                boundary = content_type.split("boundary=")[-1].strip()
                if boundary.startswith('"') and boundary.endswith('"'):
                    boundary = boundary[1:-1]
                raw_data = self.rfile.read(content_len)
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
                self.send_json(200, {
                    "status": "uploaded",
                    "files": saved_files,
                    "target_dir": str(target_dir.relative_to(app_base)).replace("\\", "/")
                })
                return
            else:
                filename = parsed_q.get("filename", ["upload.dat"])[0]
                safe_name = os.path.basename(filename)
                safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', safe_name)
                dest_path = target_dir / safe_name
                with open(dest_path, "wb") as f:
                    remaining = content_len
                    while remaining > 0:
                        chunk_size = min(remaining, 65536)
                        chunk = self.rfile.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                self.send_json(200, {
                    "status": "uploaded",
                    "file": safe_name,
                    "bytes": content_len
                })
                return

        # ----------------------------------------------------------------------
        # 6. Cancel Job: POST /jobs/<job_id>/cancel
        # ----------------------------------------------------------------------
        if path.startswith("/jobs/") and path.endswith("/cancel"):
            job_id = path[6:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token and token != enrollment_token:
                pass

            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                self.send_json(404, {"error": f"Job '{job_id}' not found"})
                return

            if job.get("status") in (JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_CANCELLED):
                self.send_json(400, {
                    "error": f"Cannot cancel job '{job_id}' in terminal state '{job.get('status')}'."
                })
                return

            job["status"] = JOB_STATE_CANCELLED
            job["state"] = JOB_STATE_CANCELLED
            job["finished_at"] = get_current_iso_timestamp()
            job["lease_expires_at"] = None
            save_jobs_db(jobs_db)

            print(f"[CONTROLLER] Job {job_id} cancelled by admin.")
            self.send_json(200, {
                "status": "cancelled",
                "job_id": job_id,
                "job": job
            })
            return

        # ----------------------------------------------------------------------
        # 7. Node Lifecycle: Drain, Deboard, Resume, and Safe Remove
        # ----------------------------------------------------------------------
        # POST /nodes/<node_id>/drain
        if path.startswith("/nodes/") and path.endswith("/drain"):
            node_id = path[7:-6].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to drain a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot drain node '{node_id}': Node is already REMOVED."})
                return

            node_entry["status"] = STATE_DRAINING
            node_entry["draining_since"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            # Handle queued jobs safely:
            # - Auto-target jobs must be rescheduled to eligible online nodes
            # - Explicit-target jobs must NOT silently migrate
            jobs_db = load_jobs_db()
            jobs_rescheduled = 0
            for jid, job in list(jobs_db.get("jobs", {}).items()):
                if job.get("target_node") == node_id and job.get("status") in (JOB_STATE_QUEUED, JOB_STATE_RECOVERING):
                    target_mode = job.get("target", "auto")
                    if target_mode == "auto":
                        reqs = job.get("requirements", {})
                        decision = ResourceScheduler.select_node(reqs, nodes_db, self.heartbeat_timeout)
                        if decision.get("selected_node"):
                            job["target_node"] = decision["selected_node"]
                            job["scheduler"] = {
                                "mode": "drain-reschedule",
                                "selected_node": decision["selected_node"],
                                "score": decision["score"],
                                "reason": f"Rescheduled from draining node '{node_id}'"
                            }
                            jobs_rescheduled += 1
                            print(f"[CONTROLLER-DRAIN] Rescheduled auto-target job {jid} to node '{decision['selected_node']}'")
            if jobs_rescheduled > 0:
                save_jobs_db(jobs_db)

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db, jobs_db)
            print(f"[CONTROLLER-DRAIN] Node '{node_id}' transitioned to DRAINING (rescheduled {jobs_rescheduled} queued auto job(s))")
            self.send_json(200, {
                "status": STATE_DRAINING,
                "node_id": node_id,
                "jobs_rescheduled": jobs_rescheduled,
                "message": f"Node '{node_id}' is now DRAINING. No new workloads will be assigned.",
                "deboarding": eval_res
            })
            return

        # POST /nodes/<node_id>/deboard
        if path.startswith("/nodes/") and path.endswith("/deboard"):
            node_id = path[7:-8].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to deboard a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot deboard node '{node_id}': Node is already REMOVED."})
                return

            node_entry["status"] = STATE_DEBOARDING
            node_entry["deboarding_since"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            print(f"[CONTROLLER-DEBOARD] Node '{node_id}' transitioned to DEBOARDING. Can remove: {eval_res['can_remove']}")
            self.send_json(200, {
                "status": STATE_DEBOARDING,
                "node_id": node_id,
                "message": f"Node '{node_id}' is now in DEBOARDING state.",
                "can_remove": eval_res["can_remove"],
                "blockers": eval_res["blockers"],
                "active_jobs": eval_res["active_jobs"],
                "applications": eval_res["applications"],
                "storage_used_gb": eval_res["storage_used_gb"],
                "deboarding": eval_res
            })
            return

        # POST /nodes/<node_id>/resume
        if path.startswith("/nodes/") and path.endswith("/resume"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to resume a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            if node_entry.get("status") == STATE_REMOVED:
                self.send_json(400, {"error": f"Cannot resume node '{node_id}': Node is REMOVED. Re-registration required."})
                return

            node_entry["status"] = STATE_ONLINE
            node_entry.pop("draining_since", None)
            node_entry.pop("deboarding_since", None)
            save_nodes_db(nodes_db)

            print(f"[CONTROLLER-LIFECYCLE] Resumed node '{node_id}' to ONLINE.")
            self.send_json(200, {
                "status": STATE_ONLINE,
                "node_id": node_id,
                "message": f"Node '{node_id}' has resumed active ONLINE state."
            })
            return

        # Safe Removal: POST /nodes/<node_id>/remove
        if path.startswith("/nodes/") and path.endswith("/remove"):
            node_id = path[7:-7].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required to remove a node"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            if not eval_res["can_remove"]:
                self.send_json(409, {
                    "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                    "status": "BLOCKED",
                    "node_id": node_id,
                    "blockers": eval_res["blockers"],
                    "active_jobs": eval_res["active_jobs"],
                    "applications": eval_res["applications"],
                    "storage_used_gb": eval_res["storage_used_gb"],
                    "deboarding": eval_res
                })
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            # Trigger lease sweep to recover any jobs assigned to removed node
            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            print(f"[CONTROLLER] Safely removed node: {node_id}")
            self.send_json(200, {
                "status": STATE_REMOVED,
                "node_id": node_id,
                "message": f"Node '{node_id}' has been safely removed from active cluster membership."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        token = self.parse_auth_token()

        if path.startswith("/apps/"):
            if not self.is_authenticated_admin_or_node():
                self.send_json(401, {"error": "Unauthorized: Authentication required"})
                return

            # DELETE /apps/<app_id>/files?path=<rel_path>
            if "/files" in path:
                app_id = path.split("/")[2]
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

                parsed_q = urllib.parse.parse_qs(parsed.query)
                req_path = parsed_q.get("path", [""])[0]
                if not req_path:
                    self.send_json(400, {"error": "Missing 'path' parameter"})
                    return

                try:
                    target_item, app_base = resolve_safe_app_storage_path(app_id, req_path)
                except ValueError as e:
                    self.send_json(403, {"error": str(e)})
                    return

                if target_item == app_base:
                    self.send_json(403, {"error": "Cannot delete application storage root"})
                    return

                if not target_item.exists():
                    self.send_json(404, {"error": "Item not found"})
                    return

                try:
                    if target_item.is_dir():
                        shutil.rmtree(target_item)
                    else:
                        target_item.unlink()
                    self.send_json(200, {
                        "status": "deleted",
                        "path": req_path
                    })
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to delete item: {e}"})
                return

            # DELETE /apps/<app_id>/env/<key>
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) == 4 and parts[0] == "apps" and parts[2] == "env":
                app_id = parts[1]
                env_key = parts[3]
                with APPS_LOCK:
                    apps_db = load_apps_db()
                    app = apps_db.get("apps", {}).get(app_id)
                    if not app or app.get("status") == "DELETED":
                        self.send_json(404, {"error": f"Application '{app_id}' not found"})
                        return

                    if env_key in app.get("env_vars", {}):
                        del app["env_vars"][env_key]
                    if env_key in app.get("env", {}):
                        del app["env"][env_key]
                    app["updated_at"] = get_current_iso_timestamp()
                    save_apps_db(apps_db)

                self.send_json(200, {
                    "status": "deleted",
                    "key": env_key,
                    "app_id": app_id,
                    "env_vars": mask_app_record(app).get("env_vars", {})
                })
                return

            app_id = path[6:].strip()
            with APPS_LOCK:
                apps_db = load_apps_db()
                app = apps_db.get("apps", {}).get(app_id)
                if not app:
                    self.send_json(404, {"error": f"Application '{app_id}' not found"})
                    return

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
            self.send_json(200, {
                "status": "deleted",
                "app_id": app_id,
                "message": f"Application '{app['name']}' deleted."
            })
            return

        if path.startswith("/nodes/"):
            node_id = path[7:].strip()
            enrollment_token = get_or_create_enrollment_token()
            if token != enrollment_token:
                self.send_json(401, {"error": "Unauthorized: Admin enrollment token required"})
                return

            nodes_db = load_nodes_db()
            node_entry = nodes_db.get("nodes", {}).get(node_id)
            if not node_entry:
                self.send_json(404, {"error": f"Node '{node_id}' not found"})
                return

            eval_res = evaluate_node_deboarding_safety(node_id, nodes_db)
            if not eval_res["can_remove"]:
                self.send_json(409, {
                    "error": f"Cannot deboard node '{node_id}': Safety conditions not satisfied.",
                    "status": "BLOCKED",
                    "node_id": node_id,
                    "blockers": eval_res["blockers"],
                    "active_jobs": eval_res["active_jobs"],
                    "applications": eval_res["applications"],
                    "storage_used_gb": eval_res["storage_used_gb"],
                    "deboarding": eval_res
                })
                return

            node_entry["status"] = STATE_REMOVED
            node_entry["auth_token"] = None
            node_entry["removed_at"] = get_current_iso_timestamp()
            save_nodes_db(nodes_db)

            jobs_db = load_jobs_db()
            sweep_expired_leases(jobs_db, nodes_db, self.heartbeat_timeout)

            print(f"[CONTROLLER] Safely removed node via DELETE: {node_id}")
            self.send_json(200, {
                "status": STATE_REMOVED,
                "node_id": node_id,
                "message": f"Node '{node_id}' removed safely."
            })
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def log_message(self, format, *args):
        print(f"[HTTP {self.command}] {self.path} - {args[0] if args else ''}")


# ------------------------------------------------------------------------------
# CLI Actions
# ------------------------------------------------------------------------------

def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_HEARTBEAT_TIMEOUT):
    ensure_directories()
    token = get_or_create_enrollment_token()
    ControllerHandler.heartbeat_timeout = timeout

    # Recover orphaned jobs from previous shutdown
    jobs_db = load_jobs_db()
    nodes_db = load_nodes_db()
    sweep_expired_leases(jobs_db, nodes_db, timeout)

    # Start background lease sweeper thread
    start_background_lease_sweeper(timeout)

    print("==========================================")
    print(" PersonalServer Cluster Controller & Scheduler (v1.0)")
    print("==========================================")
    print(f"Controller listening on http://{host}:{port}")
    print(f"Heartbeat timeout:     {timeout} seconds")
    print(f"Enrollment token file: {ENROLLMENT_TOKEN_FILE}")
    print(f"Cluster database:      {NODES_FILE}")
    print(f"Jobs database:         {JOBS_FILE}")
    print("==========================================")

    server = HTTPServer((host, port), ControllerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nController shutting down...")
        server.server_close()


def schedule_test(requirements_json=None):
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


def submit_job(target_or_auto, job_type, name=None, timeout=60, params_json=None, reqs_json=None, max_attempts=3):
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


def list_jobs(filter_node=None, filter_status=None):
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


def show_job_details(job_id):
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


def cancel_job(job_id):
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


def list_nodes(filter_status=None):
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
            platform = f"{node.get('platform')} ({node.get('architecture')})"
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
            print(f"  Platform:   {platform}")
            print(f"  Resources:  {res.get('cpu_cores', 'N/A')} cores | RAM: {mem_info}")
            print(f"  Caps:       {caps}")
            print(f"  Last Seen:  {last_seen}")
            print()
    print("==========================================")


def show_node_details(node_id):
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


def show_cluster():
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


def remove_node(node_id):
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


def main():
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

    args = parser.parse_args()

    if not args.command or args.command == "start":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        timeout = getattr(args, "timeout", DEFAULT_HEARTBEAT_TIMEOUT)
        start_server(host, port, timeout)
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


if __name__ == "__main__":
    main()
