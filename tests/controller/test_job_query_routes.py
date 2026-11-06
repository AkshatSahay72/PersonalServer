#!/usr/bin/env python3
"""
Tests for Job Query Route Handlers
==================================
Comprehensive tests for read-only job listing, filtering, search, sorting,
server-side pagination, metrics observability, job detail inspection,
and legacy handler delegation.
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from config.platform_config import override_controller_paths
from controller.constants import (
    JOB_STATE_CANCELLED,
    JOB_STATE_CLAIMED,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
)
from controller.ha_helper import get_ha_manager
from controller.http.job_query_routes import handle_job_query_get
from controller.http.job_routes import handle_job_get
from controller.stores import save_jobs_db, save_nodes_db, save_workflows_db, save_artifacts_db


class TestJobQueryRoutes(unittest.TestCase):
    """Tests the extracted job query route handlers."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_test_job_query_"))
        self.data_dir = self.test_dir / "data"
        self.config_dir = self.test_dir / "config"
        self.secrets_dir = self.config_dir / "secrets"
        self.runtime_dir = self.test_dir / "runtime"
        self.logs_dir = self.test_dir / "logs"
        self.storage_dir = self.test_dir / "storage"

        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            secrets_dir=self.secrets_dir,
            runtime_dir=self.runtime_dir,
            logs_dir=self.logs_dir,
            storage_dir=self.storage_dir
        )
        self.paths = self.paths_ctx.__enter__()
        get_ha_manager(paths=self.paths, role="leader", force_reload=True)

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_nodes_db({
            "nodes": {
                "node-alpha": {"node_id": "node-alpha", "name": "Node Alpha", "status": "ONLINE", "last_seen": now_iso},
                "node-beta": {"node_id": "node-beta", "name": "Node Beta", "status": "ONLINE", "last_seen": now_iso},
                "node-gamma": {"node_id": "node-gamma", "name": "Node Gamma", "status": "ONLINE", "last_seen": now_iso},
            }
        })
        save_workflows_db({"workflows": {}})
        save_artifacts_db({"artifacts": {}})

        # Create mock HTTP handler
        self.handler = MagicMock()
        self.handler.heartbeat_timeout = 60
        self.response_status = None
        self.response_json = None

        def _send_json(status: int, data: dict):
            self.response_status = status
            self.response_json = data

        self.handler.send_json.side_effect = _send_json

    def tearDown(self):
        try:
            self.paths_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            get_ha_manager(force_reload=True)
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _seed_sample_jobs(self):
        jobs = {
            "job-001": {
                "job_id": "job-001",
                "type": "backup",
                "status": JOB_STATE_SUCCEEDED,
                "target_node": "node-alpha",
                "assigned_node": "node-alpha",
                "created_at": "2026-11-01T10:00:00Z",
                "result": {"duration_ms": 1200},
                "execution_duration_sec": 1.2,
                "attempt": 1,
                "attempts_history": [{"attempt": 1, "status": "SUCCEEDED"}]
            },
            "job-002": {
                "job_id": "job-002",
                "type": "echo",
                "status": JOB_STATE_QUEUED,
                "target_node": "node-beta",
                "assigned_node": None,
                "created_at": "2026-11-02T10:00:00Z",
                "attempt": 1,
                "attempts_history": []
            },
            "job-003": {
                "job_id": "job-003",
                "type": "compile",
                "status": JOB_STATE_FAILED,
                "target_node": "node-alpha",
                "assigned_node": "node-alpha",
                "created_at": "2026-11-03T10:00:00Z",
                "retry_reason": "Out of memory",
                "result": {"duration_ms": 5000},
                "execution_duration_sec": 5.0,
                "attempt": 2,
                "attempts_history": [{"attempt": 1, "status": "FAILED"}, {"attempt": 2, "status": "FAILED"}]
            },
            "job-004": {
                "job_id": "job-004",
                "type": "backup",
                "status": JOB_STATE_RUNNING,
                "target_node": "node-gamma",
                "assigned_node": "node-gamma",
                "created_at": "2026-11-03T12:00:00Z",
                "lease_expires_at": time.time() + 60,
                "attempt": 1,
                "attempts_history": []
            }
        }
        save_jobs_db({"jobs": jobs})
        return jobs

    def test_01_empty_jobs_listing(self):
        """GET /jobs on empty database returns empty array."""
        save_jobs_db({"jobs": {}})
        handled = handle_job_query_get(self.handler, "/jobs", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["jobs"], [])
        self.assertEqual(self.response_json["count"], 0)
        self.assertEqual(self.response_json["total_count"], 0)

    def test_02_populated_jobs_listing_default_sort(self):
        """GET /jobs returns all jobs sorted recent-first by default."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 4)
        # Recent first: job-004 (12:00), job-003 (10:00), job-002, job-001
        self.assertEqual(self.response_json["jobs"][0]["job_id"], "job-004")
        self.assertEqual(self.response_json["jobs"][-1]["job_id"], "job-001")

    def test_03_filter_by_status(self):
        """Filter jobs by status case-insensitively."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"status": ["queued"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 1)
        self.assertEqual(self.response_json["jobs"][0]["job_id"], "job-002")

    def test_04_filter_by_node(self):
        """Filter jobs by target or assigned node."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"node": ["node-alpha"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 2)
        job_ids = {j["job_id"] for j in self.response_json["jobs"]}
        self.assertEqual(job_ids, {"job-001", "job-003"})

    def test_05_filter_by_type(self):
        """Filter jobs by workload type."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"type": ["backup"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 2)
        job_ids = {j["job_id"] for j in self.response_json["jobs"]}
        self.assertEqual(job_ids, {"job-001", "job-004"})

    def test_06_search_query(self):
        """Search query matches job_id, type, or node."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"search": ["compile"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 1)
        self.assertEqual(self.response_json["jobs"][0]["job_id"], "job-003")

    def test_07_sorting_options(self):
        """Sort by oldest and duration."""
        self._seed_sample_jobs()
        # Sort oldest
        handle_job_query_get(self.handler, "/jobs", {"sort": ["oldest"]})
        self.assertEqual(self.response_json["jobs"][0]["job_id"], "job-001")

        # Sort duration (job-003 has 5000ms duration)
        handle_job_query_get(self.handler, "/jobs", {"sort": ["duration"]})
        self.assertEqual(self.response_json["jobs"][0]["job_id"], "job-003")

    def test_08_pagination_pages(self):
        """Server-side pagination with page and limit."""
        self._seed_sample_jobs()
        # Page 1, limit 2
        handled = handle_job_query_get(self.handler, "/jobs", {"limit": ["2"], "page": ["1"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 2)
        self.assertEqual(self.response_json["total_count"], 4)
        self.assertEqual(self.response_json["page"], 1)
        self.assertEqual(self.response_json["page_size"], 2)
        self.assertEqual(self.response_json["total_pages"], 2)
        page1_ids = [j["job_id"] for j in self.response_json["jobs"]]

        # Page 2, limit 2
        handled2 = handle_job_query_get(self.handler, "/jobs", {"limit": ["2"], "page": ["2"]})
        self.assertTrue(handled2)
        self.assertEqual(self.response_json["count"], 2)
        self.assertEqual(self.response_json["page"], 2)
        page2_ids = [j["job_id"] for j in self.response_json["jobs"]]

        self.assertEqual(len(set(page1_ids + page2_ids)), 4)

    def test_09_pagination_offset(self):
        """Server-side pagination with explicit offset."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"limit": ["2"], "offset": ["2"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 2)
        self.assertEqual(self.response_json["page"], 2)

    def test_10_invalid_query_parameters_handled_safely(self):
        """Invalid non-integer limit/page values fall back to defaults gracefully."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs", {"limit": ["abc"], "page": ["xyz"]})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["page"], 1)
        self.assertEqual(self.response_json["page_size"], 25)

    def test_11_job_detail_found(self):
        """GET /jobs/<job_id> returns job details and diagnostics."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs/job-003", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["job"]["job_id"], "job-003")
        diag = self.response_json["diagnostics"]
        self.assertEqual(diag["attempts_total"], 2)
        self.assertEqual(diag["current_attempt"], 2)
        self.assertFalse(diag["is_active"])
        self.assertEqual(diag["last_failure_reason"], "Out of memory")

    def test_12_job_detail_not_found(self):
        """GET /jobs/<job_id> returns 404 for unknown job."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs/job-nonexistent", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 404)
        self.assertIn("not found", self.response_json["error"])

    def test_13_job_metrics(self):
        """GET /jobs/metrics calculates queue depth and stats."""
        self._seed_sample_jobs()
        handled = handle_job_query_get(self.handler, "/jobs/metrics", {})
        self.assertTrue(handled)
        self.assertEqual(self.response_status, 200)
        m = self.response_json
        self.assertEqual(m["total_jobs"], 4)
        self.assertEqual(m["queue_depth"], 1)  # only job-002 is QUEUED
        self.assertEqual(m["active_leases"], 1)  # job-004 is RUNNING with active lease
        self.assertIn("status_counts", m)
        self.assertEqual(m["status_counts"]["QUEUED"], 1)
        self.assertEqual(m["status_counts"]["SUCCEEDED"], 1)
        self.assertEqual(m["status_counts"]["FAILED"], 1)
        self.assertEqual(m["status_counts"]["RUNNING"], 1)

    def test_14_legacy_handler_delegation(self):
        """Directly invoking handle_job_get delegates to job query handler."""
        self._seed_sample_jobs()
        # Listing
        handled_list = handle_job_get(self.handler, "/jobs", {})
        self.assertTrue(handled_list)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["count"], 4)

        # Metrics
        handled_metrics = handle_job_get(self.handler, "/jobs/metrics", {})
        self.assertTrue(handled_metrics)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["total_jobs"], 4)

        # Details
        handled_detail = handle_job_get(self.handler, "/jobs/job-001", {})
        self.assertTrue(handled_detail)
        self.assertEqual(self.response_status, 200)
        self.assertEqual(self.response_json["job"]["job_id"], "job-001")

    def test_15_subresource_artifacts_not_intercepted(self):
        """Sub-resources like /jobs/<id>/artifacts return False in job query handler."""
        handled = handle_job_query_get(self.handler, "/jobs/job-001/artifacts", {})
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
