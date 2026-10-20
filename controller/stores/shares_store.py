#!/usr/bin/env python3
"""
Storage Shares Persistence Store
================================
Persistent registry for public storage share links.
"""

import sys
import json
import secrets
from typing import Optional, Dict, Any
from pathlib import Path

from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths
from controller.stores.base import DB_LOCK, atomic_save_json


def load_shares_db(paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Loads registered storage shares database.
    Rolls back to `.bak` if primary file is corrupted.
    Returns default schema if unrecoverable.
    """
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        shares_file = getattr(curr_paths, "shares_file", curr_paths.data_dir / "shares.json")
        backup_file = getattr(curr_paths, "backup_shares_file", curr_paths.data_dir / "shares.json.bak")
        if shares_file.exists():
            try:
                with open(shares_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "shares" in data:
                        return data
                    return {"shares": {}}
            except Exception as e:
                print(f"Warning: Corrupt {shares_file} ({e}). Rolling back to backup...", file=sys.stderr)
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            data = json.load(bf)
                            if isinstance(data, dict) and "shares" in data:
                                return data
                    except Exception:
                        pass
        return {"shares": {}}


def save_shares_db(db: Dict[str, Any], paths: Optional[Any] = None) -> None:
    """Atomically persists the shares database and creates a backup."""
    curr_paths = paths or get_controller_paths()
    with DB_LOCK:
        curr_paths.ensure_directories()
        shares_file = getattr(curr_paths, "shares_file", curr_paths.data_dir / "shares.json")
        backup_file = getattr(curr_paths, "backup_shares_file", curr_paths.data_dir / "shares.json.bak")
        atomic_save_json(shares_file, backup_file, db, lock=DB_LOCK, bump_ha=True)


def get_share_by_id(share_id: str, paths: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a share record by its unguessable share identifier."""
    if not share_id or not isinstance(share_id, str):
        return None
    db = load_shares_db(paths)
    return db.get("shares", {}).get(share_id)


def find_active_share(node_id: str, rel_path: str, paths: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """
    Finds the active (non-revoked) share record for a given node and relative file path.
    Matches exact canonical path, with case-normalized fallback for cross-platform tolerance.
    """
    if not rel_path:
        return None
    clean_path = rel_path.replace("\\", "/").strip("/")
    target_node = (node_id or "").strip()
    if target_node.lower() in ("auto", "cluster", "local", "self", "default", "none", "null"):
        target_node = ""

    db = load_shares_db(paths)
    for share in db.get("shares", {}).values():
        if not share.get("revoked", False):
            s_node = (share.get("node_id") or "").strip()
            s_path = (share.get("path") or "").replace("\\", "/").strip("/")
            if (s_path == clean_path or s_path.lower() == clean_path.lower()) and (not target_node or not s_node or s_node == target_node):
                return share
    return None


def create_share(node_id: str, rel_path: str, filename: str = "", is_dir: bool = False, paths: Optional[Any] = None) -> Dict[str, Any]:
    """
    Creates a new public share record with a cryptographically secure random token.
    Immediately revokes any existing active share for the same file/folder and node.
    """
    clean_path = rel_path.replace("\\", "/").strip("/")
    target_node = (node_id or "").strip()
    if target_node.lower() in ("auto", "cluster", "local", "self", "default", "none", "null"):
        target_node = ""
    clean_filename = filename or Path(clean_path).name or clean_path
    item_type = "folder" if is_dir else "file"

    with DB_LOCK:
        db = load_shares_db(paths)
        shares = db.setdefault("shares", {})

        # Revoke existing active shares for this specific file/folder/node
        for s in shares.values():
            if not s.get("revoked", False):
                s_node = (s.get("node_id") or "").strip()
                s_path = (s.get("path") or "").replace("\\", "/").strip("/")
                if (s_path == clean_path or s_path.lower() == clean_path.lower()) and (not target_node or not s_node or s_node == target_node):
                    s["revoked"] = True

        # Generate fresh, unguessable URL-safe identifier (16 bytes = 22 base64url chars)
        share_id = secrets.token_urlsafe(16)
        while share_id in shares:
            share_id = secrets.token_urlsafe(16)

        record = {
            "share_id": share_id,
            "node_id": target_node,
            "path": clean_path,
            "filename": clean_filename,
            "is_dir": bool(is_dir),
            "item_type": item_type,
            "created_at": get_current_iso_timestamp(),
            "revoked": False,
            "access": "link",
            "permission": "view"
        }

        shares[share_id] = record
        save_shares_db(db, paths)
        return record


def find_active_folder_share_for_path(node_id: str, rel_path: str, paths: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """
    Finds an active (non-revoked) folder share that contains the given relative path.
    Checks each parent directory segment of rel_path from immediate parent up to root.
    """
    if not rel_path:
        return None
    clean_path = rel_path.replace("\\", "/").strip("/")
    target_node = (node_id or "").strip()
    if target_node.lower() in ("auto", "cluster", "local", "self", "default", "none", "null"):
        target_node = ""

    db = load_shares_db(paths)
    parts = clean_path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:i])
        for share in db.get("shares", {}).values():
            if not share.get("revoked", False) and (share.get("is_dir") or share.get("item_type") == "folder"):
                s_node = (share.get("node_id") or "").strip()
                s_path = (share.get("path") or "").replace("\\", "/").strip("/")
                if (s_path == prefix or s_path.lower() == prefix.lower()) and (not target_node or not s_node or s_node == target_node):
                    return share
    return None


def revoke_share(share_id_or_path: str, node_id: Optional[str] = None, paths: Optional[Any] = None) -> bool:
    """
    Revokes public access for a share identified by token or file/folder path.
    If revoking a folder share, cascades revocation to any descendant file links.
    """
    if not share_id_or_path:
        return False

    with DB_LOCK:
        db = load_shares_db(paths)
        shares = db.setdefault("shares", {})
        revoked_any = False
        revoked_folder_paths = set()

        # Direct token match
        if share_id_or_path in shares:
            target = shares[share_id_or_path]
            if not target.get("revoked"):
                target["revoked"] = True
                revoked_any = True
                if target.get("is_dir") or target.get("item_type") == "folder":
                    revoked_folder_paths.add((target.get("path") or "").replace("\\", "/").strip("/"))

        # Path match
        clean_path = share_id_or_path.replace("\\", "/").strip("/")
        target_node = (node_id or "").strip()
        if target_node.lower() in ("auto", "cluster", "local", "self", "default", "none", "null"):
            target_node = ""
        for s in shares.values():
            if not s.get("revoked", False):
                s_node = (s.get("node_id") or "").strip()
                s_path = (s.get("path") or "").replace("\\", "/").strip("/")
                if (s_path == clean_path or s_path.lower() == clean_path.lower()) and (not target_node or not s_node or s_node == target_node):
                    s["revoked"] = True
                    revoked_any = True
                    if s.get("is_dir") or s.get("item_type") == "folder":
                        revoked_folder_paths.add(s_path)

        # Cascading revocation: ensure any active shares for descendants of revoked folders are revoked
        if revoked_folder_paths:
            for s in shares.values():
                if not s.get("revoked", False):
                    s_path = (s.get("path") or "").replace("\\", "/").strip("/")
                    for rfp in revoked_folder_paths:
                        if s_path == rfp or s_path.startswith(rfp + "/"):
                            s["revoked"] = True
                            revoked_any = True

        if revoked_any:
            save_shares_db(db, paths)
        return revoked_any

