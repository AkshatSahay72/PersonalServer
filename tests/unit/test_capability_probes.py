#!/usr/bin/env python3
"""
Unit tests for Worker-Side Capability Verification Probes (V1.4)
================================================================
Verifies:
1. Python execution probe (success, missing binary, timeout, non-zero exit).
2. Docker probe (success, missing binary, daemon down, timeout).
3. udocker probe (success, missing binary, non-zero exit).
4. Strict separation of advertised capabilities vs. verified capabilities.
5. Error sanitization, secret redaction, and bounded error lengths.
6. Integration with LocalWorker and SimulatedWorker reporting.
7. Fault tolerance (probe failures never crash the worker agent).
"""

import os
import sys
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agent.capability_prober import (
    _clean_error,
    probe_docker,
    probe_python,
    probe_udocker,
    run_capability_probes,
)
from agent.local_worker import LocalWorker
from agent.simulated_worker import SimulatedWorker


class TestCapabilityProber(unittest.TestCase):
    """Tests for capability verification probes."""

    # -------------------------------------------------------------------------
    # 1. Python Execution Probes
    # -------------------------------------------------------------------------

    def test_01_probe_python_live_success(self):
        """probe_python on current interpreter successfully verifies Python execution."""
        res = probe_python(sys.executable, timeout=5.0)
        self.assertTrue(res["verified"])
        self.assertEqual(res["capability"], "python3")
        self.assertTrue(res["installed"])
        self.assertIsNotNone(res["version"])
        self.assertIn(f"{sys.version_info[0]}.{sys.version_info[1]}", res["version"])
        self.assertIsNone(res["error"])
        self.assertIsNotNone(res["timestamp"])

    def test_02_probe_python_missing_binary(self):
        """probe_python reports clean failure when binary is missing."""
        res = probe_python("/path/to/nonexistent/python_bin_xyz_999", timeout=2.0)
        self.assertFalse(res["verified"])
        self.assertEqual(res["capability"], "python3")
        self.assertFalse(res["installed"])
        self.assertIsNone(res["version"])
        self.assertIn("not found", res["error"])

    def test_03_probe_python_timeout(self):
        """probe_python handles timeout without raising exception."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1.5)):
            res = probe_python(sys.executable, timeout=1.5)
            self.assertFalse(res["verified"])
            self.assertIn("timed out after 1.5s", res["error"])

    def test_04_probe_python_nonzero_exit(self):
        """probe_python captures non-zero exit code and stderr cleanly."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "ImportError: No module named critical_lib"
        with patch("subprocess.run", return_value=mock_proc):
            res = probe_python(sys.executable, timeout=3.0)
            self.assertFalse(res["verified"])
            self.assertIn("critical_lib", res["error"])

    # -------------------------------------------------------------------------
    # 2. Docker Execution Probes
    # -------------------------------------------------------------------------

    def test_05_probe_docker_success(self):
        """probe_docker returns verified: True when docker info succeeds."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "24.0.7\n"
        mock_proc.stderr = ""

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", return_value=mock_proc):
                res = probe_docker(timeout=3.0)
                self.assertTrue(res["verified"])
                self.assertEqual(res["capability"], "container_runtime:docker")
                self.assertTrue(res["installed"])
                self.assertEqual(res["version"], "24.0.7")
                self.assertIsNone(res["error"])

    def test_06_probe_docker_missing_executable(self):
        """probe_docker returns verified: False and installed: False when docker is absent."""
        with patch("shutil.which", return_value=None):
            res = probe_docker(timeout=3.0)
            self.assertFalse(res["verified"])
            self.assertFalse(res["installed"])
            self.assertIn("not found on PATH", res["error"])

    def test_07_probe_docker_daemon_down(self):
        """probe_docker detects daemon connection failure (binary exists but daemon stopped)."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", return_value=mock_proc):
                res = probe_docker(timeout=3.0)
                self.assertFalse(res["verified"])
                self.assertTrue(res["installed"])
                self.assertIn("Docker daemon unreachable", res["error"])
                self.assertIn("Cannot connect to the Docker daemon", res["error"])

    def test_08_probe_docker_timeout(self):
        """probe_docker handles hanging daemon socket via timeout."""
        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=2.0)):
                res = probe_docker(timeout=2.0)
                self.assertFalse(res["verified"])
                self.assertIn("timed out after 2.0s", res["error"])

    # -------------------------------------------------------------------------
    # 3. udocker Execution Probes
    # -------------------------------------------------------------------------

    def test_09_probe_udocker_success(self):
        """probe_udocker returns verified: True when udocker version succeeds."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "udocker 1.3.16\nPython 3.10.12\n"
        mock_proc.stderr = ""

        with patch("shutil.which", return_value="/usr/local/bin/udocker"):
            with patch("subprocess.run", return_value=mock_proc):
                res = probe_udocker(timeout=3.0)
                self.assertTrue(res["verified"])
                self.assertEqual(res["capability"], "container_runtime:udocker")
                self.assertTrue(res["installed"])
                self.assertIn("1.3.16", res["version"])
                self.assertIsNone(res["error"])

    def test_10_probe_udocker_failure(self):
        """probe_udocker returns verified: False when udocker fails."""
        mock_proc = MagicMock()
        mock_proc.returncode = 127
        mock_proc.stdout = ""
        mock_proc.stderr = "udocker: fatal: python proot engine crashed"

        with patch("shutil.which", return_value="/usr/local/bin/udocker"):
            with patch("subprocess.run", return_value=mock_proc):
                res = probe_udocker(timeout=3.0)
                self.assertFalse(res["verified"])
                self.assertIn("fatal: python proot engine crashed", res["error"])

    # -------------------------------------------------------------------------
    # 4. Advertised vs. Verified Capability Separation
    # -------------------------------------------------------------------------

    def test_11_run_capability_probes_preserves_advertised_while_enforcing_verified(self):
        """
        run_capability_probes keeps advertised capabilities intact, but only
        includes probe-passing capabilities in verified_capabilities.
        """
        # Node advertises Docker and a custom feature, but Docker daemon is down
        advertised = {
            "compute": True,
            "python3": True,
            "container_runtime:docker": True,
            "custom_experimental_feature": True
        }

        # Mock: Python passes, Docker fails
        py_mock = MagicMock(returncode=0, stdout="PersonalServer-Probe:3.11.0", stderr="")
        docker_mock = MagicMock(returncode=1, stdout="", stderr="docker.sock connection refused")

        def mock_run(cmd, *args, **kwargs):
            if "docker" in cmd[0]:
                return docker_mock
            return py_mock

        with patch("shutil.which", side_effect=lambda x: "/usr/bin/docker" if x == "docker" else None):
            with patch("subprocess.run", side_effect=mock_run):
                probe_res = run_capability_probes(advertised, timeout=3.0)

        adv = probe_res["advertised_capabilities"]
        ver = probe_res["verified_capabilities"]
        results = probe_res["probe_results"]

        # Advertised includes everything declared
        self.assertTrue(adv["container_runtime:docker"])
        self.assertTrue(adv["custom_experimental_feature"])
        self.assertTrue(adv["python3"])

        # Verified includes Python and Compute, but NOT Docker or unverified custom feature!
        self.assertTrue(ver["compute"])
        self.assertTrue(ver["python3"])
        self.assertNotIn("container_runtime:docker", ver)
        self.assertNotIn("custom_experimental_feature", ver)

        # Probe results contain exact failure reason for Docker
        self.assertIn("container_runtime:docker", results)
        self.assertFalse(results["container_runtime:docker"]["verified"])
        self.assertIn("connection refused", results["container_runtime:docker"]["error"])

    # -------------------------------------------------------------------------
    # 5. Error Sanitization and Token Redaction
    # -------------------------------------------------------------------------

    def test_12_clean_error_redacts_tokens_and_bounds_length(self):
        """_clean_error redacts tokens and truncates excessive output to under 160 chars."""
        secret_err = "Auth failed for Bearer secret_enrollment_token_1234567890abcdef! " + ("A" * 300)
        cleaned = _clean_error(secret_err)
        self.assertNotIn("secret_enrollment_token_1234567890abcdef", cleaned)
        self.assertIn("[REDACTED]", cleaned)
        self.assertLessEqual(len(cleaned), 163)  # 160 + "..."

    # -------------------------------------------------------------------------
    # 6. LocalWorker & SimulatedWorker Integration
    # -------------------------------------------------------------------------

    def test_13_local_worker_runs_probes_and_populates_metadata(self):
        """LocalWorker populates advertised_capabilities, verified_capabilities, and probe_results."""
        worker = LocalWorker(controller_url="http://127.0.0.1:8000")
        meta = worker.get_node_metadata()

        self.assertIn("capabilities", meta)
        self.assertIn("advertised_capabilities", meta)
        self.assertIn("verified_capabilities", meta)
        self.assertIn("probe_results", meta)

        # Python should be verified on the host running the test
        self.assertTrue(meta["verified_capabilities"].get("python3"))
        self.assertIn("python3", meta["probe_results"])
        self.assertTrue(meta["probe_results"]["python3"]["verified"])

    def test_14_simulated_worker_probe_reporting(self):
        """SimulatedWorker includes probe_results in get_metadata() and heartbeat payload."""
        sim = SimulatedWorker(
            node_id="worker-probe-test",
            controller_url="http://127.0.0.1:8000",
            advertised_capabilities={"compute": True, "gpu": True},
            verified_capabilities={"compute": True},
            probe_results={"gpu": {"verified": False, "error": "CUDA driver not found"}}
        )
        meta = sim.get_metadata()
        self.assertEqual(meta["advertised_capabilities"]["gpu"], True)
        self.assertNotIn("gpu", meta["verified_capabilities"])
        self.assertIn("gpu", meta["probe_results"])
        self.assertEqual(meta["probe_results"]["gpu"]["error"], "CUDA driver not found")


if __name__ == "__main__":
    unittest.main()
