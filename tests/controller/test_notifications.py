#!/usr/bin/env python3
"""
Unit Tests for Failure Notification & Alert Deduplication
==========================================================
Validates that:
- Failure notifications are deduplicated by (job_id, attempt, error_hash).
- Identical error messages on the same job within the cooldown period are suppressed.
- Different error messages trigger new notifications.
- Terminal failure alerts are properly recorded.
- Persistent job records track notification history and suppression counts.
"""

import unittest
import time

from controller.notifications import FailureNotificationDeduplicator, notify_job_failure


class TestFailureNotificationDeduplication(unittest.TestCase):

    def setUp(self):
        self.dedup = FailureNotificationDeduplicator(default_cooldown_sec=5.0)

    def test_first_notification_is_dispatched(self):
        job = {"job_id": "test-job-001", "attempt": 1, "target_node": "node-01"}
        res = self.dedup.notify_job_failure(job, "Worker connection timed out", is_terminal=False)

        self.assertTrue(res["sent"])
        self.assertFalse(res["suppressed"])
        self.assertIn("error_hash", res)
        self.assertEqual(job["notifications"]["total_sent"], 1)
        self.assertEqual(job["notifications"]["total_suppressed"], 0)

    def test_exact_attempt_duplicate_is_suppressed(self):
        job = {"job_id": "test-job-002", "attempt": 1, "target_node": "node-01"}
        res1 = self.dedup.notify_job_failure(job, "Script exit code 1", is_terminal=False)
        self.assertTrue(res1["sent"])

        # Second sweep / notification on same attempt and same error
        res2 = self.dedup.notify_job_failure(job, "Script exit code 1", is_terminal=False)
        self.assertFalse(res2["sent"])
        self.assertTrue(res2["suppressed"])
        self.assertEqual(job["notifications"]["total_sent"], 1)
        self.assertEqual(job["notifications"]["total_suppressed"], 1)

    def test_cooldown_suppresses_identical_error_on_next_attempt(self):
        job = {"job_id": "test-job-003", "attempt": 1, "target_node": "node-01"}
        res1 = self.dedup.notify_job_failure(job, "Database lock timeout", is_terminal=False, cooldown_sec=10.0)
        self.assertTrue(res1["sent"])

        # Attempt 2 fails immediately with exact same error
        job["attempt"] = 2
        res2 = self.dedup.notify_job_failure(job, "Database lock timeout", is_terminal=False, cooldown_sec=10.0)
        self.assertFalse(res2["sent"])
        self.assertTrue(res2["suppressed"])

    def test_different_error_triggers_new_notification(self):
        job = {"job_id": "test-job-004", "attempt": 1, "target_node": "node-01"}
        res1 = self.dedup.notify_job_failure(job, "Disk full error", is_terminal=False)
        self.assertTrue(res1["sent"])

        # Attempt 2 fails with completely different reason
        job["attempt"] = 2
        res2 = self.dedup.notify_job_failure(job, "Permission denied accessing /tmp", is_terminal=False)
        self.assertTrue(res2["sent"])
        self.assertNotEqual(res1["error_hash"], res2["error_hash"])
        self.assertEqual(job["notifications"]["total_sent"], 2)

    def test_terminal_failure_is_recorded(self):
        job = {"job_id": "test-job-005", "attempt": 3, "target_node": "node-01"}
        res = self.dedup.notify_job_failure(job, "Max retries exceeded (3)", is_terminal=True)
        self.assertTrue(res["sent"])
        self.assertTrue(res["is_terminal"])
        self.assertEqual(len(job["notifications"]["history"]), 1)
        self.assertTrue(job["notifications"]["history"][0]["is_terminal"])


if __name__ == "__main__":
    unittest.main()
