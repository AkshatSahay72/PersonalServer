#!/usr/bin/env python3
"""
Unit Tests for PersonalServer Multi-Application Manifest Foundation (V2.4)
==========================================================================
Validates:
1. Application manifest schema validation and serialization
2. Path normalization and sanitization
3. Rejection of credentials, tokens, and secrets
4. Rejection of directory traversal and malformed paths
5. Rejection of reserved system routes
6. Route conflict detection (duplicate names, duplicate paths)
7. Multi-app registration isolation (updates/deletions don't affect other apps)
8. API route precedence over frontend fallback
9. Prefix stripping modes (strip_app, strip_all, preserve)
10. Deployment adapter validation and configuration boundaries
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from common.app_manifest import (
    ApplicationManifest,
    ManifestRegistry,
    AppRouteResolver,
    ManifestValidationError,
    RouteConflictError,
    VercelDeploymentAdapter,
    RenderDeploymentAdapter,
    PersonalServerSelfHostedAdapter
)


class TestAppManifestValidation(unittest.TestCase):

    def setUp(self):
        self.valid_recallflow = {
            "schema_version": "2.4",
            "name": "RecallFlow",
            "public_path": "/RecallFlow",
            "display_name": "RecallFlow Knowledge Agent",
            "status": "active",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "https://recallflow-client.vercel.app",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "spa_fallback": True,
                        "assets_prefix": "/assets"
                    },
                    "build": {
                        "builder": "windows-on-demand",
                        "command": "npm run build",
                        "base_path": "/RecallFlow",
                        "output_dir": "dist",
                        "artifact_id": "art-0fa243a82034",
                        "artifact_sha256": "7d4cc60804efe0601f59697152cbc6c8b6484c64247568e90e382a288607e7ef"
                    }
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": "https://recallflow-api.onrender.com",
                    "runtime": "python",
                    "health_endpoint": "/api/health",
                    "routing": {
                        "path_prefix": "/api",
                        "strip_mode": "strip_app",
                        "api_priority": True
                    }
                }
            },
            "health_check": {
                "path": "/api/health",
                "interval_seconds": 30,
                "timeout_seconds": 5
            },
            "release": {
                "version": "1.0.0",
                "release_id": "rel-20261026-001",
                "last_result": "SUCCESS"
            }
        }

    def test_01_valid_manifest_parse_and_serialize(self):
        manifest = ApplicationManifest.from_dict(self.valid_recallflow)
        self.assertEqual(manifest.name, "RecallFlow")
        self.assertEqual(manifest.public_path, "/RecallFlow")
        self.assertIn("frontend", manifest.components)
        self.assertIn("backend", manifest.components)
        self.assertEqual(manifest.components["frontend"].provider, "vercel")
        self.assertEqual(manifest.components["backend"].provider, "render")

        data = manifest.to_dict()
        self.assertEqual(data["name"], "RecallFlow")
        self.assertEqual(data["components"]["backend"]["routing"]["strip_mode"], "strip_app")
        self.assertTrue(data["components"]["backend"]["routing"]["api_priority"])

    def test_02_reject_secrets_in_manifest(self):
        """Manifests must strictly reject secrets, tokens, and passwords."""
        bad_manifest = dict(self.valid_recallflow)
        bad_manifest["components"]["backend"]["api_key"] = "sk-live-12345"
        with self.assertRaises(ManifestValidationError) as ctx:
            ApplicationManifest.from_dict(bad_manifest)
        self.assertIn("forbidden", str(ctx.exception))

        bad_manifest2 = dict(self.valid_recallflow)
        bad_manifest2["auth_token"] = "secret-token"
        with self.assertRaises(ManifestValidationError):
            ApplicationManifest.from_dict(bad_manifest2)

    def test_03_reject_reserved_system_paths(self):
        """Application paths cannot shadow controller or system routes."""
        for reserved in ["/api", "/storage", "/health", "/admin", "/cluster"]:
            bad = dict(self.valid_recallflow)
            bad["public_path"] = reserved
            with self.assertRaises(ManifestValidationError):
                ApplicationManifest.from_dict(bad)

    def test_04_reject_directory_traversal_and_malformed_paths(self):
        """Rejects path traversal tokens and malformed characters."""
        for dangerous in ["/app/../admin", "/test/../../etc/passwd", "/app\\test", "/app\x00secret"]:
            bad = dict(self.valid_recallflow)
            bad["public_path"] = dangerous
            with self.assertRaises(ManifestValidationError):
                ApplicationManifest.from_dict(bad)

    def test_05_reject_invalid_destinations(self):
        """Component destinations must be valid HTTP/HTTPS URLs."""
        bad = dict(self.valid_recallflow)
        bad["components"]["frontend"]["destination"] = "ftp://invalid-server"
        with self.assertRaises(ManifestValidationError):
            ApplicationManifest.from_dict(bad)

        bad2 = dict(self.valid_recallflow)
        bad2["components"]["backend"]["destination"] = ""
        with self.assertRaises(ManifestValidationError):
            ApplicationManifest.from_dict(bad2)

    def test_06_reject_invalid_component_type_or_provider(self):
        bad = dict(self.valid_recallflow)
        bad["components"]["frontend"]["type"] = "unsupported_type"
        with self.assertRaises(ManifestValidationError):
            ApplicationManifest.from_dict(bad)

        bad2 = dict(self.valid_recallflow)
        bad2["components"]["frontend"]["provider"] = "unknown_cloud"
        with self.assertRaises(ManifestValidationError):
            ApplicationManifest.from_dict(bad2)


class TestManifestRegistryAndIsolation(unittest.TestCase):

    def setUp(self):
        self.registry = ManifestRegistry()
        self.app1 = {
            "name": "RecallFlow",
            "public_path": "/RecallFlow",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "https://recallflow.vercel.app",
                    "routing": {"path_prefix": "/"}
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": "https://recallflow-api.onrender.com",
                    "routing": {"path_prefix": "/api", "api_priority": True}
                }
            }
        }
        self.app2 = {
            "name": "MemoVault",
            "public_path": "/MemoVault",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "https://memovault.vercel.app",
                    "routing": {"path_prefix": "/"}
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": "https://memovault-api.onrender.com",
                    "routing": {"path_prefix": "/api", "api_priority": True}
                }
            }
        }

    def test_01_register_multiple_applications_without_conflict(self):
        m1 = self.registry.register(self.app1)
        m2 = self.registry.register(self.app2)
        self.assertEqual(m1.name, "RecallFlow")
        self.assertEqual(m2.name, "MemoVault")
        self.assertEqual(len(self.registry.list_all()), 2)

    def test_02_reject_duplicate_application_name(self):
        self.registry.register(self.app1)
        duplicate = dict(self.app2)
        duplicate["name"] = "recallflow"  # Case-insensitive duplicate
        duplicate["public_path"] = "/MemoVault"
        with self.assertRaises(RouteConflictError):
            self.registry.register(duplicate)

    def test_03_reject_conflicting_public_path(self):
        self.registry.register(self.app1)
        conflict = dict(self.app2)
        conflict["name"] = "AnotherApp"
        conflict["public_path"] = "/recallflow"  # Conflicts with /RecallFlow
        with self.assertRaises(RouteConflictError):
            self.registry.register(conflict)

    def test_04_application_update_isolation(self):
        """Updating MemoVault must not alter RecallFlow."""
        self.registry.register(self.app1)
        self.registry.register(self.app2)

        # Update MemoVault
        updated = self.registry.update("MemoVault", {"display_name": "MemoVault V2"})
        self.assertEqual(updated.display_name, "MemoVault V2")

        # Verify RecallFlow remains unchanged
        rf = self.registry.get_by_name("RecallFlow")
        self.assertIsNotNone(rf)
        self.assertEqual(rf.public_path, "/RecallFlow")
        self.assertEqual(rf.components["backend"].destination, "https://recallflow-api.onrender.com")

    def test_05_application_removal_isolation(self):
        """Removing one app marks it deleted without affecting other registered apps."""
        self.registry.register(self.app1)
        self.registry.register(self.app2)

        self.assertTrue(self.registry.remove("RecallFlow"))
        self.assertIsNone(self.registry.get_by_name("RecallFlow"))

        # MemoVault remains active
        mv = self.registry.get_by_name("MemoVault")
        self.assertIsNotNone(mv)
        self.assertEqual(mv.status, "active")


class TestAppRouteResolver(unittest.TestCase):

    def setUp(self):
        self.registry = ManifestRegistry()
        self.registry.register({
            "name": "RecallFlow",
            "public_path": "/RecallFlow",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "https://recallflow.vercel.app",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "spa_fallback": True,
                        "assets_prefix": "/assets"
                    }
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": "https://recallflow-api.onrender.com",
                    "routing": {
                        "path_prefix": "/api",
                        "strip_mode": "strip_app",
                        "api_priority": True
                    }
                }
            }
        })
        self.registry.register({
            "name": "MemoVault",
            "public_path": "/MemoVault",
            "components": {
                "frontend": {
                    "type": "frontend",
                    "provider": "vercel",
                    "destination": "https://memovault.vercel.app",
                    "routing": {
                        "path_prefix": "/",
                        "strip_mode": "strip_app",
                        "spa_fallback": True
                    }
                },
                "backend": {
                    "type": "backend",
                    "provider": "render",
                    "destination": "https://memovault-api.onrender.com",
                    "routing": {
                        "path_prefix": "/api",
                        "strip_mode": "strip_all",  # Strips /api
                        "api_priority": True
                    }
                }
            }
        })

    def test_01_trailing_slash_redirect(self):
        """GET /RecallFlow without slash redirects to /RecallFlow/."""
        res, err = AppRouteResolver.resolve("/RecallFlow", self.registry)
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertTrue(res.is_trailing_slash_redirect)
        self.assertEqual(res.redirect_target, "/RecallFlow/")

    def test_02_api_route_precedence(self):
        """Routes matching /RecallFlow/api/* must resolve to backend with is_api=True."""
        res, err = AppRouteResolver.resolve("/RecallFlow/api/notes", self.registry)
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertEqual(res.app_name, "RecallFlow")
        self.assertEqual(res.component_type, "backend")
        self.assertTrue(res.is_api)
        self.assertFalse(res.spa_fallback)  # API errors must never fall back to SPA HTML
        # strip_mode: strip_app forwards /api/notes
        self.assertEqual(res.upstream_url, "https://recallflow-api.onrender.com/api/notes")

    def test_03_frontend_route_resolution(self):
        """Frontend routes resolve to Vercel with spa_fallback=True."""
        res, err = AppRouteResolver.resolve("/RecallFlow/dashboard", self.registry)
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertEqual(res.component_type, "frontend")
        self.assertFalse(res.is_api)
        self.assertTrue(res.spa_fallback)
        self.assertEqual(res.upstream_url, "https://recallflow.vercel.app/dashboard")

    def test_04_frontend_assets_no_spa_fallback(self):
        """Static assets (/RecallFlow/assets/*) resolve to frontend but disable spa_fallback."""
        res, err = AppRouteResolver.resolve("/RecallFlow/assets/main.js", self.registry)
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertEqual(res.component_type, "frontend")
        self.assertFalse(res.spa_fallback)
        self.assertEqual(res.upstream_url, "https://recallflow.vercel.app/assets/main.js")

    def test_05_cross_application_isolation(self):
        """MemoVault routes must strictly resolve to MemoVault destinations."""
        res_rf, _ = AppRouteResolver.resolve("/RecallFlow/api/query", self.registry)
        res_mv, _ = AppRouteResolver.resolve("/MemoVault/api/query", self.registry)

        self.assertIn("recallflow-api", res_rf.upstream_url)
        self.assertIn("memovault-api", res_mv.upstream_url)
        self.assertNotIn("memovault", res_rf.upstream_url)
        self.assertNotIn("recallflow", res_mv.upstream_url)

    def test_06_strip_mode_strip_all(self):
        """MemoVault backend uses strip_mode: strip_all (strips /MemoVault/api -> /query)."""
        res, _ = AppRouteResolver.resolve("/MemoVault/api/query", self.registry)
        self.assertEqual(res.upstream_url, "https://memovault-api.onrender.com/query")

    def test_07_unknown_route_returns_none(self):
        """Paths not matching any registered application prefix return None."""
        res, err = AppRouteResolver.resolve("/UnknownApp/test", self.registry)
        self.assertIsNone(res)
        self.assertIsNone(err)


class TestDeploymentAdapters(unittest.TestCase):

    def test_01_adapter_boundaries(self):
        v_cfg = VercelDeploymentAdapter.get_required_config()
        self.assertIn("VERCEL_TOKEN", v_cfg["required_env_vars"])
        self.assertIn("Vercel Rewrites", v_cfg["external_routing_layer"])

        r_cfg = RenderDeploymentAdapter.get_required_config()
        self.assertIn("RENDER_API_KEY", r_cfg["required_env_vars"])

        s_cfg = PersonalServerSelfHostedAdapter.get_required_config()
        self.assertEqual(s_cfg["build_target"], "cap:build")
        self.assertEqual(s_cfg["storage_authority"], "Vivo Y31 Controller")


if __name__ == "__main__":
    unittest.main()
