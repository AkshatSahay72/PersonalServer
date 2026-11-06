#!/usr/bin/env python3
"""
PersonalServer GitHub Webhook Manager
====================================
Authentication, delivery deduplication, branch/commit validation,
monotonicity/stale-push protection, policy dispatching, and zero-secret exposure.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from common.time_utils import get_current_iso_timestamp
from config.platform_config import get_controller_paths

logger = logging.getLogger("personalserver.webhooks")

WEBHOOK_POLICY_AUTOMATIC = "automatic"
WEBHOOK_POLICY_APPROVAL = "manual_approval"
WEBHOOK_POLICY_BUILD_ONLY = "build_only"
VALID_POLICIES = {WEBHOOK_POLICY_AUTOMATIC, WEBHOOK_POLICY_APPROVAL, WEBHOOK_POLICY_BUILD_ONLY}

DELIVERY_TTL_SECONDS = 86400  # 24 hours deduplication window


class WebhookManager:
    """Manages GitHub webhook delivery verification, deduplication, and asynchronous execution."""

    _lock = threading.Lock()
    _deliveries_cache: Dict[str, float] = {}

    @classmethod
    def _get_deliveries_file(cls) -> Path:
        paths = get_controller_paths()
        deliveries_dir = paths.data_dir / "webhooks"
        deliveries_dir.mkdir(parents=True, exist_ok=True)
        return deliveries_dir / "deliveries.json"

    @classmethod
    def _load_deliveries(cls) -> None:
        deliv_file = cls._get_deliveries_file()
        if deliv_file.exists():
            try:
                data = json.loads(deliv_file.read_text(encoding="utf-8"))
                now = time.time()
                cls._deliveries_cache = {k: float(v) for k, v in data.items() if now - float(v) < DELIVERY_TTL_SECONDS}
            except Exception:
                cls._deliveries_cache = {}

    @classmethod
    def _save_deliveries(cls) -> None:
        deliv_file = cls._get_deliveries_file()
        try:
            deliv_file.write_text(json.dumps(cls._deliveries_cache, indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def is_duplicate_delivery(cls, delivery_id: str) -> bool:
        """Checks if delivery ID was already processed within the deduplication window."""
        if not delivery_id:
            return False
        with cls._lock:
            if not cls._deliveries_cache:
                cls._load_deliveries()
            now = time.time()
            if delivery_id in cls._deliveries_cache:
                if now - cls._deliveries_cache[delivery_id] < DELIVERY_TTL_SECONDS:
                    return True
            cls._deliveries_cache[delivery_id] = now
            # Prune old entries
            cls._deliveries_cache = {k: v for k, v in cls._deliveries_cache.items() if now - v < DELIVERY_TTL_SECONDS}
            cls._save_deliveries()
            return False

    @staticmethod
    def verify_signature(raw_body: bytes, secret: str, signature_header: Optional[str]) -> bool:
        """
        Validates HMAC-SHA256 signature against the shared secret in constant time.
        Strict zero secret leakage on failure.
        """
        if not secret or not signature_header or not raw_body:
            return False
        sig_header = signature_header.strip()
        if not sig_header.startswith("sha256="):
            return False

        expected_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected_sig, sig_header)

    @staticmethod
    def generate_webhook_secret() -> str:
        """Generates a cryptographically strong 40-character hex webhook secret."""
        return secrets.token_hex(20)

    @staticmethod
    def check_is_stale_commit(app: Dict[str, Any], incoming_commit: str, incoming_timestamp: Optional[str] = None) -> Tuple[bool, str]:
        """
        Prevents stale builds from overwriting newer releases.
        Returns (is_stale, reason).
        """
        active_dep_id = app.get("active_deployment_id")
        deployments = app.get("deployments", [])

        active_dep = None
        for d in deployments:
            if d.get("deployment_id") == active_dep_id:
                active_dep = d
                break

        if not active_dep:
            return False, ""

        active_commit = active_dep.get("commit")
        if active_commit and incoming_commit and active_commit.lower() == incoming_commit.lower():
            return True, f"Commit {incoming_commit[:8]} is already deployed in active release {active_dep_id}."

        active_started = active_dep.get("started_at")
        if incoming_timestamp and active_started and incoming_timestamp < active_started:
            return True, f"Push commit timestamp ({incoming_timestamp}) is older than active release ({active_started})."

        return False, ""

    @staticmethod
    def dispatch_webhook_deploy_async(
        app_id: str,
        commit_sha: str,
        branch: str,
        policy: str,
        target_node: Optional[str] = None,
        delivery_id: Optional[str] = None,
        payload_repository: Optional[str] = None
    ) -> None:
        """
        Asynchronously executes the configured auto-deploy policy in a background worker thread.
        Never blocks the webhook HTTP response.
        """
        def _runner():
            from controller.stores import APPS_LOCK, load_apps_db, save_apps_db
            with APPS_LOCK:
                db = load_apps_db()
                app = db.get("apps", {}).get(app_id)
                if not app:
                    return

                wh_cfg = app.get("webhook", {})
                wh_cfg["last_delivery_id"] = delivery_id
                wh_cfg["last_delivery_at"] = get_current_iso_timestamp()
                wh_cfg["last_commit"] = commit_sha
                wh_cfg["last_branch"] = branch
                wh_cfg["last_status"] = "PROCESSING"
                wh_cfg["last_error"] = None
                app["webhook"] = wh_cfg
                save_apps_db(db)

            try:
                try:
                    from common.dns_utils import enable_dns_fallback
                    enable_dns_fallback()
                except Exception:
                    pass

                source = app.get("source") or {}
                repo = source.get("repository") or payload_repository
                root_dir = source.get("root_directory", ".")

                if not repo:
                    raise ValueError(f"No repository configured for application '{app_id}' and none provided in webhook payload.")

                if policy == WEBHOOK_POLICY_BUILD_ONLY:
                    # Fetch and package only
                    from controller.blueprints import fetch_github_source
                    fetch_res = fetch_github_source(
                        app_id=app_id,
                        repository=repo,
                        branch=branch,
                        root_directory=root_dir,
                        commit_sha=commit_sha
                    )
                    with APPS_LOCK:
                        db = load_apps_db()
                        app = db.get("apps", {}).get(app_id)
                        if app:
                            wh_cfg = app.setdefault("webhook", {})
                            wh_cfg["last_status"] = "SUCCESS"
                            wh_cfg["last_message"] = f"Artifact built successfully for commit {commit_sha[:8]} (build_only policy)."
                            save_apps_db(db)

                elif policy == WEBHOOK_POLICY_APPROVAL:
                    # Fetch, package, and mark pending approval
                    from controller.blueprints import fetch_github_source
                    fetch_res = fetch_github_source(
                        app_id=app_id,
                        repository=repo,
                        branch=branch,
                        root_directory=root_dir,
                        commit_sha=commit_sha
                    )
                    with APPS_LOCK:
                        db = load_apps_db()
                        app = db.get("apps", {}).get(app_id)
                        if app:
                            wh_cfg = app.setdefault("webhook", {})
                            wh_cfg["last_status"] = "PENDING_APPROVAL"
                            wh_cfg["pending_commit"] = commit_sha
                            wh_cfg["last_message"] = f"Commit {commit_sha[:8]} packaged and waiting for operator approval."
                            save_apps_db(db)

                else:
                    # Automatic Deployment: fetch source, package artifact, verify checksum, and deploy release
                    from controller.blueprints import fetch_github_source, resolve_safe_app_storage_path, APP_STORAGE_ROOT
                    from common.artifact_manager import compute_file_sha256
                    from controller.deployment_manager import DeploymentManager
                    import zipfile

                    fetch_res = fetch_github_source(
                        app_id=app_id,
                        repository=repo,
                        branch=branch,
                        root_directory=root_dir,
                        commit_sha=commit_sha
                    )

                    src_context, _ = resolve_safe_app_storage_path(app_id, f"source/{root_dir}".rstrip("/."))
                    art_file = None
                    art_sha = None

                    if src_context.exists():
                        art_dir = APP_STORAGE_ROOT / app_id / "artifacts"
                        art_dir.mkdir(parents=True, exist_ok=True)
                        art_file = art_dir / f"{app_id}-{commit_sha[:8]}.zip"
                        with zipfile.ZipFile(art_file, "w", zipfile.ZIP_DEFLATED) as zf:
                            for f_p in src_context.rglob("*"):
                                if f_p.is_file():
                                    zf.write(f_p, arcname=str(f_p.relative_to(src_context)))
                        art_sha, _ = compute_file_sha256(art_file)

                    res = DeploymentManager.deploy_application(
                        app_id=app_id,
                        artifact_path=art_file,
                        artifact_sha256=art_sha,
                        node_override=target_node,
                        commit=commit_sha,
                        branch=branch,
                        trigger="github_push"
                    )

                    with APPS_LOCK:
                        db = load_apps_db()
                        app = db.get("apps", {}).get(app_id)
                        if app:
                            wh_cfg = app.setdefault("webhook", {})
                            wh_cfg["last_status"] = "SUCCESS"
                            wh_cfg["last_message"] = f"Deployed commit {commit_sha[:8]} ({res.get('status')})."
                            save_apps_db(db)

            except Exception as e:
                from config.structured_logger import redact_sensitive_text
                with APPS_LOCK:
                    db = load_apps_db()
                    app = db.get("apps", {}).get(app_id)
                    if app:
                        wh_cfg = app.setdefault("webhook", {})
                        wh_cfg["last_status"] = "FAILED"
                        clean_err = redact_sensitive_text(str(e))
                        wh_cfg["last_error"] = clean_err
                        wh_cfg["last_message"] = f"Auto-deployment failed: {clean_err}"
                        save_apps_db(db)

        t = threading.Thread(target=_runner, daemon=True, name=f"wh-deploy-{app_id}")
        t.start()
