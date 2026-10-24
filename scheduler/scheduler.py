#!/usr/bin/env python3
"""
PersonalServer Resource-Aware Scheduler
=======================================
Deterministic, explainable node selection based on node eligibility,
capabilities, roles, and available system resources.
"""

import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.arch_utils import ARCH_MAP, normalize_architecture

# Centralized Node State Constants
STATE_ONLINE = "ONLINE"
STATE_DRAINING = "DRAINING"
STATE_DEBOARDING = "DEBOARDING"
STATE_OFFLINE = "OFFLINE"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_REMOVED = "REMOVED"
STATE_UNKNOWN = "UNKNOWN"

DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds


def parse_memory_mb(mem_str):
    """
    Parse memory string (e.g. '2.5Gi/5.6Gi', '5697', '512MB') and return estimated
    (total_mb, used_mb, available_mb).
    """
    if isinstance(mem_str, (int, float)):
        return float(mem_str), 0.0, float(mem_str)

    if not mem_str or not isinstance(mem_str, str):
        return 0.0, 0.0, 0.0

    def unit_to_mb(val, unit):
        val = float(val)
        unit = unit.upper()
        if "G" in unit:
            return val * 1024.0
        elif "M" in unit:
            return val
        elif "K" in unit:
            return val / 1024.0
        elif "T" in unit:
            return val * 1024.0 * 1024.0
        return val

    # Format: "used/total" e.g. "2.6Gi/5.6Gi"
    if "/" in mem_str:
        parts = mem_str.split("/")
        u_match = re.search(r"([\d.]+)\s*([a-zA-Z]*)", parts[0])
        t_match = re.search(r"([\d.]+)\s*([a-zA-Z]*)", parts[1])
        used_mb = unit_to_mb(u_match.group(1), u_match.group(2)) if u_match else 0.0
        total_mb = unit_to_mb(t_match.group(1), t_match.group(2)) if t_match else used_mb
        available_mb = max(0.0, total_mb - used_mb)
        return total_mb, used_mb, available_mb

    m = re.search(r"([\d.]+)\s*([a-zA-Z]*)", mem_str)
    if m:
        mb = unit_to_mb(m.group(1), m.group(2))
        return mb, 0.0, mb
    return 0.0, 0.0, 0.0


def parse_storage_gb(storage_str):
    """Parse storage string (e.g. '10G/107G (10%)', '106') and return (total_gb, used_gb, available_gb)."""
    if isinstance(storage_str, (int, float)):
        return float(storage_str), 0.0, float(storage_str)

    if not storage_str or not isinstance(storage_str, str):
        return 0.0, 0.0, 0.0

    def unit_to_gb(val, unit):
        val = float(val)
        unit = unit.upper()
        if "T" in unit:
            return val * 1024.0
        elif "G" in unit:
            return val
        elif "M" in unit:
            return val / 1024.0
        return val

    if "/" in storage_str:
        parts = storage_str.split("/")
        u_match = re.search(r"([\d.]+)\s*([a-zA-Z]*)", parts[0])
        t_match = re.search(r"([\d.]+)\s*([a-zA-Z]*)", parts[1])
        used_gb = unit_to_gb(u_match.group(1), u_match.group(2)) if u_match else 0.0
        total_gb = unit_to_gb(t_match.group(1), t_match.group(2)) if t_match else used_gb
        available_gb = max(0.0, total_gb - used_gb)
        return total_gb, used_gb, available_gb

    m = re.search(r"([\d.]+)\s*([a-zA-Z]*)", storage_str)
    if m:
        gb = unit_to_gb(m.group(1), m.group(2))
        return gb, 0.0, gb
    return 0.0, 0.0, 0.0


