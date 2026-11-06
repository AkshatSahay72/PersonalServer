#!/usr/bin/env python3
"""
PersonalServer Cluster Leader Ranking & Selection Engine
=========================================================
Implements deterministic, priority and performance-based leader ranking,
node leadership eligibility verification, and failover candidate ranking.

Rules:
1. Only eligible nodes (leader_eligible=True) with status ONLINE are candidates.
2. The Windows laptop / worker-only nodes are strictly excluded (leader_eligible=False).
3. A healthy current leader retains leadership (no automatic preemption by returning nodes).
4. Priority carries the dominant weight so administrative intent is respected over transient metric fluctuations.
5. Ties are broken deterministically by node_id.
"""

from typing import Dict, Any, List, Optional, Tuple
import time

# Configurable default weights (sum to 1.0)
DEFAULT_WEIGHT_PRIORITY = 0.50
DEFAULT_WEIGHT_UPTIME = 0.20
DEFAULT_WEIGHT_RESOURCES = 0.15
DEFAULT_WEIGHT_NETWORK = 0.15

# Default per-node role initializations
DEFAULT_NODE_POLICIES = {
    # Vivo Y31 (Leader)
    "server-5387a86bf36116b1": {
        "leader_eligible": True,
        "preferred_leader": True,
        "priority": 100,
        "worker_enabled": True,
        "build_worker_enabled": False,
    },
    # Realme 5 Pro (Standby / Worker)
    "server-95bad5ff01424d4c8d184330d6d2e394": {
        "leader_eligible": True,
        "preferred_leader": False,
        "priority": 90,
        "worker_enabled": True,
        "build_worker_enabled": False,
    },
    # Windows Laptop (Aki - Dedicated Compute & Build Worker)
    "server-win-c03532e6881c": {
        "leader_eligible": False,
        "preferred_leader": False,
        "priority": 0,
        "worker_enabled": True,
        "build_worker_enabled": True,
    }
}


