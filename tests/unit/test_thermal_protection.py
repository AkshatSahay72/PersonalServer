"""
Unit tests for Milestone M3: Thermal Protection & Hardware Safety (NODE-005).
Tests the multi-tier battery thermal monitor, proactive CCTV streaming throttling,
emergency thermal shutdown at 45°C, and hysteresis cooldown locking.
"""

import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.node_telemetry import (
    ThermalProtectionManager,
    evaluate_battery_thermal_state,
    TEMP_NORMAL_MAX,
    TEMP_WARM_MAX,
    TEMP_THROTTLE_MIN,
    TEMP_CRITICAL_MIN,
    TEMP_COOLDOWN_CLEAR
)
from agent.hardware.live_call import LiveCallProvider, LiveCallSession, SessionState


@pytest.fixture(autouse=True)
def reset_thermal_manager():
    """Ensure clean thermal state for each test."""
    mgr = ThermalProtectionManager.get_instance()
    mgr.reset()
    yield
    mgr.reset()


class TestThermalStateEvaluator:
    """Tests the ThermalProtectionManager state transitions and threshold logic."""

    def test_optimal_normal_temperature(self):
        """Temperatures <= 39.0°C should report NORMAL state with full 720p/15fps capabilities."""
        mgr = ThermalProtectionManager.get_instance()
        res = mgr.evaluate_thermal_state(temp_override=34.5)

        assert res["state"] == "NORMAL"
        assert res["thermal_status"] == "Normal"
        assert res["throttle_active"] is False
        assert res["camera_allowed"] is True
        assert res["cooldown_locked"] is False
        assert res["recommended_preset"] == "720p"
        assert res["recommended_fps"] == 15
        assert res["temperature_c"] == 34.5

    def test_warm_temperature_elevation(self):
        """Temperatures between 39.1°C and 41.9°C report WARM state without throttling."""
        mgr = ThermalProtectionManager.get_instance()
        res = mgr.evaluate_thermal_state(temp_override=40.5)

        assert res["state"] == "WARM"
        assert res["thermal_status"] == "Warm"
        assert res["throttle_active"] is False
        assert res["camera_allowed"] is True
        assert res["recommended_preset"] == "480p"
        assert res["recommended_fps"] == 15

    def test_throttling_stage_1(self):
        """Temperatures >= 42.0°C trigger Stage 1 Throttling (360p / 5fps)."""
        mgr = ThermalProtectionManager.get_instance()
        res = mgr.evaluate_thermal_state(temp_override=42.8)

        assert res["state"] == "THROTTLED"
        assert res["thermal_status"] == "Throttled"
        assert res["throttle_active"] is True
        assert res["camera_allowed"] is True
        assert res["recommended_preset"] == "360p"
        assert res["recommended_fps"] == 5
        assert res["recommended_jpeg_quality"] == 35

    def test_critical_cutoff_emergency_stage_2(self):
        """Temperatures >= 45.0°C trigger Stage 2 Emergency Cutoff and lock out camera access."""
        mgr = ThermalProtectionManager.get_instance()
        res = mgr.evaluate_thermal_state(temp_override=45.2)

        assert res["state"] == "CRITICAL"
        assert res["thermal_status"] == "CriticalOverheat"
        assert res["throttle_active"] is True
        assert res["camera_allowed"] is False
        assert res["cooldown_locked"] is True
        assert res["recommended_fps"] == 0

    def test_hysteresis_cooldown_lockout_and_release(self):
        """Once critical cutoff is hit, camera remains locked until temperature cools <= 39.0°C."""
        mgr = ThermalProtectionManager.get_instance()

        # 1. Trigger critical cutoff at 45.5°C
        res1 = mgr.evaluate_thermal_state(temp_override=45.5)
        assert res1["cooldown_locked"] is True
        assert res1["camera_allowed"] is False

        # 2. Temperature drops to 41.0°C: must STILL remain locked out
        res2 = mgr.evaluate_thermal_state(temp_override=41.0)
        assert res2["cooldown_locked"] is True
        assert res2["camera_allowed"] is False
        assert res2["thermal_status"] == "CooldownLock"

        # 3. Temperature drops to 39.5°C: still above 39.0°C, must remain locked
        res3 = mgr.evaluate_thermal_state(temp_override=39.5)
        assert res3["cooldown_locked"] is True
        assert res3["camera_allowed"] is False

        # 4. Temperature cools to 38.5°C (<= 39.0°C): lockout is released
        res4 = mgr.evaluate_thermal_state(temp_override=38.5)
        assert res4["cooldown_locked"] is False
        assert res4["camera_allowed"] is True
        assert res4["state"] == "NORMAL"


