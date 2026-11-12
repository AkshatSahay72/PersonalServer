#!/usr/bin/env python3
"""
Phase 6 Physical Validation on Vivo V2334 — D3 Native Android Sensor Runtime
============================================================================
Executes against real Android hardware on Vivo V2334 (server-v2334-a531).
Never fabricates or substitutes readings.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import SENSOR_MIN_INTERVAL_MS, SensorType
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.participation_manager import ParticipationState
from agent.device_runtime.runtime import DeviceRuntime
from agent.device_runtime.session_manager import DeviceSessionState


def check_lingering_termux_sensor_procs() -> list:
    try:
        res = subprocess.run(["pgrep", "-af", "termux-sensor"], capture_output=True, text=True, timeout=2.0)
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip() and "pgrep" not in ln]
        return lines
    except Exception:
        return []


def main():
    print("=" * 72)
    print(" D3 Physical Validation — Native Android Sensor Runtime on Vivo V2334")
    print("=" * 72)

    NativeAndroidBridge.reset_instance()
    rt = DeviceRuntime(node_id="server-v2334-a531", default_participation=ParticipationState.AVAILABLE)
    bridge = NativeAndroidBridge.get_instance()

    # 1. Native bridge connectivity
    print("\n[1] Native Bridge Connectivity")
    print(f"  State: {bridge.state.value}, Ready: {bridge.is_ready()}, Transport: {bridge.transport.transport_type}")
    assert bridge.is_ready(), "Bridge is not ready on V2334"

    # 2 & 3. Sensor discovery without activation & available physical sensors
    print("\n[2-3] Sensor Discovery (Read-only, No Active Sessions)")
    active_before = rt.sessions.list_active()
    assert len(active_before) == 0, "Discovery must not activate sessions"
    for st in SensorType:
        cap_id = f"sensor.{st.value}"
        desc = rt.registry.get(cap_id)
        assert desc is not None, f"Capability {cap_id} not registered in CapabilityRegistry"
        details = desc.details or {}
        print(
            f"  {cap_id:22s} | state={desc.state.value:10s} | avail={details.get('available')} "
            f"| hw_name={details.get('name')} | units={details.get('units')} | src={details.get('source')}"
        )
        assert desc.state.value != "ACTIVE", f"{cap_id} must not be ACTIVE right after discovery"

    # 4-7. Live Hardware Readings for each of the 5 sensor categories + [9] Session Start & Stop
    print("\n[4-7 & 9] Live Hardware Sensor Acquisition (Start -> Read -> Stop)")
    for st in SensorType:
        cap_id = f"sensor.{st.value}"
        desc = rt.registry.get(cap_id)
        if not (desc and desc.details.get("available")):
            print(f"  {st.value}: UNAVAILABLE on hardware (skipped)")
            continue

        started = rt.start_session(
            cap_id,
            parameters={"sampling_interval_ms": 200, "max_duration_sec": 15.0},
            timeout_sec=15.0,
        )
        assert started.get("success") is True, f"Failed to start {cap_id}: {started}"
        sid = started["session"]["session_id"]
        assert rt.registry.get_state(cap_id).value == "ACTIVE", f"{cap_id} state should be ACTIVE"

        read_res = rt.read_sensor_session(sid, max_samples=3, timeout=5.0)
        assert read_res.get("success") is True, f"Read failed for {cap_id}: {read_res}"
        readings = read_res.get("readings", [])
        assert len(readings) >= 1, f"Expected >=1 live reading for {cap_id}, got 0"
        sample = readings[0]
        if st in (SensorType.ACCELEROMETER, SensorType.GYROSCOPE, SensorType.MAGNETOMETER):
            val_str = f"x={sample.get('x'):.4f}, y={sample.get('y'):.4f}, z={sample.get('z'):.4f} {sample.get('units')}"
        else:
            val_str = f"value={sample.get('value')} {sample.get('units')}"
        print(f"  {st.value:15s} | session={sid} | samples={len(readings)} | {val_str}")

        stopped = rt.stop_session(sid, reason="Phase 6 reading verification complete")
        assert stopped.get("success") is True
        assert rt.registry.get_state(cap_id).value == "READY", f"{cap_id} did not return to READY"

    # 8. Sampling interval enforcement
    print("\n[8] Sampling Interval Enforcement")
    bad_rate = rt.start_session("sensor.accelerometer", parameters={"sampling_interval_ms": -5}, timeout_sec=10.0)
    assert bad_rate.get("success") is False
    print(f"  Negative rate rejected: {bad_rate.get('error')}")

    clamped_start = rt.start_session("sensor.accelerometer", parameters={"sampling_interval_ms": 5}, timeout_sec=10.0)
    assert clamped_start.get("success") is True
    clamped_sid = clamped_start["session"]["session_id"]
    extra = clamped_start["session"]["extra_data"]
    print(
        f"  Requested=5ms -> Actual={extra['actual_interval_ms']}ms "
        f"(floor={SENSOR_MIN_INTERVAL_MS}ms, rate_clamped={extra['rate_clamped']})"
    )
    assert extra["actual_interval_ms"] == SENSOR_MIN_INTERVAL_MS
    assert extra["rate_clamped"] is True
    rt.stop_session(clamped_sid, reason="Rate test complete")

    # 10. Cleanup after interruption / watchdog expiration
    print("\n[10] Interrupt / Watchdog Cleanup Verification")
    int_start = rt.start_session("sensor.accelerometer", parameters={"sampling_interval_ms": 200}, timeout_sec=10.0)
    assert int_start.get("success") is True
    int_sid = int_start["session"]["session_id"]
    time.sleep(1.2)
    dsess = rt.sessions.get_session(int_sid)
    dsess.mark_timed_out()
    assert dsess.state == DeviceSessionState.TIMED_OUT
    assert rt.registry.get_state("sensor.accelerometer").value == "READY"
    time.sleep(0.5)
    lingering = check_lingering_termux_sensor_procs()
    print(f"  Watchdog expired session {int_sid} -> lingering termux-sensor procs: {len(lingering)}")
    assert len(lingering) == 0, f"Found lingering processes after cleanup: {lingering}"

    # 11. Native service restart recovery
    print("\n[11] Bridge Shutdown & Restart Recovery")
    bridge.shutdown()
    assert bridge.state.value == "UNINITIALIZED"
    assert bridge.initialize() is True
    assert bridge.is_ready() is True
    rec_start = rt.start_session("sensor.accelerometer", parameters={"sampling_interval_ms": 200}, timeout_sec=10.0)
    assert rec_start.get("success") is True
    rec_read = rt.read_sensor_session(rec_start["session"]["session_id"], max_samples=2, timeout=5.0)
    assert rec_read.get("success") is True and len(rec_read.get("readings", [])) >= 1
    rt.stop_session(rec_start["session"]["session_id"], reason="Restart recovery verified")
    print(f"  Bridge recovered cleanly and delivered {len(rec_read['readings'])} post-restart accelerometer sample(s)")

    # 12. Existing battery and thermal providers remain functional
    print("\n[12] Regression Check — Battery & Thermal Providers")
    bat = AndroidBatteryProvider(bridge=bridge).get_battery_status()
    th = AndroidThermalProvider(bridge=bridge).get_thermal_status()
    metrics = rt.monitor.sample()
    print(f"  Battery: {bat.percentage}% ({bat.status}, {bat.temperature_c}°C, {bat.voltage_v}V)")
    print(f"  Thermal: {th.thermal_status} (temp={th.battery_temperature_c}°C)")
    print(f"  ResourceMonitor: battery={metrics.battery_percent}%, temp={metrics.thermal_celsius}°C")
    assert bat.percentage is not None
    assert th.battery_temperature_c is not None

    rt.shutdown()
    time.sleep(0.3)
    assert len(check_lingering_termux_sensor_procs()) == 0
    print("\n" + "=" * 72)
    print(" D3 PHYSICAL VALIDATION COMPLETE: ALL 12 CHECKS PASSED ON VIVO V2334")
    print("=" * 72)


if __name__ == "__main__":
    main()

