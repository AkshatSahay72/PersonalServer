#!/usr/bin/env python3
"""
Applications Persistence Store
==============================
Persistent registry for deployed container and script applications.
"""

import sys
import json
from typing import Optional, Dict, Any
from pathlib import Path

from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_apps_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads registered applications database.
    Rolls back to `.bak` if primary file is corrupted.
    Returns default schema if unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        apps_file = curr_paths.apps_file
        backup_file = curr_paths.backup_apps_file
        if apps_file.exists():
            try:
                with open(apps_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Corrupt {apps_file} ({e}). Rolling back to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            return json.load(bf)
                    except Exception:
                        pass
        return {"apps": {}}


def save_apps_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the applications database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        atomic_save_json(curr_paths.apps_file, curr_paths.backup_apps_file, db, lock=DB_LOCK, bump_ha=True)
