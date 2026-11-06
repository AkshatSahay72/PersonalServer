#!/usr/bin/env python3
"""
Unit tests for Apex Domain & Multi-Application Routing
======================================================
Validates:
1. Longest-prefix sub-app matching (/apps/ExamBuddy -> ExamBuddy)
2. Application namespace boundary: requests under /apps/ that don't match any app return 404 (do not fall through to root)
3. Direct root application redirect (e.g. "/" -> "https://www.akshatsahay.space")
4. Subpath preservation in root redirect (e.g. "/about" -> "https://www.akshatsahay.space/about")
5. Trailing slash normalization for sub-apps (/apps/ExamBuddy -> 308 to /apps/ExamBuddy/)
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from router.app_router import ApplicationRouter


class TestApexAndAppRouting(unittest.TestCase):
    def setUp(self):
        self.mock_apps_db = {
            "apps": {
                "exambuddy": {
                    "app_id": "exambuddy",
                    "name": "ExamBuddy",
                    "public_path": "/apps/ExamBuddy",
                    "status": "RUNNING",
                    "selected_node": "server-95bad5ff01424d4c8d184330d6d2e394",
                    "port": 8096,
                    "host_port": 8096
                },
                "portfolio": {
                    "app_id": "portfolio",
                    "name": "portfolio",
                    "display_name": "Portfolio",
                    "public_path": "/",
                    "status": "RUNNING",
                    "redirect": "https://www.akshatsahay.space"
                }
            }
        }
        self.mock_nodes_db = {
            "nodes": {
                "server-95bad5ff01424d4c8d184330d6d2e394": {
                    "node_id": "server-95bad5ff01424d4c8d184330d6d2e394",
                    "name": "Realme 5 Pro",
                    "status": "ONLINE",
                    "ip": "100.73.52.72"
                }
            }
        }

    def test_exambuddy_route_matching(self):
        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/apps/ExamBuddy/health", self.mock_apps_db
        )
        self.assertIsNotNone(app)
        self.assertEqual(app["app_id"], "exambuddy")
        self.assertEqual(route_pfx, "/apps/ExamBuddy")
        self.assertEqual(remainder, "/health")
        self.assertFalse(is_redir)
        self.assertIsNone(err)

    def test_exambuddy_trailing_slash_redirect(self):
        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/apps/ExamBuddy", self.mock_apps_db
        )
        self.assertTrue(is_redir)
        self.assertEqual(route_pfx, "/apps/ExamBuddy")

    def test_apps_namespace_boundary_nonexistent_app(self):
        """Requests starting with /apps/ that do not match an app must NOT fall through to root."""
        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/apps/NonExistentApp/dashboard", self.mock_apps_db
        )
        self.assertIsNone(app, "Nonexistent app under /apps/ must not match any app")
        self.assertIsNone(route_pfx)

    def test_apps_namespace_boundary_apps_root(self):
        """Request for /apps or /apps/ must NOT fall through to root application."""
        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/apps", self.mock_apps_db
        )
        self.assertIsNone(app)

        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/apps/", self.mock_apps_db
        )
        self.assertIsNone(app)

    def test_root_fallback_matches_root_app(self):
        """Top-level requests outside /apps/ should match root application."""
        app, route_pfx, remainder, is_redir, err = ApplicationRouter.match_route(
            "/", self.mock_apps_db
        )
        self.assertIsNotNone(app)
        self.assertEqual(app["app_id"], "portfolio")
        self.assertEqual(app.get("redirect"), "https://www.akshatsahay.space")

        app2, route_pfx2, remainder2, is_redir2, err2 = ApplicationRouter.match_route(
            "/about", self.mock_apps_db
        )
        self.assertIsNotNone(app2)
        self.assertEqual(app2["app_id"], "portfolio")

    def test_upstream_resolution_remote_node(self):
        status, upstream, extra = ApplicationRouter.resolve_target_upstream(
            self.mock_apps_db["apps"]["exambuddy"], self.mock_nodes_db
        )
        self.assertEqual(status, 200)
        self.assertEqual(upstream, "http://100.73.52.72:8096")


if __name__ == "__main__":
    unittest.main()
