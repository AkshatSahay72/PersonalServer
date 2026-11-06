#!/usr/bin/env python3
"""
PersonalServer Resource-Aware Universal Scheduler (US v1.0)
===========================================================
Deterministic, explainable node selection based on node eligibility,
capabilities, roles, mobile/thermal telemetry, and available system resources.

Implements Milestones J3.1, J3.2, and J3.3:
- J3.2 8-Stage Ordered Eligibility Pipeline with standardized rejection taxonomy.
- J3.1/J3.2 Multi-Factor Soft Scoring Model (RAM 30, CPU 25, Storage 10, Mobile/Thermal 15,
  Network 10, Availability Class 10, Preferred +15, Concurrency -20, Clamped 0-100).
- J3.3 Priority Aging Formula: Effective Priority = Base + min(50, floor(dwell_sec / 60) * 5).
- J3.3 Concurrency-safe atomic resource reservation ledger integration.
"""

import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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

DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds (matches J2 telemetry TTL)

# J3.2 Standardized Rejection Reason Taxonomy
REJECTION_NODE_OFFLINE = "NODE_OFFLINE"
REJECTION_NODE_DRAINING = "NODE_DRAINING"
REJECTION_INSUFFICIENT_UNRESERVED_RAM = "INSUFFICIENT_UNRESERVED_RAM"
REJECTION_INSUFFICIENT_UNRESERVED_STORAGE = "INSUFFICIENT_UNRESERVED_STORAGE"
REJECTION_INSUFFICIENT_CPU = "INSUFFICIENT_CPU"
REJECTION_BATTERY_POWER_DISALLOWED = "BATTERY_POWER_DISALLOWED"
REJECTION_BATTERY_LEVEL_BELOW_MINIMUM = "BATTERY_LEVEL_BELOW_MINIMUM"
REJECTION_THERMAL_THROTTLING_CRITICAL = "THERMAL_THROTTLING_CRITICAL"
REJECTION_CAPABILITY_VERIFICATION_STALE = "CAPABILITY_VERIFICATION_STALE"
REJECTION_HARDWARE_LEASE_CONFLICT = "HARDWARE_LEASE_CONFLICT"
REJECTION_UNSATISFIABLE_CAPABILITY = "UNSATISFIABLE_CAPABILITY"
REJECTION_UNSUPPORTED_ARCHITECTURE = "UNSUPPORTED_ARCHITECTURE"
REJECTION_DOCKER_ENGINE_NOT_AVAILABLE = "DOCKER_ENGINE_NOT_AVAILABLE"
REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED = "HARDWARE_PERIPHERAL_UNPERMITTED"
REJECTION_ROLE_MISMATCH = "ROLE_MISMATCH"
REJECTION_AVAILABILITY_CLASS_MISMATCH = "AVAILABILITY_CLASS_MISMATCH"
REJECTION_POLICY_DISABLED_CAPABILITY = "POLICY_DISABLED_CAPABILITY"
REJECTION_ANTI_AFFINITY_CONFLICT = "ANTI_AFFINITY_CONFLICT"


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
    if not node or not isinstance(node, dict):
        return STATE_UNKNOWN

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
        if not isinstance(last_hb, dict):
            last_hb = {}
        services = last_hb.get("services", {})
        role = node.get("role", "compute")
        if role == "compute":
            # For compute workers, worker daemon availability is critical
            if services and services.get("node_worker") in ("unresponsive", "degraded", "stopped"):
                return STATE_UNHEALTHY
        else:
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

    try:
        running_containers = int(hb_workloads.get("running_containers", 0))
    except (ValueError, TypeError):
        running_containers = 0

    active_jobs = 0
    running_jobs = 0
    if node_jobs and isinstance(node_jobs, list):
        for j in node_jobs:
            st = j.get("status")
            if st in ("CLAIMED", "ASSIGNED", "RUNNING", "RECOVERING"):
                active_jobs += 1
            if st == "RUNNING":
                running_jobs += 1

    workloads_info = {
        "active_jobs": active_jobs,
        "running_jobs": running_jobs,
        "running_containers": max(0, running_containers)
    }

    capabilities_info = node.get("capabilities", {}) or {}
    battery_info = hb_system.get("battery") or last_hb.get("battery") or node.get("telemetry", {}).get("mobile_state", {}) or {}
    network_info = hb_system.get("network") or last_hb.get("network") or node.get("telemetry", {}).get("network", {}) or {}
    thermals_info = hb_system.get("thermals") or last_hb.get("thermals") or node.get("telemetry", {}).get("mobile_state", {}) or {}
    cpu_details_info = hb_system.get("cpu_details") or last_hb.get("cpu_details") or {}

    if "temperature_c" in thermals_info and thermals_info["temperature_c"] is not None:
        cpu_info["temperature_c"] = thermals_info["temperature_c"]
        cpu_info["thermal_status"] = thermals_info.get("status") or thermals_info.get("thermal_state", "Normal")

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
    """Extracts (advertised_capabilities, verified_capabilities) from a node record."""
    advertised: Dict[str, bool] = {}
    verified: Dict[str, bool] = {}

    if not node or not isinstance(node, dict):
        return advertised, verified

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