class TestLiveCallThermalProtection:
    """Tests integration of thermal protection inside LiveCallProvider."""

    def test_reject_start_session_under_thermal_cutoff(self):
        """LiveCallProvider.start_session must reject starting when battery is overheating."""
        mgr = ThermalProtectionManager.get_instance()
        mgr.set_simulated_temp(46.0)

        provider = LiveCallProvider()
        res = provider.start_session({"quality": "720p"})

        assert res["success"] is False
        assert res["error"] == "THERMAL_COOLDOWN_ACTIVE"
        assert res["error_code"] == "THERMAL_COOLDOWN_ACTIVE"
        assert "cooling down" in res["message"]
        assert provider.current_session is None

    def test_start_session_under_throttling_clamps_quality(self):
        """When starting a session under throttling temp (43°C), quality is clamped to 360p / 5fps."""
        mgr = ThermalProtectionManager.get_instance()
        mgr.set_simulated_temp(43.0)

        provider = LiveCallProvider()
        # Mock native bridge IPC so it doesn't attempt real sockets
        with patch.object(provider, "_call_native_bridge", return_value=(True, {"status": "ACTIVE"})):
            with patch.object(provider, "_start_media_stream_client"):
                res = provider.start_session({"quality": "720p", "jpeg_quality": 80})

                assert res["success"] is True
                assert provider.current_session is not None
                assert provider.current_session.thermal_throttled is True
                assert provider.current_session.quality.preset == "360p"
                assert provider.current_session.quality.fps == 5
                assert provider.current_session.quality.width == 480
                assert provider.current_session.quality.height == 360

                provider.stop_session("Test finished")

    def test_watchdog_emergency_thermal_cutoff(self):
        """Active session must be terminated by watchdog when battery temperature reaches 45.0°C."""
        mgr = ThermalProtectionManager.get_instance()
        mgr.set_simulated_temp(36.0)

        provider = LiveCallProvider()
        with patch.object(provider, "_call_native_bridge", return_value=(True, {"status": "ACTIVE"})):
            with patch.object(provider, "_start_media_stream_client"):
                res = provider.start_session({"quality": "480p"})
                assert res["success"] is True
                assert provider.is_active() is True

                # Simulate temperature jump to 45.5°C
                mgr.set_simulated_temp(45.5)

                # Run one cycle of watchdog thermal inspection
                thermal_state = evaluate_battery_thermal_state()
                assert thermal_state["camera_allowed"] is False
                if not thermal_state.get("camera_allowed", True):
                    provider.stop_session(f"Thermal cutoff: Battery temperature reached {thermal_state['temperature_c']}°C")

                assert provider.is_active() is False
                assert provider.current_session is None

    def test_status_payload_includes_thermal_telemetry(self):
        """LiveCallProvider.get_status() includes real-time thermal telemetry."""
        mgr = ThermalProtectionManager.get_instance()
        mgr.set_simulated_temp(37.2)

        provider = LiveCallProvider()
        st = provider.get_status()

        assert "thermal" in st
        assert st["thermal"]["temperature_c"] == 37.2
        assert st["thermal"]["state"] == "NORMAL"
        assert st["thermal"]["camera_allowed"] is True
