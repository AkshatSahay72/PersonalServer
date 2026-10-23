#!/usr/bin/env python3
"""
Structured Logging, Correlation Tracing, and Failure Diagnostics Test Suite
===========================================================================
Validates credential and secret redaction, correlation ID extraction and propagation,
structured logging event ring buffer, and useful failure diagnostics for PersonalServer.
"""

import unittest
from pathlib import Path

from config.structured_logger import (
    StructuredLogger,
    redact_sensitive_text,
    redact_sensitive_data,
    generate_correlation_id,
    extract_correlation_id,
    controller_logger
)
from agent.job_executor import JobExecutor, JOB_STATE_FAILED, JOB_STATE_TIMEOUT, JOB_STATE_SUCCEEDED

BASE_DIR = Path(__file__).resolve().parents[2]


class TestLogRedaction(unittest.TestCase):
    """Verify credentials and secrets are stripped and never exposed in logs."""

    def test_redact_sensitive_text_tokens(self):
        sample = "Error: Invalid token: 9f8e7d6c5b4a3a2b1c0d during Bearer a1b2c3d4e5f6g7h8 handshake"
        redacted = redact_sensitive_text(sample)
        self.assertNotIn("9f8e7d6c5b4a3a2b1c0d", redacted)
        self.assertNotIn("a1b2c3d4e5f6g7h8", redacted)
        self.assertIn("token: [REDACTED]", redacted)
        self.assertIn("Bearer [REDACTED]", redacted)

    def test_redact_sensitive_text_passwords_and_secrets(self):
        sample = "Connecting with password=SuperSecretPassword123! and secret: mySuperSecretKey"
        redacted = redact_sensitive_text(sample)
        self.assertNotIn("SuperSecretPassword123!", redacted)
        self.assertNotIn("mySuperSecretKey", redacted)
        self.assertIn("password=[REDACTED]", redacted)
        self.assertIn("secret: [REDACTED]", redacted)

    def test_redact_sensitive_text_auth_tokens(self):
        sample = "Header: x-auth-token: abcdef0123456789 and auth_token='myauthtoken999'"
        redacted = redact_sensitive_text(sample)
        self.assertNotIn("abcdef0123456789", redacted)
        self.assertNotIn("myauthtoken999", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_sensitive_dict_nested(self):
        payload = {
            "job_id": "job-12345",
            "node_id": "node01",
            "auth_token": "super-secret-auth-token-12345",
            "config": {
                "enrollment_token": "enrollment-secret-token-abcdef",
                "database_password": "DatabasePassword999!",
                "safe_field": "hello world",
                "nested_array": [
                    {"api_key": "private-key-abcdef", "count": 42}
                ]
            }
        }
        redacted = redact_sensitive_data(payload)
        # Safe fields preserved
        self.assertEqual(redacted["job_id"], "job-12345")
        self.assertEqual(redacted["node_id"], "node01")
        self.assertEqual(redacted["config"]["safe_field"], "hello world")
        self.assertEqual(redacted["config"]["nested_array"][0]["count"], 42)

        # Sensitive fields redacted
        self.assertIn("[REDACTED]", str(redacted["auth_token"]))
        self.assertIn("[REDACTED]", str(redacted["config"]["enrollment_token"]))
        self.assertIn("[REDACTED]", str(redacted["config"]["database_password"]))
        self.assertIn("[REDACTED]", str(redacted["config"]["nested_array"][0]["api_key"]))

        # Verify raw secrets absent from stringified representation
        raw_dump = str(redacted)
        self.assertNotIn("super-secret-auth-token-12345", raw_dump)
        self.assertNotIn("enrollment-secret-token-abcdef", raw_dump)
        self.assertNotIn("DatabasePassword999!", raw_dump)
        self.assertNotIn("private-key-abcdef", raw_dump)


class TestCorrelationIdTracing(unittest.TestCase):
    """Verify correlation ID extraction, generation, and formatting."""

    def test_generate_correlation_id(self):
        corr1 = generate_correlation_id()
        corr2 = generate_correlation_id()
        self.assertTrue(corr1.startswith("corr-"))
        self.assertTrue(corr2.startswith("corr-"))
        self.assertNotEqual(corr1, corr2)

    def test_extract_correlation_id_from_headers(self):
        headers = {"X-Correlation-ID": "corr-client-test-123"}
        self.assertEqual(extract_correlation_id(headers), "corr-client-test-123")

        headers_req = {"X-Request-ID": "req-custom-456"}
        self.assertEqual(extract_correlation_id(headers_req), "req-custom-456")

    def test_extract_correlation_id_from_body(self):
        body = {"correlation_id": "corr-from-body-789"}
        self.assertEqual(extract_correlation_id({}, body), "corr-from-body-789")

    def test_extract_correlation_id_fallback(self):
        corr = extract_correlation_id(None, None)
        self.assertTrue(corr.startswith("corr-"))


class TestStructuredLogger(unittest.TestCase):
    """Verify in-memory event recording and query filtering."""

    def setUp(self):
        self.logger = StructuredLogger(subsystem="TestSubsystem", max_history=50)

    def test_log_records_event_with_redaction(self):
        entry = self.logger.info(
            "node.registered",
            message="Registered with token: secret-token-xyz987",
            job_id="job-999",
            node_id="node02",
            auth_token="raw-token-12345",
            safe_metric=100
        )

        self.assertEqual(entry["subsystem"], "TestSubsystem")
        self.assertEqual(entry["event"], "node.registered")
        self.assertEqual(entry["level"], "INFO")
        self.assertEqual(entry["job_id"], "job-999")
        self.assertEqual(entry["node_id"], "node02")
        self.assertNotIn("secret-token-xyz987", entry["message"])
        self.assertIn("[REDACTED]", entry["message"])
        self.assertIn("[REDACTED]", str(entry["details"]["auth_token"]))
        self.assertEqual(entry["details"]["safe_metric"], 100)

    def test_get_recent_events_filtering(self):
        corr_target = "corr-special-target"
        self.logger.info("job.scheduled", "Job 1 scheduled", correlation_id="corr-1", job_id="job-1")
        self.logger.error("worker.failed", "Worker failed", correlation_id=corr_target, job_id="job-2")
        self.logger.info("job.succeeded", "Job 3 succeeded", correlation_id="corr-3", job_id="job-3")

        # Filter by level
        errors = self.logger.get_recent_events(level="ERROR")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["event"], "worker.failed")

        # Filter by correlation_id
        corr_events = self.logger.get_recent_events(correlation_id=corr_target)
        self.assertEqual(len(corr_events), 1)
        self.assertEqual(corr_events[0]["job_id"], "job-2")

        # Filter by event name
        sched_events = self.logger.get_recent_events(event="job.scheduled")
        self.assertEqual(len(sched_events), 1)
        self.assertEqual(sched_events[0]["job_id"], "job-1")


