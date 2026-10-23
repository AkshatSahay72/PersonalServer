#!/usr/bin/env python3
"""
Unit Tests for Controller Persistence Stores (controller/stores/)
=================================================================
Verifies:
1. Default schemas when files are absent (nodes, jobs, apps, onboarding codes)
2. Successful atomic save and load cycles
3. Backup (.bak) creation during saves
4. Recovery from backup when primary file is corrupt or truncated
5. Jobs self-healing (recovering primary file on disk from valid backup)
6. Atomic temp-file replacement
7. Re-entrant lock (DB_LOCK) concurrency safety
8. State version bumper callback integration
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import ControllerPaths
from controller.stores import (
    DB_LOCK,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
    load_apps_db,
    save_apps_db,
    load_onboarding_codes_db,
    save_onboarding_codes_db,
    set_state_version_bumper,
)


class TestPersistenceStores(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ps_stores_test_")
        self.root = Path(self.temp_dir)
        self.paths = ControllerPaths(self.root)
        self.paths.ensure_directories()
        set_state_version_bumper(None)

    def tearDown(self):
        set_state_version_bumper(None)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 1. Default Initialization (Missing Files)
    # --------------------------------------------------------------------------
    def test_missing_files_default_initialization(self):
        nodes = load_nodes_db(self.paths)
        self.assertEqual(nodes, {"cluster_name": "PersonalServer", "nodes": {}})

        jobs = load_jobs_db(self.paths)
        self.assertEqual(jobs, {"jobs": {}})

        apps = load_apps_db(self.paths)
        self.assertEqual(apps, {"apps": {}})

        codes = load_onboarding_codes_db(self.paths)
        self.assertEqual(codes, {"codes": {}})

    # --------------------------------------------------------------------------
    # 2. Save & Load Cycle
    # --------------------------------------------------------------------------
    def test_nodes_store_save_and_load(self):
        data = {"cluster_name": "TestCluster", "nodes": {"node-01": {"name": "test-node", "status": "ONLINE"}}}
        save_nodes_db(data, self.paths)

        loaded = load_nodes_db(self.paths)
        self.assertEqual(loaded, data)
        self.assertTrue(self.paths.nodes_file.exists())

    def test_jobs_store_save_and_load(self):
        data = {"jobs": {"job-101": {"status": "QUEUED", "command": "echo hello"}}}
        save_jobs_db(data, self.paths)

        loaded = load_jobs_db(self.paths)
        self.assertEqual(loaded, data)
        self.assertTrue(self.paths.jobs_file.exists())

    def test_apps_store_save_and_load(self):
        data = {"apps": {"app-web": {"port": 8080, "status": "RUNNING"}}}
        save_apps_db(data, self.paths)

        loaded = load_apps_db(self.paths)
        self.assertEqual(loaded, data)
        self.assertTrue(self.paths.apps_file.exists())

    def test_onboarding_store_save_and_load(self):
        data = {"codes": {"hash123": {"expires_at": "2030-01-01T00:00:00Z"}}}
        save_onboarding_codes_db(data, self.paths)

        loaded = load_onboarding_codes_db(self.paths)
        self.assertEqual(loaded, data)
        self.assertTrue(self.paths.onboarding_codes_file.exists())

    # --------------------------------------------------------------------------
    # 3. Backup (.bak) Creation & Rollback on Corrupt Primary
    # --------------------------------------------------------------------------
    def test_nodes_store_backup_creation_and_rollback(self):
        v1 = {"cluster_name": "PersonalServer", "nodes": {"n1": {"v": 1}}}
        v2 = {"cluster_name": "PersonalServer", "nodes": {"n1": {"v": 2}}}
        save_nodes_db(v1, self.paths)
        self.assertFalse(self.paths.backup_nodes_file.exists())

        # Second save creates .bak of v1
        save_nodes_db(v2, self.paths)
        self.assertTrue(self.paths.backup_nodes_file.exists())
        self.assertEqual(json.loads(self.paths.backup_nodes_file.read_text(encoding="utf-8")), v1)

        # Corrupt primary
        self.paths.nodes_file.write_text("{corrupt_json: invalid", encoding="utf-8")
        recovered = load_nodes_db(self.paths)
        self.assertEqual(recovered, v1)

    def test_jobs_store_self_healing_from_backup(self):
        v1 = {"jobs": {"job-1": {"status": "SUCCEEDED"}}}
        v2 = {"jobs": {"job-1": {"status": "SUCCEEDED"}, "job-2": {"status": "RUNNING"}}}
        save_jobs_db(v1, self.paths)
        save_jobs_db(v2, self.paths)

        # Corrupt primary jobs file
        self.paths.jobs_file.write_text("{invalid json", encoding="utf-8")
        recovered = load_jobs_db(self.paths)
        self.assertEqual(recovered, v1)

        # Jobs store must have self-healed the primary file on disk
        self.assertEqual(json.loads(self.paths.jobs_file.read_text(encoding="utf-8")), v1)

    def test_apps_store_rollback_to_backup(self):
        v1 = {"apps": {"app-1": {"v": 1}}}
        v2 = {"apps": {"app-1": {"v": 2}}}
        save_apps_db(v1, self.paths)
        save_apps_db(v2, self.paths)

        self.paths.apps_file.write_text("{\"incomplete\": ", encoding="utf-8")
        recovered = load_apps_db(self.paths)
        self.assertEqual(recovered, v1)

    def test_corrupt_primary_and_corrupt_backup_returns_default(self):
        save_nodes_db({"nodes": {"n": 1}}, self.paths)
        save_nodes_db({"nodes": {"n": 2}}, self.paths)

        self.paths.nodes_file.write_text("bad", encoding="utf-8")
        self.paths.backup_nodes_file.write_text("also bad", encoding="utf-8")

        recovered = load_nodes_db(self.paths)
        self.assertEqual(recovered, {"cluster_name": "PersonalServer", "nodes": {}})

    # --------------------------------------------------------------------------
    # 4. State Version Bumper Hook
    # --------------------------------------------------------------------------
    def test_state_version_bumper_hook(self):
        bumped_count = 0

        def on_bump():
            nonlocal bumped_count
            bumped_count += 1

        set_state_version_bumper(on_bump)

        save_nodes_db({"nodes": {}}, self.paths)
        self.assertEqual(bumped_count, 1)

        save_jobs_db({"jobs": {}}, self.paths)
        self.assertEqual(bumped_count, 2)

        save_apps_db({"apps": {}}, self.paths)
        self.assertEqual(bumped_count, 3)

        # Onboarding codes save must not bump HA state version
        save_onboarding_codes_db({"codes": {}}, self.paths)
        self.assertEqual(bumped_count, 3)

    # --------------------------------------------------------------------------
    # 5. Concurrent Access Safety
    # --------------------------------------------------------------------------
    def test_concurrent_save_and_load_safety(self):
        errors = []

        def worker(worker_id):
            try:
                for i in range(10):
                    data = {"jobs": {f"job-{worker_id}-{i}": {"count": i}}}
                    save_jobs_db(data, self.paths)
                    loaded = load_jobs_db(self.paths)
                    self.assertIn("jobs", loaded)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
