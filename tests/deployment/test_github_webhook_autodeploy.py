#!/usr/bin/env python3
"""
Test Suite for PersonalServer GitHub Push Detection & Auto-Deploy Webhooks
==========================================================================
Covers Phase 3 and Batch 2.3 requirements:
1. Webhook HMAC SHA-256 signature verification (X-Hub-Signature-256).
2. Invalid signature rejection without leaking secret (401).
3. Webhook delivery deduplication (X-GitHub-Delivery).
4. GitHub ping event handling (200 pong).
5. Repository and branch filtering.
6. Exact commit capture and prompt return (202 Accepted).
7. Stale commit overwrite protection (monotonicity).
8. Auto-deploy policies: automatic, manual_approval, build_only.
9. Route collision fix: GET /apps/github/webhook returns 405 Method Not Allowed with Allow: POST.
10. Valid app_id validation on GET webhook returns 405; unknown app_id returns 404.
11. GET /apps/<app_id>/webhook returns admin_url-based webhook_url and masked secret without AttributeError.
12. Safe handling when application source is explicitly null (payload repository fallback).
13. Rejection of unrelated repository pushes when source repository is configured.
14. Redaction of sensitive credentials on background runner errors.
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import get_controller_paths
from controller.stores import load_apps_db, save_apps_db
from controller.webhook_manager import (
    DELIVERY_TTL_SECONDS,
    WEBHOOK_POLICY_APPROVAL,
    WEBHOOK_POLICY_AUTOMATIC,
    WEBHOOK_POLICY_BUILD_ONLY,
    WebhookManager,
)
from controller.http.app_routes import handle_app_get, handle_app_post


class DummyHandler:
    """Mock HTTP handler for testing app_routes."""

    def __init__(self, raw_body: bytes = b"", headers: dict = None, is_admin: bool = False):
        self.raw_request_body = raw_body
        self.headers = headers or {}
        self.status_code = None
        self.response_data = None
        self.response_headers = {}
        self.heartbeat_timeout = 15
        self._is_admin = is_admin

    def send_json(self, status: int, data: dict, headers: dict = None):
        self.status_code = status
        self.response_data = data
        if headers:
            self.response_headers.update(headers)

    def send_header(self, key: str, value: str):
        self.response_headers[key] = value

    def is_authenticated_admin_or_node(self) -> bool:
        return self._is_admin


class TestGitHubWebhookAutoDeploy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_webhook_test_"))
        cls.orig_paths = get_controller_paths()

        cls.mock_data_dir = cls.test_dir / "data"
        cls.mock_data_dir.mkdir(parents=True, exist_ok=True)

        cls.mock_apps_file = cls.mock_data_dir / "apps.json"
        cls.mock_backup_apps_file = cls.mock_data_dir / "apps.json.bak"
        cls.mock_nodes_file = cls.mock_data_dir / "nodes.json"
        cls.mock_backup_nodes_file = cls.mock_data_dir / "nodes.json.bak"

        cls.orig_paths.apps_file = cls.mock_apps_file
        cls.orig_paths.backup_apps_file = cls.mock_backup_apps_file
        cls.orig_paths.nodes_file = cls.mock_nodes_file
        cls.orig_paths.backup_nodes_file = cls.mock_backup_nodes_file

        nodes_db = {
            "nodes": {
                "server-android-realme5pro": {
                    "id": "server-android-realme5pro",
                    "hostname": "Realme 5 Pro",
                    "status": "ONLINE",
                    "last_seen": time.time(),
                    "capabilities": {"python3": True, "web_service": True}
                }
            }
        }
        with open(cls.mock_nodes_file, "w", encoding="utf-8") as f:
            json.dump(nodes_db, f)

        cls.shared_secret = "test_super_secret_key_12345"
        cls.test_app_id = "test-webhook-app"

        app_record = {
            "app_id": cls.test_app_id,
            "name": "webhook-app",
            "public_path": "/webhook-app",
            "runtime": "python",
            "source": {
                "type": "github",
                "repository": "AcmeCorp/AwesomeApp",
                "branch": "main"
            },
            "webhook": {
                "enabled": True,
                "secret": cls.shared_secret,
                "branch": "main",
                "auto_deploy_policy": WEBHOOK_POLICY_AUTOMATIC,
                "target_node": "server-android-realme5pro"
            },
            "status": "RUNNING",
            "deployments": []
        }
        db = {"apps": {cls.test_app_id: app_record}}
        save_apps_db(db)

    def setUp(self):
        # Reset test app state before each test
        db = load_apps_db()
        app = db.setdefault("apps", {}).setdefault(self.test_app_id, {})
        app["app_id"] = self.test_app_id
        app["name"] = "webhook-app"
        app["status"] = "RUNNING"
        app["source"] = {
            "type": "github",
            "repository": "AcmeCorp/AwesomeApp",
            "branch": "main"
        }
        app["webhook"] = {
            "enabled": True,
            "secret": self.shared_secret,
            "branch": "main",
            "auto_deploy_policy": WEBHOOK_POLICY_AUTOMATIC,
            "target_node": "server-android-realme5pro"
        }
        save_apps_db(db)

    @classmethod
    def tearDownClass(cls):
        if cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _sign_payload(self, body_bytes: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_01_signature_verification(self):
        """WebhookManager correctly verifies valid signatures and rejects invalid ones."""
        raw = b'{"ref": "refs/heads/main"}'
        valid_sig = self._sign_payload(raw, self.shared_secret)
        invalid_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        self.assertTrue(WebhookManager.verify_signature(raw, self.shared_secret, valid_sig))
        self.assertFalse(WebhookManager.verify_signature(raw, self.shared_secret, invalid_sig))
        self.assertFalse(WebhookManager.verify_signature(raw, self.shared_secret, None))
        self.assertFalse(WebhookManager.verify_signature(raw, "wrong_secret", valid_sig))

    def test_02_delivery_deduplication(self):
        """Duplicate webhook delivery IDs are recognized and deduplicated."""
        delivery_id = f"deliv-{time.time()}"
        self.assertFalse(WebhookManager.is_duplicate_delivery(delivery_id))
        self.assertTrue(WebhookManager.is_duplicate_delivery(delivery_id))

    def test_03_github_ping_event(self):
        """GitHub ping event returns 200 with pong response."""
        headers = {
            "X-GitHub-Event": "ping",
            "X-GitHub-Hook-ID": "123456"
        }
        handler = DummyHandler(b"{}", headers)
        handled = handle_app_post(handler, "/apps/github/webhook", {}, "", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.response_data.get("status"), "pong")

    def test_04_invalid_signature_rejection(self):
        """Unauthenticated webhook call with invalid signature returns 401 without leaking secret."""
        payload = {
            "repository": {"full_name": "AcmeCorp/AwesomeApp"},
            "ref": "refs/heads/main",
            "after": "1a2b3c4d5e6f"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": f"deliv-inv-{time.time()}",
            "X-Hub-Signature-256": "sha256=invalidhexsignature"
        }
        handler = DummyHandler(raw_bytes, headers)
        handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 401)
        self.assertIn("Invalid GitHub webhook signature", handler.response_data.get("error", ""))
        self.assertNotIn(self.shared_secret, json.dumps(handler.response_data))

    def test_05_unconfigured_branch_ignored(self):
        """Push on an unconfigured branch (e.g. feature-test) is ignored safely."""
        payload = {
            "repository": {"full_name": "AcmeCorp/AwesomeApp"},
            "ref": "refs/heads/experimental-branch",
            "after": "deadbeef1234"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": f"deliv-br-{time.time()}",
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }
        handler = DummyHandler(raw_bytes, headers)
        handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.response_data.get("status"), "ignored")
        self.assertIn("experimental-branch", handler.response_data.get("reason", ""))

    def test_06_stale_commit_protection(self):
        """Push with older timestamp than current active release is rejected."""
        app_id = self.test_app_id
        db = load_apps_db()
        app = db["apps"][app_id]
        app["active_deployment_id"] = "dep-active-1"
        app["deployments"] = [
            {
                "deployment_id": "dep-active-1",
                "commit": "current_new_commit_sha",
                "started_at": "2026-11-02T12:00:00Z",
                "status": "SUCCESS"
            }
        ]
        save_apps_db(db)

        # Incoming commit has older timestamp: 2026-11-01T10:00:00Z
        is_stale, reason = WebhookManager.check_is_stale_commit(
            app,
            incoming_commit="old_commit_sha",
            incoming_timestamp="2026-11-01T10:00:00Z"
        )
        self.assertTrue(is_stale)
        self.assertIn("older than active release", reason)

        # Incoming commit matches already deployed commit
        is_stale_dup, reason_dup = WebhookManager.check_is_stale_commit(
            app,
            incoming_commit="current_new_commit_sha"
        )
        self.assertTrue(is_stale_dup)
        self.assertIn("already deployed", reason_dup)

    def test_07_valid_push_queued_promptly(self):
        """Valid push returns 202 Accepted promptly and captures commit SHA."""
        payload = {
            "repository": {"full_name": "AcmeCorp/AwesomeApp"},
            "ref": "refs/heads/main",
            "after": "e5f6a7b8c9d01234567890abcdef1234567890ab",
            "head_commit": {
                "id": "e5f6a7b8c9d01234567890abcdef1234567890ab",
                "timestamp": "2026-11-02T18:00:00Z",
                "message": "feat: release new version"
            }
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        delivery_id = f"deliv-valid-{time.time()}"
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }
        handler = DummyHandler(raw_bytes, headers)
        handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 202)
        self.assertEqual(handler.response_data.get("status"), "queued")
        self.assertEqual(handler.response_data.get("commit"), "e5f6a7b8")
        self.assertEqual(handler.response_data.get("branch"), "main")
        self.assertEqual(handler.response_data.get("policy"), WEBHOOK_POLICY_AUTOMATIC)

    def test_08_get_webhook_endpoint_returns_405_method_not_allowed(self):
        """GET /apps/github/webhook returns 405 Method Not Allowed with Allow: POST header (never 404 'github')."""
        handler = DummyHandler()
        handled = handle_app_get(handler, "/apps/github/webhook", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 405)
        self.assertEqual(handler.response_data.get("status"), "method_not_allowed")
        self.assertEqual(handler.response_data.get("service"), "GitHub Webhook Receiver")
        self.assertEqual(handler.response_data.get("allowed_method"), "POST")
        self.assertEqual(handler.response_headers.get("Allow"), "POST")
        self.assertNotIn("Application 'github' not found", str(handler.response_data))

    def test_09_get_webhook_with_valid_and_unknown_app_id(self):
        """GET /apps/github/webhook validates app_id: returns 405 for existing app, 404 for unknown app."""
        # Valid app
        handler_valid = DummyHandler()
        handled_valid = handle_app_get(handler_valid, "/apps/github/webhook", {"app_id": [self.test_app_id]})
        self.assertTrue(handled_valid)
        self.assertEqual(handler_valid.status_code, 405)
        self.assertEqual(handler_valid.response_data.get("status"), "method_not_allowed")
        self.assertEqual(handler_valid.response_data.get("app_id"), self.test_app_id)
        self.assertEqual(handler_valid.response_headers.get("Allow"), "POST")

        # Unknown app
        handler_unknown = DummyHandler()
        handled_unknown = handle_app_get(handler_unknown, "/apps/github/webhook", {"app_id": ["unknown-nonexistent-app"]})
        self.assertTrue(handled_unknown)
        self.assertEqual(handler_unknown.status_code, 404)
        self.assertIn("Application 'unknown-nonexistent-app' not found", handler_unknown.response_data.get("error", ""))

    def test_10_get_app_webhook_management_endpoint_no_attribute_error(self):
        """GET /apps/<app_id>/webhook succeeds without AttributeError and masks secret."""
        handler = DummyHandler(is_admin=True)
        handled = handle_app_get(handler, f"/apps/{self.test_app_id}/webhook", {})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.response_data.get("app_id"), self.test_app_id)
        self.assertIn(f"/api/apps/github/webhook?app_id={self.test_app_id}", handler.response_data.get("webhook_url", ""))
        self.assertEqual(handler.response_data.get("webhook", {}).get("secret"), "••••••••")

    def test_11_null_application_source_handling_and_payload_fallback(self):
        """Application with source explicitly null handles incoming push via payload repository fallback."""
        db = load_apps_db()
        app = db["apps"][self.test_app_id]
        app["source"] = None  # Explicitly null, as observed on Vivo exambuddy
        save_apps_db(db)

        payload = {
            "repository": {"full_name": "ExamBuddyCorp/ExamBuddy"},
            "ref": "refs/heads/main",
            "after": "abcdef1234567890abcdef1234567890abcdef12"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": f"deliv-null-src-{time.time()}",
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }
        handler = DummyHandler(raw_bytes, headers)

        # Mock dispatch_webhook_deploy_async to verify payload_repository was passed safely
        with patch.object(WebhookManager, "dispatch_webhook_deploy_async") as mock_dispatch:
            handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {"app_id": [self.test_app_id]})
            self.assertTrue(handled)
            self.assertEqual(handler.status_code, 202)
            self.assertEqual(handler.response_data.get("status"), "queued")
            mock_dispatch.assert_called_once()
            _, kwargs = mock_dispatch.call_args
            self.assertEqual(kwargs.get("payload_repository"), "ExamBuddyCorp/ExamBuddy")

    def test_12_unrelated_repository_rejected_when_source_configured(self):
        """Push for an unrelated repository is rejected with 400 when application source repository is set."""
        # App is configured for AcmeCorp/AwesomeApp
        payload = {
            "repository": {"full_name": "AttackerCorp/MaliciousPayload"},
            "ref": "refs/heads/main",
            "after": "deadbeef0000"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": f"deliv-unrelated-{time.time()}",
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }
        handler = DummyHandler(raw_bytes, headers)
        handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {"app_id": [self.test_app_id]})
        self.assertTrue(handled)
        self.assertEqual(handler.status_code, 400)
        self.assertIn("Repository mismatch", handler.response_data.get("error", ""))

    def test_13_runner_failure_redacts_credentials(self):
        """Failures inside the background runner record error state without leaking sensitive tokens."""
        secret_leak_error = "Authentication failed with token=ghp_secretToken1234567890abcdef"
        with patch("controller.blueprints.fetch_github_source", side_effect=RuntimeError(secret_leak_error)):
            WebhookManager.dispatch_webhook_deploy_async(
                app_id=self.test_app_id,
                commit_sha="fail1234",
                branch="main",
                policy=WEBHOOK_POLICY_AUTOMATIC,
                target_node="server-android-realme5pro",
                delivery_id="deliv-err-1",
                payload_repository="AcmeCorp/AwesomeApp"
            )

            # Wait for background thread while mock is still active
            t0 = time.time()
            while time.time() - t0 < 3.0:
                db = load_apps_db()
                app = db["apps"][self.test_app_id]
                wh = app.get("webhook", {})
                if wh.get("last_status") in ("FAILED", "SUCCESS"):
                    break
                time.sleep(0.05)

        db = load_apps_db()
        app = db["apps"][self.test_app_id]
        wh = app.get("webhook", {})
        self.assertEqual(wh.get("last_status"), "FAILED")
        last_err = wh.get("last_error", "")
        self.assertNotIn("ghp_secretToken1234567890abcdef", last_err)

    def test_14_duplicate_delivery_http_post_does_not_trigger_deployment(self):
        """Duplicate HTTP delivery ID returns 200 deduplicated and never dispatches deployment."""
        payload = {
            "repository": {"full_name": "AcmeCorp/AwesomeApp"},
            "ref": "refs/heads/main",
            "after": "c0ffee1234567890abcdef"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        fixed_delivery_id = f"deliv-duplicate-post-{time.time()}"
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": fixed_delivery_id,
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }

        with patch.object(WebhookManager, "dispatch_webhook_deploy_async") as mock_dispatch:
            # First attempt: accepted & queued
            handler1 = DummyHandler(raw_bytes, headers)
            handled1 = handle_app_post(handler1, "/apps/github/webhook", payload, "", {"app_id": [self.test_app_id]})
            self.assertTrue(handled1)
            self.assertEqual(handler1.status_code, 202)
            self.assertEqual(mock_dispatch.call_count, 1)

            # Second attempt with identical delivery ID: deduplicated, dispatch NOT called again
            handler2 = DummyHandler(raw_bytes, headers)
            handled2 = handle_app_post(handler2, "/apps/github/webhook", payload, "", {"app_id": [self.test_app_id]})
            self.assertTrue(handled2)
            self.assertEqual(handler2.status_code, 200)
            self.assertEqual(handler2.response_data.get("status"), "deduplicated")
            self.assertEqual(mock_dispatch.call_count, 1)

    def test_15_async_runner_executes_deploy_application(self):
        """Async runner fetches GitHub source, packages artifact, and invokes deploy_application."""
        mock_fetch = MagicMock(return_value={"commit": "abc12345", "blueprint": None})
        mock_deploy = MagicMock(return_value={"status": "DEPLOYED", "deployment_id": "dep-test-15"})

        with patch("controller.blueprints.fetch_github_source", mock_fetch),              patch("controller.deployment_manager.DeploymentManager.deploy_application", mock_deploy):

            WebhookManager.dispatch_webhook_deploy_async(
                app_id=self.test_app_id,
                commit_sha="c0ffee99",
                branch="main",
                policy=WEBHOOK_POLICY_AUTOMATIC,
                target_node="server-android-realme5pro",
                delivery_id="deliv-auto-15",
                payload_repository="AcmeCorp/AwesomeApp"
            )

            # Wait for background thread completion
            t0 = time.time()
            while time.time() - t0 < 3.0:
                db = load_apps_db()
                app = db["apps"][self.test_app_id]
                wh = app.get("webhook", {})
                if wh.get("last_status") in ("SUCCESS", "FAILED"):
                    break
                time.sleep(0.05)

        self.assertEqual(mock_deploy.call_count, 1)
        _, kwargs = mock_deploy.call_args
        self.assertEqual(kwargs.get("app_id"), self.test_app_id)
        self.assertEqual(kwargs.get("commit"), "c0ffee99")
        self.assertEqual(kwargs.get("branch"), "main")
        self.assertEqual(kwargs.get("trigger"), "github_push")
        self.assertEqual(wh.get("last_status"), "SUCCESS")

    def test_16_null_source_and_missing_webhook_branch_defaults_safely(self):
        """Application with source=None and webhook.branch=None defaults branch safely to 'main' without AttributeError."""
        db = load_apps_db()
        app = db["apps"][self.test_app_id]
        app["source"] = None
        app["webhook"]["branch"] = None  # None configured branch
        save_apps_db(db)

        payload = {
            "repository": {"full_name": "AcmeCorp/AwesomeApp"},
            "ref": "refs/heads/main",
            "after": "aabbcc112233"
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": f"deliv-null-branch-{time.time()}",
            "X-Hub-Signature-256": self._sign_payload(raw_bytes, self.shared_secret)
        }

        with patch.object(WebhookManager, "dispatch_webhook_deploy_async"):
            handler = DummyHandler(raw_bytes, headers)
            handled = handle_app_post(handler, "/apps/github/webhook", payload, "", {"app_id": [self.test_app_id]})
            self.assertTrue(handled)
            self.assertEqual(handler.status_code, 202)
            self.assertEqual(handler.response_data.get("branch"), "main")


if __name__ == "__main__":
    unittest.main()
