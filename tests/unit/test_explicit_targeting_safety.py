"""
Unit tests for PersonalServer D1.1 Isolation Hardening — Explicit Targeting Safety.
Verifies that explicit targeting of experimental or specialized nodes never bypasses:
1. worker_enabled == false
2. scheduler_eligible == false
3. participation_state == OFF
4. Required capability unavailable or permission denied
5. Safe execution when all conditions permit
6. Preservation of existing behavior for production nodes
"""

import time
try:
    import pytest
except ImportError:
    pytest = None
from scheduler.scheduler import (
    ResourceScheduler,
    STATE_ONLINE,
    REJECTION_ROLE_MISMATCH,
    REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED,
    REJECTION_UNSATISFIABLE_CAPABILITY,
)


def _make_experimental_node(
    node_id="server-v2334-a531",
    worker_enabled=False,
    scheduler_eligible=False,
    participation_state="OFF",
    verified_capabilities=None,
    permissions=None,
):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if verified_capabilities is None:
        verified_capabilities = {
            "compute": {
                "state": "READY",
                "is_available": True,
                "details": {"architecture": "aarch64", "cores": 8},
            },
            "power": {
                "state": "PERMISSION_REQUIRED",
                "is_available": False,
                "details": {"battery_accessible": False},
            },
        }
    if permissions is None:
        permissions = {
            "android.permission.CAMERA": False,
            "android.permission.RECORD_AUDIO": False,
        }

    return {
        "node_id": node_id,
        "name": "Vivo V2334 (Experimental)",
        "role": "experimental-worker",
        "platform": "termux",
        "architecture": "aarch64",
        "status": STATE_ONLINE,
        "last_seen": now_iso,
        "worker_enabled": worker_enabled,
        "scheduler_eligible": scheduler_eligible,
        "participation_state": participation_state,
        "resources": {"cpu_cores": 8, "ram_mb": 7419, "storage_gb": 54},
        "capabilities": {
            "compute": True,
            "power": False,
        },
        "verified_capabilities": verified_capabilities,
        "permissions": permissions,
        "last_heartbeat": {
            "timestamp": now_iso,
            "system": {
                "cpu_cores": 8,
                "cpu_percent": 0.0,
                "ram_total_mb": 7419,
                "ram_available_mb": 2000,
                "storage_total_gb": 226,
                "storage_available_gb": 54,
            },
        },
    }


def _make_production_node(node_id="realme-5-pro"):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "node_id": node_id,
        "name": "Realme 5 Pro",
        "role": "compute",
        "platform": "termux",
        "architecture": "aarch64",
        "status": STATE_ONLINE,
        "last_seen": now_iso,
        "worker_enabled": True,
        "scheduler_eligible": True,
        "participation_state": "AVAILABLE",
        "resources": {"cpu_cores": 8, "ram_mb": 7500, "storage_gb": 64},
        "capabilities": {"compute": True, "storage": True, "network": True},
        "verified_capabilities": {"compute": True, "storage": True, "network": True},
        "last_heartbeat": {
            "timestamp": now_iso,
            "system": {
                "cpu_cores": 8,
                "cpu_percent": 0.0,
                "ram_total_mb": 7500,
                "ram_available_mb": 3500,
                "storage_total_gb": 64,
                "storage_available_gb": 20,
            },
        },
    }


def test_1_explicitly_targeted_experimental_node_while_off():
    """1. Explicitly targeted experimental node while OFF must be rejected."""
    node = _make_experimental_node(
        worker_enabled=True,
        scheduler_eligible=True,
        participation_state="OFF",
    )
    reqs = {"target_node": "server-v2334-a531", "role": "experimental-worker"}

    # Eligibility check must fail
    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
    assert not eligible
    assert "ADMINISTRATIVE_LOCK" in reason
    assert "participation_state=OFF" in reason

    # Scheduler select_node must reject candidate
    nodes_db = {"nodes": {node["node_id"]: node}}
    decision = ResourceScheduler.select_node(reqs, nodes_db)
    assert decision["selected_node"] is None
    assert node["node_id"] in decision["rejected"]
    assert "ADMINISTRATIVE_LOCK" in decision["rejected"][node["node_id"]]


