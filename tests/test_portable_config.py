#!/usr/bin/env python3
"""
Tests for Portable Controller Configuration, Path Normalization & Runtime Isolation
===================================================================================
Verifies:
1. Missing and invalid configuration validation.
2. Windows-style and Linux-style path normalization.
3. Default local controller startup settings and directory validation.
4. Environment variable overrides for FHS/custom directories and network settings.
5. Secret and token redaction for logging and diagnostics.
6. Backward compatibility with existing platform configuration and storage.
"""

import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.platform_config import (
    PlatformConfig,
    PlatformConfigError,
    ControllerPaths,
    ControllerRuntimeConfig,
    normalize_path,
    redact_sensitive_dict,
    get_controller_paths,
    get_controller_runtime_config,
    override_controller_paths,
    get_platform_config
)


class TestPortableConfiguration(unittest.TestCase):

    def setUp(self):
        # Save environment variables to restore in tearDown
        self.orig_env = os.environ.copy()

    def tearDown(self):
        # Restore environment
        os.environ.clear()
        os.environ.update(self.orig_env)
        # Reset cached singletons
        get_controller_paths(force_reload=True)
        get_controller_runtime_config(force_reload=True)

    def test_missing_and_invalid_configuration(self):
        """Test validation error cases for missing and malformed configuration."""
        # 1. Missing domain
        with self.assertRaises(PlatformConfigError) as ctx:
            PlatformConfig({"platform": {"domain": ""}})
        self.assertIn("domain", str(ctx.exception).lower())

        # 2. Invalid domain with slashes or spaces
        with self.assertRaises(PlatformConfigError):
            PlatformConfig({"platform": {"domain": "bad domain/com"}})

        # 3. Invalid scheme
        with self.assertRaises(PlatformConfigError) as ctx:
            PlatformConfig({
                "platform": {
                    "domain": "test.local",
                    "application": {"scheme": "ftp"}
                }
            })
        self.assertIn("scheme", str(ctx.exception).lower())

        # 4. Invalid controller port: non-integer
        os.environ["PERSONALSERVER_CONTROLLER_PORT"] = "not-a-port"
        with self.assertRaises(PlatformConfigError) as ctx:
            ControllerRuntimeConfig()
        self.assertIn("personalserver_controller_port", str(ctx.exception).lower())

        # 5. Invalid controller port: out of range
        os.environ["PERSONALSERVER_CONTROLLER_PORT"] = "99999"
        with self.assertRaises(PlatformConfigError) as ctx:
            ControllerRuntimeConfig()
        self.assertIn("between 1 and 65535", str(ctx.exception).lower())

        # 6. Invalid heartbeat timeout: negative/zero
        os.environ["PERSONALSERVER_CONTROLLER_PORT"] = "8000"
        os.environ["PERSONALSERVER_HEARTBEAT_TIMEOUT"] = "0"
        with self.assertRaises(PlatformConfigError) as ctx:
            ControllerRuntimeConfig()
        self.assertIn("positive integer", str(ctx.exception).lower())

        # 7. Invalid lease grace: negative
        os.environ["PERSONALSERVER_HEARTBEAT_TIMEOUT"] = "60"
        os.environ["PERSONALSERVER_LEASE_GRACE_SEC"] = "-5"
        with self.assertRaises(PlatformConfigError) as ctx:
            ControllerRuntimeConfig()
        self.assertIn("non-negative integer", str(ctx.exception).lower())

        # 8. Invalid onboarding TTL: non-positive
        os.environ["PERSONALSERVER_LEASE_GRACE_SEC"] = "20"
        os.environ["PERSONALSERVER_ONBOARDING_TTL_SEC"] = "-1"
        with self.assertRaises(PlatformConfigError) as ctx:
            ControllerRuntimeConfig()
        self.assertIn("positive integer", str(ctx.exception).lower())

    def test_windows_and_linux_style_paths(self):
        """Test cross-platform path normalization for Windows and Linux syntaxes."""
        # Linux absolute paths
        linux_path = "/var/lib/personalserver/data"
        norm_linux = normalize_path(linux_path)
        self.assertIsInstance(norm_linux, Path)
        self.assertTrue(norm_linux.as_posix().endswith("/var/lib/personalserver/data"))

        # Windows paths
        win_path = r"C:\PersonalServer\controller\data"
        norm_win = normalize_path(win_path)
        self.assertIsInstance(norm_win, Path)
        self.assertTrue(str(norm_win).endswith("controller" + os.sep + "data") or str(norm_win).endswith("controller/data"))

        # Relative paths
        rel_path = "controller/data"
        norm_rel = normalize_path(rel_path)
        self.assertIsInstance(norm_rel, Path)
        self.assertTrue(norm_rel.is_absolute())

        # Empty / None
        self.assertIsNone(normalize_path(None))
        self.assertIsNone(normalize_path(""))

    def test_default_local_controller_startup(self):
        """Verify safe defaults for local development controller startup."""
        paths = get_controller_paths(force_reload=True)
        cfg = get_controller_runtime_config(force_reload=True)

        # Network defaults
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8000)
        self.assertEqual(cfg.heartbeat_timeout, 60)
        self.assertEqual(cfg.lease_grace_sec, 20)
        self.assertEqual(cfg.onboarding_ttl_sec, 900)

        # Path defaults relative to BASE_DIR
        self.assertEqual(paths.base_dir, BASE_DIR)
        self.assertEqual(paths.data_dir, (BASE_DIR / "controller" / "data").resolve())
        self.assertEqual(paths.secrets_dir, (BASE_DIR / "config" / "secrets").resolve())
        self.assertEqual(paths.runtime_dir, (BASE_DIR / "runtime").resolve())
        self.assertEqual(paths.logs_dir, (BASE_DIR / "logs").resolve())
        self.assertEqual(paths.storage_dir, (BASE_DIR / "storage").resolve())

        # Directory validation should succeed without error in local workspace
        paths.validate()

    def test_environment_variable_overrides(self):
        """Verify environment variables allow relocating all controller directories and runtime settings."""
        temp_dir = Path(tempfile.mkdtemp(prefix="ps_test_portable_"))
        try:
            custom_data = temp_dir / "custom_data"
            custom_secrets = temp_dir / "custom_secrets"
            custom_runtime = temp_dir / "custom_runtime"
            custom_logs = temp_dir / "custom_logs"
            custom_storage = temp_dir / "custom_storage"

            os.environ["PERSONALSERVER_DATA_DIR"] = str(custom_data)
            os.environ["PERSONALSERVER_SECRETS_DIR"] = str(custom_secrets)
            os.environ["PERSONALSERVER_RUNTIME_DIR"] = str(custom_runtime)
            os.environ["PERSONALSERVER_LOGS_DIR"] = str(custom_logs)
            os.environ["PERSONALSERVER_STORAGE_DIR"] = str(custom_storage)

            os.environ["PERSONALSERVER_CONTROLLER_HOST"] = "127.0.0.1"
            os.environ["PERSONALSERVER_CONTROLLER_PORT"] = "9000"
            os.environ["PERSONALSERVER_HEARTBEAT_TIMEOUT"] = "45"
            os.environ["PERSONALSERVER_LEASE_GRACE_SEC"] = "15"
            os.environ["PERSONALSERVER_ONBOARDING_TTL_SEC"] = "600"

            paths = get_controller_paths(force_reload=True)
            cfg = get_controller_runtime_config(force_reload=True)

            self.assertEqual(paths.data_dir, custom_data.resolve())
            self.assertEqual(paths.secrets_dir, custom_secrets.resolve())
            self.assertEqual(paths.runtime_dir, custom_runtime.resolve())
            self.assertEqual(paths.logs_dir, custom_logs.resolve())
            self.assertEqual(paths.storage_dir, custom_storage.resolve())

            self.assertEqual(cfg.host, "127.0.0.1")
            self.assertEqual(cfg.port, 9000)
            self.assertEqual(cfg.heartbeat_timeout, 45)
            self.assertEqual(cfg.lease_grace_sec, 15)
            self.assertEqual(cfg.onboarding_ttl_sec, 600)

            # Validate should create and verify writable directories
            paths.validate()
            self.assertTrue(custom_data.is_dir())
            self.assertTrue(custom_secrets.is_dir())
            self.assertTrue(custom_runtime.is_dir())
            self.assertTrue(custom_logs.is_dir())
            self.assertTrue(custom_storage.is_dir())

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_override_controller_paths_context_manager(self):
        """Verify override_controller_paths context manager isolates paths during tests."""
        temp_dir = Path(tempfile.mkdtemp(prefix="ps_ctx_test_"))
        try:
            custom_data = temp_dir / "ctx_data"
            custom_data.mkdir(parents=True)

            with override_controller_paths(data_dir=custom_data) as overridden:
                self.assertEqual(overridden.data_dir, custom_data.resolve())
                self.assertEqual(get_controller_paths().data_dir, custom_data.resolve())

            # Automatically restored
            self.assertNotEqual(get_controller_paths().data_dir, custom_data.resolve())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_secret_redaction(self):
        """Verify sensitive credentials, auth tokens, passwords, and cookies are masked recursively."""
        payload = {
            "cluster_name": "PersonalServer",
            "auth_token": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "enrollment_token": "secret1234567890abcdef",
            "password": "SuperSecretPassword!",
            "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC...",
            "cf_access_client_secret": "short",
            "cookie_secret": "my-session-cookie-secret",
            "safe_metadata": {
                "node_id": "server-01",
                "nested_token": "nested_secret_value_12345",
                "system": {
                    "cpu_count": 8,
                    "ram_mb": 16384
                }
            },
            "list_of_nodes": [
                {"node_id": "node-1", "token": "node1_token_value_xyz"},
                {"node_id": "node-2", "name": "worker-2"}
            ]
        }

        redacted = redact_sensitive_dict(payload)

        # Public / non-sensitive keys preserved
        self.assertEqual(redacted["cluster_name"], "PersonalServer")
        self.assertEqual(redacted["safe_metadata"]["node_id"], "server-01")
        self.assertEqual(redacted["safe_metadata"]["system"]["cpu_count"], 8)
        self.assertEqual(redacted["list_of_nodes"][1]["name"], "worker-2")

        # Long secrets masked with prefix/suffix
        self.assertIn("[REDACTED]", redacted["auth_token"])
        self.assertNotIn("c3d4e5f60718293a4b5c6d", redacted["auth_token"])
        self.assertIn("[REDACTED]", redacted["enrollment_token"])

        # Short secrets masked completely
        self.assertEqual(redacted["cf_access_client_secret"], "[REDACTED]")

        # Nested secrets masked
        self.assertIn("[REDACTED]", redacted["safe_metadata"]["nested_token"])
        self.assertIn("[REDACTED]", redacted["list_of_nodes"][0]["token"])

    def test_backward_compatibility_with_existing_platform_config(self):
        """Verify backward compatibility with platform.yaml and existing PlatformConfig attributes."""
        cfg = get_platform_config()

        # Existing fields
        self.assertTrue(hasattr(cfg, "primary_domain"))
        self.assertTrue(hasattr(cfg, "admin_host"))
        self.assertTrue(hasattr(cfg, "admin_url"))
        self.assertTrue(hasattr(cfg, "api_host"))
        self.assertTrue(hasattr(cfg, "api_url"))
        self.assertTrue(hasattr(cfg, "app_base_url"))
        self.assertTrue(hasattr(cfg, "app_scheme"))

        # Newly added fields
        self.assertTrue(hasattr(cfg, "apps_host"))
        self.assertTrue(hasattr(cfg, "apps_url"))

        # Existing to_dict schema compatibility
        data = cfg.to_dict()
        self.assertIn("domain", data)
        self.assertIn("hosts", data)
        self.assertIn("application", data)
        self.assertIn("apps", data["hosts"])


if __name__ == "__main__":
    unittest.main()
