#!/usr/bin/env python3
"""
PersonalServer Artifact Manager
===============================
Artifact identification, SHA-256 integrity verification, safe atomic promotion,
streaming transfer helpers, and sandbox path traversal protection.
"""

import os
import sys
import json
import hashlib
import shutil
import secrets
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from common.time_utils import get_current_iso_timestamp


def generate_artifact_id() -> str:
    """Generates an immutable unique artifact identifier."""
    return f"art-{secrets.token_hex(6)}"


def compute_stream_sha256(stream, chunk_size: int = 65536) -> Tuple[str, int]:
    """
    Computes the SHA-256 checksum and total byte size of a stream.
    Returns (hex_sha256, total_bytes).
    """
    hasher = hashlib.sha256()
    total_bytes = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        hasher.update(chunk)
        total_bytes += len(chunk)
    return hasher.hexdigest(), total_bytes


def compute_file_sha256(filepath: Path, chunk_size: int = 65536) -> Tuple[str, int]:
    """Computes the SHA-256 checksum and byte size of an existing file."""
    with open(filepath, "rb") as f:
        return compute_stream_sha256(f, chunk_size)


MAX_ARTIFACT_SIZE_BYTES = 64 * 1024 * 1024  # 64 MB maximum artifact size


def validate_artifact_path(base_dir: Path, filename: str) -> Path:
    """
    Guards against directory traversal attacks.
    Ensures filename is a clean single-component name and resolved path is strictly inside base_dir.
    """
    if not filename or not isinstance(filename, str):
        raise ValueError("Invalid or missing artifact filename.")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"Path traversal detected: '{filename}' contains path separators or '..'.")
    sanitized_name = Path(filename).name.strip()
    if not sanitized_name or sanitized_name in (".", ".."):
        raise ValueError("Invalid or unsafe artifact filename.")
    target_path = (base_dir / sanitized_name).resolve()
    try:
        target_path.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(f"Path traversal detected: '{filename}' escapes artifact directory.")
    return target_path


def validate_job_output_path(sandbox_dir: Path, declared_path: str) -> Path:
    """
    Validates a job's declared output path inside its dedicated sandbox.
    Rejects directory traversal, paths escaping the sandbox, symlinks escaping the sandbox,
    and unsupported file types (directories, sockets, pipes).
    """
    if not declared_path or not isinstance(declared_path, str):
        raise ValueError("Invalid or missing declared artifact path.")
    
    cleaned = declared_path.strip()
    if not cleaned or ".." in cleaned.split("/") or ".." in cleaned.split("\\"):
        raise ValueError(f"Path traversal detected: '{declared_path}' contains '..'.")
    
    resolved_base = sandbox_dir.resolve()
    target_path = (sandbox_dir / cleaned).resolve()

    try:
        target_path.relative_to(resolved_base)
    except ValueError:
        raise ValueError(f"Path traversal detected: '{declared_path}' escapes job sandbox.")

    # Symlink escape verification
    raw_path = sandbox_dir / cleaned
    if raw_path.is_symlink() or os.path.islink(str(raw_path)):
        real_target = Path(os.path.realpath(str(raw_path)))
        try:
            real_target.relative_to(resolved_base)
        except ValueError:
            raise ValueError(f"Symlink escape detected: artifact '{declared_path}' links to outside of sandbox.")

    # Unsupported file type verification (if file exists)
    if target_path.exists():
        if target_path.is_dir():
            raise ValueError(f"Invalid artifact type: '{declared_path}' is a directory, not a file.")
        if target_path.is_socket() or target_path.is_fifo() or target_path.is_block_device() or target_path.is_char_device():
            raise ValueError(f"Invalid artifact type: '{declared_path}' is a special device or socket.")

    return target_path


