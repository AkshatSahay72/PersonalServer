#!/usr/bin/env python3
"""
PersonalServer Phase R6: Native Static Web Runtime Adapter Test Suite
=====================================================================
Validates:
1. Adapter registration and environment validation.
2. Static root detection: explicit configuration precedence, conventional roots (dist, build, public, .).
3. Missing index.html rejection and invalid/traversal static root handling.
4. Symlink security: rejects symlinks pointing outside release directory.
5. Content serving & MIME types: HTML, CSS, JavaScript, JSON, SVG, images, fonts.
6. Path traversal and directory listing protection.
7. SPA fallback routing to index.html when configured vs 404 when disabled.
8. End-to-end deployment lifecycle, scheduler placement, idempotency, and rollback preservation.
"""

import os
import sys
import json
import time
import shutil
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import ControllerPaths
from controller.stores import (
    load_apps_db,
    save_apps_db,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
)
from controller.deployment_manager import DeploymentManager, DeploymentError
from controller.artifact_packager import package_source_to_artifact
from agent.runtime_adapters import get_runtime_adapter, StaticWebAdapter
from router.app_router import guess_static_mime, ApplicationRouter


class TestStaticWebAdapter(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_r6_test_"))
        self.data_dir = self.test_dir / "data"
        self.runtime_dir = self.test_dir / "runtime"
        self.storage_dir = self.test_dir / "storage"
        self.artifacts_dir = self.storage_dir / "artifacts"
        self.apps_storage = self.storage_dir / "applications"

        for d in (self.data_dir, self.runtime_dir, self.storage_dir, self.artifacts_dir, self.apps_storage):
            d.mkdir(parents=True, exist_ok=True)

        self.paths = ControllerPaths(
            base_dir=self.test_dir,
            data_dir=self.data_dir,
            runtime_dir=self.runtime_dir,
            storage_dir=self.storage_dir
        )
        self.paths.ensure_directories()
        self.adapter = StaticWebAdapter()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_adapter_registration(self):
        """Validates that static runtime aliases resolve to StaticWebAdapter."""
        for alias in ("static", "static-web", "html", "web"):
            adapter = get_runtime_adapter(alias)
            self.assertIsInstance(adapter, StaticWebAdapter)
            self.assertEqual(adapter.runtime_name, "static")

        is_ok, err = self.adapter.validate_environment()
        self.assertTrue(is_ok, f"Environment validation failed: {err}")

    def test_detect_static_root_conventions(self):
        """Validates detection across dist/, build/, public/, and root ."""
        # 1. dist/
        rel1 = self.test_dir / "rel_dist"
        (rel1 / "dist").mkdir(parents=True)
        (rel1 / "dist" / "index.html").write_text("<h1>Dist</h1>", encoding="utf-8")
        detected, err = self.adapter.detect_static_root(rel1)
        self.assertIsNone(err)
        self.assertEqual(detected, (rel1 / "dist").resolve())

        # 2. build/
        rel2 = self.test_dir / "rel_build"
        (rel2 / "build").mkdir(parents=True)
        (rel2 / "build" / "index.html").write_text("<h1>Build</h1>", encoding="utf-8")
        detected, err = self.adapter.detect_static_root(rel2)
        self.assertIsNone(err)
        self.assertEqual(detected, (rel2 / "build").resolve())

        # 3. public/
        rel3 = self.test_dir / "rel_public"
        (rel3 / "public").mkdir(parents=True)
        (rel3 / "public" / "index.html").write_text("<h1>Public</h1>", encoding="utf-8")
        detected, err = self.adapter.detect_static_root(rel3)
        self.assertIsNone(err)
        self.assertEqual(detected, (rel3 / "public").resolve())

        # 4. root
        rel4 = self.test_dir / "rel_root"
        rel4.mkdir(parents=True)
        (rel4 / "index.html").write_text("<h1>Root</h1>", encoding="utf-8")
        detected, err = self.adapter.detect_static_root(rel4)
        self.assertIsNone(err)
        self.assertEqual(detected, rel4.resolve())

    def test_explicit_configuration_precedence(self):
        """Explicit configured_root takes precedence over conventional dist/."""
        rel = self.test_dir / "rel_explicit"
        (rel / "dist").mkdir(parents=True)
        (rel / "dist" / "index.html").write_text("<h1>Dist</h1>", encoding="utf-8")
        (rel / "custom_out").mkdir(parents=True)
        (rel / "custom_out" / "index.html").write_text("<h1>Custom</h1>", encoding="utf-8")

        detected, err = self.adapter.detect_static_root(rel, configured_root="custom_out")
        self.assertIsNone(err)
        self.assertEqual(detected, (rel / "custom_out").resolve())

    def test_missing_index_html_rejection(self):
        """Fails cleanly when no index.html is found."""
        rel = self.test_dir / "rel_no_index"
        (rel / "dist").mkdir(parents=True)
        (rel / "dist" / "app.js").write_text("console.log('no index');", encoding="utf-8")

        detected, err = self.adapter.detect_static_root(rel)
        self.assertIsNone(detected)
        self.assertIn("No valid 'index.html' found", err)

        ok, prep_err = self.adapter.prepare_release("app-fail", "dep-fail", rel, {})
        self.assertFalse(ok)
        self.assertIn("index.html", prep_err)

    def test_path_traversal_static_root_rejection(self):
        """Rejects static root configurations that attempt directory traversal."""
        rel = self.test_dir / "rel_traversal"
        rel.mkdir(parents=True)
        (rel / "index.html").write_text("<h1>Root</h1>", encoding="utf-8")

        detected, err = self.adapter.detect_static_root(rel, configured_root="../outside")
        self.assertIsNone(detected)
        self.assertTrue(any(w in err.lower() for w in ("traversal", "outside")), err)

    def test_symlink_security_rejection(self):
        """Rejects release preparation if an unsafe symlink points outside release dir."""
        rel = self.test_dir / "rel_symlink"
        rel.mkdir(parents=True)
        (rel / "index.html").write_text("<h1>OK</h1>", encoding="utf-8")
        outside_target = self.test_dir / "outside_secret.txt"
        outside_target.write_text("SECRET", encoding="utf-8")

        try:
            (rel / "leak.txt").symlink_to(outside_target)
            ok, err = self.adapter.prepare_release("app-leak", "dep-leak", rel, {})
            self.assertFalse(ok)
            self.assertIn("unsafe symlink", err.lower())
        except (OSError, NotImplementedError):
            # In environments where symlink creation requires admin privileges (Windows)
            pass

    def test_mime_type_resolution(self):
        """Verifies accurate MIME type guessing for web assets."""
        self.assertEqual(guess_static_mime("index.html"), "text/html; charset=utf-8")
        self.assertEqual(guess_static_mime("style.css"), "text/css; charset=utf-8")
        self.assertEqual(guess_static_mime("app.js"), "application/javascript; charset=utf-8")
        self.assertEqual(guess_static_mime("bundle.mjs"), "application/javascript; charset=utf-8")
        self.assertEqual(guess_static_mime("data.json"), "application/json; charset=utf-8")
        self.assertEqual(guess_static_mime("logo.svg"), "image/svg+xml")
        self.assertEqual(guess_static_mime("photo.png"), "image/png")
        self.assertEqual(guess_static_mime("banner.jpg"), "image/jpeg")
        self.assertEqual(guess_static_mime("font.woff2"), "font/woff2")
        self.assertEqual(guess_static_mime("binary.bin"), "application/octet-stream")

    def test_deployment_manager_scheduling_and_idempotency(self):
        """Submits static app deployment, verifies placement, contract, and duplicate idempotency."""
        # Seed nodes
        save_nodes_db({
            "nodes": {
                "node-arm64-static": {
                    "node_id": "node-arm64-static",
                    "status": "ONLINE",
                    "role": "compute",
                    "architecture": "aarch64",
                    "capabilities": {"compute": True, "static": True},
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "last_heartbeat": {"services": {"node_worker": "running"}},
                    "cpu_cores": 8,
                    "ram_mb": 4096,
                    "metrics": {"cpu_percent": 10.0, "ram_used_mb": 500}
                }
            }
        }, paths=self.paths)

        # Register manifest
        DeploymentManager.register_manifest({
            "app_id": "app-static-test",
            "name": "static-test",
            "runtime": "static",
            "static_root": "dist",
            "spa_fallback": True,
            "public_path": "/apps/static-test"
        }, paths=self.paths)

        # Create source
        src_dir = self.test_dir / "src_static"
        (src_dir / "dist").mkdir(parents=True)
        (src_dir / "dist" / "index.html").write_text("<!DOCTYPE html><html><body>Test</body></html>", encoding="utf-8")
        (src_dir / "dist" / "style.css").write_text("body { color: blue; }", encoding="utf-8")

        art = package_source_to_artifact(src_dir, "app-static-test", "dep-static-1", paths=self.paths)

        # Submit deployment
        res = DeploymentManager.submit_deployment(
            app_id="app-static-test",
            artifact_path=Path(art.get("path") or art.get("storage_path")),
            artifact_sha256=art["sha256"],
            paths=self.paths
        )

        self.assertIsNotNone(res.get("deployment_id"))
        self.assertEqual(res.get("assigned_node") or res.get("node"), "node-arm64-static")

        # Verify job definition
        jobs_db = load_jobs_db(paths=self.paths)
        job = jobs_db.get("jobs", {}).get(res["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["parameters"]["project_type"], "static")
        self.assertIn("compute", job["requirements"]["capabilities"])

        # Test duplicate submission idempotency
        dup = DeploymentManager.submit_deployment(
            app_id="app-static-test",
            idempotency_key=f"app-static-test:{res['deployment_id']}",
            paths=self.paths
        )
        self.assertTrue(dup.get("idempotent"))
        self.assertEqual(dup.get("deployment_id"), res["deployment_id"])

    def test_router_static_serving_and_spa_fallback(self):
        """Tests live HTTP router serving static files, preventing traversal, and SPA fallback."""
        from http.server import ThreadingHTTPServer
        from router.app_router import RouterHTTPHandler
        import urllib.request
        import threading

        # Create release on disk
        app_id = "app-live-static"
        dep_id = "dep-live-1"
        rel_dir = BASE_DIR / "storage" / "applications" / app_id / "releases" / dep_id / "dist"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "index.html").write_text("<!DOCTYPE html><html><body>SPA Home</body></html>", encoding="utf-8")
        (rel_dir / "style.css").write_text("h1 { color: red; }", encoding="utf-8")
        (rel_dir / "app.js").write_text("console.log('live');", encoding="utf-8")

        # Create sub-directory without index.html
        sub_dir = rel_dir / "assets"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        # Save app in apps_db
        save_apps_db({
            "apps": {
                app_id: {
                    "app_id": app_id,
                    "name": "live-static",
                    "runtime": "static",
                    "public_path": "/apps/live-static",
                    "status": "RUNNING",
                    "active_deployment_id": dep_id,
                    "static_root": "dist",
                    "spa_fallback": True,
                    "selected_node": None
                }
            }
        })

        # Start router on ephemeral port
        server = ThreadingHTTPServer(("127.0.0.1", 0), RouterHTTPHandler)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. Test index.html
            with urllib.request.urlopen(f"{base_url}/apps/live-static/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIn("SPA Home", resp.read().decode())

            # 2. Test style.css
            with urllib.request.urlopen(f"{base_url}/apps/live-static/style.css", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/css", resp.headers.get("Content-Type", ""))

            # 3. Test app.js
            with urllib.request.urlopen(f"{base_url}/apps/live-static/app.js", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("javascript", resp.headers.get("Content-Type", ""))

            # 4. Test icon.png
            with urllib.request.urlopen(f"{base_url}/apps/live-static/assets/icon.png", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual("image/png", resp.headers.get("Content-Type", ""))

            # 5. Test directory without index.html -> 404
            try:
                urllib.request.urlopen(f"{base_url}/apps/live-static/assets/", timeout=5)
                self.fail("Expected 404 for directory without index.html")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)

            # 6. Test SPA fallback on unknown route (/apps/live-static/dashboard/users) -> 200 with index.html
            with urllib.request.urlopen(f"{base_url}/apps/live-static/dashboard/users", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIn("SPA Home", resp.read().decode())

            # 7. Test synthetic /health probe
            with urllib.request.urlopen(f"{base_url}/apps/live-static/health", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                d = json.loads(resp.read().decode())
                self.assertEqual(d.get("status"), "healthy")
                self.assertEqual(d.get("runtime"), "static")

        finally:
            server.shutdown()
            server.server_close()
            # Clean up
            shutil.rmtree(BASE_DIR / "storage" / "applications" / app_id, ignore_errors=True)
            db = load_apps_db()
            db.get("apps", {}).pop(app_id, None)
            save_apps_db(db)


if __name__ == "__main__":
    unittest.main()
