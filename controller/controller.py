#!/usr/bin/env python3
"""
PersonalServer Controller
=========================
Thin application entry point and backward-compatibility facade for PersonalServer Controller.

Modular architecture:
- `controller/constants.py`: Cluster states, job states, transitions, port ranges, workload allowlist.
- `controller/ha_helper.py`: High Availability manager, leader lease tracking, consensus helper.
- `controller/blueprints.py`: Blueprint validation, app routing, GitHub source fetching, app masking.
- `controller/onboarding.py`: Temporary onboarding codes, enrollment tokens.
- `controller/node_manager.py`: Node sanitization, telemetry extraction, deboarding safety, summaries.
- `controller/lease_sweeper.py`: Lease sweeping, automatic failure recovery, background sweep thread.
- `controller/stores/`: Atomic, thread-safe JSON persistence for nodes, jobs, apps, and codes.
- `controller/http/`: Standardized BaseHandler, domain route handlers (HA, Node, Job, App), ControllerHandler.
- `controller/cli.py`: Administrative CLI dispatch and command implementations.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Platform Configuration & Paths
from common.crypto_utils import constant_time_compare
from config.platform_config import (
    get_controller_paths,
    get_controller_runtime_config,
    get_platform_config,
    redact_sensitive_dict,
)
from config.structured_logger import (
    controller_logger,
    extract_correlation_id,
    generate_correlation_id,
    redact_sensitive_data,
    redact_sensitive_text,
)

PATHS = get_controller_paths()
CONFIG_DIR = PATHS.config_dir
SECRETS_DIR = PATHS.secrets_dir
CONTROLLER_DIR = BASE_DIR / "controller"
DATA_DIR = PATHS.data_dir

NODES_FILE = PATHS.nodes_file
BACKUP_NODES_FILE = PATHS.backup_nodes_file
JOBS_FILE = PATHS.jobs_file
BACKUP_JOBS_FILE = PATHS.backup_jobs_file
APPS_FILE = PATHS.apps_file
BACKUP_APPS_FILE = PATHS.backup_apps_file
ONBOARDING_CODES_FILE = PATHS.onboarding_codes_file
BACKUP_ONBOARDING_CODES_FILE = PATHS.backup_onboarding_codes_file
ENROLLMENT_TOKEN_FILE = PATHS.enrollment_token_file

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HOST = RUNTIME_CFG.host
DEFAULT_PORT = RUNTIME_CFG.port
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout
DEFAULT_LEASE_GRACE_SEC = RUNTIME_CFG.lease_grace_sec
DEFAULT_ONBOARDING_TTL_SEC = RUNTIME_CFG.onboarding_ttl_sec

# Persistence Stores
from controller.stores import (
    APPS_LOCK,
    DB_LOCK,
    ONBOARDING_LOCK,
    load_apps_db,
    load_jobs_db,
    load_nodes_db,
    load_onboarding_codes_db,
    save_apps_db,
    save_jobs_db,
    save_nodes_db,
    save_onboarding_codes_db,
    set_state_version_bumper,
)

# Constants & State Enums
from controller.constants import (
    ALLOWLISTED_WORKLOADS,
    APP_STATE_BUILDING,
    APP_STATE_CONFIGURING,
    APP_STATE_CREATED,
    APP_STATE_DEPLOYING,
    APP_STATE_FAILED,
    APP_STATE_FETCHING_SOURCE,
    APP_STATE_REMOVING,
    APP_STATE_RUNNING,
    APP_STATE_SCHEDULING,
    APP_STATE_STOPPED,
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RECOVERING,
    JOB_STATE_REJECTED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_TIMEOUT,
    PORT_RANGE_END,
    PORT_RANGE_START,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_REGISTERING,
    STATE_REMOVED,
    STATE_UNHEALTHY,
    STATE_UNKNOWN,
    VALID_JOB_TRANSITIONS,
    VALID_ROLES,
    validate_job_transition,
)

# High Availability (HA) Helper
from controller.ha_helper import (
    HA_MANAGER,
    HAManager,
    HAState,
    LeaderLease,
    _bump_ha_state_version,
    find_active_leader,
    get_ha_manager,
    resolve_controller_node_id,
)

# Blueprints & Application Deployment Helpers
from controller.blueprints import (
    APP_STORAGE_ROOT,
    RESERVED_ROUTES,
    allocate_host_port,
    fetch_github_source,
    mask_app_record,
    parse_and_validate_blueprint,
    parse_yaml_or_json,
    resolve_safe_app_storage_path,
    validate_app_route,
)

# Node Onboarding & Enrollment
from controller.onboarding import (
    generate_onboarding_code,
    get_or_create_enrollment_token,
    hash_onboarding_code,
    validate_and_consume_onboarding_code,
)

# Node Management & Telemetry
from controller.node_manager import (
    evaluate_node_deboarding_safety,
    get_cluster_summary,
    get_cluster_utilization,
    inspect_node_storage_safety,
    sanitize_node_record,
)

# Lease Sweeping & Automatic Recovery
from controller.lease_sweeper import (
    start_background_lease_sweeper,
    sweep_expired_leases,
)

# Scheduler
from scheduler.scheduler import (
    ResourceScheduler,
    compute_node_liveness,
    extract_node_telemetry,
)

# HTTP Request Handlers
from controller.http import (
    BaseHandler,
    ControllerHandler,
)

# CLI Administration & Server Launcher
from controller.cli import (
    cancel_job,
    list_jobs,
    list_nodes,
    main,
    remove_node,
    schedule_test,
    show_cluster,
    show_job_details,
    show_node_details,
    start_server,
    submit_job,
)


def ensure_directories() -> None:
    """Ensures controller state and persistence directories exist."""
    PATHS.ensure_directories()


__all__ = [
    # HTTP Handler & Server
    "ControllerHandler",
    "BaseHandler",
    "start_server",
    "main",
    "ensure_directories",
    # Stores
    "load_nodes_db",
    "save_nodes_db",
    "load_jobs_db",
    "save_jobs_db",
    "load_apps_db",
    "save_apps_db",
    "load_onboarding_codes_db",
    "save_onboarding_codes_db",
    "DB_LOCK",
    "ONBOARDING_LOCK",
    "APPS_LOCK",
    "set_state_version_bumper",
    # Constants & Transitions
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
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "DEFAULT_LEASE_GRACE_SEC",
    "DEFAULT_ONBOARDING_TTL_SEC",
    # Subsystem Helpers
    "constant_time_compare",
    "get_ha_manager",
    "HAManager",
    "HAState",
    "LeaderLease",
    "find_active_leader",
    "resolve_controller_node_id",
    "_bump_ha_state_version",
    "mask_app_record",
    "validate_app_route",
    "allocate_host_port",
    "fetch_github_source",
    "parse_and_validate_blueprint",
    "parse_yaml_or_json",
    "resolve_safe_app_storage_path",
    "generate_onboarding_code",
    "get_or_create_enrollment_token",
    "hash_onboarding_code",
    "validate_and_consume_onboarding_code",
    "sanitize_node_record",
    "inspect_node_storage_safety",
    "evaluate_node_deboarding_safety",
    "get_cluster_summary",
    "get_cluster_utilization",
    "sweep_expired_leases",
    "start_background_lease_sweeper",
    "ResourceScheduler",
    "compute_node_liveness",
    "extract_node_telemetry",
    # CLI Commands
    "schedule_test",
    "submit_job",
    "list_jobs",
    "show_job_details",
    "cancel_job",
    "list_nodes",
    "show_node_details",
    "show_cluster",
    "remove_node",
]

if __name__ == "__main__":
    main()
