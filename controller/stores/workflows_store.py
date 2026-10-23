#!/usr/bin/env python3
"""
Workflows Persistence Store
===========================
Persistent storage for multi-step DAG workflows and task dependency graphs.
"""

import sys
import json
from typing import Optional, Dict, Any
from pathlib import Path

from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_workflows_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads workflows database.
    Rolls back to `.bak` if primary file is corrupted.
    Returns default schema if unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        wf_file = getattr(curr_paths, "workflows_file", curr_paths.data_dir / "workflows.json")
        backup_file = getattr(curr_paths, "backup_workflows_file", curr_paths.data_dir / "workflows.json.bak")
        if wf_file.exists():
            try:
                with open(wf_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Corrupt {wf_file} ({e}). Rolling back to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            return json.load(bf)
                    except Exception:
                        pass
        return {"workflows": {}}


def save_workflows_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the workflows database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        wf_file = getattr(curr_paths, "workflows_file", curr_paths.data_dir / "workflows.json")
        backup_file = getattr(curr_paths, "backup_workflows_file", curr_paths.data_dir / "workflows.json.bak")
        atomic_save_json(wf_file, backup_file, db, lock=DB_LOCK, bump_ha=True)