def test_2_explicitly_targeted_node_scheduler_eligibility_disabled():
    """2. Explicitly targeted node with scheduler eligibility disabled must be rejected."""
    node = _make_experimental_node(
        worker_enabled=True,
        scheduler_eligible=False,
        participation_state="AVAILABLE",
    )
    reqs = {"target_node": "server-v2334-a531", "role": "experimental-worker"}

    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
    assert not eligible
    assert "ADMINISTRATIVE_LOCK" in reason
    assert "scheduler_eligible=False" in reason

    nodes_db = {"nodes": {node["node_id"]: node}}
    decision = ResourceScheduler.select_node(reqs, nodes_db)
    assert decision["selected_node"] is None
    assert node["node_id"] in decision["rejected"]
    assert "ADMINISTRATIVE_LOCK" in decision["rejected"][node["node_id"]]


def test_3_explicitly_targeted_node_worker_disabled():
    """3. Explicitly targeted node with worker disabled must be rejected."""
    node = _make_experimental_node(
        worker_enabled=False,
        scheduler_eligible=True,
        participation_state="AVAILABLE",
    )
    reqs = {"target_node": "server-v2334-a531", "role": "experimental-worker"}

    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
    assert not eligible
    assert "ADMINISTRATIVE_LOCK" in reason
    assert "worker_enabled=False" in reason

    nodes_db = {"nodes": {node["node_id"]: node}}
    decision = ResourceScheduler.select_node(reqs, nodes_db)
    assert decision["selected_node"] is None
    assert node["node_id"] in decision["rejected"]
    assert "ADMINISTRATIVE_LOCK" in decision["rejected"][node["node_id"]]


def test_4_explicitly_targeted_node_permission_unavailable():
    """4. Explicitly targeted node with required permission/capability unavailable must be rejected."""
    node = _make_experimental_node(
        worker_enabled=True,
        scheduler_eligible=True,
        participation_state="AVAILABLE",
        verified_capabilities={
            "compute": {"state": "READY", "is_available": True},
            "power": {"state": "PERMISSION_REQUIRED", "is_available": False},
        },
        permissions={"android.permission.CAMERA": False},
    )

    # 4a: Capability in PERMISSION_REQUIRED state
    reqs_cap = {
        "target_node": "server-v2334-a531",
        "capabilities": ["power"],
    }
    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs_cap)
    assert not eligible
    assert "HARDWARE_PERIPHERAL_UNPERMITTED" in reason or "UNSATISFIABLE" in reason

    nodes_db = {"nodes": {node["node_id"]: node}}
    decision = ResourceScheduler.select_node(reqs_cap, nodes_db)
    assert decision["selected_node"] is None
    assert node["node_id"] in decision["rejected"]

    # 4b: Explicit permission requested but denied/missing on node
    reqs_perm = {
        "target_node": "server-v2334-a531",
        "required_permissions": ["android.permission.CAMERA"],
    }
    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs_perm)
    assert not eligible
    assert "HARDWARE_PERIPHERAL_UNPERMITTED" in reason
    assert "android.permission.CAMERA" in reason


def test_5_explicitly_targeted_node_when_all_conditions_permit():
    """5. Explicitly targeted node when all safety conditions permit execution must be scheduled."""
    node = _make_experimental_node(
        worker_enabled=True,
        scheduler_eligible=True,
        participation_state="AVAILABLE",
        verified_capabilities={
            "compute": {"state": "READY", "is_available": True},
        },
    )
    reqs = {
        "target_node": "server-v2334-a531",
        "capabilities": ["compute"],
    }

    eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
    assert eligible, f"Expected node to be eligible, but got: {reason}"

    nodes_db = {"nodes": {node["node_id"]: node}}
    decision = ResourceScheduler.select_node(reqs, nodes_db)
    assert decision["selected_node"] == "server-v2334-a531"
    assert decision["score"] > 0


def test_6_preserve_existing_behavior_production_nodes():
    """Preserve existing behavior: production workloads schedule to production workers, never experimental."""
    exp_node = _make_experimental_node(
        worker_enabled=True,
        scheduler_eligible=True,
        participation_state="AVAILABLE",
    )
    prod_node = _make_production_node()

    nodes_db = {
        "nodes": {
            exp_node["node_id"]: exp_node,
            prod_node["node_id"]: prod_node,
        }
    }

    # Standard general job with no explicit target
    general_reqs = {"capabilities": ["compute"]}
    decision = ResourceScheduler.select_node(general_reqs, nodes_db)

    # Must select production node, reject experimental node for specialized role mismatch
    assert decision["selected_node"] == "realme-5-pro"
    assert exp_node["node_id"] in decision["rejected"]
    assert "Specialized role 'experimental-worker' requires explicit job role request" in decision["rejected"][exp_node["node_id"]]