def compute_node_liveness(node, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
    """Evaluate live status based on heartbeat timestamp and service states."""
    if node.get("status") == STATE_REMOVED:
        return STATE_REMOVED

    last_seen_str = node.get("last_seen")
    if not last_seen_str:
        return STATE_UNKNOWN

    try:
        if isinstance(last_seen_str, (int, float)):
            last_seen_dt = datetime.fromtimestamp(last_seen_str, tz=timezone.utc)
        else:
            last_seen_dt = datetime.fromisoformat(str(last_seen_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta_seconds = (now - last_seen_dt).total_seconds()

        if delta_seconds > timeout_seconds:
            return STATE_OFFLINE

        last_hb = node.get("last_heartbeat", {})
        services = last_hb.get("services", {})
        if services and any(v in ("unresponsive", "degraded", "stopped") for v in services.values()):
            return STATE_UNHEALTHY

        node_status = node.get("status")
        if node_status in (STATE_DRAINING, STATE_DEBOARDING):
            return node_status

        return STATE_ONLINE
    except Exception:
        return STATE_UNKNOWN


def extract_node_telemetry(node, status=None, node_jobs=None):
    """
    Extracts standardized, cross-platform resource and workload telemetry for a node.
    Distinguishes Capacity, Usage, and Available without inventing metrics.
    """
    if not node or not isinstance(node, dict):
        return {
            "status": status or STATE_UNKNOWN,
            "cpu": {"cores": 0},
            "memory": {"total_mb": 0.0, "used_mb": 0.0, "available_mb": 0.0, "used_percent": 0.0},
            "storage": {"total_gb": 0.0, "used_gb": 0.0, "available_gb": 0.0, "used_percent": 0.0},
            "workloads": {"active_jobs": 0, "running_jobs": 0, "running_containers": 0},
            "capabilities": {}
        }

    liveness = status or compute_node_liveness(node)
    resources = node.get("resources", {}) or {}
    last_hb = node.get("last_heartbeat", {}) or {}
    hb_system = last_hb.get("system", {}) or {}
    hb_workloads = last_hb.get("workloads", {}) or {}

    # CPU
    try:
        cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))
    except (ValueError, TypeError):
        cpu_cores = 1

    cpu_info = {"cores": max(0, cpu_cores)}

    load_avg = hb_system.get("load_average")
    if load_avg is not None and isinstance(load_avg, list) and len(load_avg) > 0:
        try:
            cpu_info["load_average"] = [float(x) for x in load_avg]
        except (ValueError, TypeError):
            pass

    cpu_pct = hb_system.get("cpu_percent")
    if cpu_pct is not None and isinstance(cpu_pct, (int, float)):
        cpu_info["cpu_percent"] = round(float(cpu_pct), 1)

    # Memory
    mem_details = hb_system.get("memory_details")
    if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
        try:
            total_mem_mb = float(mem_details.get("total_mb", 0.0))
            used_mem_mb = float(mem_details.get("used_mb", 0.0))
            avail_mem_mb = float(mem_details.get("available_mb", 0.0))
            mem_pct = float(mem_details.get("used_percent", 0.0))
        except (ValueError, TypeError):
            total_mem_mb, used_mem_mb, avail_mem_mb, mem_pct = 0.0, 0.0, 0.0, 0.0
    else:
        mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
        total_mem_mb, used_mem_mb, avail_mem_mb = parse_memory_mb(mem_data)
        mem_pct = round((used_mem_mb / total_mem_mb * 100), 1) if total_mem_mb > 0 else 0.0

    memory_info = {
        "total_mb": max(0.0, round(total_mem_mb, 1)),
        "used_mb": max(0.0, round(used_mem_mb, 1)),
        "available_mb": max(0.0, round(avail_mem_mb, 1)),
        "used_percent": max(0.0, min(100.0, round(mem_pct, 1)))
    }

    # Storage
    st_details = hb_system.get("storage_details")
    if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
        try:
            total_st_gb = float(st_details.get("total_gb", 0.0))
            used_st_gb = float(st_details.get("used_gb", 0.0))
            avail_st_gb = float(st_details.get("available_gb", 0.0))
            st_pct = float(st_details.get("used_percent", 0.0))
        except (ValueError, TypeError):
            total_st_gb, used_st_gb, avail_st_gb, st_pct = 0.0, 0.0, 0.0, 0.0
    else:
        st_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
        total_st_gb, used_st_gb, avail_st_gb = parse_storage_gb(st_data)
        st_pct = round((used_st_gb / total_st_gb * 100), 1) if total_st_gb > 0 else 0.0

    storage_info = {
        "total_gb": max(0.0, round(total_st_gb, 1)),
        "used_gb": max(0.0, round(used_st_gb, 1)),
        "available_gb": max(0.0, round(avail_st_gb, 1)),
        "used_percent": max(0.0, min(100.0, round(st_pct, 1)))
    }

    # Workloads
    try:
        running_containers = int(hb_workloads.get("running_containers", 0))
    except (ValueError, TypeError):
        running_containers = 0

    active_jobs = 0
    running_jobs = 0
    if node_jobs and isinstance(node_jobs, list):
        for j in node_jobs:
            st = j.get("status")
            if st in ("CLAIMED", "RUNNING", "RECOVERING"):
                active_jobs += 1
            if st == "RUNNING":
                running_jobs += 1

    workloads_info = {
        "active_jobs": active_jobs,
        "running_jobs": running_jobs,
        "running_containers": max(0, running_containers)
    }

    capabilities_info = node.get("capabilities", {}) or {}

    battery_info = hb_system.get("battery") or last_hb.get("battery") or {}
    network_info = hb_system.get("network") or last_hb.get("network") or {}
    thermals_info = hb_system.get("thermals") or last_hb.get("thermals") or {}
    cpu_details_info = hb_system.get("cpu_details") or last_hb.get("cpu_details") or {}

    if "temperature_c" in thermals_info and thermals_info["temperature_c"] is not None:
        cpu_info["temperature_c"] = thermals_info["temperature_c"]
        cpu_info["thermal_status"] = thermals_info.get("status", "Normal")

    return {
        "status": liveness,
        "cpu": cpu_info,
        "memory": memory_info,
        "storage": storage_info,
        "workloads": workloads_info,
        "capabilities": capabilities_info,
        "battery": battery_info,
        "network": network_info,
        "thermals": thermals_info,
        "cpu_details": cpu_details_info
    }


def extract_node_capabilities(node: Dict[str, Any]) -> Tuple[Dict[str, bool], Dict[str, bool]]:
    """
    Extracts (advertised_capabilities, verified_capabilities) from a node record.
    Distinguishes advertised capabilities from capabilities actually verified by the worker.
    Mere reporting in capabilities does not classify as verified unless explicitly verified.
    """
    advertised: Dict[str, bool] = {}
    verified: Dict[str, bool] = {}

    if not node or not isinstance(node, dict):
        return advertised, verified

    # 1. Parse advertised / reported capabilities
    raw_caps = node.get("advertised_capabilities")
    if raw_caps is None:
        raw_caps = node.get("capabilities", {})

    if isinstance(raw_caps, list):
        for c in raw_caps:
            advertised[str(c)] = True
    elif isinstance(raw_caps, dict):
        for k, v in raw_caps.items():
            if isinstance(v, dict):
                advertised[str(k)] = bool(v.get("enabled", True) if "enabled" in v else v.get("advertised", True))
                if v.get("verified") is True:
                    verified[str(k)] = True
            else:
                advertised[str(k)] = bool(v)

    # 2. Parse verified capabilities
    raw_verified = node.get("verified_capabilities")
    if isinstance(raw_verified, list):
        for c in raw_verified:
            verified[str(c)] = True
    elif isinstance(raw_verified, dict):
        for k, v in raw_verified.items():
            if isinstance(v, dict):
                if v.get("verified") is True:
                    verified[str(k)] = True
            elif bool(v):
                verified[str(k)] = True

    return advertised, verified


class ResourceScheduler:
    """
    Deterministic, resource-aware scheduler that filters eligible nodes
    and scores them based on available memory, CPU load, and storage,
    accounting for active job resource reservations and concurrency limits.
    """

    @classmethod
    def is_node_eligible(cls, node, job_requirements=None, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT, reserved_mem_mb=0.0):
        """
        Check if a single node satisfies all hard constraints of job_requirements.
        Returns (is_eligible: bool, reason: str).
        """
        if not node or not isinstance(node, dict):
            return False, "Node record is missing or invalid"

        reqs = job_requirements or {}
        if "requirements" in reqs and isinstance(reqs.get("requirements"), dict):
            reqs = reqs["requirements"]

        liveness = compute_node_liveness(node, timeout_seconds)
        node_status = node.get("status")
        if liveness in (STATE_REMOVED, STATE_DRAINING, STATE_DEBOARDING, STATE_OFFLINE, STATE_UNHEALTHY, STATE_UNKNOWN) or node_status in (STATE_REMOVED, STATE_DRAINING, STATE_DEBOARDING):
            return False, f"Node status is {liveness or node_status}"

        # 1. Role
        req_role = reqs.get("role")
        if req_role:
            node_role = node.get("role", "compute").lower()
            if node_role != req_role.lower() and node_role != "hybrid":
                return False, f"Role mismatch: node is '{node_role}', required '{req_role}'"

        # 2. Capabilities (Advertised vs Verified)
        advertised_caps, verified_caps = extract_node_capabilities(node)
        require_verified = bool(reqs.get("require_verified", False))

        # Check required capabilities
        req_caps = reqs.get("capabilities", [])
        if isinstance(req_caps, str):
            req_caps = [req_caps]

        # In standard mode, a capability is supported if present in advertised or verified capabilities
        missing = [c for c in req_caps if not advertised_caps.get(c, False) and not verified_caps.get(c, False)]
        if missing:
            return False, f"Missing required capabilities: {missing}"

        # If require_verified is requested, all capabilities in 'capabilities' must be verified
        if require_verified:
            unverified = [c for c in req_caps if not verified_caps.get(c, False)]
            if unverified:
                return False, f"Capabilities advertised but unverified by worker: {unverified}"

        # Explicit verified_capabilities requirement
        req_verified = reqs.get("verified_capabilities", [])
        if isinstance(req_verified, str):
            req_verified = [req_verified]
        missing_verified = [c for c in req_verified if not verified_caps.get(c, False)]
        if missing_verified:
            adv_unverified = [c for c in missing_verified if advertised_caps.get(c, False)]
            if adv_unverified:
                return False, f"Capabilities advertised but unverified by worker: {adv_unverified}"
            return False, f"Missing required verified capabilities: {missing_verified}"

        # Any capabilities (at least one must match)
        req_any = reqs.get("any_capabilities")
        if req_any and isinstance(req_any, list):
            check_caps = verified_caps if require_verified else {**advertised_caps, **verified_caps}
            if not any(check_caps.get(c, False) for c in req_any):
                if require_verified:
                    return False, f"Missing any of required verified capabilities: {req_any}"
                return False, f"Missing any of required capabilities: {req_any}"

        req_any_verified = reqs.get("any_verified_capabilities")
        if req_any_verified and isinstance(req_any_verified, list):
            if not any(verified_caps.get(c, False) for c in req_any_verified):
                return False, f"Missing any of required verified capabilities: {req_any_verified}"

        # 3. Architecture & Platform
        node_raw_arch = node.get("architecture") or node.get("system", {}).get("arch") or node.get("system", {}).get("architecture", "")
        req_arch = reqs.get("architecture")
        if req_arch:
            norm_req = normalize_architecture(req_arch)
            norm_node = normalize_architecture(node_raw_arch)
            if norm_req and norm_node and norm_req != norm_node:
                return False, f"Architecture mismatch: node is '{node_raw_arch}', required '{req_arch}'"

        supp_archs = reqs.get("supported_architectures")
        if supp_archs and isinstance(supp_archs, list):
            norm_supp = {normalize_architecture(a) for a in supp_archs if a}
            norm_node = normalize_architecture(node_raw_arch)
            if norm_node and norm_supp and norm_node not in norm_supp:
                return False, "Image does not support this node architecture."

        req_platform = reqs.get("platform")
        if req_platform and node.get("platform", "").lower() != req_platform.lower():
            return False, f"Platform mismatch: node is '{node.get('platform')}', required '{req_platform}'"

        # 4. Resources: CPU Cores, Memory, Storage
        resources = node.get("resources", {}) or {}
        last_hb = node.get("last_heartbeat", {}) or {}
        hb_system = last_hb.get("system", {}) or {}

        # CPU Cores
        min_cpu = int(reqs.get("min_cpu_cores", 0) or reqs.get("cpu_cores", 0) or reqs.get("min_cpu", 0))
        if min_cpu > 0:
            cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))
            if cpu_cores < min_cpu:
                return False, f"Insufficient CPU cores: node has {cpu_cores}, required {min_cpu}"

        # Memory (MB)
        min_mem = float(reqs.get("min_memory_mb", 0) or reqs.get("memory_mb", 0) or reqs.get("min_memory", 0))
        if min_mem > 0:
            mem_details = hb_system.get("memory_details")
            if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
                avail_mem_mb = float(mem_details.get("available_mb", 0.0))
            else:
                mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
                _, _, avail_mem_mb = parse_memory_mb(mem_data)

            effective_avail_mem_mb = max(0.0, avail_mem_mb - reserved_mem_mb)
            if effective_avail_mem_mb < min_mem:
                if reserved_mem_mb > 0:
                    return False, f"Insufficient unreserved memory: ~{effective_avail_mem_mb:.0f} MB available (after {reserved_mem_mb:.0f}MB active reservations), required {min_mem:.0f} MB"
                return False, f"Insufficient available memory: ~{effective_avail_mem_mb:.0f} MB available, required {min_mem:.0f} MB"

        # Storage (GB)
        min_st = float(reqs.get("min_storage_gb", 0) or reqs.get("storage_gb", 0) or reqs.get("min_storage", 0))
        if min_st > 0:
            st_details = hb_system.get("storage_details")
            if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
                avail_st_gb = float(st_details.get("available_gb", 0.0))
            else:
                storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
                _, _, avail_st_gb = parse_storage_gb(storage_data)

            if avail_st_gb < min_st:
                return False, f"Insufficient available storage: ~{avail_st_gb:.1f} GB available, required {min_st:.1f} GB"

        return True, "Eligible"

    @staticmethod
    def select_node(job_requirements=None, nodes_db=None, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT, cluster_jobs=None):
        """
        Evaluate all cluster nodes and select the highest-scoring eligible node.
        Accounts for active job count and memory reservations.
        """
        # Support flexible parameter ordering if caller passed nodes_db first
        if isinstance(job_requirements, dict) and "nodes" in job_requirements and (nodes_db is None or not isinstance(nodes_db, dict) or "nodes" not in nodes_db):
            job_requirements, nodes_db = nodes_db, job_requirements

        reqs = job_requirements or {}
        nodes = (nodes_db or {}).get("nodes", {})
        if not nodes and isinstance(nodes_db, dict) and any(isinstance(v, dict) and ("status" in v or "capabilities" in v) for v in nodes_db.values()):
            nodes = nodes_db

        if not nodes:
            return {
                "selected_node": None,
                "score": 0.0,
                "reason": "No registered nodes exist in cluster.",
                "candidates": [],
                "rejected": {}
            }

        # Calculate reservations from active in-flight jobs
        reserved_by_node = {}
        active_counts = {}
        if cluster_jobs and isinstance(cluster_jobs, dict):
            for j in cluster_jobs.values():
                nid = j.get("target_node") or j.get("assigned_node")
                st = str(j.get("status", "")).upper()
                if nid and st in ("ASSIGNED", "CLAIMED", "RUNNING"):
                    j_reqs = j.get("requirements", {}) or {}
                    j_mem = float(j_reqs.get("min_memory_mb") or j_reqs.get("memory_mb") or 0.0)
                    reserved_by_node[nid] = reserved_by_node.get(nid, 0.0) + j_mem
                    active_counts[nid] = active_counts.get(nid, 0) + 1

        candidates = []
        rejected = {}

        for node_id, node in nodes.items():
            reserved_mem = reserved_by_node.get(node_id, 0.0)
            eligible, reason = ResourceScheduler.is_node_eligible(
                node,
                reqs,
                timeout_seconds=timeout_seconds,
                reserved_mem_mb=reserved_mem
            )
            if not eligible:
                rejected[node_id] = reason
                continue

            # Resources extraction for scoring
            resources = node.get("resources", {}) or {}
            last_hb = node.get("last_heartbeat", {}) or {}
            hb_system = last_hb.get("system", {}) or {}

            cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))

            mem_details = hb_system.get("memory_details")
            if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
                total_mem_mb = float(mem_details.get("total_mb", 0.0))
                used_mem_mb = float(mem_details.get("used_mb", 0.0))
                avail_mem_mb = float(mem_details.get("available_mb", 0.0))
            else:
                mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
                total_mem_mb, used_mem_mb, avail_mem_mb = parse_memory_mb(mem_data)

            effective_avail_mem_mb = max(0.0, avail_mem_mb - reserved_mem)

            st_details = hb_system.get("storage_details")
            if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
                total_st_gb = float(st_details.get("total_gb", 0.0))
                avail_st_gb = float(st_details.get("available_gb", 0.0))
            else:
                storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
                total_st_gb, used_st_gb, avail_st_gb = parse_storage_gb(storage_data)

            # Load factor
            load_avg = hb_system.get("load_average", [])
            if load_avg and len(load_avg) >= 1:
                load_1m = float(load_avg[0])
                load_ratio = min(load_1m / max(cpu_cores, 1), 2.0)
            elif hb_system.get("cpu_percent") is not None:
                cpu_pct = float(hb_system.get("cpu_percent", 0.0))
                load_1m = round((cpu_pct / 100.0) * max(cpu_cores, 1), 2)
                load_ratio = min((cpu_pct / 100.0) * 2.0, 2.0)
            else:
                load_1m = 1.0
                load_ratio = min(load_1m / max(cpu_cores, 1), 2.0)

            # Scoring (0 - 100) with least-loaded penalty and preferred bonus
            mem_ratio = (effective_avail_mem_mb / max(total_mem_mb, 1.0)) if total_mem_mb > 0 else 0.5
            score_mem = min(40.0, mem_ratio * 40.0)
            score_cpu = max(0.0, (1.0 - (load_ratio / 2.0)) * 40.0)
            st_ratio = (avail_st_gb / max(total_st_gb, 1.0)) if total_st_gb > 0 else 0.5
            score_storage = min(20.0, st_ratio * 20.0)

            # Active concurrency penalty: -5 pts per active job (max 20 pts penalty)
            active_cnt = active_counts.get(node_id, 0)
            active_penalty = min(20.0, active_cnt * 5.0)

            # Preferred node bonus: +15 pts
            preferred_bonus = 15.0 if reqs.get("preferred_node") == node_id else 0.0

            total_score = round(max(0.0, score_mem + score_cpu + score_storage - active_penalty + preferred_bonus), 2)

            explanation = (
                f"ONLINE | role={node.get('role', 'compute')} | "
                f"free RAM: ~{effective_avail_mem_mb:.0f}MB ({mem_ratio*100:.0f}%) | "
                f"active jobs: {active_cnt} | "
                f"load: {load_1m:.2f}/{cpu_cores}c | "
                f"score: {total_score}/100"
            )

            liveness = compute_node_liveness(node, timeout_seconds)
            candidates.append({
                "node_id": node_id,
                "name": node.get("name", "unknown"),
                "role": node.get("role", "compute"),
                "score": total_score,
                "score_breakdown": {
                    "memory_pts": round(score_mem, 1),
                    "cpu_pts": round(score_cpu, 1),
                    "storage_pts": round(score_storage, 1),
                    "active_penalty": round(active_penalty, 1),
                    "preferred_bonus": round(preferred_bonus, 1)
                },
                "metrics": {
                    "cpu_cores": cpu_cores,
                    "load_1m": load_1m,
                    "avail_mem_mb": round(effective_avail_mem_mb, 0),
                    "reserved_mem_mb": round(reserved_mem, 0),
                    "active_jobs": active_cnt,
                    "avail_storage_gb": round(avail_st_gb, 1)
                },
                "telemetry": extract_node_telemetry(node, liveness),
                "reason": explanation
            })

        if not candidates:
            return {
                "selected_node": None,
                "score": 0.0,
                "reason": f"No eligible nodes met the requirements. Rejections: {rejected}",
                "candidates": [],
                "rejected": rejected
            }

        candidates.sort(key=lambda c: (-c["score"], c["node_id"]))
        best = candidates[0]

        return {
            "selected_node": best["node_id"],
            "score": best["score"],
            "reason": f"Selected highest scoring eligible node: {best['node_id']} ({best['reason']})",
            "candidates": candidates,
            "rejected": rejected
        }


def is_node_eligible(
    node: dict,
    requirements: Optional[dict] = None,
    timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
    reserved_mem_mb: float = 0.0
) -> bool:
    """
    Evaluates whether an individual node meets health, state, and resource requirements.
    """
    eligible, _ = ResourceScheduler.is_node_eligible(
        node,
        requirements,
        timeout_seconds=timeout_seconds,
        reserved_mem_mb=reserved_mem_mb
    )
    return eligible


if __name__ == "__main__":
    print("PersonalServer ResourceScheduler module loaded.")
