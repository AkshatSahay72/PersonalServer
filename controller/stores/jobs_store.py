#!/usr/bin/env python3
"""
Jobs Persistence Store
======================
Persistent storage for workload execution history, lease states, and scheduling results.
Features automatic self-healing of primary storage from backup upon corruption.
"""

import sys
import json
import shutil
from typing import Optional, Dict, Any
from pathlib import Path

from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_jobs_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads persistent jobs database.
    Rolls back to `.bak` and self-heals primary file if corruption is detected.
    Returns default schema if unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        jobs_file = curr_paths.jobs_file
        backup_file = curr_paths.backup_jobs_file
        if jobs_file.exists():
            try:
                with open(jobs_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Corrupt {jobs_file} ({e}). Rolling back to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            recovered = json.load(bf)
                        # Self-heal primary file from valid backup
                        try:
                            shutil.copy2(backup_file, jobs_file)
                        except Exception:
                            pass
                        return recovered
                    except Exception:
                        pass
        return {"jobs": {}}


def save_jobs_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the workload jobs database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        atomic_save_json(curr_paths.jobs_file, curr_paths.backup_jobs_file, db, lock=DB_LOCK, bump_ha=True)
