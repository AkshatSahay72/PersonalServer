#!/usr/bin/env python3
"""
Base Persistence Infrastructure
===============================
Provides re-entrant file locking and high-availability state version synchronization.
"""

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
APPS_LOCK = threading.Lock()

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


def atomic_save_json(target_file: Path, backup_file: Path, data: dict, lock: threading.RLock = DB_LOCK, bump_ha: bool = True) -> None:
    """
    Atomically writes JSON data to a file using a temporary file and `.bak` copy.
    Guarantees that readers never observe partially written files.
    """
    with lock:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        if target_file.exists():
            try:
                shutil.copy2(target_file, backup_file)
            except Exception:
                pass

        temp_file.replace(target_file)
        if bump_ha:
            bump_state_version()
