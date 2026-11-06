#!/usr/bin/env python3
"""
PersonalServer Artifact Packager & Extractor
============================================
Handles safe, deterministic packaging of application source trees into versioned
release zip artifacts, with strict secret exclusion, path traversal protection,
SHA-256 calculation, and registration in the cluster artifact database.
"""

import os
import sys
import zipfile
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Set

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.artifact_manager import (
    generate_artifact_id,
    compute_file_sha256,
    validate_artifact_path,
    store_artifact_bytes,
)
from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths
from controller.stores import load_artifacts_db, save_artifacts_db

EXCLUDED_DIR_NAMES: Set[str] = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    ".virtualenv",
    ".idea",
    ".vscode",
    "node_modules",
}

EXCLUDED_FILE_SUFFIXES: Set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".swp",
    ".swo",
    ".tmp",
}

EXCLUDED_FILE_NAMES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

EXCLUDED_SECRET_PATTERNS: Set[str] = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}


def is_path_excluded(rel_path: Path) -> bool:
    """Checks whether a relative file or directory path should be excluded from release packaging."""
    parts = rel_path.parts
    for part in parts:
        if part in EXCLUDED_DIR_NAMES:
            return True
        if part.startswith(".env"):
            return True

    name = rel_path.name
    if name in EXCLUDED_FILE_NAMES or name in EXCLUDED_SECRET_PATTERNS:
        return True

    suffix = rel_path.suffix.lower()
    if suffix in EXCLUDED_FILE_SUFFIXES:
        return True

    if suffix in (".pem", ".key", ".p12", ".pfx", ".kdbx"):
        return True

    return False


def package_source_to_artifact(
    source_dir: Path,
    app_id: str,
    deployment_id: str,
    commit_sha: str = "latest",
    storage_root: Optional[Path] = None,
    paths: Optional[Any] = None,
    authorized_nodes: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Packages application source files into a safe, versioned release zip artifact.
    Excludes .git, secrets, virtual environments, caches, and dangerous files.
    Calculates SHA-256, stores artifact atomically, and registers metadata in artifacts_db.
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.exists() or not source_dir.is_dir():
        raise ValueError(f"Source directory '{source_dir}' does not exist or is not a directory.")

    ctrl_paths = paths or get_controller_paths()
    ctrl_paths.ensure_directories()
    artifacts_storage = storage_root or getattr(ctrl_paths, "artifacts_dir", BASE_DIR / "storage" / "artifacts")
    artifacts_storage = Path(artifacts_storage).resolve()
    artifacts_storage.mkdir(parents=True, exist_ok=True)

    art_id = generate_artifact_id()
    short_commit = commit_sha[:8] if commit_sha and commit_sha != "latest" else "release"
    filename = f"{app_id}-{short_commit}-{deployment_id}.zip"

    # Temporary staging zip path
    staging_dir = artifacts_storage / "_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_zip = staging_dir / f"{filename}.tmp"

    try:
        with zipfile.ZipFile(staging_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(source_dir):
                rel_root = Path(root).relative_to(source_dir)
                dirs[:] = [d for d in dirs if not is_path_excluded(rel_root / d)]

                for file in files:
                    file_path = Path(root) / file
                    rel_file = rel_root / file

                    if is_path_excluded(rel_file):
                        continue

                    # Protect against symlinks escaping source_dir
                    if file_path.is_symlink():
                        try:
                            resolved_symlink = file_path.resolve()
                            resolved_symlink.relative_to(source_dir)
                        except ValueError:
                            continue

                    arcname = str(rel_file).replace("\\", "/")
                    zf.write(file_path, arcname=arcname)

        with open(staging_zip, "rb") as f:
            content_bytes = f.read()

        sha256 = hashlib.sha256(content_bytes).hexdigest()

        # Check existing artifacts in artifacts_db for matching SHA-256 (Canonical artifact identity model)
        db = load_artifacts_db(paths=ctrl_paths)
        canonical_art = None
        canonical_id = None
        for exist_id, exist_entry in db.get("artifacts", {}).items():
            if exist_entry.get("sha256", "").lower() == sha256.lower():
                canonical_art = exist_entry
                canonical_id = exist_entry.get("canonical_artifact_id") or exist_id
                break

        now_iso = get_current_iso_timestamp()

        if canonical_art:
            # Re-use existing storage, register art_id as an explicit alias of canonical artifact
            record = {
                "artifact_id": art_id,
                "canonical_artifact_id": canonical_id,
                "is_alias": True,
                "filename": filename,
                "sha256": sha256,
                "size_bytes": len(content_bytes),
                "path": canonical_art.get("path"),
                "content_type": "application/zip",
                "created_at": now_iso,
                "authorized_nodes": list(dict.fromkeys((canonical_art.get("authorized_nodes") or []) + list(authorized_nodes or []))),
                "metadata": {
                    "app_id": app_id,
                    "deployment_id": deployment_id,
                    "commit_sha": commit_sha,
                    "type": "application-release",
                    "alias_of": canonical_id,
                    "deduplicated": True
                }
            }
            canonical_art.setdefault("aliases", [])
            if art_id not in canonical_art["aliases"]:
                canonical_art["aliases"].append(art_id)
            canonical_art.setdefault("authorized_nodes", [])
            for n in (authorized_nodes or []):
                if n not in canonical_art["authorized_nodes"]:
                    canonical_art["authorized_nodes"].append(n)
        else:
            record = store_artifact_bytes(
                storage_root=artifacts_storage,
                artifact_id=art_id,
                filename=filename,
                content_bytes=content_bytes,
                content_type="application/zip",
                metadata={
                    "app_id": app_id,
                    "deployment_id": deployment_id,
                    "commit_sha": commit_sha,
                    "type": "application-release",
                }
            )
            record["canonical_artifact_id"] = art_id
            record["is_alias"] = False
            record["aliases"] = []
            record["authorized_nodes"] = list(authorized_nodes or [])

        # Persist into controller artifacts database
        db.setdefault("artifacts", {})[art_id] = record
        save_artifacts_db(db, paths=ctrl_paths)

        return record

    finally:
        if staging_zip.exists():
            try:
                staging_zip.unlink()
            except Exception:
                pass


def safe_extract_artifact(archive_path: Path, target_dir: Path) -> None:
    """
    Safely extracts a release zip archive into target_dir with strict path traversal checks.
    Guards against Zip Slip vulnerabilities.
    """
    archive_path = Path(archive_path).resolve()
    target_dir = Path(target_dir).resolve()

    if not archive_path.exists() or not archive_path.is_file():
        raise ValueError(f"Artifact archive not found: {archive_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            member_path = member.filename
            if member_path.startswith("/") or member_path.startswith("\\"):
                raise ValueError(f"Unsafe absolute path in zip archive: '{member_path}'")
            if ".." in member_path.split("/") or ".." in member_path.split("\\"):
                raise ValueError(f"Path traversal detected in zip archive: '{member_path}'")

            dest_file = (target_dir / member_path).resolve()
            try:
                dest_file.relative_to(target_dir)
            except ValueError:
                raise ValueError(f"Zip slip escape detected for file: '{member_path}'")

            zf.extract(member, target_dir)