def apply_default_leadership_metadata(node_id: str, node_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures a node record has all leadership, preference, and worker flags populated."""
    policy = DEFAULT_NODE_POLICIES.get(node_id, {})
    
    # Check platform heuristics if not in predefined map
    platform = str(node_data.get("platform", "")).lower()
    is_windows = "win" in platform
    default_eligible = False if is_windows else policy.get("leader_eligible", True)
    
    if "leader_eligible" not in node_data:
        node_data["leader_eligible"] = policy.get("leader_eligible", default_eligible)
    if "preferred_leader" not in node_data:
        node_data["preferred_leader"] = policy.get("preferred_leader", False)
    if "priority" not in node_data:
        node_data["priority"] = policy.get("priority", 50 if node_data["leader_eligible"] else 0)
    if "worker_enabled" not in node_data:
        node_data["worker_enabled"] = policy.get("worker_enabled", True)
    if "build_worker_enabled" not in node_data:
        node_data["build_worker_enabled"] = policy.get("build_worker_enabled", is_windows)
    if "performance_score" not in node_data:
        node_data["performance_score"] = 0.0

    return node_data


def calculate_node_performance_score(
    node: Dict[str, Any],
    heartbeat_timeout_sec: float = 30.0,
    weights: Optional[Dict[str, float]] = None
) -> float:
    """
    Calculates a normalized 0.0 - 100.0 score for a node based on:
    - Administrative Priority (0 - 100, normalized)
    - Uptime / Availability Stability
    - CPU & Memory Resource Headroom
    - Network / Heartbeat Freshness
    """
    if not node.get("leader_eligible", False):
        return 0.0
    if str(node.get("status", "")).upper() != "ONLINE":
        return 0.0

    w = weights or {
        "priority": DEFAULT_WEIGHT_PRIORITY,
        "uptime": DEFAULT_WEIGHT_UPTIME,
        "resources": DEFAULT_WEIGHT_RESOURCES,
        "network": DEFAULT_WEIGHT_NETWORK,
    }

    # 1. Priority Component (0 - 100)
    raw_prio = float(node.get("priority", 50))
    prio_score = max(0.0, min(100.0, raw_prio))

    # 2. Uptime / Stability Component
    # Check last_seen or last_heartbeat
    last_hb = node.get("last_heartbeat", {})
    sys_metrics = last_hb.get("system", {}) if isinstance(last_hb, dict) else {}
    uptime_val = sys_metrics.get("uptime", "")
    uptime_score = 80.0  # default baseline for healthy node
    if uptime_val and uptime_val != "N/A" and uptime_val != "simulated":
        try:
            # Parse uptime seconds if integer/float string
            sec = float(uptime_val)
            uptime_score = min(100.0, 50.0 + (sec / 86400.0) * 50.0)  # max at 1 day
        except (ValueError, TypeError):
            uptime_score = 85.0

    # 3. Resources Component (CPU & RAM availability)
    cpu_pct = float(sys_metrics.get("cpu_percent", 20.0) or 20.0)
    cpu_headroom = max(0.0, 100.0 - cpu_pct)
    res = node.get("resources", {})
    ram_mb = float(res.get("ram_mb", 4096) or 4096)
    # Give higher baseline to nodes with at least 3GB RAM
    ram_score = min(100.0, (ram_mb / 6144.0) * 100.0)
    resources_score = (cpu_headroom * 0.5) + (ram_score * 0.5)

    # 4. Network Component (Heartbeat freshness)
    now = time.time()
    last_seen_epoch = 0.0
    last_seen_str = node.get("last_seen")
    if last_seen_str:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(last_seen_str).replace("Z", "+00:00"))
            last_seen_epoch = dt.timestamp()
        except Exception:
            last_seen_epoch = now
    else:
        last_seen_epoch = now

    age_sec = max(0.0, now - last_seen_epoch)
    if age_sec >= heartbeat_timeout_sec:
        network_score = 0.0
    else:
        network_score = max(0.0, 100.0 * (1.0 - (age_sec / heartbeat_timeout_sec)))

    total_score = (
        (w["priority"] * prio_score) +
        (w["uptime"] * uptime_score) +
        (w["resources"] * resources_score) +
        (w["network"] * network_score)
    )

    return round(total_score, 2)


def rank_leader_candidates(
    nodes: Dict[str, Dict[str, Any]],
    current_leader_id: Optional[str] = None,
    preferred_leader_id: Optional[str] = None,
    mode: str = "automatic",
    heartbeat_timeout_sec: float = 30.0,
    weights: Optional[Dict[str, float]] = None
) -> List[Tuple[str, float, Dict[str, Any]]]:
    """
    Ranks eligible, healthy nodes for cluster leadership.
    Returns list of tuples: (node_id, score, node_data) ordered highest to lowest score.

    In 'manual_preference' mode:
    If preferred_leader_id is online and eligible, it is ranked first.

    If current_leader_id is provided, online, and eligible, it retains leadership
    (non-preemption: higher score candidates do not trigger automatic churn).
    """
    scored_candidates = []

    for nid, node in nodes.items():
        if str(node.get("status", "")).upper() == "REMOVED":
            continue

        # Ensure all leadership policy fields exist
        apply_default_leadership_metadata(nid, node)

        # 1. Eligibility Check
        if not node.get("leader_eligible", False):
            node["performance_score"] = 0.0
            continue

        # 2. Status Check (Must be ONLINE)
        if str(node.get("status", "")).upper() != "ONLINE":
            node["performance_score"] = 0.0
            continue

        score = calculate_node_performance_score(node, heartbeat_timeout_sec, weights)
        node["performance_score"] = score
        scored_candidates.append((nid, score, node))

    # Sort descending by score, tie-break by node_id
    scored_candidates.sort(key=lambda item: (item[1], item[0]), reverse=True)

    # In manual preference mode, if preferred leader is in candidates, hoist it to top
    if mode == "manual_preference" and preferred_leader_id:
        for idx, (nid, score, node) in enumerate(scored_candidates):
            if nid == preferred_leader_id:
                pref_item = scored_candidates.pop(idx)
                scored_candidates.insert(0, pref_item)
                break

    return scored_candidates


def select_best_leader(
    nodes: Dict[str, Dict[str, Any]],
    current_leader_id: Optional[str] = None,
    preferred_leader_id: Optional[str] = None,
    mode: str = "automatic",
    heartbeat_timeout_sec: float = 30.0
) -> Optional[Tuple[str, str]]:
    """
    Selects the authoritative leader node.
    Returns (leader_node_id, reason) or None if no valid eligible candidates exist.
    """
    ranked = rank_leader_candidates(
        nodes=nodes,
        current_leader_id=current_leader_id,
        preferred_leader_id=preferred_leader_id,
        mode=mode,
        heartbeat_timeout_sec=heartbeat_timeout_sec
    )

    if not ranked:
        return None

    # Check non-preemption: If current leader is still healthy and eligible, it holds leadership!
    if current_leader_id:
        for nid, score, node in ranked:
            if nid == current_leader_id:
                # If in manual preference mode and preferred leader is different, handover takes precedence
                if mode == "manual_preference" and preferred_leader_id and preferred_leader_id != current_leader_id:
                    # Check if preferred leader is available
                    if ranked[0][0] == preferred_leader_id:
                        return (preferred_leader_id, f"Manual preference selected node '{preferred_leader_id}'")
                return (current_leader_id, "Current leader is healthy and retains leadership (non-preemption)")

    # Otherwise select the top ranked candidate
    best_nid, best_score, _ = ranked[0]
    reason = (
        f"Selected preferred leader '{best_nid}'"
        if (mode == "manual_preference" and best_nid == preferred_leader_id)
        else f"Selected highest ranked candidate '{best_nid}' (Score: {best_score})"
    )
    return (best_nid, reason)