def compute_effective_priority(base_priority: int, submitted_at_epoch: float, now_epoch: Optional[float] = None) -> int:
    """
    Implements J3.3 Priority Aging:
    Effective Priority = Base Priority + min(50, floor(queue_dwell_seconds / 60) * 5)
    """
    now = now_epoch if now_epoch is not None else time.time()
    dwell_sec = max(0.0, now - float(submitted_at_epoch))
    age_boost = min(50, math.floor(dwell_sec / 60.0) * 5)
    return int(base_priority) + int(age_boost)


class ResourceScheduler:
    """
    PersonalServer Universal Scheduler (US v1.0).
    Implements J3.1/J3.2/J3.3 specifications:
    - 8-stage hard eligibility pipeline.
    - Standardized rejection reason taxonomy.
    - Normalized multi-factor soft scoring model (0.0 to 100.0).
    - Concurrency-aware reservation headroom calculation.
    """

    @classmethod
    def is_node_eligible(
        cls,
        node: Dict[str, Any],
        job_requirements: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
        reserved_mem_mb: float = 0.0,
        reserved_storage_gb: float = 0.0,
        reserved_cpu_cores: int = 0,
        locked_devices: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """
        Executes the exact 8-Stage Ordered Eligibility Pipeline from J3.2 §7.1.
        Returns (is_eligible: bool, reason: str).
        """
        if not node or not isinstance(node, dict):
            return False, "Node record is missing or invalid"

        reqs = job_requirements or {}
        if "requirements" in reqs and isinstance(reqs.get("requirements"), dict):
            reqs = reqs["requirements"]

        # =========================================================================
        # Stage 1 (Liveness & Freshness Gate)
        # =========================================================================
        liveness = compute_node_liveness(node, timeout_seconds)
        node_status = node.get("status")
        if liveness in (STATE_OFFLINE, STATE_UNHEALTHY, STATE_REMOVED, STATE_UNKNOWN) or node_status in (STATE_OFFLINE, STATE_REMOVED):
            return False, f"{REJECTION_NODE_OFFLINE}: Node is {liveness or node_status}"

        # =========================================================================
        # Stage 2 (Administrative & Policy Gate)
        # =========================================================================
        if liveness in (STATE_DRAINING, STATE_DEBOARDING) or node_status in (STATE_DRAINING, STATE_DEBOARDING):
            if not reqs.get("allow_drain", False):
                return False, f"{REJECTION_NODE_DRAINING}: Node is cordoned or draining"

        # Policy overrides: disabled capabilities
        policy_overrides = node.get("policy_overrides", {})
        disabled_caps = set(policy_overrides.get("disabled_capabilities", []))
        req_caps = reqs.get("capabilities", [])
        if isinstance(req_caps, str):
            req_caps = [c.strip() for c in req_caps.split(",") if c.strip()]
        for cap in req_caps:
            if cap in disabled_caps:
                return False, f"{REJECTION_POLICY_DISABLED_CAPABILITY}: Capability '{cap}' is administratively disabled on node"

        # Anti-affinity hints
        hints = reqs.get("hints", {})
        anti_affinity = hints.get("node_anti_affinity", [])
        node_labels = node.get("labels", {})
        if anti_affinity:
            for label_key in anti_affinity:
                if label_key in node_labels:
                    return False, f"{REJECTION_ANTI_AFFINITY_CONFLICT}: Node label '{label_key}' matches job anti-affinity constraint"

        # Role compatibility
        req_role = reqs.get("role")
        if req_role:
            node_role = node.get("role", "compute").lower()
            if node_role != req_role.lower() and node_role != "hybrid":
                return False, f"{REJECTION_ROLE_MISMATCH}: Node role '{node_role}' does not match required '{req_role}'"

        # =========================================================================
        # Stage 3 (Availability Class Gate)
        # =========================================================================
        node_avail_class = node.get("availability_class")
        if not node_avail_class:
            platform = node.get("platform", "").lower()
            role = node.get("role", "").lower()
            if role == "controller":
                node_avail_class = "Always-on"
            elif platform == "termux":
                node_avail_class = "Persistent"
            elif platform in ("windows", "linux", "darwin"):
                node_avail_class = "Opportunistic"
            else:
                node_avail_class = "Opportunistic"

        # Opportunistic workers strictly reject service modality (J3 §5)
        workload_mode = str(reqs.get("workload_mode", "batch")).lower()
        if node_avail_class == "Opportunistic" and workload_mode == "service":
            return False, f"{REJECTION_AVAILABILITY_CLASS_MISMATCH}: Opportunistic node cannot schedule long-running service"

        req_avail_class = reqs.get("availability_class")
        if req_avail_class:
            if req_avail_class.lower() == "always-on" and node_avail_class != "Always-on":
                return False, f"{REJECTION_AVAILABILITY_CLASS_MISMATCH}: Node class '{node_avail_class}' does not meet required 'Always-on'"

        # =========================================================================
        # Stage 4 (Runtime & Architecture Compatibility Gate)
        # =========================================================================
        node_raw_arch = node.get("architecture") or node.get("system", {}).get("arch") or node.get("system", {}).get("architecture", "")
        req_arch = reqs.get("architecture")
        if req_arch and req_arch.lower() != "any":
            norm_req = normalize_architecture(req_arch)
            norm_node = normalize_architecture(node_raw_arch)
            if norm_req and norm_node and norm_req != norm_node:
                return False, f"{REJECTION_UNSUPPORTED_ARCHITECTURE}: Node architecture '{node_raw_arch}' does not match '{req_arch}'"

        supp_archs = reqs.get("supported_architectures")
        if supp_archs and isinstance(supp_archs, list):
            norm_supp = {normalize_architecture(a) for a in supp_archs if a}
            norm_node = normalize_architecture(node_raw_arch)
            if norm_node and norm_supp and norm_node not in norm_supp:
                return False, f"{REJECTION_UNSUPPORTED_ARCHITECTURE}: Workload does not support node architecture '{node_raw_arch}'"

        req_platform = reqs.get("platform")
        if req_platform and node.get("platform", "").lower() != req_platform.lower():
            return False, f"Platform mismatch: node is '{node.get('platform')}', required '{req_platform}'"

        # Capabilities & Docker vs udocker check
        advertised_caps, verified_caps = extract_node_capabilities(node)
        require_verified = bool(reqs.get("require_verified", False))

        # Check Docker Engine requirement specifically (J3.2 §3.2(7): udocker NEVER satisfies docker)
        req_runtime = str(reqs.get("runtime", "")).lower()
        if "container_runtime:docker" in req_caps or "docker" in req_caps or req_runtime in ("docker", "container/docker", "container_runtime:docker"):
            has_docker = verified_caps.get("container_runtime:docker", False) or verified_caps.get("docker", False)
            if not has_docker:
                return False, f"{REJECTION_DOCKER_ENGINE_NOT_AVAILABLE}: Genuine Docker Engine required (udocker does not satisfy)"

        # Check general capability presence
        missing = [c for c in req_caps if not advertised_caps.get(c, False) and not verified_caps.get(c, False)]
        if missing:
            return False, f"{REJECTION_UNSATISFIABLE_CAPABILITY}: Missing required capabilities {missing}"

        if require_verified:
            unverified = [c for c in req_caps if not verified_caps.get(c, False)]
            if unverified:
                return False, f"{REJECTION_UNSATISFIABLE_CAPABILITY}: Required capabilities unverified by worker {unverified}"

        # Explicit verified_capabilities requirement
        req_verified = reqs.get("verified_capabilities", [])
        if isinstance(req_verified, str):
            req_verified = [req_verified]
        missing_verified = [c for c in req_verified if not verified_caps.get(c, False)]
        if missing_verified:
            return False, f"{REJECTION_UNSATISFIABLE_CAPABILITY}: Missing required verified capabilities {missing_verified}"

        # =========================================================================
        # Stage 5 (Hardware Peripheral State Gate)
        # =========================================================================
        node_hardware = node.get("hardware", {}) or {}
        req_hardware = reqs.get("hardware", [])
        if isinstance(req_hardware, str):
            req_hardware = [req_hardware]

        for hw_item in req_hardware:
            hw_info = node_hardware.get(hw_item, {})
            hw_status = hw_info.get("status") if isinstance(hw_info, dict) else None
            # Must be ACCESSIBLE; reject DETECTED, UNAVAILABLE, UNKNOWN
            if hw_status != "ACCESSIBLE":
                if hw_status == "DETECTED":
                    return False, f"{REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED}: Device '{hw_item}' is DETECTED but permission is not ACCESSIBLE"
                return False, f"{REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED}: Device '{hw_item}' status is '{hw_status or 'MISSING'}'"

        # Exclusive device conflict check
        req_exclusive = reqs.get("exclusive_devices", [])
        if reqs.get("gpu", False) or "gpu" in req_caps:
            req_exclusive.append("gpu:0")

        if locked_devices and req_exclusive:
            for dev in req_exclusive:
                if dev in locked_devices:
                    return False, f"{REJECTION_HARDWARE_LEASE_CONFLICT}: Exclusive device '{dev}' is currently locked by another active job"

        # =========================================================================
        # Stage 6 (Capability Verification Freshness Gate)
        # =========================================================================
        metadata = node.get("metadata", {})
        if metadata.get("is_stale_verification", False) and require_verified:
            return False, f"{REJECTION_CAPABILITY_VERIFICATION_STALE}: Node verification TTL expired"

        # =========================================================================
        # Stage 7 (Mobile Battery & Thermal Gate)
        # =========================================================================
        last_hb = node.get("last_heartbeat", {}) or {}
        hb_system = last_hb.get("system", {}) or {}
        battery_data = hb_system.get("battery") or last_hb.get("battery") or node.get("telemetry", {}).get("mobile_state", {}) or {}
        thermal_data = hb_system.get("thermals") or last_hb.get("thermals") or node.get("telemetry", {}).get("mobile_state", {}) or {}

        # Thermal Critical Gate
        thermal_state = str(thermal_data.get("status") or thermal_data.get("thermal_state", "Normal")).lower()
        if thermal_state == "critical":
            return False, f"{REJECTION_THERMAL_THROTTLING_CRITICAL}: Node is in Critical thermal throttle"

        # Battery AC Gate
        allow_on_battery = hints.get("allow_on_battery", True)
        power_source = str(battery_data.get("plugged") or battery_data.get("power_source", "PLUGGED_AC")).upper()
        is_on_battery = ("BATTERY" in power_source) or (power_source in ("NONE", "UNPLUGGED"))
        if not allow_on_battery and is_on_battery:
            return False, f"{REJECTION_BATTERY_POWER_DISALLOWED}: Job requires AC mains power; node is on battery"

        # Battery Floor Gate
        min_battery_percent = float(hints.get("min_battery_percent", 0.0))
        if min_battery_percent > 0.0 and is_on_battery:
            curr_battery = float(battery_data.get("percentage", 100))
            if curr_battery < min_battery_percent:
                return False, f"{REJECTION_BATTERY_LEVEL_BELOW_MINIMUM}: Battery level ({curr_battery:.0f}%) below minimum required ({min_battery_percent:.0f}%)"

        # =========================================================================
        # Stage 8 (Resource Headroom Gate Post-Reservation)
        # =========================================================================
        resources = node.get("resources", {}) or {}

        # CPU Cores
        min_cpu = int(reqs.get("min_cpu_cores", 0) or reqs.get("cpu_cores", 0) or reqs.get("min_cpu", 0))
        if min_cpu > 0:
            cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))
            eff_avail_cores = max(0, cpu_cores - reserved_cpu_cores)
            # Permitted overcommit ratio: 2.0x on AC, 1.0x on battery
            max_cores_alloc = cpu_cores * (1.0 if is_on_battery else 2.0)
            if (reserved_cpu_cores + min_cpu) > max_cores_alloc:
                return False, f"{REJECTION_INSUFFICIENT_CPU}: Node has {cpu_cores} cores ({reserved_cpu_cores} allocated), required {min_cpu}"

        # Physical RAM Floor & Headroom Buffer (J3.3 §8.1)
        # Android: max(300MB, 0.10 * C_physical); Windows: max(1024MB, 0.05 * C_physical)
        is_android = node.get("platform", "").lower() == "termux"
        mem_details = hb_system.get("memory_details")
        if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
            total_mem_mb = float(mem_details.get("total_mb", 0.0))
            avail_mem_mb = float(mem_details.get("available_mb", 0.0))
        else:
            mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
            total_mem_mb, _, avail_mem_mb = parse_memory_mb(mem_data)

        safety_headroom = max(300.0, 0.10 * total_mem_mb) if is_android else max(1024.0, 0.05 * total_mem_mb)
        effective_avail_mem_mb = max(0.0, (avail_mem_mb - safety_headroom) - reserved_mem_mb)

        min_mem = float(reqs.get("min_memory_mb", 0) or reqs.get("memory_mb", 0) or reqs.get("min_memory", 0))
        if min_mem > 0:
            if effective_avail_mem_mb < min_mem:
                return False, f"{REJECTION_INSUFFICIENT_UNRESERVED_RAM}: ~{effective_avail_mem_mb:.0f} MB allocatable headroom (after safety buffer {safety_headroom:.0f}MB and reservations {reserved_mem_mb:.0f}MB), required {min_mem:.0f} MB"

        # Storage (GB)
        st_details = hb_system.get("storage_details")
        if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
            avail_st_gb = float(st_details.get("available_gb", 0.0))
        else:
            storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
            _, _, avail_st_gb = parse_storage_gb(storage_data)

        effective_avail_st_gb = max(0.0, avail_st_gb - reserved_storage_gb)
        min_st = float(reqs.get("min_storage_gb", 0) or reqs.get("storage_gb", 0) or reqs.get("min_storage", 0))
        if min_st > 0:
            if effective_avail_st_gb < min_st:
                return False, f"{REJECTION_INSUFFICIENT_UNRESERVED_STORAGE}: ~{effective_avail_st_gb:.1f} GB available, required {min_st:.1f} GB"

        return True, "Eligible"

    @classmethod
    def calculate_score(
        cls,
        node: Dict[str, Any],
        reqs: Dict[str, Any],
        effective_avail_mem_mb: float,
        total_mem_mb: float,
        effective_avail_st_gb: float,
        total_st_gb: float,
        active_cnt: int
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calculates the exact multi-factor soft score according to J3.1/J3.2 §4.1:
        1. Memory Headroom: 0.0 to 30.0 pts -> (avail / total) * 30.0
        2. CPU Headroom: 0.0 to 25.0 pts -> max(0, 1.0 - load_1m / cores) * 25.0
        3. Storage Headroom: 0.0 to 10.0 pts -> (avail / total) * 10.0
        4. Mobile & Thermal: 0.0 to 15.0 pts (AC Power bonus + Thermal margin)
        5. Network Latency: 0.0 to 10.0 pts -> max(0, 10.0 - (ping_ms / 10.0))
        6. Availability Class Fit: 0.0 to 10.0 pts
        7. Preferred Node Bonus: +15.0 pts
        8. Concurrency Penalty: -5.0 pts per active running job (max -20.0 pts)
        Normalized & Clamped: min(100.0, max(0.0, raw_score))
        """
        last_hb = node.get("last_heartbeat", {}) or {}
        hb_system = last_hb.get("system", {}) or {}
        resources = node.get("resources", {}) or {}
        node_id = node.get("node_id")

        # 1. Memory
        mem_ratio = (effective_avail_mem_mb / max(total_mem_mb, 1.0)) if total_mem_mb > 0 else 0.5
        score_mem = min(30.0, max(0.0, mem_ratio * 30.0))

        # 2. CPU
        try:
            cpu_cores = int(hb_system.get("cpu_cores") or resources.get("cpu_cores", 1))
        except (ValueError, TypeError):
            cpu_cores = 1

        load_avg = hb_system.get("load_average", [])
        if load_avg and len(load_avg) >= 1:
            load_1m = float(load_avg[0])
        elif hb_system.get("cpu_percent") is not None:
            load_1m = (float(hb_system.get("cpu_percent", 0.0)) / 100.0) * cpu_cores
        else:
            load_1m = 0.5
        score_cpu = max(0.0, min(25.0, (1.0 - (load_1m / max(cpu_cores, 1))) * 25.0))

        # 3. Storage
        st_ratio = (effective_avail_st_gb / max(total_st_gb, 1.0)) if total_st_gb > 0 else 0.5
        score_storage = min(10.0, max(0.0, st_ratio * 10.0))

        # 4. Mobile & Thermal (15 pts max)
        battery_data = hb_system.get("battery") or last_hb.get("battery") or node.get("telemetry", {}).get("mobile_state", {}) or {}
        thermal_data = hb_system.get("thermals") or last_hb.get("thermals") or node.get("telemetry", {}).get("mobile_state", {}) or {}

        # Thermal margin: Cool = +7.0, Normal = +4.0, Warm = 0.0 (J3 §4.2)
        thermal_st = str(thermal_data.get("status") or thermal_data.get("thermal_state", "Normal")).lower()
        if thermal_st == "cool":
            thermal_pts = 7.0
        elif thermal_st == "normal":
            thermal_pts = 4.0
        else:
            thermal_pts = 0.0

        # AC Power bonus: +8.0 pts if PLUGGED_AC or not mobile
        power_source = str(battery_data.get("plugged") or battery_data.get("power_source", "PLUGGED_AC")).upper()
        is_mobile = node.get("mobile", {}).get("is_mobile", False) or node.get("platform", "").lower() == "termux"
        if not is_mobile or "AC" in power_source:
            ac_pts = 8.0
        else:
            ac_pts = max(0.0, min(6.0, float(battery_data.get("percentage", 50)) / 100.0 * 6.0))

        score_mobile = min(15.0, thermal_pts + ac_pts)

        # 5. Network Latency (10 pts max)
        network_data = hb_system.get("network") or last_hb.get("network") or node.get("telemetry", {}).get("network", {}) or {}
        ping_ms = float(network_data.get("ping_ms", 15.0) or 15.0)
        score_network = max(0.0, min(10.0, 10.0 - (ping_ms / 10.0)))

        # 6. Availability Class Fit (10 pts max)
        node_avail_class = node.get("availability_class", "Persistent")
        req_class = reqs.get("availability_class")
        if req_class and req_class.lower() == node_avail_class.lower():
            score_class = 10.0
        elif node_avail_class == "Always-on":
            score_class = 8.0
        elif node_avail_class == "Persistent":
            score_class = 7.0
        else:
            score_class = 4.0

        # 7. Preferred Node Bonus (+15 pts)
        preferred_node = reqs.get("preferred_node") or reqs.get("target_node")
        preferred_bonus = 15.0 if preferred_node and preferred_node == node_id else 0.0

        # 8. Concurrency Penalty (-5 pts per active job, max 20 pts)
        concurrency_penalty = min(20.0, max(0.0, active_cnt * 5.0))

        # Raw Score & Clamping
        raw_score = (
            score_mem
            + score_cpu
            + score_storage
            + score_mobile
            + score_network
            + score_class
            + preferred_bonus
            - concurrency_penalty
        )
        final_score = round(max(0.0, min(100.0, raw_score)), 2)

        breakdown = {
            "memory_pts": round(score_mem, 1),
            "cpu_pts": round(score_cpu, 1),
            "storage_pts": round(score_storage, 1),
            "mobile_pts": round(score_mobile, 1),
            "network_pts": round(score_network, 1),
            "class_pts": round(score_class, 1),
            "preferred_bonus": round(preferred_bonus, 1),
            "concurrency_penalty": round(concurrency_penalty, 1)
        }

        return final_score, breakdown

    @staticmethod
    def select_node(
        job_requirements: Optional[Dict[str, Any]] = None,
        nodes_db: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT,
        cluster_jobs: Optional[Dict[str, Any]] = None,
        reservations_db: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Evaluates cluster nodes across the 8-Stage Eligibility Pipeline and selects
        the highest-scoring candidate with deterministic tie-breaking (J3 §4).
        """
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

        # Calculate resource reservations from authoritative reservation ledger and active jobs
        now = time.time()
        reserved_by_node: Dict[str, float] = {}
        reserved_st_by_node: Dict[str, float] = {}
        reserved_cpu_by_node: Dict[str, int] = {}
        locked_devices_by_node: Dict[str, List[str]] = {}
        active_counts: Dict[str, int] = {}

        # 1. Authoritative reservation ledger
        if reservations_db and isinstance(reservations_db, dict):
            for res in reservations_db.values():
                nid = res.get("node_id")
                st = res.get("state")
                exp = res.get("commit_expires_at")
                if st in ("PROVISIONAL", "COMMITTED") and exp is not None and now > float(exp):
                    continue
                if nid and st in ("PROVISIONAL", "COMMITTED", "ACTIVE", "RELEASING"):
                    r_res = res.get("resources", {})
                    reserved_by_node[nid] = reserved_by_node.get(nid, 0.0) + float(r_res.get("memory_mb", 0.0))
                    reserved_st_by_node[nid] = reserved_st_by_node.get(nid, 0.0) + float(r_res.get("storage_gb", 0.0))
                    reserved_cpu_by_node[nid] = reserved_cpu_by_node.get(nid, 0) + int(r_res.get("cpu_cores", 0))
                    for dev in r_res.get("exclusive_devices", []):
                        locked_devices_by_node.setdefault(nid, []).append(dev)
                    active_counts[nid] = active_counts.get(nid, 0) + 1

        # 2. Legacy active jobs deduction fallback (if reservation ledger not supplied)
        if cluster_jobs and isinstance(cluster_jobs, dict) and not reservations_db:
            for j in cluster_jobs.values():
                nid = j.get("target_node") or j.get("assigned_node")
                st = str(j.get("status", "")).upper()
                if nid and st in ("ASSIGNED", "CLAIMED", "RUNNING"):
                    j_reqs = j.get("requirements", {}) or {}
                    j_mem = float(j_reqs.get("min_memory_mb") or j_reqs.get("memory_mb") or 0.0)
                    j_st = float(j_reqs.get("min_storage_gb") or j_reqs.get("storage_gb") or 0.0)
                    j_cpu = int(j_reqs.get("min_cpu_cores") or j_reqs.get("cpu_cores") or 0)
                    reserved_by_node[nid] = reserved_by_node.get(nid, 0.0) + j_mem
                    reserved_st_by_node[nid] = reserved_st_by_node.get(nid, 0.0) + j_st
                    reserved_cpu_by_node[nid] = reserved_cpu_by_node.get(nid, 0) + j_cpu
                    active_counts[nid] = active_counts.get(nid, 0) + 1

        candidates = []
        rejected = {}

        for node_id, node in nodes.items():
            reserved_mem = reserved_by_node.get(node_id, 0.0)
            reserved_st = reserved_st_by_node.get(node_id, 0.0)
            reserved_cpu = reserved_cpu_by_node.get(node_id, 0)
            locked_devs = locked_devices_by_node.get(node_id, [])

            eligible, reason = ResourceScheduler.is_node_eligible(
                node,
                reqs,
                timeout_seconds=timeout_seconds,
                reserved_mem_mb=reserved_mem,
                reserved_storage_gb=reserved_st,
                reserved_cpu_cores=reserved_cpu,
                locked_devices=locked_devs
            )
            if not eligible:
                rejected[node_id] = reason
                continue

            # Resources extraction for scoring
            resources = node.get("resources", {}) or {}
            last_hb = node.get("last_heartbeat", {}) or {}
            hb_system = last_hb.get("system", {}) or {}

            mem_details = hb_system.get("memory_details")
            if mem_details and isinstance(mem_details, dict) and "total_mb" in mem_details:
                total_mem_mb = float(mem_details.get("total_mb", 0.0))
                avail_mem_mb = float(mem_details.get("available_mb", 0.0))
            else:
                mem_data = hb_system.get("memory") or str(resources.get("ram_mb", "0"))
                total_mem_mb, _, avail_mem_mb = parse_memory_mb(mem_data)

            is_android = node.get("platform", "").lower() == "termux"
            safety_headroom = max(300.0, 0.10 * total_mem_mb) if is_android else max(1024.0, 0.05 * total_mem_mb)
            effective_avail_mem_mb = max(0.0, (avail_mem_mb - safety_headroom) - reserved_mem)

            st_details = hb_system.get("storage_details")
            if st_details and isinstance(st_details, dict) and "total_gb" in st_details:
                total_st_gb = float(st_details.get("total_gb", 0.0))
                avail_st_gb = float(st_details.get("available_gb", 0.0))
            else:
                storage_data = hb_system.get("storage") or str(resources.get("storage_gb", "0"))
                total_st_gb, _, avail_st_gb = parse_storage_gb(storage_data)

            effective_avail_st_gb = max(0.0, avail_st_gb - reserved_st)
            active_cnt = active_counts.get(node_id, 0)

            total_score, breakdown = ResourceScheduler.calculate_score(
                node=node,
                reqs=reqs,
                effective_avail_mem_mb=effective_avail_mem_mb,
                total_mem_mb=total_mem_mb,
                effective_avail_st_gb=effective_avail_st_gb,
                total_st_gb=total_st_gb,
                active_cnt=active_cnt
            )

            explanation = (
                f"ONLINE | score={total_score}/100 | "
                f"free RAM: ~{effective_avail_mem_mb:.0f}MB | "
                f"active jobs: {active_cnt}"
            )

            liveness = compute_node_liveness(node, timeout_seconds)
            candidates.append({
                "node_id": node_id,
                "name": node.get("name", "unknown"),
                "role": node.get("role", "compute"),
                "score": total_score,
                "score_breakdown": breakdown,
                "metrics": {
                    "avail_mem_mb": round(effective_avail_mem_mb, 0),
                    "reserved_mem_mb": round(reserved_mem, 0),
                    "active_jobs": active_cnt,
                    "avail_storage_gb": round(effective_avail_st_gb, 1)
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

        # Deterministic tie-breaking: highest score first, then lexical node_id
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
    """Evaluates whether an individual node meets health, state, and resource requirements."""
    eligible, _ = ResourceScheduler.is_node_eligible(
        node,
        requirements,
        timeout_seconds=timeout_seconds,
        reserved_mem_mb=reserved_mem_mb
    )
    return eligible


if __name__ == "__main__":
    print("PersonalServer ResourceScheduler module loaded.")
