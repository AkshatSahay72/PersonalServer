#!/usr/bin/env python3
"""
Base Persistence Infrastructure
===============================
Provides re-entrant file locking and high-availability state version synchronization.
"""

import os
import sys
import json
import shutil
import threading
from typing import Callable, Optional
from pathlib import Path

# Centralized DB lock protecting controller state mutations
DB_LOCK = threading.RLock()

# Specialized subsystem locks
ONBOARDING_LOCK = threading.Lock()
APPS_LOCK = threading.RLock()

# Optional callback hook for HA state version bumping
_state_version_bumper: Optional[Callable[[], None]] = None


def set_state_version_bumper(callback: Optional[Callable[[], None]]) -> None:
    """Registers a callback to bump the HA state version upon successful atomic writes."""
    global _state_version_bumper
    _state_version_bumper = callback


def bump_state_version() -> None:
    """Triggers the registered HA state version bump if available."""
    global _state_version_bumper
    if _state_version_bumper:
        try:
            _state_version_bumper()
        except Exception:
            pass


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of parent directory so atomic renames survive power loss."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _is_valid_json_file(file_path: Path) -> bool:
    """Returns True only if file_path exists, is non-empty, and parses cleanly as JSON."""
    try:
        if not file_path.exists() or file_path.stat().st_size == 0:
            return False
        with open(file_path, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def atomic_write_json_file(target_file: Path, data: dict) -> None:
    """Writes JSON atomically using flush + fsync + replace + parent dir fsync."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_file.with_name(f"{target_file.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp_file.replace(target_file)
        _fsync_dir(target_file.parent)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass


def atomic_save_json(target_file: Path, backup_file: Path, data: dict, lock: threading.RLock = DB_LOCK, bump_ha: bool = True) -> None:
    """
    Atomically writes JSON data to a file using a temporary file and `.bak` copy.
    Uses fsync barriers and validates existing data before rotating `.bak` so
    a corrupt or un-flushed primary file never overwrites a healthy backup.
    """
    with lock:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_file.with_name(f"{target_file.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            if backup_file is not None and _is_valid_json_file(target_file):
                bak_tmp = backup_file.with_name(f"{backup_file.name}.tmp.{os.getpid()}.{threading.get_ident()}")
                try:
                    with open(target_file, "rb") as src, open(bak_tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    bak_tmp.replace(backup_file)
                except Exception:
                    if bak_tmp.exists():
                        try:
                            bak_tmp.unlink()
                        except Exception:
                            pass

            temp_file.replace(target_file)
            _fsync_dir(target_file.parent)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    # State-version bump must occur strictly AFTER DB_LOCK is released to prevent AB-BA deadlock
    if bump_ha:
        bump_state_version()
