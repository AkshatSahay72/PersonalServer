#!/usr/bin/env python3
"""
PersonalServer Result & Artifact Pipeline (J5)
==============================================
Manages attempt-specific output collection, path traversal security,
independent SHA-256 verification, and recoverable two-phase artifact promotion.

Implements J1/J5 requirements:
1. Attempt-specific output discovery based on job spec declared outputs.
2. Safe relative path validation (no absolute paths, no '..' traversal, no symlink escape).
3. Size enforcement: 50 MB per-file limit (MAX_SINGLE_ARTIFACT_LIMIT) and aggregate storage limits.
4. Required vs. optional output validation: process exit 0 is rejected if required outputs are missing.
5. Recoverable two-phase CAS promotion:
   Phase 1: Stage to isolated staging area with independent SHA-256 computation.
   Phase 2: Atomic rename/promote into permanent storage, updating artifacts_db and jobs_db atomically.
6. Garbage collection and cleanup of orphaned staging files and expired temporary workspaces.
"""

import os
import sys
import json
import time
import glob
import shutil
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.time_utils import get_current_iso_timestamp
from common.artifact_manager import (
    validate_artifact_path,
    compute_file_sha256,
    generate_artifact_id,
    MAX_ARTIFACT_SIZE_BYTES
)
from controller.stores.base import DB_LOCK
from controller.stores.artifacts_store import load_artifacts_db, save_artifacts_db
from controller.stores.jobs_store import load_jobs_db, save_jobs_db

# J1 Specification limits: 50 MB per-file limit default
MAX_SINGLE_ARTIFACT_LIMIT = 50 * 1024 * 1024  # 50 MB
DEFAULT_AGGREGATE_LIMIT = 200 * 1024 * 1024   # 200 MB


