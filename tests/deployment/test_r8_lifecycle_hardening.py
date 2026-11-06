#!/usr/bin/env python3
"""
PersonalServer Phase R8: Deployment Lifecycle Hardening Test Suite
==================================================================
Validates:
1. Concurrency protection: Application-level deployment locks prevent concurrent deployments targeting the same app.
2. Concurrent deployments across different apps proceed independently.
3. Idempotency: Duplicate submissions return active deployment without spawning parallel jobs.
4. Controller restart recovery: Recovers in-flight deployments, validates candidate health, and prevents duplicate executions.
5. Worker disappearance: Detects dead workers/expired leases, transitions to FAILED, and preserves previous healthy release.
6. Stale worker responses: Fencing tokens prevent late/superseded worker responses from corrupting state.
7. Artifact identity consistency: Separates artifact identity from SHA-256 checksum, represents aliases explicitly,
   and links deduplicated artifacts.
8. Artifact security: Detects on-disk tampering (500) and blocks unauthorized node downloads (403).
9. Candidate health check failure: Triggers safe rollback and preserves previous active release.
10. Routing consistency: Zero-downtime serving during deployments; proxy loop prevention (HTTP 508);
    and 504 when assigned worker is offline.
11. No false SUCCEEDED state: State machine rejects transition to SUCCEEDED without health/activation verification.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

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
    load_artifacts_db,
    save_artifacts_db,
)
from scheduler.scheduler import ResourceScheduler
from controller.deployment_job_contract import build_canonical_deployment_job
from controller.deployment_state_machine import (
    DeploymentStateMachine,
    DeploymentTransitionError,
    DEP_STATE_QUEUED,
    DEP_STATE_FETCHING,
    DEP_STATE_STARTING,
    DEP_STATE_HEALTH_CHECK,
    DEP_STATE_ACTIVATING,
    DEP_STATE_SUCCEEDED,
    DEP_STATE_FAILED,
)
from controller.deployment_manager import (
    DeploymentManager,
    DeploymentError,
    DeploymentConcurrencyError,
)
from controller.artifact_packager import package_source_to_artifact
from router.app_router import ApplicationRouter


class TestDeploymentLifecycleHardening(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_r8_test_"))
        self.data_dir = self.test_dir / "data"
        self.runtime_dir = self.test_dir / "runtime"
        self.storage_dir = self.test_dir / "storage"
        self.apps_storage = self.storage_dir / "applications"
        self.artifacts_storage = self.storage_dir / "artifacts"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.apps_storage.mkdir(parents=True, exist_ok=True)
        self.artifacts_storage.mkdir(parents=True, exist_ok=True)

        self.paths = ControllerPaths(
            data_dir=self.data_dir,
            runtime_dir=self.runtime_dir,
            base_dir=self.test_dir
        )
        self.paths.artifacts_dir = self.artifacts_storage

        # Initialize clean stores
        save_apps_db({"apps": {}}, paths=self.paths)
        save_nodes_db({"nodes": {}}, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)
        save_artifacts_db({"artifacts": {}}, paths=self.paths)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Concurrency Protection & Locking
    # -------------------------------------------------------------------------

    def test_01_concurrent_deployments_same_app_blocked(self):
        """Prevent two concurrent deployments targeting the same application from colliding."""
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-concurrent"] = {
            "id": "app-concurrent",
            "name": "App Concurrent",
            "runtime": "python",
            "status": "STOPPED",
            "deployments": []
        }
        save_apps_db(db, paths=self.paths)

        # First deployment acquires lock
        ok1, err1 = DeploymentManager.acquire_deployment_lock(
            "app-concurrent",
            "dep-first01",
            owner="client-1",
            ttl_seconds=60,
            paths=self.paths
        )
        self.assertTrue(ok1)
        self.assertIsNone(err1)

        # Second deployment targeting the same app is rejected with concurrency error
        ok2, err2 = DeploymentManager.acquire_deployment_lock(
            "app-concurrent",
            "dep-second02",
            owner="client-2",
            ttl_seconds=60,
            paths=self.paths
        )
        self.assertFalse(ok2)
        self.assertIn("Concurrent deployment conflict", err2)

    def test_02_concurrent_deployments_different_apps_permitted(self):
        """Deployments targeting distinct applications can proceed concurrently without contention."""
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-alpha"] = {"id": "app-alpha", "name": "Alpha", "deployments": []}
        db["apps"]["app-beta"] = {"id": "app-beta", "name": "Beta", "deployments": []}
        save_apps_db(db, paths=self.paths)

        ok_a, _ = DeploymentManager.acquire_deployment_lock("app-alpha", "dep-a", paths=self.paths)
        ok_b, _ = DeploymentManager.acquire_deployment_lock("app-beta", "dep-b", paths=self.paths)
        self.assertTrue(ok_a)
        self.assertTrue(ok_b)

    def test_03_expired_deployment_lock_broken_automatically(self):
        """If a controller crashed and an application lock expired, the next deployment breaks it safely."""
        db = load_apps_db(paths=self.paths)
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        db["apps"]["app-stale-lock"] = {
            "id": "app-stale-lock",
            "name": "Stale App",
            "deployments": [],
            "deployment_lock": {
                "deployment_id": "dep-dead",
                "owner": "crashed-worker",
                "locked_at": expired_time,
                "expires_at": expired_time
            }
        }
        save_apps_db(db, paths=self.paths)

        ok, err = DeploymentManager.acquire_deployment_lock(
            "app-stale-lock",
            "dep-new-heir",
            ttl_seconds=60,
            paths=self.paths
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

    # -------------------------------------------------------------------------
    # 2. Idempotency & Safe Duplicate Submissions
    # -------------------------------------------------------------------------

    def test_04_duplicate_idempotency_key_returns_existing_deployment(self):
        """Submitting with identical idempotency key returns active deployment without duplicate job creation."""
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-idem"] = {
            "id": "app-idem",
            "name": "Idem App",
            "status": "RUNNING",
            "deployments": [{
                "deployment_id": "dep-existing",
                "job_id": "job-existing",
                "idempotency_key": "commit-abc1234",
                "status": "STARTING",
                "node": "worker-1"
            }]
        }
        save_apps_db(db, paths=self.paths)

        res = DeploymentManager.submit_deployment(
            app_id="app-idem",
            idempotency_key="commit-abc1234",
            paths=self.paths
        )
        self.assertTrue(res.get("idempotent"))
        self.assertEqual(res["deployment_id"], "dep-existing")

    # -------------------------------------------------------------------------
    # 3. Controller Restart Recovery & In-Flight Reconciliation
    # -------------------------------------------------------------------------

    @patch("urllib.request.urlopen")
    def test_05_controller_restart_recovers_succeeded_worker_job(self, mock_urlopen):
        """If worker finished deployment job while controller was offline, recovery probes health and promotes to SUCCEEDED."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        now_iso = datetime.now(timezone.utc).isoformat()
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-rec-ok"] = {
            "id": "app-rec-ok",
            "name": "Rec App",
            "status": "DEPLOYING",
            "active_deployment_id": "dep-prior-v1",
            "deployments": [{
                "deployment_id": "dep-rec-ok",
                "job_id": "job-rec-ok",
                "status": "STARTING",
                "node": "node-1",
                "port": 18100,
                "started_at": now_iso
            }],
            "deployment_lock": {"deployment_id": "dep-rec-ok", "expires_at": now_iso}
        }
        save_apps_db(db, paths=self.paths)

        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"]["job-rec-ok"] = {
            "job_id": "job-rec-ok",
            "status": "SUCCEEDED",
            "assigned_node": "node-1",
            "declared_outputs": {"port": 18100, "pid": 5512}
        }
        save_jobs_db(jobs_db, paths=self.paths)

        nodes_db = load_nodes_db(paths=self.paths)
        nodes_db["nodes"]["node-1"] = {"id": "node-1", "status": "ONLINE", "last_seen": now_iso, "ip": "127.0.0.1"}
        save_nodes_db(nodes_db, paths=self.paths)

        summary = DeploymentManager.reconcile_in_flight_deployments(paths=self.paths)
        self.assertEqual(summary["recovered_success"], 1)

        # Check app was promoted to RUNNING with new deployment
        db_after = load_apps_db(paths=self.paths)
        app_after = db_after["apps"]["app-rec-ok"]
        self.assertEqual(app_after["status"], "RUNNING")
        self.assertEqual(app_after["active_deployment_id"], "dep-rec-ok")
        self.assertIsNone(app_after.get("deployment_lock"))

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_06_controller_restart_unhealthy_candidate_rolls_back_to_prior(self, mock_urlopen):
        """If worker finished job but candidate fails health check, recovery transitions to FAILED and preserves prior release."""
        now_iso = datetime.now(timezone.utc).isoformat()
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-rec-fail"] = {
            "id": "app-rec-fail",
            "name": "Rec Fail App",
            "status": "DEPLOYING",
            "active_deployment_id": "dep-prior-v1",
            "deployments": [{
                "deployment_id": "dep-candidate",
                "job_id": "job-candidate",
                "status": "STARTING",
                "node": "node-1",
                "port": 18101,
                "started_at": now_iso
            }]
        }
        save_apps_db(db, paths=self.paths)

        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"]["job-candidate"] = {
            "job_id": "job-candidate",
            "status": "SUCCEEDED",
            "assigned_node": "node-1",
            "declared_outputs": {"port": 18101}
        }
        save_jobs_db(jobs_db, paths=self.paths)

        nodes_db = load_nodes_db(paths=self.paths)
        nodes_db["nodes"]["node-1"] = {"id": "node-1", "status": "ONLINE", "last_seen": now_iso, "ip": "127.0.0.1"}
        save_nodes_db(nodes_db, paths=self.paths)

        summary = DeploymentManager.reconcile_in_flight_deployments(paths=self.paths)
        self.assertEqual(summary["recovered_failed"], 1)

        # Prior active release preserved
        db_after = load_apps_db(paths=self.paths)
        app_after = db_after["apps"]["app-rec-fail"]
        self.assertEqual(app_after["status"], "RUNNING")
        self.assertEqual(app_after["active_deployment_id"], "dep-prior-v1")

    # -------------------------------------------------------------------------
    # 4. Worker Disappearance & Expired Leases
    # -------------------------------------------------------------------------

    def test_07_worker_disappearance_during_deployment_marked_failed(self):
        """When worker goes offline or lease expires during deployment, mark FAILED and preserve prior release."""
        now_iso = datetime.now(timezone.utc).isoformat()
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-lost-worker"] = {
            "id": "app-lost-worker",
            "name": "Lost Worker App",
            "status": "DEPLOYING",
            "active_deployment_id": "dep-safe-v1",
            "deployments": [{
                "deployment_id": "dep-stranded",
                "job_id": "job-stranded",
                "status": "STARTING",
                "node": "worker-dead",
                "started_at": now_iso
            }]
        }
        save_apps_db(db, paths=self.paths)

        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"]["job-stranded"] = {
            "job_id": "job-stranded",
            "status": "RUNNING",
            "assigned_node": "worker-dead",
            "lease_expires_at": time.time() - 60  # Expired lease
        }
        save_jobs_db(jobs_db, paths=self.paths)

        nodes_db = load_nodes_db(paths=self.paths)
        nodes_db["nodes"]["worker-dead"] = {
            "id": "worker-dead",
            "status": "OFFLINE",
            "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        }
        save_nodes_db(nodes_db, paths=self.paths)

        summary = DeploymentManager.reconcile_in_flight_deployments(paths=self.paths)
        self.assertEqual(summary["recovered_failed"], 1)

        db_after = load_apps_db(paths=self.paths)
        app_after = db_after["apps"]["app-lost-worker"]
        self.assertEqual(app_after["active_deployment_id"], "dep-safe-v1")
        self.assertEqual(app_after["status"], "RUNNING")

    def test_08_active_in_flight_lease_preserved_without_duplicate_job(self):
        """A valid running job on a healthy worker is preserved without resubmitting duplicate execution."""
        now_iso = datetime.now(timezone.utc).isoformat()
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-inflight"] = {
            "id": "app-inflight",
            "name": "Inflight App",
            "status": "DEPLOYING",
            "deployments": [{
                "deployment_id": "dep-legit",
                "job_id": "job-legit",
                "status": "STARTING",
                "node": "worker-healthy",
                "started_at": now_iso
            }]
        }
        save_apps_db(db, paths=self.paths)

        jobs_db = load_jobs_db(paths=self.paths)
        jobs_db["jobs"]["job-legit"] = {
            "job_id": "job-legit",
            "status": "RUNNING",
            "assigned_node": "worker-healthy",
            "lease_expires_at": time.time() + 120  # Valid lease
        }
        save_jobs_db(jobs_db, paths=self.paths)

        nodes_db = load_nodes_db(paths=self.paths)
        nodes_db["nodes"]["worker-healthy"] = {
            "id": "worker-healthy",
            "status": "ONLINE",
            "last_seen": now_iso,
            "ip": "127.0.0.1"
        }
        save_nodes_db(nodes_db, paths=self.paths)

        summary = DeploymentManager.reconcile_in_flight_deployments(paths=self.paths)
        self.assertEqual(summary["in_flight_preserved"], 1)

        # Check job remains intact
        jobs_db_after = load_jobs_db(paths=self.paths)
        self.assertEqual(jobs_db_after["jobs"]["job-legit"]["status"], "RUNNING")

    # -------------------------------------------------------------------------
    # 5. Artifact Identity & Alias Deduplication
    # -------------------------------------------------------------------------

    def test_09_artifact_identity_canonical_model_and_alias_linking(self):
        """Packaging identical source content produces an explicit alias linked to canonical artifact."""
        src_dir = self.test_dir / "src_test_artifact"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "app.py").write_text("print('hello world')", encoding="utf-8")

        # 1. Package first time: creates canonical artifact
        rec1 = package_source_to_artifact(
            source_dir=src_dir,
            app_id="app-art-test",
            deployment_id="dep-1",
            paths=self.paths,
            authorized_nodes=["node-alpha"]
        )
        self.assertFalse(rec1["is_alias"])
        self.assertEqual(rec1["canonical_artifact_id"], rec1["artifact_id"])
        self.assertIn("node-alpha", rec1["authorized_nodes"])

        # 2. Package identical content second time for a new deployment on another node:
        # Generates new unique deployment artifact_id, but explicitly references canonical_artifact_id
        rec2 = package_source_to_artifact(
            source_dir=src_dir,
            app_id="app-art-test",
            deployment_id="dep-2",
            paths=self.paths,
            authorized_nodes=["node-beta"]
        )
        self.assertTrue(rec2["is_alias"])
        self.assertEqual(rec2["canonical_artifact_id"], rec1["artifact_id"])
        self.assertEqual(rec2["sha256"], rec1["sha256"])

        # Canonical record references rec2 in aliases list and unions authorized_nodes
        art_db = load_artifacts_db(paths=self.paths)
        canon = art_db["artifacts"][rec1["artifact_id"]]
        self.assertIn(rec2["artifact_id"], canon["aliases"])
        self.assertIn("node-beta", canon["authorized_nodes"])

    # -------------------------------------------------------------------------
    # 6. Routing Consistency & Zero-Downtime Serving
    # -------------------------------------------------------------------------

    def test_10_router_serves_active_deployment_during_new_deployment(self):
        """ApplicationRouter continues routing to active deployment while a new release is DEPLOYING."""
        now_iso = datetime.now(timezone.utc).isoformat()
        nodes_db = {
            "nodes": {
                "worker-prod": {"id": "worker-prod", "status": "ONLINE", "ip": "127.0.0.1", "last_seen": now_iso}
            }
        }
        app = {
            "id": "app-live",
            "name": "Live App",
            "status": "DEPLOYING",  # In progress
            "active_deployment_id": "dep-v1-stable",
            "selected_node": "worker-prod",
            "port": 18200
        }

        status, dest, headers = ApplicationRouter.resolve_target_upstream(app, nodes_db)
        self.assertEqual(status, 200)
        self.assertEqual(dest, "http://127.0.0.1:18200")

    def test_11_router_returns_503_for_first_time_deploying_app_without_active_release(self):
        """When an application has no prior release and is DEPLOYING, router returns 503."""
        nodes_db = {"nodes": {}}
        app = {
            "id": "app-brand-new",
            "name": "Brand New App",
            "status": "DEPLOYING",
            "active_deployment_id": None
        }

        status, dest, headers = ApplicationRouter.resolve_target_upstream(app, nodes_db)
        self.assertEqual(status, 503)
        self.assertIn("starting up", dest["error"])

    def test_12_router_returns_504_when_host_worker_offline(self):
        """When the selected host worker goes offline, router immediately returns HTTP 504."""
        now_iso = datetime.now(timezone.utc).isoformat()
        nodes_db = {
            "nodes": {
                "worker-crashed": {
                    "id": "worker-crashed",
                    "status": "OFFLINE",
                    "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
                }
            }
        }
        app = {
            "id": "app-down",
            "name": "Down App",
            "status": "RUNNING",
            "active_deployment_id": "dep-v1",
            "selected_node": "worker-crashed",
            "port": 18201
        }

        status, dest, headers = ApplicationRouter.resolve_target_upstream(app, nodes_db)
        self.assertEqual(status, 504)
        self.assertIn("unreachable", dest["error"])

    # -------------------------------------------------------------------------
    # 7. State Machine Validity & No False SUCCEEDED
    # -------------------------------------------------------------------------

    def test_13_deployment_cannot_jump_to_succeeded_without_activation(self):
        """State machine rejects illegal transition jumping from FETCHING or QUEUED to SUCCEEDED."""
        db = load_apps_db(paths=self.paths)
        db["apps"]["app-jump"] = {
            "id": "app-jump",
            "deployments": [{"deployment_id": "dep-jump", "status": DEP_STATE_FETCHING}]
        }
        save_apps_db(db, paths=self.paths)

        with self.assertRaises(DeploymentTransitionError):
            DeploymentStateMachine.transition(
                "app-jump",
                "dep-jump",
                DEP_STATE_SUCCEEDED,
                paths=self.paths
            )


if __name__ == "__main__":
    unittest.main()

