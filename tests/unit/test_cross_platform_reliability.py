#!/usr/bin/env python3
"""
Test Suite for Cross-Platform Reliability and Test Runner Consistency
====================================================================
Validates:
1. is_pid_alive behaves consistently across Windows and POSIX without requiring external ps.
2. Hardware probing handles Win32, Linux /proc, and missing sysfs gracefully without unhandled exceptions.
3. Test runner suite accounting: verifies that all test_*.py files in tests/ are accounted for
   in scripts/run_tests.py (UNIT_TEST_FILES + INTEGRATION_TEST_FILES).
4. Unrooted / non-Docker capability safety: ensures JobExecutor returns explicit, graceful
   rejection structures when Docker daemon is unavailable, rather than crashing.
"""

import os
import sys
import glob
import ast
import unittest
from unittest.mock import patch, MagicMock
import importlib.util
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "agent"))

import agent.job_executor as job_executor
from agent.job_executor import JobExecutor, JOB_STATE_FAILED
from agent.local_worker import probe_hardware_resources


def _load_node_agent():
    spec = importlib.util.spec_from_file_location("node_agent_mod", str(BASE_DIR / "agent" / "node-agent.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

NODE_AGENT = _load_node_agent()


class TestCrossPlatformProcessHandling(unittest.TestCase):
    """Verifies process liveness detection across operating systems with pure mocks."""

    def test_is_pid_alive_invalid_inputs(self):
        """Invalid PID inputs must safely return False without invoking OS calls."""
        self.assertFalse(NODE_AGENT.is_pid_alive(None))
        self.assertFalse(NODE_AGENT.is_pid_alive(""))
        self.assertFalse(NODE_AGENT.is_pid_alive("   "))
        self.assertFalse(NODE_AGENT.is_pid_alive("not-a-number"))
        self.assertFalse(NODE_AGENT.is_pid_alive("-9999"))
        self.assertFalse(NODE_AGENT.is_pid_alive("0"))
        self.assertFalse(NODE_AGENT.is_pid_alive("-1"))

    def test_windows_never_calls_os_kill(self):
        """Verify that on Windows, is_pid_alive never invokes os.kill under any input."""
        with patch.object(NODE_AGENT.os, "kill") as mock_kill:
            with patch.object(NODE_AGENT, "sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.object(NODE_AGENT, "_is_pid_alive_windows", return_value=True) as mock_win:
                    res = NODE_AGENT.is_pid_alive("12345")
                    self.assertTrue(res)
                    mock_win.assert_called_once_with(12345, pattern=None)
                    mock_kill.assert_not_called()

    def test_windows_active_process_lifecycle(self):
        """Active process on Windows opens handle, verifies state, and closes handle."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 4242  # valid mock handle
        mock_kernel32.WaitForSingleObject.return_value = 0x102  # WAIT_TIMEOUT (still running)

        def mock_get_exit_code(handle, byref_var):
            if hasattr(byref_var, "_obj"):
                byref_var._obj.value = 259  # STILL_ACTIVE
            return 1
        mock_kernel32.GetExitCodeProcess.side_effect = mock_get_exit_code

        res = NODE_AGENT._is_pid_alive_windows(12345, kernel32=mock_kernel32)
        self.assertTrue(res)
        mock_kernel32.OpenProcess.assert_called_once()
        mock_kernel32.WaitForSingleObject.assert_called_once_with(4242, 0)
        mock_kernel32.CloseHandle.assert_called_once_with(4242)

    def test_windows_64bit_handle_lifecycle(self):
        """Verify 64-bit Windows process handles (> 32-bit integer) are handled without truncation."""
        handle_64bit = 0x1FFFFFFFF  # Value requiring 64 bits (8589934591)
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = handle_64bit
        mock_kernel32.WaitForSingleObject.return_value = 0x102  # WAIT_TIMEOUT (running)

        def mock_get_exit_code(handle, byref_var):
            if hasattr(byref_var, "_obj"):
                byref_var._obj.value = 259
            return 1
        mock_kernel32.GetExitCodeProcess.side_effect = mock_get_exit_code

        res = NODE_AGENT._is_pid_alive_windows(12345, kernel32=mock_kernel32)
        self.assertTrue(res)
        mock_kernel32.OpenProcess.assert_called_once()
        mock_kernel32.WaitForSingleObject.assert_called_once_with(handle_64bit, 0)
        mock_kernel32.CloseHandle.assert_called_once_with(handle_64bit)

    def test_windows_handle_closure_on_exception(self):
        """Verify process handle is closed in finally block even if an exception occurs during checks."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 4242
        mock_kernel32.WaitForSingleObject.side_effect = RuntimeError("Simulated internal error")

        with self.assertRaises(RuntimeError):
            NODE_AGENT._is_pid_alive_windows(12345, kernel32=mock_kernel32)

        mock_kernel32.CloseHandle.assert_called_once_with(4242)

    def test_windows_exited_process_closure(self):
        """Exited/terminated process on Windows reports False and still closes handle."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 4242
        mock_kernel32.WaitForSingleObject.return_value = 0  # WAIT_OBJECT_0 (process signaled/exited)

        res = NODE_AGENT._is_pid_alive_windows(12345, kernel32=mock_kernel32)
        self.assertFalse(res)
        mock_kernel32.CloseHandle.assert_called_once_with(4242)

    def test_windows_nonexistent_process(self):
        """Nonexistent PID where OpenProcess returns NULL with error code returns False."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 0
        mock_kernel32.GetLastError.return_value = 87  # ERROR_INVALID_PARAMETER

        res = NODE_AGENT._is_pid_alive_windows(999999999, kernel32=mock_kernel32)
        self.assertFalse(res)
        mock_kernel32.CloseHandle.assert_not_called()

    def test_windows_access_denied_handling(self):
        """When OpenProcess fails with ERROR_ACCESS_DENIED (PID exists but permission denied)."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 0
        mock_kernel32.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED

        with patch("ctypes.get_last_error", return_value=5, create=True):
            # Without pattern, process is recognized as existing
            self.assertTrue(NODE_AGENT._is_pid_alive_windows(100, kernel32=mock_kernel32))
            # With pattern, cannot verify process details -> returns False
            self.assertFalse(NODE_AGENT._is_pid_alive_windows(100, pattern="python", kernel32=mock_kernel32))

    def test_windows_failed_api_call_handling(self):
        """When WaitForSingleObject fails (WAIT_FAILED 0xFFFFFFFF), returns False and closes handle."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 4242
        mock_kernel32.WaitForSingleObject.return_value = 0xFFFFFFFF  # WAIT_FAILED

        res = NODE_AGENT._is_pid_alive_windows(12345, kernel32=mock_kernel32)
        self.assertFalse(res)
        mock_kernel32.CloseHandle.assert_called_once_with(4242)

    def test_windows_pattern_matching_verification(self):
        """Pattern matching verifies against image path and command line, rejecting non-matches."""
        mock_handle = 4242
        mock_kernel32 = MagicMock()

        def mock_query_image(h, flags, buf, size_ref):
            buf.value = r"C:\PersonalServer\cloudflared.exe"
            return 1
        mock_kernel32.QueryFullProcessImageNameW.side_effect = mock_query_image

        # Matches in image path
        self.assertTrue(NODE_AGENT._verify_windows_process_pattern(mock_handle, 12345, "cloudflared", kernel32=mock_kernel32))
        self.assertTrue(NODE_AGENT._verify_windows_process_pattern(mock_handle, 12345, "cloudflared.exe", kernel32=mock_kernel32))

        # Command-line check with slash normalization
        with patch.object(NODE_AGENT, "run_cmd", return_value="C:\\python.exe services\\node-api\\app.py"):
            self.assertTrue(NODE_AGENT._verify_windows_process_pattern(mock_handle, 12345, "services/node-api/app.py", kernel32=mock_kernel32))

        with patch.object(NODE_AGENT, "run_cmd", return_value=""):
            self.assertFalse(NODE_AGENT._verify_windows_process_pattern(mock_handle, 12345, "unrelated-service", kernel32=mock_kernel32))

    def test_windows_python_executable_alone_does_not_match_script(self):
        """A host python.exe image must not report a match for a script pattern unless command line confirms."""
        mock_handle = 4242
        mock_kernel32 = MagicMock()

        def mock_query_image(h, flags, buf, size_ref):
            buf.value = r"C:\Python\Python313\python.exe"
            return 1
        mock_kernel32.QueryFullProcessImageNameW.side_effect = mock_query_image

        # When command line runs an unrelated script, it must return False
        with patch.object(NODE_AGENT, "run_cmd", return_value="python.exe worker/run_job.py"):
            res = NODE_AGENT._verify_windows_process_pattern(
                mock_handle, 12345, "services/node-api/app.py", kernel32=mock_kernel32
            )
            self.assertFalse(res)

        # When command line contains the script, it must return True
        with patch.object(NODE_AGENT, "run_cmd", return_value="python.exe services\\node-api\\app.py"):
            res = NODE_AGENT._verify_windows_process_pattern(
                mock_handle, 12345, "services/node-api/app.py", kernel32=mock_kernel32
            )
            self.assertTrue(res)

    def test_windows_kernel32_declarations(self):
        """On Windows, verify _get_kernel32 sets explicit 64-bit argument and return types."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        k32 = NODE_AGENT._get_kernel32()
        self.assertIsNotNone(k32)
        self.assertEqual(k32.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(k32.WaitForSingleObject.restype, wintypes.DWORD)
        self.assertEqual(k32.GetExitCodeProcess.restype, wintypes.BOOL)
        self.assertEqual(k32.CloseHandle.restype, wintypes.BOOL)

    def test_posix_pid_liveness_mocked(self):
        """Verify POSIX branch behavior with pure mocks."""
        with patch.object(NODE_AGENT, "sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch.object(NODE_AGENT.os, "kill") as mock_kill:
                # Active process
                mock_kill.return_value = None
                self.assertTrue(NODE_AGENT.is_pid_alive("54321"))
                mock_kill.assert_called_once_with(54321, 0)

            with patch.object(NODE_AGENT.os, "kill") as mock_kill:
                # Dead process
                mock_kill.side_effect = OSError(3, "No such process")
                self.assertFalse(NODE_AGENT.is_pid_alive("54321"))

            with patch.object(NODE_AGENT.os, "kill") as mock_kill:
                # With pattern matching
                mock_kill.return_value = None
                with patch.object(NODE_AGENT, "run_cmd", return_value="python3 services/node-api/app.py"):
                    self.assertTrue(NODE_AGENT.is_pid_alive("54321", pattern="node-api"))
                    self.assertFalse(NODE_AGENT.is_pid_alive("54321", pattern="unrelated"))


class TestHardwareProbingSafety(unittest.TestCase):
    """Verifies that hardware resource probing never crashes on restricted or unusual platforms."""

    def test_local_worker_probing_safe(self):
        """probe_hardware_resources must return positive integer CPU, RAM, and storage values."""
        info = probe_hardware_resources(BASE_DIR)
        self.assertIsInstance(info, dict)
        self.assertGreater(info["cpu_cores"], 0)
        self.assertGreater(info["ram_mb"], 0)
        self.assertGreater(info["storage_gb"], 0)

    def test_node_agent_probing_safe(self):
        """discover_hardware in node-agent must populate all telemetry fields safely."""
        orig_docker = NODE_AGENT.is_docker_available
        orig_udocker = NODE_AGENT.is_udocker_available
        try:
            NODE_AGENT.is_docker_available = lambda: False
            NODE_AGENT.is_udocker_available = lambda: False
            hw = NODE_AGENT.discover_hardware(BASE_DIR)
        finally:
            NODE_AGENT.is_docker_available = orig_docker
            NODE_AGENT.is_udocker_available = orig_udocker

        self.assertIn("name", hw)
        self.assertIn("role", hw)
        self.assertIn("platform", hw)
        self.assertIn("os", hw)
        self.assertIn("architecture", hw)
        self.assertIn("capabilities", hw)
        self.assertTrue(hw["capabilities"]["compute"])
        self.assertTrue(hw["capabilities"]["storage"])
        self.assertTrue(hw["capabilities"]["network"])


class TestRunnerCoverageConsistency(unittest.TestCase):
    """Ensures test suites in tests/ remain 100% accounted for in scripts/run_tests.py."""

    def test_all_test_files_accounted_for(self):
        """All test_*.py files in tests/ must be registered in run_tests.py."""
        all_test_files = {
            os.path.normpath(p)
            for p in glob.glob(str(BASE_DIR / "tests" / "**" / "test_*.py"), recursive=True)
        }

        # Parse scripts/run_tests.py AST
        run_tests_path = BASE_DIR / "scripts" / "run_tests.py"
        with open(run_tests_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        registered = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("UNIT_TEST_FILES", "INTEGRATION_TEST_FILES"):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant):
                                registered.add(os.path.normpath(str(BASE_DIR / elt.value)))

        unregistered = all_test_files - registered
        self.assertEqual(
            unregistered,
            set(),
            f"Found test files in tests/ not registered in scripts/run_tests.py: {unregistered}"
        )


class TestNonDockerGracefulRejection(unittest.TestCase):
    """Ensures that nodes without Docker report clear, understandable capability errors."""

    def test_docker_deploy_rejection_without_daemon(self):
        """When container runtime is unavailable, deploy returns explicit failure status."""
        original_detect = job_executor.detect_container_runtime
        try:
            # Force container runtime detection to report None (e.g. unrooted Android)
            job_executor.detect_container_runtime = lambda: None

            executor = JobExecutor()
            job_payload = {
                "id": "test-job-no-docker-01",
                "type": "docker-deploy",
                "parameters": {"app_name": "test-app", "image": "nginx:alpine"}
            }
            result = executor.execute(job_payload)
            self.assertEqual(result["status"], JOB_STATE_FAILED)
            self.assertIn("Neither Docker daemon nor udocker is available", result["stderr"])
        finally:
            job_executor.detect_container_runtime = original_detect

    def test_execute_script_missing_bash_handles_gracefully(self):
        """When bash is not found on Windows/minimal environments, execute_script returns code 1 with error message."""
        import pathlib
        from unittest.mock import patch, MagicMock
        mock_path = MagicMock(spec=pathlib.Path)
        mock_path.exists.return_value = True

        import io
        with patch("subprocess.run", side_effect=FileNotFoundError("bash not found")), patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            res = NODE_AGENT.execute_script(mock_path)
            self.assertEqual(res, 1)
            self.assertIn("executable not found", mock_err.getvalue())


if __name__ == "__main__":
    unittest.main()
