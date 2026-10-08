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

# Centralized Node State Constants
STATE_ONLINE = "ONLINE"
STATE_DRAINING = "DRAINING"
STATE_DEBOARDING = "DEBOARDING"
STATE_OFFLINE = "OFFLINE"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_REMOVED = "REMOVED"
STATE_UNKNOWN = "UNKNOWN"

DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds

ARCH_MAP = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "arm64v8": "arm64",
    "linux/arm64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
    "x64": "amd64",
    "linux/amd64": "amd64",
    "armv7": "armv7",
    "armv7l": "armv7",
    "armhf": "armv7",
    "i386": "386",
    "x86": "386",
    "386": "386"
}


def normalize_architecture(arch):
    """Normalize node or image architecture string to canonical form."""
    if not arch or not isinstance(arch, str):
        return ""
    clean = arch.strip().lower()
    return ARCH_MAP.get(clean, clean.split("/")[-1])


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
        last_seen_dt = datetime.fromisoformat(last_seen_str)
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

    return {
        "status": liveness,
        "cpu": cpu_info,
        "memory": memory_info,
        "storage": storage_info,
        "workloads": workloads_info,
        "capabilities": capabilities_info
    }


class ResourceScheduler:
    """
    Deterministic, resource-aware scheduler that filters eligible nodes
    and scores them based on available memory, CPU load, and storage.
    """

    @staticmethod
    def select_node(job_requirements=None, nodes_db=None, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT):
        """
        Evaluate all cluster nodes and select the highest-scoring eligible node.
        
        Parameters:
            job_requirements (dict): e.g. {
                "role": "compute",
                "capabilities": ["compute"],
                "min_cpu_cores": 2,
                "min_memory_mb": 512,
                "min_storage_gb": 10,
                "platform": "termux",
                "architecture": "aarch64"
            }
            nodes_db (dict): Cluster database containing {"nodes": {...}}
            timeout_seconds (int): Heartbeat staleness threshold
            
        Returns dict:
            {
                "selected_node": "node-id" | None,
                "score": float,
                "reason": str,
                "candidates": [...],
                "rejected": {...}
            }
        """
        # Support flexible parameter ordering if caller passed nodes_db first
        if isinstance(job_requirements, dict) and "nodes" in job_requirements and (nodes_db is None or not isinstance(nodes_db, dict) or "nodes" not in nodes_db):
            job_requirements, nodes_db = nodes_db, job_requirements

        reqs = job_requirements or {}
        nodes = (nodes_db or {}).get("nodes", {})
        if not nodes and isinstance(nodes_db, dict) and any(isinstance(v, dict) and ("status" in v or "capabilities" in v) for v in nodes_db.values()):
            # nodes_db was passed directly as dict of node_id -> node
            nodes = nodes_db

        if not nodes:
            return {
                "selected_node": None,
                "score": 0.0,
                "reason": "No registered nodes exist in cluster.",
                "candidates": [],
                "rejected": {}
            }

        candidates = []
        rejected = {}

        for node_id, node in nodes.items():
            # ------------------------------------------------------------------
            # 1. Eligibility Check (Liveness & Subsystems)
            # ------------------------------------------------------------------
            liveness = compute_node_liveness(node, timeout_seconds)
            node_status = node.get("status")
            if liveness == STATE_REMOVED or node_status == STATE_REMOVED:
                rejected[node_id] = "Node is REMOVED from cluster"
                continue
            if liveness == STATE_DRAINING or node_status == STATE_DRAINING:
                rejected[node_id] = "Node is DRAINING (no new workloads accepted)"
                continue
            if liveness == STATE_DEBOARDING or node_status == STATE_DEBOARDING:
                rejected[node_id] = "Node is DEBOARDING (no new workloads accepted)"
                continue
            if liveness == STATE_OFFLINE:
                rejected[node_id] = "Node is OFFLINE (heartbeat timeout exceeded)"
                continue
            if liveness == STATE_UNHEALTHY:
                rejected[node_id] = "Node is UNHEALTHY (services degraded)"
                continue
            if liveness == STATE_UNKNOWN:
                rejected[node_id] = "Node status is UNKNOWN"
                continue

            # ------------------------------------------------------------------
            # 2. Role Filtering
            # ------------------------------------------------------------------
            req_role = reqs.get("role")
            if req_role:
                node_role = node.get("role", "compute").lower()
                if node_role != req_role.lower() and node_role != "hybrid":
                    rejected[node_id] = f"Role mismatch: node is '{node_role}', required '{req_role}'"
                    continue

            # ------------------------------------------------------------------
            # 3. Capabilities Filtering
            # ------------------------------------------------------------------
            req_caps = reqs.get("capabilities", [])
            if isinstance(req_caps, str):
                req_caps = [req_caps]
            node_caps = node.get("capabilities", {})
            if isinstance(node_caps, list):
                node_caps_set = set(node_caps)
                has_cap = lambda c: c in node_caps_set
            elif isinstance(node_caps, dict):
                has_cap = lambda c: bool(node_caps.get(c, False))
            else:
                has_cap = lambda c: False

            missing_caps = [c for c in req_caps if not has_cap(c)]
            if missing_caps:
                rejected[node_id] = f"Missing required capabilities: {missing_caps}"
                continue

            # Support any_capabilities (e.g. ['container_runtime:docker', 'container_runtime:udocker'])
            req_any_caps = reqs.get("any_capabilities")
            if req_any_caps and isinstance(req_any_caps, list):
                if not any(has_cap(c) for c in req_any_caps):
                    rejected[node_id] = f"Missing any of required capabilities: {req_any_caps}"
                    continue

            # ------------------------------------------------------------------
            # 4. Architecture & Platform Filtering
            # ------------------------------------------------------------------
            node_raw_arch = node.get("architecture") or node.get("system", {}).get("arch") or node.get("system", {}).get("architecture", "")
            req_arch = reqs.get("architecture")
            if req_arch:
                norm_req = normalize_architecture(req_arch)
                norm_node = normalize_architecture(node_raw_arch)
                if norm_req and norm_node and norm_req != norm_node:
                    rejected[node_id] = f"Architecture mismatch: node is '{node_raw_arch}', required '{req_arch}'"
                    continue

            # Image multi-architecture support check
            supp_archs = reqs.get("supported_architectures")
            if supp_archs and isinstance(supp_archs, list):
                norm_supp = {normalize_architecture(a) for a in supp_archs if a}
                norm_node = normalize_architecture(node_raw_arch)
                if norm_node and norm_supp and norm_node not in norm_supp:
                    rejected[node_id] = "Image does not support this node architecture."
                    continue

            req_platform = reqs.get("platform")
            if req_platform and node.get("platform", "").lower() != req_platform.lower():
                rejected[node_id] = f"Platform mismatch: node is '{node.get('platform')}', required '{req_platform}'"
                continue

            # ------------------------------------------------------------------
            # 5. Resource Evaluation & Minimum Filtering
            # ------------------------------------------------------------------
            resources = node.get("resources", {})
            last_hb = node.get("last_heartbeat", {})
            hb_system = last_hb.get("system", {})

            # CPU Cores
            cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))
            min_cpu = int(reqs.get("min_cpu_cores", 0))
            if cpu_cores < min_cpu:
                rejected[node_id] = f"Insufficient CPU cores: node has {cpu_cores}, required {min_cpu}"
                continue

            # Memory
            mem_details = hb_system.get("memory_details")
            if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
                total_mem_mb = float(mem_details.get("total_mb", 0.0))
                used_mem_mb = float(mem_details.get("used_mb", 0.0))
                avail_mem_mb = float(mem_details.get("available_mb", 0.0))
            else:
                mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
                total_mem_mb, used_mem_mb, avail_mem_mb = parse_memory_mb(mem_data)

            min_mem = float(reqs.get("min_memory_mb", 0))
            if avail_mem_mb < min_mem:
                rejected[node_id] = f"Insufficient available memory: ~{avail_mem_mb:.0f} MB available, required {min_mem:.0f} MB"
                continue

            # Storage
            st_details = hb_system.get("storage_details")
            if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
                total_st_gb = float(st_details.get("total_gb", 0.0))
                used_st_gb = float(st_details.get("used_gb", 0.0))
                avail_st_gb = float(st_details.get("available_gb", 0.0))
            else:
                storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
                total_st_gb, used_st_gb, avail_st_gb = parse_storage_gb(storage_data)

            min_st = float(reqs.get("min_storage_gb", 0))
            if avail_st_gb < min_st:
                rejected[node_id] = f"Insufficient available storage: ~{avail_st_gb:.1f} GB available, required {min_st:.1f} GB"
                continue

            # CPU Load factor (supports Unix load average and Windows cpu_percent)
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

            # ------------------------------------------------------------------
            # 6. Explainable Resource-Aware Scoring (0 - 100)
            # ------------------------------------------------------------------
            # Memory score: up to 40 pts (higher available memory percentage)
            mem_ratio = (avail_mem_mb / max(total_mem_mb, 1.0)) if total_mem_mb > 0 else 0.5
            score_mem = min(40.0, mem_ratio * 40.0)

            # CPU load score: up to 40 pts (lower relative load factor)
            score_cpu = max(0.0, (1.0 - (load_ratio / 2.0)) * 40.0)

            # Storage score: up to 20 pts
            st_ratio = (avail_st_gb / max(total_st_gb, 1.0)) if total_st_gb > 0 else 0.5
            score_storage = min(20.0, st_ratio * 20.0)

            total_score = round(score_mem + score_cpu + score_storage, 2)

            explanation = (
                f"ONLINE | role={node.get('role', 'compute')} | "
                f"free RAM: ~{avail_mem_mb:.0f}MB ({mem_ratio*100:.0f}%) | "
                f"load: {load_1m:.2f}/{cpu_cores}c | "
                f"score: {total_score}/100"
            )

            candidates.append({
                "node_id": node_id,
                "name": node.get("name", "unknown"),
                "role": node.get("role", "compute"),
                "score": total_score,
                "score_breakdown": {
                    "memory_pts": round(score_mem, 1),
                    "cpu_pts": round(score_cpu, 1),
                    "storage_pts": round(score_storage, 1)
                },
                "metrics": {
                    "cpu_cores": cpu_cores,
                    "load_1m": load_1m,
                    "avail_mem_mb": round(avail_mem_mb, 0),
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

        # ----------------------------------------------------------------------
        # 7. Deterministic Selection (Score DESC, Node ID ASC)
        # ----------------------------------------------------------------------
        candidates.sort(key=lambda c: (-c["score"], c["node_id"]))
        best = candidates[0]

        return {
            "selected_node": best["node_id"],
            "score": best["score"],
            "reason": f"Selected highest scoring eligible node: {best['node_id']} ({best['reason']})",
            "candidates": candidates,
            "rejected": rejected
        }


if __name__ == "__main__":
    print("PersonalServer ResourceScheduler module loaded.")
