#!/usr/bin/env python3
"""
Nodes Persistence Store
=======================
Persistent inventory for cluster nodes and hardware registration metadata.
"""

import sys
import json
from typing import Optional, Dict, Any
from pathlib import Path

from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_nodes_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads registered cluster nodes database.
    Rolls back to `.bak` if primary file is corrupted.
    Returns default empty cluster schema if files are absent or unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        nodes_file = curr_paths.nodes_file
        backup_file = curr_paths.backup_nodes_file
        if nodes_file.exists():
            try:
                with open(nodes_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Corrupt {nodes_file} ({e}). Attempting rollback to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            return json.load(bf)
                    except Exception:
                        pass
        return {"cluster_name": "PersonalServer", "nodes": {}}


def save_nodes_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the cluster nodes database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        atomic_save_json(curr_paths.nodes_file, curr_paths.backup_nodes_file, db, lock=DB_LOCK, bump_ha=True)
