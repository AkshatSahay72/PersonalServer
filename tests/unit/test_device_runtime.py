"""
Unit Tests for Device Runtime (D1) & Production Isolation
=========================================================
Tests capability registry, participation states, permission boundaries,
resource monitoring, bounded sessions, providers, and production scheduler exclusion.
"""

import time
try:
    import pytest
except ImportError:
    pytest = None
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityState,
)
from agent.device_runtime.participation_manager import (
    ParticipationManager,
    ParticipationPolicy,
    ParticipationState,
)
from agent.device_runtime.permission_manager import (
    PermissionDescriptor,
    PermissionManager,
    PermissionStatus,
)
from agent.device_runtime.session_manager import (
    DeviceSession,
    DeviceSessionManager,
    DeviceSessionState,
)
from agent.device_runtime.resource_monitor import (
    ResourceMonitor,
    SystemResourceMetrics,
)
from agent.device_runtime.providers.compute_provider import ComputeProvider
from agent.device_runtime.providers.storage_provider import StorageProvider
from agent.device_runtime.providers.connectivity_provider import ConnectivityProvider
from agent.device_runtime.providers.power_provider import PowerProvider
from agent.device_runtime.runtime import DeviceRuntime
from scheduler.scheduler import ResourceScheduler


class TestCapabilityRegistry:
    def test_registration_and_lookup(self):
        reg = CapabilityRegistry()
        desc = CapabilityDescriptor(
            capability_id="compute",
            name="Compute Subsystem",
            subsystem="compute",
            state=CapabilityState.READY,
            supported_operations=["GET_CPU_INFO"],
        )
        reg.register(desc)

        assert reg.get("compute") is desc
        assert reg.get_state("compute") == CapabilityState.READY
        assert reg.is_operational("compute") is True
        assert reg.is_authorized("compute") is True

    def test_permission_required_state(self):
        reg = CapabilityRegistry()
        desc = CapabilityDescriptor(
            capability_id="camera",
            name="Camera",
            subsystem="camera",
            state=CapabilityState.PERMISSION_REQUIRED,
            requires_permission=True,
            permission_name="android.permission.CAMERA",
        )
        reg.register(desc)

        assert reg.get_state("camera") == CapabilityState.PERMISSION_REQUIRED
        assert reg.is_operational("camera") is False
        assert reg.is_authorized("camera") is False

    def test_state_transitions(self):
        desc = CapabilityDescriptor(
            capability_id="test_cap",
            name="Test",
            subsystem="test",
            state=CapabilityState.DETECTED,
        )
        assert desc.state == CapabilityState.DETECTED
        desc.update_state(CapabilityState.AVAILABLE)
        assert desc.state == CapabilityState.AVAILABLE
        desc.update_state(CapabilityState.ERROR, error="Driver crashed")
        assert desc.state == CapabilityState.ERROR
        assert desc.error == "Driver crashed"


class TestParticipationManager:
    def test_default_state_off(self):
        pm = ParticipationManager(default_state=ParticipationState.OFF)
        assert pm.state == ParticipationState.OFF
        assert pm.is_paused is False

        # Task admission must be rejected when OFF
        admitted, reason = pm.evaluate_task_admission()
        assert admitted is False
        assert "disabled" in reason.lower() or "off" in reason.lower()

    def test_state_transitions(self):
        pm = ParticipationManager(default_state=ParticipationState.OFF)
        assert pm.set_state(ParticipationState.STANDBY) is True
        assert pm.state == ParticipationState.STANDBY

        assert pm.set_state(ParticipationState.AVAILABLE) is True
        assert pm.state == ParticipationState.AVAILABLE

        # Start session transitions to ACTIVE
        assert pm.register_session_start() is True
        assert pm.state == ParticipationState.ACTIVE

        # Stop session transitions back to AVAILABLE
        pm.register_session_stop()
        assert pm.state == ParticipationState.AVAILABLE

    def test_immediate_pause_override(self):
        pm = ParticipationManager(default_state=ParticipationState.AVAILABLE)
        pm.register_session_start()
        assert pm.state == ParticipationState.ACTIVE

        # Immediate pause halts active state to STANDBY
        pm.pause("Thermal spike detected by user")
        assert pm.is_paused is True
        assert pm.state == ParticipationState.STANDBY

        # Cannot admit new tasks while paused
        admitted, reason = pm.evaluate_task_admission()
        assert admitted is False
        assert "paused" in reason.lower()

        # Resume allows state progression
        pm.resume()
        assert pm.is_paused is False
        assert pm.set_state(ParticipationState.AVAILABLE) is True

    def test_battery_and_thermal_admission_limits(self):
        policy = ParticipationPolicy(
            min_battery_percent=25,
            require_charging_for_heavy_tasks=True,
            max_thermal_celsius=42.0,
            max_cpu_percent=80.0,
        )
        pm = ParticipationManager(default_state=ParticipationState.AVAILABLE, policy=policy)

        # 1. Low battery rejected if discharging
        ok, reason = pm.evaluate_task_admission(battery_percent=15, is_charging=False)
        assert ok is False
        assert "battery" in reason.lower()

        # 2. Low battery admitted if plugged in and not heavy
        ok, _ = pm.evaluate_task_admission(battery_percent=15, is_charging=True, is_heavy=False)
        assert ok is True

        # 3. Heavy task rejected if discharging
        ok, reason = pm.evaluate_task_admission(battery_percent=90, is_charging=False, is_heavy=True)
        assert ok is False
        assert "charging" in reason.lower()

        # 4. Thermal ceiling exceeded
        ok, reason = pm.evaluate_task_admission(thermal_celsius=48.5)
        assert ok is False
        assert "thermal" in reason.lower() or "temperature" in reason.lower()


