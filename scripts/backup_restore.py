#!/usr/bin/env python3
"""
PersonalServer State Backup and Restoration Tool
=================================================
Safely archives and restores PersonalServer persistent state:
- Cluster inventory (nodes.json)
- Job lifecycle & history (jobs.json)
- Applications & routing registry (apps.json)
- Onboarding pairing tokens (onboarding_codes.json)
- Secrets (enrollment.token, registry credentials)
- Application storage & uploaded files (storage/)

Strictly EXCLUDES ephemeral runtime files (*.pid, *.sock, runtime/, logs/, caches).
Provides SHA256 checksums, manifest generation, and path traversal defense on restore.

Usage:
  python scripts/backup_restore.py backup [--output backups/backup-TIMESTAMP.tar.gz] [--no-storage]
  python scripts/backup_restore.py verify <ARCHIVE>
  python scripts/backup_restore.py restore <ARCHIVE> [--data-dir <DIR>] [--secrets-dir <DIR>]
"""

import os
import sys
import json
import time
import shutil
import tarfile
import hashlib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.platform_config import get_controller_paths, ControllerPaths

# Default exclusion patterns
EXCLUDED_EXTENSIONS = {".pid", ".sock", ".log", ".bak", ".pyc", ".pyo", ".swp"}
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".DS_Store", "Thumbs.db"}