def store_artifact_bytes(
    storage_root: Path,
    artifact_id: str,
    filename: str,
    content_bytes: bytes,
    expected_sha256: Optional[str] = None,
    job_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    content_type: str = "application/octet-stream",
    metadata: Optional[Dict[str, Any]] = None,
    max_bytes: int = MAX_ARTIFACT_SIZE_BYTES
) -> Dict[str, Any]:
    """
    Atomically writes artifact bytes to storage_root/<artifact_id>/<filename>
    with SHA-256 verification, size bounding, and metadata persistence.
    """
    size_bytes = len(content_bytes)
    if size_bytes > max_bytes:
        raise ValueError(f"Artifact size ({size_bytes} bytes) exceeds limit of {max_bytes} bytes.")

    art_dir = (storage_root / artifact_id).resolve()
    art_dir.mkdir(parents=True, exist_ok=True)
    target_file = validate_artifact_path(art_dir, filename)
    # Use unique tmp filename to prevent collisions during concurrent operations
    tmp_file = art_dir / f"{target_file.name}.{secrets.token_hex(4)}.tmp"

    hasher = hashlib.sha256()
    hasher.update(content_bytes)
    actual_sha256 = hasher.hexdigest()

    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(f"Checksum mismatch: expected {expected_sha256}, got {actual_sha256}")

    # Atomic write and promotion
    try:
        with open(tmp_file, "wb") as f:
            f.write(content_bytes)
        shutil.move(str(tmp_file), str(target_file))
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass

    record = {
        "artifact_id": artifact_id,
        "name": target_file.name,
        "filename": target_file.name,
        "path": str(target_file),
        "size_bytes": size_bytes,
        "sha256": actual_sha256,
        "content_type": content_type,
        "job_id": job_id,
        "workflow_id": workflow_id,
        "created_at": get_current_iso_timestamp(),
        "metadata": metadata or {}
    }

    # Persist artifact metadata JSON in the artifact directory
    meta_path = art_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(record, mf, indent=2)

    return record


def store_artifact_stream(
    storage_root: Path,
    artifact_id: str,
    filename: str,
    stream: Any,
    total_bytes_expected: int = 0,
    expected_sha256: Optional[str] = None,
    job_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    content_type: str = "application/octet-stream",
    metadata: Optional[Dict[str, Any]] = None,
    chunk_size: int = 65536,
    max_bytes: int = MAX_ARTIFACT_SIZE_BYTES
) -> Dict[str, Any]:
    """
    Streams artifact bytes from an open file or socket directly into a temporary file
    with streaming SHA-256 computation, verifying checksum and max size before atomic promotion.
    """
    if total_bytes_expected > max_bytes:
        raise ValueError(f"Artifact size ({total_bytes_expected} bytes) exceeds limit of {max_bytes} bytes.")

    art_dir = (storage_root / artifact_id).resolve()
    art_dir.mkdir(parents=True, exist_ok=True)
    target_file = validate_artifact_path(art_dir, filename)
    tmp_file = art_dir / f"{target_file.name}.{secrets.token_hex(4)}.tmp"

    hasher = hashlib.sha256()
    bytes_read = 0

    try:
        with open(tmp_file, "wb") as f:
            while True:
                to_read = chunk_size
                if total_bytes_expected > 0:
                    remaining = total_bytes_expected - bytes_read
                    if remaining <= 0:
                        break
                    to_read = min(chunk_size, remaining)
                chunk = stream.read(to_read)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                bytes_read += len(chunk)
                if bytes_read > max_bytes:
                    raise ValueError(f"Artifact size exceeded maximum allowed limit of {max_bytes} bytes.")
                f.write(chunk)
                hasher.update(chunk)

        actual_sha256 = hasher.hexdigest()
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise ValueError(f"Checksum mismatch: expected {expected_sha256}, got {actual_sha256}")

        shutil.move(str(tmp_file), str(target_file))
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass

    record = {
        "artifact_id": artifact_id,
        "name": target_file.name,
        "filename": target_file.name,
        "path": str(target_file),
        "size_bytes": bytes_read,
        "sha256": actual_sha256,
        "content_type": content_type,
        "job_id": job_id,
        "workflow_id": workflow_id,
        "created_at": get_current_iso_timestamp(),
        "metadata": metadata or {}
    }

    meta_path = art_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(record, mf, indent=2)

    return record


def verify_artifact_integrity(record: Dict[str, Any]) -> bool:
    """Verifies that an artifact on disk matches its recorded SHA-256 and size."""
    file_path = Path(record.get("path", ""))
    if not file_path.exists() or not file_path.is_file():
        return False
    expected_sha256 = record.get("sha256")
    if not expected_sha256:
        return False
    actual_sha256, actual_size = compute_file_sha256(file_path)
    return actual_sha256.lower() == expected_sha256.lower() and actual_size == record.get("size_bytes")


class ArtifactManager:
    """Convenience class interface for artifact management."""
    MAX_SIZE = MAX_ARTIFACT_SIZE_BYTES
    generate_id = staticmethod(generate_artifact_id)
    compute_stream_sha256 = staticmethod(compute_stream_sha256)
    compute_file_sha256 = staticmethod(compute_file_sha256)
    validate_path = staticmethod(validate_artifact_path)
    validate_job_output_path = staticmethod(validate_job_output_path)
    store_bytes = staticmethod(store_artifact_bytes)
    store_stream = staticmethod(store_artifact_stream)
    verify_integrity = staticmethod(verify_artifact_integrity)

