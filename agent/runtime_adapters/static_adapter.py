#!/usr/bin/env python3
"""
PersonalServer Static Web Runtime Adapter
=========================================
Implements the RuntimeAdapter interface for static web applications.
Validates static root configuration, enforces index.html presence,
guards against path traversal and unsafe symlinks, and coordinates
with the Application Router for zero-subprocess static asset serving.
"""

import os
import sys
import json
import time
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.process_manager import ProcessIdentityRecord
from agent.runtime_adapters.base import RuntimeAdapter

CONVENTIONAL_STATIC_ROOTS = ["dist", "build", "public", "."]


class StaticWebAdapter(RuntimeAdapter):
    """
    Adapter for serving static web assets directly via the Application Router
    without spawning dedicated Python or Node.js server processes.
    """

    @property
    def runtime_name(self) -> str:
        return "static"

    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        """
        Validates host storage and filesystem capabilities for static web serving.
        """
        try:
            test_dir = BASE_DIR / "storage"
            if not test_dir.exists():
                test_dir.mkdir(parents=True, exist_ok=True)
            return True, None
        except Exception as e:
            return False, f"Static web environment check failed: {e}"

    def detect_static_root(
        self,
        release_dir: Path,
        configured_root: Optional[str] = None
    ) -> Tuple[Optional[Path], Optional[str]]:
        """
        Resolves the static document root.
        Explicit configuration takes precedence over conventional candidates.
        Enforces index.html presence and path containment.
        Returns (resolved_path, error_reason).
        """
        release_dir = Path(release_dir).resolve()
        if not release_dir.is_dir():
            return None, f"Release directory '{release_dir}' does not exist or is not a directory."

        if configured_root is not None and str(configured_root).strip() != "":
            raw_root = str(configured_root).strip().replace("\\", "/")
            if raw_root.startswith("/"):
                raw_root = raw_root.lstrip("/")

            # Reject path traversal tokens
            for part in raw_root.split("/"):
                if part in ("..",):
                    return None, f"Path traversal attempt in configured static root: '{configured_root}'"

            cand_dir = (release_dir / raw_root).resolve()
            try:
                cand_dir.relative_to(release_dir)
            except ValueError:
                return None, f"Configured static root '{configured_root}' resolves outside release directory."

            if not cand_dir.is_dir():
                return None, f"Configured static root directory '{configured_root}' does not exist."

            index_file = cand_dir / "index.html"
            if not index_file.is_file():
                return None, f"Configured static root '{configured_root}' is missing required 'index.html'."

            return cand_dir, None

        # Search conventional static roots in priority order
        for cand in CONVENTIONAL_STATIC_ROOTS:
            cand_dir = (release_dir / cand).resolve()
            if cand_dir.is_dir() and (cand_dir / "index.html").is_file():
                return cand_dir, None

        return None, (
            "No valid 'index.html' found. Checked configured root and conventional "
            "directories: 'dist/', 'build/', 'public/', and release root."
        )

    def prepare_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        env_vars: Dict[str, str],
        timeout_sec: float = 60.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Prepares the static release directory:
        - Validates against unsafe symlinks escaping release_dir
        - Resolves static root containing index.html
        - Records release metadata (static_root, spa_fallback)
        """
        release_dir = Path(release_dir).resolve()
        if not release_dir.exists():
            return False, f"Release directory '{release_dir}' does not exist."

        # Security check: detect unsafe symlinks pointing outside release directory
        for root, dirs, files in os.walk(release_dir):
            for name in files + dirs:
                p = Path(root) / name
                if p.is_symlink():
                    try:
                        resolved = p.resolve()
                        resolved.relative_to(release_dir)
                    except (ValueError, RuntimeError):
                        return False, f"Security violation: unsafe symlink '{p.name}' points outside release directory."

        # Detect and validate static root
        conf_root = (
            env_vars.get("STATIC_ROOT")
            or env_vars.get("static_root")
            or env_vars.get("ROOT_DIR")
            or env_vars.get("WEB_ROOT")
        )
        static_root_path, err = self.detect_static_root(release_dir, conf_root)
        if not static_root_path:
            return False, err

        # Determine relative static root string
        try:
            rel_static = str(static_root_path.relative_to(release_dir)).replace("\\", "/")
            if rel_static == ".":
                rel_static = ""
        except ValueError:
            rel_static = ""

        spa_fallback = False
        if "SPA_FALLBACK" in env_vars:
            spa_fallback = str(env_vars.get("SPA_FALLBACK", "")).lower() in ("true", "1", "yes")
        elif "spa_fallback" in env_vars:
            val = env_vars.get("spa_fallback")
            spa_fallback = bool(val) if not isinstance(val, str) else val.lower() in ("true", "1", "yes")

        # Persist release metadata for the router
        metadata = {
            "app_id": app_id,
            "deployment_id": deployment_id,
            "runtime": "static",
            "static_root": rel_static,
            "spa_fallback": spa_fallback,
            "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        meta_file = release_dir / "release_metadata.json"
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            return False, f"Failed to write release metadata: {e}"

        return True, None

    def start_application(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        port: int,
        env_vars: Dict[str, str],
        start_cmd: Optional[List[str]] = None,
        log_file: Optional[Path] = None
    ) -> ProcessIdentityRecord:
        """
        Static applications do not spawn dedicated application processes.
        Returns a ProcessIdentityRecord identifying the static release managed
        by the Application Router.
        """
        release_dir = Path(release_dir).resolve()
        meta_file = release_dir / "release_metadata.json"
        static_root = ""
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    static_root = meta.get("static_root", "")
            except Exception:
                pass

        if log_file:
            log_file = Path(log_file)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Static release '{deployment_id}' ready for serving (root='{static_root or '.'}').\n")

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return ProcessIdentityRecord(
            app_id=app_id,
            deployment_id=deployment_id,
            pid=os.getpid(),
            port=port,
            command=["static-web", static_root or "."],
            working_dir=str(release_dir),
            log_file=str(log_file) if log_file else "",
            created_at=now_iso
        )

    def cleanup_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path
    ) -> None:
        """No background processes to terminate for static web workloads."""
        pass