def _sha256_file(path: Path) -> str:
    """Compute hex SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _is_safe_tar_member(member: tarfile.TarInfo, target_base: Path) -> bool:
    """Verify that a tar member does not escape the destination directory."""
    # Check for absolute paths or path traversal components
    p = Path(member.name)
    if p.is_absolute() or ".." in p.parts:
        return False
    resolved = (target_base / p).resolve()
    return target_base.resolve() in resolved.parents or resolved == target_base.resolve()


def create_backup(
    output_file: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    secrets_dir: Optional[Path] = None,
    storage_dir: Optional[Path] = None,
    include_storage: bool = True
) -> Dict[str, Any]:
    """
    Creates a compressed tar.gz backup archive of persistent state.
    Returns metadata dictionary including SHA256, timestamp, and archived files.
    """
    paths = get_controller_paths()
    src_data = Path(data_dir or paths.data_dir).resolve()
    src_secrets = Path(secrets_dir or paths.secrets_dir).resolve()
    src_storage = Path(storage_dir or paths.storage_dir).resolve()

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    if output_file is None:
        backup_dir = paths.base_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        out_path = backup_dir / f"personalserver_backup_{timestamp_str}.tar.gz"
    else:
        out_path = Path(output_file).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

    archived_files: List[Dict[str, Any]] = []

    def _collect_files(source_root: Path, archive_prefix: str) -> List[Tuple[Path, str]]:
        collected = []
        if not source_root.exists():
            return collected
        for root, dirs, files in os.walk(source_root):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in EXCLUDED_NAMES and not d.startswith(".")]
            for f in files:
                f_path = Path(root) / f
                if f_path.suffix.lower() in EXCLUDED_EXTENSIONS or f in EXCLUDED_NAMES:
                    continue
                rel_path = f_path.relative_to(source_root)
                arc_name = f"{archive_prefix}/{rel_path.as_posix()}"
                collected.append((f_path, arc_name))
        return collected

    to_archive: List[Tuple[Path, str]] = []
    to_archive.extend(_collect_files(src_data, "data"))
    to_archive.extend(_collect_files(src_secrets, "secrets"))
    if include_storage:
        to_archive.extend(_collect_files(src_storage, "storage"))

    # Create manifest
    manifest: Dict[str, Any] = {
        "format_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "include_storage": include_storage,
        "files": {}
    }

    for f_path, arc_name in to_archive:
        f_hash = _sha256_file(f_path)
        manifest["files"][arc_name] = {
            "size_bytes": f_path.stat().st_size,
            "sha256": f_hash
        }

    # Write tar.gz archive
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    with tarfile.open(out_path, "w:gz") as tar:
        # Add manifest
        manifest_info = tarfile.TarInfo(name="manifest.json")
        manifest_info.size = len(manifest_bytes)
        manifest_info.mtime = int(time.time())
        import io
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

        # Add collected files
        for f_path, arc_name in to_archive:
            tar.add(str(f_path), arcname=arc_name, recursive=False)

    archive_sha256 = _sha256_file(out_path)
    sha_file = out_path.with_name(f"{out_path.name}.sha256")
    sha_file.write_text(f"{archive_sha256}  {out_path.name}\n", encoding="utf-8")

    result = {
        "status": "CREATED",
        "archive_path": str(out_path),
        "checksum_file": str(sha_file),
        "sha256": archive_sha256,
        "archive_size_bytes": out_path.stat().st_size,
        "files_count": len(to_archive),
        "created_at": manifest["created_at"]
    }
    return result


def verify_backup_archive(archive_path: Path) -> Tuple[bool, Dict[str, Any]]:
    """
    Validates backup archive integrity, manifest completeness, and checksums.
    Returns (True, report) if valid, or (False, report) if damaged or tampered.
    """
    p = Path(archive_path).resolve()
    if not p.exists() or not p.is_file():
        return False, {"status": "INVALID", "error": f"Archive file not found: {p}"}

    try:
        with tarfile.open(p, "r:gz") as tar:
            names = tar.getnames()
            if "manifest.json" not in names:
                return False, {"status": "INVALID", "error": "Archive is missing manifest.json"}

            manifest_file = tar.extractfile("manifest.json")
            if not manifest_file:
                return False, {"status": "INVALID", "error": "Unable to read manifest.json"}
            manifest = json.loads(manifest_file.read().decode("utf-8"))

            checked_files = []
            for arc_name, meta in manifest.get("files", {}).items():
                if arc_name not in names:
                    return False, {"status": "INVALID", "error": f"File '{arc_name}' listed in manifest but missing in archive"}
                extracted = tar.extractfile(arc_name)
                if extracted is None:
                    continue
                content = extracted.read()
                actual_hash = hashlib.sha256(content).hexdigest()
                expected_hash = meta.get("sha256")
                if actual_hash != expected_hash:
                    return False, {"status": "INVALID", "error": f"Checksum mismatch for '{arc_name}': expected {expected_hash}, got {actual_hash}"}
                checked_files.append(Path(arc_name).name)

            return True, {
                "status": "VALID",
                "archive_path": str(p),
                "created_at": manifest.get("created_at"),
                "files_count": len(checked_files),
                "files": checked_files
            }
    except Exception as e:
        return False, {"status": "INVALID", "error": f"Failed to parse tar archive: {e}"}


def restore_backup(
    archive_path: Path,
    target_data_dir: Optional[Path] = None,
    target_secrets_dir: Optional[Path] = None,
    target_storage_dir: Optional[Path] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Restores persistent databases, secrets, and storage from a verified backup archive.
    Strictly prevents directory traversal payloads.
    """
    p = Path(archive_path).resolve()
    is_valid, report = verify_backup_archive(p)
    if not is_valid:
        raise ValueError(f"Backup verification failed: {report.get('error')}")

    paths = get_controller_paths()
    dst_data = Path(target_data_dir or paths.data_dir).resolve()
    dst_secrets = Path(target_secrets_dir or paths.secrets_dir).resolve()
    dst_storage = Path(target_storage_dir or paths.storage_dir).resolve()

    for d in (dst_data, dst_secrets, dst_storage):
        d.mkdir(parents=True, exist_ok=True)

    extracted_items = []
    with tarfile.open(p, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "manifest.json":
                continue

            # Traversal security check
            parts = Path(member.name).parts
            if not parts:
                continue

            section = parts[0]
            if section == "data":
                target_base = dst_data
            elif section == "secrets":
                target_base = dst_secrets
            elif section == "storage":
                target_base = dst_storage
            else:
                # Disallow unexpected root sections
                raise ValueError(f"Security error: Invalid root prefix in archive member: '{member.name}'")

            subpath = Path(*parts[1:])
            target_file = (target_base / subpath).resolve()

            # Ensure destination is strictly inside target_base
            if target_base not in target_file.parents and target_file != target_base:
                raise ValueError(f"Security error: Path traversal detected in archive member '{member.name}'")

            if dry_run:
                extracted_items.append(str(target_file))
                continue

            if member.isdir():
                target_file.mkdir(parents=True, exist_ok=True)
            else:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                source_f = tar.extractfile(member)
                if source_f:
                    target_file.write_bytes(source_f.read())
                extracted_items.append(str(target_file))

    return {
        "status": "RESTORED",
        "archive": str(p),
        "target_data_dir": str(dst_data),
        "target_secrets_dir": str(dst_secrets),
        "target_storage_dir": str(dst_storage),
        "files_restored": len(extracted_items),
        "dry_run": dry_run
    }


def main():
    parser = argparse.ArgumentParser(description="PersonalServer Backup and Restore Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Backup subcommand
    backup_parser = subparsers.add_parser("backup", help="Create a state backup archive")
    backup_parser.add_argument("--output", "-o", type=str, help="Destination .tar.gz file path")
    backup_parser.add_argument("--no-storage", action="store_true", help="Exclude storage directory")

    # Verify subcommand
    verify_parser = subparsers.add_parser("verify", help="Verify integrity of an archive")
    verify_parser.add_argument("archive", type=str, help="Path to .tar.gz archive")

    # Restore subcommand
    restore_parser = subparsers.add_parser("restore", help="Restore state from an archive")
    restore_parser.add_argument("archive", type=str, help="Path to .tar.gz archive")
    restore_parser.add_argument("--data-dir", type=str, help="Override target data directory")
    restore_parser.add_argument("--secrets-dir", type=str, help="Override target secrets directory")
    restore_parser.add_argument("--storage-dir", type=str, help="Override target storage directory")
    restore_parser.add_argument("--dry-run", action="store_true", help="Test restore without writing files")

    args = parser.parse_args()

    if args.command == "backup":
        print("[+] Creating PersonalServer state backup...")
        res = create_backup(
            output_file=Path(args.output) if args.output else None,
            include_storage=not args.no_storage
        )
        print(f"[OK] Backup created successfully: {res['archive_path']}")
        print(f"    SHA256: {res['sha256']}")
        print(f"    Size:   {res['archive_size_bytes']} bytes ({res['files_count']} files)")

    elif args.command == "verify":
        print(f"[+] Verifying archive: {args.archive}")
        is_valid, report = verify_backup_archive(Path(args.archive))
        if is_valid:
            print(f"[OK] Archive is VALID. {report['files_count']} files verified with SHA256 integrity.")
        else:
            print(f"[!] Archive INVALID: {report.get('error')}")
            sys.exit(1)

    elif args.command == "restore":
        print(f"[+] Restoring from archive: {args.archive}")
        res = restore_backup(
            archive_path=Path(args.archive),
            target_data_dir=Path(args.data_dir) if args.data_dir else None,
            target_secrets_dir=Path(args.secrets_dir) if args.secrets_dir else None,
            target_storage_dir=Path(args.storage_dir) if args.storage_dir else None,
            dry_run=args.dry_run
        )
        mode = "Dry run completed." if args.dry_run else "Restored successfully."
        print(f"[OK] {mode} {res['files_restored']} files extracted into state directories.")


if __name__ == "__main__":
    main()