class ResultManager:
    """
    Manages result artifact discovery, security verification, and CAS promotion.
    """

    @classmethod
    def validate_safe_relative_path(cls, base_dir: Path, relative_path: str) -> Path:
        """
        Validates that relative_path is strictly relative, contains no '..',
        and resolves entirely inside base_dir without following malicious symlinks.
        """
        raw_path = str(relative_path or "").strip()
        if not raw_path:
            raise ValueError("Output path cannot be empty")

        if os.path.isabs(raw_path) or raw_path.startswith("/") or raw_path.startswith("\\"):
            raise ValueError(f"Absolute output path rejected: '{raw_path}'")

        if ".." in raw_path:
            raise ValueError(f"Path traversal ('..') rejected: '{raw_path}'")

        resolved_target = (base_dir / raw_path).resolve()
        resolved_base = base_dir.resolve()

        # Symlink escape and root escape check
        if not str(resolved_target).startswith(str(resolved_base)):
            raise ValueError(f"Output path escapes workspace: '{raw_path}'")

        # Reject if any component in the path is a symlink pointing outside base_dir
        curr = base_dir / raw_path
        if curr.is_symlink():
            link_target = curr.resolve()
            if not str(link_target).startswith(str(resolved_base)):
                raise ValueError(f"Symlink escapes workspace: '{raw_path}' -> '{link_target}'")

        return resolved_target

    @classmethod
    def collect_declared_outputs(
        cls,
        workspace_dir: Path,
        declared_outputs: List[Dict[str, Any]],
        max_aggregate_bytes: int = DEFAULT_AGGREGATE_LIMIT
    ) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
        """
        Discovers and validates all outputs declared in the J1 specification.
        Returns (success: bool, manifest_list: list, error_message: str).
        """
        manifest = []
        aggregate_bytes = 0

        for decl in declared_outputs:
            name = decl.get("name") or "output"
            pattern = decl.get("path_pattern") or decl.get("path") or name
            is_required = bool(decl.get("required", True))
            max_size = int(decl.get("max_size_bytes") or MAX_SINGLE_ARTIFACT_LIMIT)

            # Check pattern safety
            try:
                # If pattern contains glob characters (*, ?), resolve relative to workspace
                if any(c in pattern for c in ("*", "?", "[")):
                    matches = list(workspace_dir.glob(pattern))
                else:
                    target_file = cls.validate_safe_relative_path(workspace_dir, pattern)
                    matches = [target_file] if target_file.exists() else []
            except ValueError as ve:
                return False, [], f"Security violation in declared output '{name}': {ve}"

            if not matches:
                if is_required:
                    return False, [], f"Missing required output artifact '{name}' matching pattern '{pattern}'"
                continue

            for match_path in matches:
                if not match_path.is_file():
                    continue

                try:
                    cls.validate_safe_relative_path(workspace_dir, str(match_path.relative_to(workspace_dir)))
                except ValueError as ve:
                    return False, [], f"Security violation in discovered file: {ve}"

                file_size = match_path.stat().st_size
                if file_size > max_size:
                    return False, [], f"Output file '{match_path.name}' ({file_size} bytes) exceeds limit ({max_size} bytes)"

                aggregate_bytes += file_size
                if aggregate_bytes > max_aggregate_bytes:
                    return False, [], f"Aggregate outputs ({aggregate_bytes} bytes) exceed storage limit ({max_aggregate_bytes} bytes)"

                sha256_hex, _ = compute_file_sha256(match_path)

                manifest.append({
                    "name": name,
                    "filename": match_path.name,
                    "relative_path": str(match_path.relative_to(workspace_dir)),
                    "abs_path": str(match_path),
                    "size_bytes": file_size,
                    "sha256": sha256_hex,
                    "required": is_required,
                    "format": decl.get("format", "application/octet-stream")
                })

        return True, manifest, None

    @classmethod
    def promote_artifact_cas(
        cls,
        storage_root: Path,
        source_file: Path,
        expected_sha256: str,
        filename: str,
        job_id: str,
        producing_node: Optional[str] = None,
        workflow_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes recoverable two-phase promotion into permanent CAS artifact storage:
        Phase 1: Verify source checksum independently.
        Phase 2: Atomic CAS promotion into storage_root/<art_id>/<filename>.
        """
        if not source_file.exists() or not source_file.is_file():
            raise FileNotFoundError(f"Source artifact file not found: {source_file}")

        actual_sha256, actual_size = compute_file_sha256(source_file)
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise ValueError(f"Checksum mismatch: expected {expected_sha256}, calculated {actual_sha256}")

        art_id = generate_artifact_id()
        art_dir = (storage_root / art_id).resolve()
        art_dir.mkdir(parents=True, exist_ok=True)
        target_file = validate_artifact_path(art_dir, filename)

        # Staging file inside destination directory
        staging_tmp = art_dir / f"{target_file.name}.{secrets.token_hex(4)}.staging"

        try:
            shutil.copy2(str(source_file), str(staging_tmp))
            # Verify staging copy before atomic rename
            stage_sha, stage_size = compute_file_sha256(staging_tmp)
            if stage_sha.lower() != actual_sha256.lower() or stage_size != actual_size:
                raise IOError("Staging artifact corrupted during copy")

            # Atomic Compare-And-Swap replacement
            staging_tmp.replace(target_file)
        finally:
            if staging_tmp.exists():
                try:
                    staging_tmp.unlink()
                except Exception:
                    pass

        record = {
            "artifact_id": art_id,
            "name": filename,
            "filename": filename,
            "path": str(target_file),
            "size_bytes": actual_size,
            "sha256": actual_sha256,
            "content_type": "application/octet-stream",
            "job_id": job_id,
            "workflow_id": workflow_id,
            "producing_node": producing_node,
            "created_at": get_current_iso_timestamp(),
            "promoted": True
        }

        # Write metadata.json alongside artifact
        meta_file = art_dir / "metadata.json"
        with open(meta_file, "w", encoding="utf-8") as mf:
            json.dump(record, mf, indent=2)

        return record

    @classmethod
    def commit_job_result_and_artifacts(
        cls,
        job_id: str,
        attempt: int,
        fencing_token: int,
        status: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        promoted_artifacts: List[Dict[str, Any]]
    ) -> Tuple[bool, Optional[str]]:
        """
        Atomically records job outcome and links promoted artifacts in jobs_db and artifacts_db.
        Validates fencing generation under DB_LOCK to prevent stale completions.
        """
        with DB_LOCK:
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if not job:
                return False, f"Job '{job_id}' not found"

            curr_gen = int(job.get("lease_generation", 1))
            if fencing_token < curr_gen:
                return False, f"Precondition Failed: Stale fencing token {fencing_token} < current {curr_gen}"

            # Strict integrity check: ensure all promoted artifact files physically exist on disk before DB commit
            for art_rec in promoted_artifacts:
                art_p = Path(art_rec.get("path", ""))
                if not art_p.exists() or not art_p.is_file():
                    return False, f"Artifact persistence integrity error: file '{art_p}' missing from CAS storage"

            artifacts_db = load_artifacts_db()
            arts_map = artifacts_db.setdefault("artifacts", {})

            for art_rec in promoted_artifacts:
                art_id = art_rec.get("artifact_id")
                if art_id:
                    arts_map[art_id] = art_rec

            save_artifacts_db(artifacts_db)

            # Update job state
            job["status"] = status
            job["state"] = status
            job["finished_at"] = get_current_iso_timestamp()
            job["lease_expires_at"] = None
            job["result"] = {
                "status": status,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "artifacts": promoted_artifacts
            }

            job.setdefault("artifacts", []).extend(promoted_artifacts)

            # Release reservation safely
            try:
                from controller.stores.reservations import release_reservation
                release_reservation(jobs_db, job_id, attempt=attempt, fencing_token=fencing_token)
            except Exception:
                pass

            save_jobs_db(jobs_db)
            return True, None

    @classmethod
    def reconcile_promoted_artifacts(cls, storage_root: Path) -> int:
        """
        Scans storage_root for promoted artifacts that exist on disk (containing metadata.json)
        but are missing from artifacts_db (e.g. Due to controller crash during two-phase commit).
        Reconciles records under DB_LOCK to restore database consistency.
        """
        if not storage_root.exists():
            return 0

        reconciled = 0
        with DB_LOCK:
            artifacts_db = load_artifacts_db()
            arts_map = artifacts_db.setdefault("artifacts", {})
            changed = False

            for item in storage_root.iterdir():
                if item.is_dir():
                    meta_path = item / "metadata.json"
                    if meta_path.exists() and meta_path.is_file():
                        try:
                            with open(meta_path, "r", encoding="utf-8") as mf:
                                record = json.load(mf)
                            art_id = record.get("artifact_id")
                            if art_id and art_id not in arts_map:
                                # Verify target file exists
                                target_path = Path(record.get("path", item / record.get("filename", "")))
                                if target_path.exists() and target_path.is_file():
                                    arts_map[art_id] = record
                                    reconciled += 1
                                    changed = True
                        except Exception:
                            pass

            if changed:
                save_artifacts_db(artifacts_db)

        return reconciled

    @classmethod
    def cleanup_orphaned_staging(cls, storage_root: Path, max_age_seconds: float = 3600.0) -> int:
        """
        Cleans up abandoned staging files (.staging, .tmp) older than max_age_seconds.
        Never removes active promoted artifacts.
        """
        cleaned = 0
        now = time.time()
        if not storage_root.exists():
            return 0

        for root, _, files in os.walk(str(storage_root)):
            for f in files:
                if f.endswith((".staging", ".tmp")):
                    f_path = Path(root) / f
                    try:
                        if (now - f_path.stat().st_mtime) > max_age_seconds:
                            f_path.unlink()
                            cleaned += 1
                    except Exception:
                        pass
        return cleaned