class TestPermissionManager:
    def test_permission_initialization(self):
        mgr = PermissionManager()
        assert mgr.is_granted("android.permission.CAMERA") is False
        desc = mgr.get("android.permission.CAMERA")
        assert desc is not None
        assert desc.status == PermissionStatus.PROMPT_REQUIRED

    def test_check_capability_permissions(self):
        mgr = PermissionManager()
        # Camera capability requires android.permission.CAMERA
        granted, missing = mgr.check_capability_permissions("camera")
        assert granted is False
        assert "android.permission.CAMERA" in missing

        # Grant permission
        mgr.set_status("android.permission.CAMERA", PermissionStatus.GRANTED)
        granted, missing = mgr.check_capability_permissions("camera")
        assert granted is True
        assert len(missing) == 0


class TestSessionManager:
    def test_session_lifecycle(self):
        cleanup_called = False

        def cleanup_hook(sess):
            nonlocal cleanup_called
            cleanup_called = True

        sm = DeviceSessionManager(sweep_interval_sec=0.1)
        sess = sm.create_session("compute", timeout_sec=10.0, cleanup_callback=cleanup_hook)
        assert sess.state == DeviceSessionState.REQUESTED
        sess.start()
        assert sess.state == DeviceSessionState.ACTIVE
        assert sess.remaining_sec > 0

        sm.close_session(sess.session_id, "Normal completion")
        assert sess.state == DeviceSessionState.CLOSED
        assert cleanup_called is True
        sm.shutdown()

    def test_session_watchdog_timeout(self):
        sm = DeviceSessionManager(sweep_interval_sec=0.05)
        # 0.1s timeout
        sess = sm.create_session("camera", timeout_sec=5.0)
        sess.timeout_sec = 0.05 # override to short duration for test
        sess.start()
        assert sess.state == DeviceSessionState.ACTIVE

        # Sleep to let watchdog trigger
        time.sleep(0.15)
        assert sess.state == DeviceSessionState.TIMED_OUT
        assert "timeout" in (sess.stop_reason or "").lower()
        sm.shutdown()


class TestProviders:
    def test_compute_provider(self):
        p = ComputeProvider()
        desc = p.discover()
        assert desc.capability_id == "compute"
        assert desc.state == CapabilityState.READY

        res = p.execute("GET_CPU_INFO")
        assert res["success"] is True
        assert "cores" in res

    def test_storage_provider(self):
        p = StorageProvider()
        desc = p.discover()
        assert desc.capability_id == "storage"
        assert desc.state == CapabilityState.READY

        res = p.execute("GET_STORAGE_METRICS")
        assert res["success"] is True
        assert "storage_total_gb" in res

    def test_connectivity_provider(self):
        p = ConnectivityProvider()
        desc = p.discover()
        assert desc.capability_id == "connectivity"
        assert desc.state == CapabilityState.READY

        res = p.execute("GET_NETWORK_STATUS")
        assert res["success"] is True

    def test_power_provider_honest_unavailable(self):
        p = PowerProvider()
        # Force non-readable paths
        with patch.object(p, "_check_battery_readable", return_value=False), \
             patch.object(p, "_check_thermals_readable", return_value=False):
            desc = p.discover()
            assert desc.capability_id == "power"
            assert desc.state == CapabilityState.PERMISSION_REQUIRED

            res = p.execute("GET_BATTERY_STATUS")
            assert res["success"] is False
            assert "PERMISSION_REQUIRED" in res["error"]

            # Internal handler direct check
            h_res = p._handle_operation("GET_BATTERY_STATUS", {})
            assert h_res["success"] is False
            assert h_res["status"] == "UNAVAILABLE"
            assert h_res["battery_percent"] is None


