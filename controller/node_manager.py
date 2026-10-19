#!/usr/bin/env python3
"""
Controller Node Manager & Cluster Telemetry Subsystem
=====================================================
Node state sanitization, storage isolation inspection, safe deboarding verification,
cluster inventory summaries, and real-time cluster resource utilization metrics.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths, get_controller_runtime_config
from controller.constants import (
    APP_STATE_DEPLOYING,
    APP_STATE_RUNNING,
    JOB_STATE_CLAIMED,
    JOB_STATE_RECOVERING,
    JOB_STATE_RUNNING,
    STATE_DEBOARDING,
    STATE_DRAINING,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_REMOVED,
    STATE_UNHEALTHY,
    STATE_UNKNOWN,
)
from controller.stores import load_apps_db, load_jobs_db, load_nodes_db
from scheduler.scheduler import compute_node_liveness, extract_node_telemetry

BASE_DIR = Path(__file__).resolve().parent.parent
PATHS = get_controller_paths()
CONFIG_DIR = PATHS.config_dir
APP_STORAGE_ROOT = (PATHS.storage_dir / "applications").resolve()
RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout  # seconds


def sanitize_node_record(node: Dict[str, Any], timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT) -> Dict[str, Any]:
    """Returns a sanitized copy of node record without credentials, with computed liveness status."""
    node_copy = dict(node)
    node_copy.pop("auth_token", None)
    node_copy["status"] = compute_node_liveness(node, timeout_seconds)
    return node_copy


def inspect_node_storage_safety(node_id: str, node_entry: Dict[str, Any], associated_apps: List[Tuple[str, Dict[str, Any]]]) -> Tuple[int, int]:
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


def evaluate_node_deboarding_safety(
    node_id: str,
    nodes_db: Optional[Dict[str, Any]] = None,
    jobs_db: Optional[Dict[str, Any]] = None,
    apps_db: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
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
    blockers: List[str] = []

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


def get_cluster_summary(db: Dict[str, Any], timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT) -> Dict[str, Any]:
    """Generates an inventory summary of cluster nodes grouped by state."""
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


def get_cluster_utilization(
    nodes_db: Optional[Dict[str, Any]] = None,
    jobs_db: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT
) -> Dict[str, Any]:
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
    jobs_by_node: Dict[str, list] = {}
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


__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "sanitize_node_record",
    "inspect_node_storage_safety",
    "evaluate_node_deboarding_safety",
    "get_cluster_summary",
    "get_cluster_utilization",
]
