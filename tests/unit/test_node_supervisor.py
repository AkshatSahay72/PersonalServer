"""
Unit tests for Milestone M3: Node Process Supervisor & Autonomous Self-Healing (NODE-006).
Tests detection of stopped/dead processes, automated resurrection, rate limiting, and PID persistence.
"""

import os
import sys
import time
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scripts.node_supervisor import (
    NodeProcessSupervisor,
    is_pid_alive,
    is_port_responding,
    MAX_RESTARTS_PER_WINDOW,
    WINDOW_SECONDS,
    COOLDOWN_SECONDS
)


@pytest.fixture
def temp_supervisor_env(tmp_path):
    """Creates a temporary workspace sandbox for testing the process supervisor."""
    runtime_dir = tmp_path / "runtime"
    logs_dir = tmp_path / "logs"
    config_dir = tmp_path / "config"
    services_dir = tmp_path / "services" / "node-api"
    agent_dir = tmp_path / "agent"

    runtime_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    services_dir.mkdir(parents=True)
    agent_dir.mkdir(parents=True)

    # Create dummy app files
    (services_dir / "app.py").write_text("# dummy app\n")
    (agent_dir / "node-agent.py").write_text("# dummy agent\n")

    supervisor = NodeProcessSupervisor(base_dir=tmp_path)
    return supervisor, tmp_path


class TestNodeProcessSupervisor:
    """Tests the NodeProcessSupervisor self-healing engine."""

    def test_pid_alive_detection(self):
        """Current process PID must be detected as alive; non-existent PID as dead."""
        current_pid = os.getpid()
        assert is_pid_alive(current_pid) is True
        assert is_pid_alive(9999999) is False
        assert is_pid_alive(0) is False
        assert is_pid_alive(-1) is False

    def test_detect_dead_services(self, temp_supervisor_env):
        """When services are not running, inspect_and_heal detects them as dead."""
        supervisor, tmp_path = temp_supervisor_env

        with patch("scripts.node_supervisor.is_port_responding", return_value=False):
            report = supervisor.inspect_and_heal(auto_resurrect=False)
            assert report["all_healthy"] is False
            assert "node-api" in report["services"]
            assert report["services"]["node-api"]["healthy"] is False
            assert report["services"]["node-api"]["action_taken"] == "NONE"

    def test_auto_resurrect_service(self, temp_supervisor_env):
        """When auto_resurrect is enabled, supervisor spawns the dead service and writes PID."""
        supervisor, tmp_path = temp_supervisor_env

        mock_proc = MagicMock()
        mock_proc.pid = 4321

        with patch("scripts.node_supervisor.is_port_responding", return_value=False):
            with patch("subprocess.Popen", return_value=mock_proc):
                report = supervisor.inspect_and_heal(auto_resurrect=True)

                assert "node-api" in report["services"]
                assert report["services"]["node-api"]["action_taken"] == "RESURRECTED"
                assert report["services"]["node-api"]["resurrections"] == 1
                assert supervisor.resurrection_counts["node-api"] == 1

                # Verify PID was recorded on disk
                pid_file = tmp_path / "runtime" / "node-api.pid"
                assert pid_file.exists()
                assert pid_file.read_text().strip() == "4321"

    def test_restart_rate_limiting_anti_churn(self, temp_supervisor_env):
        """Supervisor enforces anti-churn rate limiting when a process repeatedly crashes."""
        supervisor, tmp_path = temp_supervisor_env

        svc_def = {
            "name": "Test Service",
            "cmd": ["echo", "test"],
            "pid_file": tmp_path / "runtime" / "test.pid",
            "log_file": tmp_path / "logs" / "test.log"
        }

        mock_proc = MagicMock()
        mock_proc.pid = 5555

        with patch("subprocess.Popen", return_value=mock_proc):
            # First MAX_RESTARTS_PER_WINDOW restarts should succeed
            for i in range(MAX_RESTARTS_PER_WINDOW):
                ok, msg = supervisor.resurrect_service("flapping-service", svc_def)
                assert ok is True

            # The 6th restart within 60s must be blocked by rate limit cooldown
            ok_blocked, blocked_msg = supervisor.resurrect_service("flapping-service", svc_def)
            assert ok_blocked is False
            assert "Crash rate limit reached" in blocked_msg

            # Immediate next attempt also blocked
            can_restart, reason = supervisor.can_restart("flapping-service")
            assert can_restart is False
            assert "cooldown period" in reason

    def test_healthy_service_skipped(self, temp_supervisor_env):
        """An already running process with live PID must not be re-spawned."""
        supervisor, tmp_path = temp_supervisor_env

        # Write our own PID as node-worker and node-heartbeat
        (tmp_path / "runtime" / "node-worker.pid").write_text(str(os.getpid()) + "\n")
        (tmp_path / "runtime" / "node-heartbeat.pid").write_text(str(os.getpid()) + "\n")
        (tmp_path / "runtime" / "node-api.pid").write_text(str(os.getpid()) + "\n")

        with patch("scripts.node_supervisor.is_port_responding", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                report = supervisor.inspect_and_heal(auto_resurrect=True)
                assert report["services"]["node-worker"]["healthy"] is True
                assert report["services"]["node-worker"]["pid"] == os.getpid()
                assert report["services"]["node-worker"]["action_taken"] == "NONE"
                assert mock_popen.call_count == 0