class TestDeviceRuntimeCoordinator:
    def test_runtime_admission_guard(self):
        rt = DeviceRuntime(node_id="server-v2334-a531", default_participation=ParticipationState.OFF)

        # Operation rejected when participation is OFF
        res = rt.execute_operation("compute", "GET_CPU_INFO")
        assert res["success"] is False
        assert "disabled" in res["error"].lower() or "off" in res["error"].lower()

        # Set to AVAILABLE -> Operation allowed
        rt.participation.set_state(ParticipationState.AVAILABLE)
        res = rt.execute_operation("compute", "GET_CPU_INFO")
        assert res["success"] is True

        rt.shutdown()

    def test_telemetry_generation(self):
        rt = DeviceRuntime(node_id="server-v2334-a531", default_participation=ParticipationState.STANDBY)
        telem = rt.get_telemetry()
        assert telem["node_id"] == "server-v2334-a531"
        assert telem["participation"]["state"] == "STANDBY"
        assert "compute" in telem["capabilities"]
        assert "storage" in telem["capabilities"]
        assert "system" in telem
        rt.shutdown()


class _FixedSchedulerDateTime(datetime):
    """Deterministic UTC clock pinned 10s after the 2026-11-10T00:00:00Z test fixtures."""

    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 11, 10, 0, 0, 10, tzinfo=timezone.utc)
        return fixed if tz is None else fixed.astimezone(tz)


class TestProductionSchedulerIsolation:
    """
    Mandatory proof that the experimental node CANNOT receive production workloads
    or participate in leader election.
    """

    def test_scheduler_rejects_experimental_worker_role(self):
        node = {
            "node_id": "server-v2334-a531",
            "name": "Vivo V2334 (Experimental)",
            "role": "experimental-worker",
            "status": "ONLINE",
            "last_seen": "2026-11-10T00:00:00Z",
            "last_heartbeat": {"timestamp": "2026-11-10T00:00:00Z"},
            "worker_enabled": False,
            "scheduler_eligible": False,
            "leader_eligible": False,
            "participation_state": "OFF",
            "capabilities": {"compute": True, "storage": True},
        }

        with patch("scheduler.scheduler.datetime", _FixedSchedulerDateTime):
            # 1. Standard production job with default requirements (no role specified)
            eligible, reason = ResourceScheduler.is_node_eligible(
                node=node,
                job_requirements={"requirements": {"cpu": 1, "ram": 512}},
            )
            assert eligible is False
            # Rejection must catch administrative lock or specialized role gate
            assert "ADMINISTRATIVE_LOCK" in reason or "ROLE_MISMATCH" in reason

            # 2. Production job explicitly requiring standard "compute" role
            eligible, reason = ResourceScheduler.is_node_eligible(
                node=node,
                job_requirements={"requirements": {"role": "compute", "cpu": 1, "ram": 512}},
            )
            assert eligible is False
            assert "ADMINISTRATIVE_LOCK" in reason or "ROLE_MISMATCH" in reason

    def test_scheduler_rejects_when_worker_enabled_false_even_if_role_matches(self):
        node = {
            "node_id": "server-v2334-a531",
            "role": "experimental-worker",
            "status": "ONLINE",
            "last_seen": "2026-11-10T00:00:00Z",
            "last_heartbeat": {"timestamp": "2026-11-10T00:00:00Z"},
            "worker_enabled": False,
            "scheduler_eligible": False,
            "participation_state": "OFF",
        }

        with patch("scheduler.scheduler.datetime", _FixedSchedulerDateTime):
            eligible, reason = ResourceScheduler.is_node_eligible(
                node=node,
                job_requirements={"requirements": {"role": "experimental-worker"}},
            )
        assert eligible is False
        assert "ADMINISTRATIVE_LOCK" in reason

    def test_scheduler_rejects_when_participation_state_is_off(self):
        node = {
            "node_id": "server-v2334-a531",
            "role": "experimental-worker",
            "status": "ONLINE",
            "last_seen": "2026-11-10T00:00:00Z",
            "last_heartbeat": {"timestamp": "2026-11-10T00:00:00Z"},
            "worker_enabled": True,
            "scheduler_eligible": True,
            "participation_state": "OFF",
        }

        with patch("scheduler.scheduler.datetime", _FixedSchedulerDateTime):
            eligible, reason = ResourceScheduler.is_node_eligible(
                node=node,
                job_requirements={"requirements": {"role": "experimental-worker"}},
            )
        assert eligible is False
        assert "ADMINISTRATIVE_LOCK" in reason
        assert "participation_state=OFF" in reason
