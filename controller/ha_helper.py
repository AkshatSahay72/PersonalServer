#!/usr/bin/env python3
"""
Controller High Availability (HA) Helper
========================================
HA state versioning callbacks and centralized singleton HAManager instance resolution.
"""

from typing import Optional

from config.ha_manager import (
    HAManager,
    HAState,
    LeaderLease,
    find_active_leader,
    resolve_controller_node_id,
)
from config.platform_config import get_controller_paths, get_controller_runtime_config
from controller.stores.base import set_state_version_bumper

HA_MANAGER: Optional[HAManager] = None


def _bump_ha_state_version() -> None:
    """Bumps HA state version when local persistence stores mutate."""
    global HA_MANAGER
    if HA_MANAGER:
        try:
            HA_MANAGER.bump_state_version()
        except Exception:
            pass


set_state_version_bumper(_bump_ha_state_version)


def get_ha_manager(paths=None, role: str = "auto", node_id: Optional[str] = None, node_url: Optional[str] = None, force_reload: bool = False) -> HAManager:
    """
    Centralized resolver for the cluster HAManager singleton instance.
    """
    global HA_MANAGER
    curr_paths = paths or get_controller_paths()
    if HA_MANAGER is None or force_reload or (paths and getattr(HA_MANAGER, "paths", None) != paths):
        cfg = get_controller_runtime_config()
        resolved_node_id = resolve_controller_node_id(node_id=node_id, paths=curr_paths)
        HA_MANAGER = HAManager(
            node_id=resolved_node_id,
            node_url=node_url or f"http://{cfg.host if cfg.host != '0.0.0.0' else '127.0.0.1'}:{cfg.port}",
            role=role,
            paths=curr_paths
        )
        HA_MANAGER.initialize()
    return HA_MANAGER


__all__ = [
    "HA_MANAGER",
    "_bump_ha_state_version",
    "get_ha_manager",
    "HAManager",
    "HAState",
    "LeaderLease",
    "find_active_leader",
    "resolve_controller_node_id",
]