class TestJobExecutorFailureDiagnostics(unittest.TestCase):
    """Verify execution results include structured failure diagnostics and scrubbed errors."""

    def test_successful_job_diagnostics(self):
        job = {
            "job_id": "job-test-success",
            "type": "echo",
            "parameters": {"message": "Hello diagnostics"},
            "timeout": 10,
            "correlation_id": "corr-echo-test"
        }
        res = JobExecutor.execute(job)
        self.assertEqual(res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["correlation_id"], "corr-echo-test")
        self.assertIn("diagnostics", res)
        self.assertEqual(res["diagnostics"]["attempt"], 1)
        self.assertIsNone(res["diagnostics"]["failure_reason"])
        self.assertFalse(res["diagnostics"]["is_timeout"])

    def test_failing_job_diagnostics_and_redaction(self):
        # Workload simulating failure that prints sensitive token to stderr
        sensitive_err = "Fatal crash while checking token: Bearer mysecrettoken123456"
        job = {
            "job_id": "job-test-failure",
            "type": "failing-test",
            "parameters": {"error_message": sensitive_err},
            "timeout": 10,
            "attempt": 2,
            "correlation_id": "corr-fail-test"
        }
        res = JobExecutor.execute(job)
        self.assertEqual(res["status"], JOB_STATE_FAILED)
        self.assertNotEqual(res["exit_code"], 0)
        self.assertEqual(res["correlation_id"], "corr-fail-test")
        self.assertEqual(res["attempt"], 2)

        # Ensure credentials scrubbed from stderr and diagnostics
        self.assertNotIn("mysecrettoken123456", res["stderr"])
        self.assertIn("Bearer [REDACTED]", res["stderr"])
        self.assertIn("diagnostics", res)
        self.assertEqual(res["diagnostics"]["attempt"], 2)
        self.assertIn("Bearer [REDACTED]", res["diagnostics"]["failure_reason"])

    def test_timeout_job_diagnostics(self):
        job = {
            "job_id": "job-test-timeout",
            "type": "timeout-test",
            "parameters": {"sleep": 5},
            "timeout": 1,
            "correlation_id": "corr-timeout-test"
        }
        res = JobExecutor.execute(job)
        self.assertEqual(res["status"], JOB_STATE_TIMEOUT)
        self.assertEqual(res["exit_code"], -1)
        self.assertIn("diagnostics", res)
        self.assertTrue(res["diagnostics"]["is_timeout"])
        self.assertIn("timed out", res["diagnostics"]["failure_reason"].lower())


if __name__ == "__main__":
    unittest.main()
