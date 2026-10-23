#!/usr/bin/env python3
"""
Artifacts Persistence Store
===========================
Persistent metadata registry for cluster artifacts, checksums, and transfers.
"""

import sys
import json
from typing import Optional, Dict, Any
from pathlib import Path

from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_artifacts_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads artifacts database metadata.
    Rolls back to `.bak` if primary file is corrupted.
    Returns default schema if unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        art_file = getattr(curr_paths, "artifacts_file", curr_paths.data_dir / "artifacts.json")
        backup_file = getattr(curr_paths, "backup_artifacts_file", curr_paths.data_dir / "artifacts.json.bak")
        if art_file.exists():
            try:
                with open(art_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Corrupt {art_file} ({e}). Rolling back to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            return json.load(bf)
                    except Exception:
                        pass
        return {"artifacts": {}}


def save_artifacts_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the artifacts metadata database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        art_file = getattr(curr_paths, "artifacts_file", curr_paths.data_dir / "artifacts.json")
        backup_file = getattr(curr_paths, "backup_artifacts_file", curr_paths.data_dir / "artifacts.json.bak")
        atomic_save_json(art_file, backup_file, db, lock=DB_LOCK, bump_ha=True)
