#!/usr/bin/env python3
"""
Tests for PersonalServer Centralized Platform Domain & URL Configuration
========================================================================
Validates that public URLs, admin hosts, and API endpoints are derived
strictly from configuration (config/platform.yaml) and change dynamically
when configuration is modified, without requiring source code changes.
"""

import unittest
import json
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.platform_config import (
    get_platform_config,
    override_platform_config,
    PlatformConfig,
    PlatformConfigError
)
from controller.controller import mask_app_record, validate_app_route


class TestPlatformConfiguration(unittest.TestCase):

    def test_current_production_configuration(self):
        """Verify that production config produces the current production URLs."""
        cfg = get_platform_config()

        self.assertEqual(cfg.primary_domain, "akshatsahay.space")
        self.assertEqual(cfg.admin_host, "server.akshatsahay.space")
        self.assertEqual(cfg.admin_url, "https://server.akshatsahay.space")
        self.assertEqual(cfg.api_host, "api.akshatsahay.space")
        self.assertEqual(cfg.api_url, "https://api.akshatsahay.space")
        self.assertEqual(cfg.app_scheme, "https")
        self.assertEqual(cfg.app_base_url, "https://akshatsahay.space")

        # Application URL generation
        self.assertEqual(cfg.get_app_public_url("/recallflow"), "https://akshatsahay.space/recallflow/")
        self.assertEqual(cfg.get_app_public_url("thoughtrag"), "https://akshatsahay.space/thoughtrag/")
        self.assertEqual(cfg.get_app_public_url(""), "-")
        self.assertEqual(cfg.get_app_public_url(None), "-")

    def test_temporary_configuration_substitution(self):
        """
        Verify that in-memory configuration replacement (e.g. example.test)
        dynamically re-derives all URLs without touching source code.
        """
        test_domain = "example.test"

        with override_platform_config(test_domain) as test_cfg:
            self.assertEqual(test_cfg.primary_domain, "example.test")
            self.assertEqual(test_cfg.admin_host, "server.example.test")
            self.assertEqual(test_cfg.admin_url, "https://server.example.test")
            self.assertEqual(test_cfg.api_host, "api.example.test")
            self.assertEqual(test_cfg.api_url, "https://api.example.test")
            self.assertEqual(test_cfg.app_base_url, "https://example.test")

            # Generated application URLs must use the new domain
            self.assertEqual(test_cfg.get_app_public_url("/recallflow"), "https://example.test/recallflow/")
            self.assertEqual(test_cfg.get_app_public_url("minivault"), "https://example.test/minivault/")

        # Verify restoration of original production config
        restored = get_platform_config()
        self.assertEqual(restored.primary_domain, "akshatsahay.space")
        self.assertEqual(restored.get_app_public_url("/recallflow"), "https://akshatsahay.space/recallflow/")

    def test_non_secret_safety_of_serialization(self):
        """Verify to_dict() outputs clean public configuration containing no secrets."""
        cfg = get_platform_config()
        data = cfg.to_dict()

        serialized = json.dumps(data).lower()
        forbidden_substrings = ["password", "token", "secret", "private_key", "credential", "cloudflared"]
        for forbidden in forbidden_substrings:
            self.assertNotIn(forbidden, serialized, f"Forbidden term '{forbidden}' detected in platform config export!")

        self.assertIn("domain", data)
        self.assertIn("hosts", data)
        self.assertIn("application", data)
        self.assertEqual(data["domain"], "akshatsahay.space")

    def test_app_record_masking_attaches_dynamic_public_url(self):
        """Verify that application records dynamically carry public_url derived from config."""
        sample_app = {
            "app_id": "app-test-01",
            "name": "recallflow",
            "route": {
                "enabled": True,
                "type": "path",
                "path": "/recallflow",
                "strip_prefix": True,
                "public_access": True
            }
        }

        # Under current production domain
        masked = mask_app_record(sample_app)
        self.assertEqual(masked["public_url"], "https://akshatsahay.space/recallflow/")

        # Under substituted domain
        with override_platform_config("custom-corp.org"):
            masked_custom = mask_app_record(sample_app)
            self.assertEqual(masked_custom["public_url"], "https://custom-corp.org/recallflow/")

        # Restored
        masked_after = mask_app_record(sample_app)
        self.assertEqual(masked_after["public_url"], "https://akshatsahay.space/recallflow/")

    def test_disabled_route_public_url(self):
        """Verify disabled routes return '-' for public_url."""
        disabled_app = {
            "app_id": "app-test-02",
            "name": "internal-worker",
            "route": {
                "enabled": False,
                "path": "/internal"
            }
        }
        masked = mask_app_record(disabled_app)
        self.assertEqual(masked["public_url"], "-")

    def test_malformed_config_validation(self):
        """Verify that malformed or missing config fields raise descriptive errors."""
        with self.assertRaises(PlatformConfigError):
            PlatformConfig({})

        with self.assertRaises(PlatformConfigError):
            PlatformConfig({"platform": {}})

        with self.assertRaises(PlatformConfigError):
            PlatformConfig({"platform": {"domain": "invalid domain with spaces"}})

        with self.assertRaises(PlatformConfigError):
            PlatformConfig({"platform": {"domain": "akshatsahay.space", "application": {"scheme": "ftp"}}})


if __name__ == "__main__":
    unittest.main()
