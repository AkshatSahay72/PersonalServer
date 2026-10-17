#!/usr/bin/env python3
"""
Storage Utilities
=================
Sandboxed path resolution and path traversal protection for cluster and application storage.
"""

import os
import re
from pathlib import Path
from typing import Union


def resolve_safe_path(base_dir: Union[str, Path], relative_path_str: str) -> Path:
    """
    Strictly resolves and validates that relative_path_str stays within base_dir.
    Rejects path traversal, absolute path escapes, drive letters, null bytes,
    and outside directory access.
    Raises ValueError on invalid or escaping paths.
    """
    base = Path(base_dir).resolve()

    if not relative_path_str:
        return base

    # Reject null bytes
    if "\0" in relative_path_str:
        raise ValueError("Invalid path: Null byte detected")

    # Reject absolute path escapes or drive letters (e.g. /etc, \windows, C:\...)
    if (
        relative_path_str.startswith("/")
        or relative_path_str.startswith("\\")
        or (len(relative_path_str) > 1 and relative_path_str[1] == ":")
    ):
        raise ValueError("Access Denied: Absolute path escape detected")

    # Normalize backslashes to forward slashes
    clean_path = relative_path_str.replace("\\", "/")

    # Check for prohibited segments
    parts = Path(clean_path).parts
    if ".." in parts or any(".." in p for p in parts):
        raise ValueError("Access Denied: Path traversal detected")

    norm_rel = os.path.normpath(clean_path).lstrip("./\\")
    target = (base / norm_rel).resolve()

    # Strict containment check
    try:
        common = os.path.commonpath([str(target), str(base)])
        if os.path.abspath(common) != str(base):
            raise ValueError("Access Denied: Path escapes storage root")
        # Secondary check using relative_to
        target.relative_to(base)
    except Exception as e:
        if isinstance(e, ValueError) and "Access Denied" in str(e):
            raise
        raise ValueError(f"Access Denied: Path verification failed ({e})")

    return target


def validate_app_id(app_id: str) -> str:
    """
    Validates that an application identifier matches [a-zA-Z0-9_-]+.
    Raises ValueError on invalid application IDs.
    """
    if not app_id or not isinstance(app_id, str) or not re.match(r'^[a-zA-Z0-9_-]+$', app_id):
        raise ValueError("Invalid application ID")
    return app_id
