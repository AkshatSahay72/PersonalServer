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
        last_seen_dt = datetime.fromisoformat(last_seen_str)
        now = datetime.now(timezone.utc)
        delta_seconds = (now - last_seen_dt).total_seconds()

        if delta_seconds > timeout_seconds:
            return STATE_OFFLINE

        last_hb = node.get("last_heartbeat", {})
        services = last_hb.get("services", {})
        if services and any(v in ("unresponsive", "degraded", "stopped") for v in services.values()):
            return STATE_UNHEALTHY

        return STATE_ONLINE
    except Exception:
        return STATE_UNKNOWN


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
        reqs = job_requirements or {}
        nodes = (nodes_db or {}).get("nodes", {})

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
            if liveness == STATE_REMOVED:
                rejected[node_id] = "Node is REMOVED from cluster"
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
            missing_caps = [c for c in req_caps if not node_caps.get(c, False)]
            if missing_caps:
                rejected[node_id] = f"Missing required capabilities: {missing_caps}"
                continue

            # ------------------------------------------------------------------
            # 4. Architecture & Platform Filtering
            # ------------------------------------------------------------------
            req_arch = reqs.get("architecture")
            if req_arch and node.get("architecture", "").lower() != req_arch.lower():
                rejected[node_id] = f"Architecture mismatch: node is '{node.get('architecture')}', required '{req_arch}'"
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
            mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
            total_mem_mb, used_mem_mb, avail_mem_mb = parse_memory_mb(mem_data)
            min_mem = float(reqs.get("min_memory_mb", 0))
            if avail_mem_mb < min_mem:
                rejected[node_id] = f"Insufficient available memory: ~{avail_mem_mb:.0f} MB available, required {min_mem:.0f} MB"
                continue

            # Storage
            storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
            total_st_gb, used_st_gb, avail_st_gb = parse_storage_gb(storage_data)
            min_st = float(reqs.get("min_storage_gb", 0))
            if avail_st_gb < min_st:
                rejected[node_id] = f"Insufficient available storage: ~{avail_st_gb:.1f} GB available, required {min_st:.1f} GB"
                continue

            # CPU Load factor
            load_avg = hb_system.get("load_average", [])
            load_1m = float(load_avg[0]) if load_avg and len(load_avg) >= 1 else 1.0
            load_ratio = min(load_1m / max(cpu_cores, 1), 2.0)  # normalized (0.0 to 2.0)

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
