#!/usr/bin/env python3
"""
Physical Device Verification for PersonalServer Native Android Bridge (D2)
Target: Vivo V2334 (server-v2334-a531)
"""

import sys
import os
import json
from pathlib import Path

# Ensure PersonalServer root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import PROTOCOL_VERSION
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.providers.power_provider import PowerProvider
from agent.device_runtime.resource_monitor import ResourceMonitor

def main():
    print("=" * 65)
    print(" PersonalServer Native Android Bridge Physical Verification")
    print(f" Protocol Version: v{PROTOCOL_VERSION}")
    print("=" * 65)

    bridge = NativeAndroidBridge.get_instance()
    init_ok = bridge.initialize()
    print(f"Bridge initialized: {init_ok}")
    print(f"Bridge state: {bridge.state.value}")
    print(f"Transport type: {bridge.transport.transport_type}")
    print(f"Transport available: {bridge.transport.is_available()}")

    print("\n--- 1. Permission States ---")
    perms = bridge.permissions.to_dict()
    for pid, pdata in perms.items():
        print(f"  {pid}: {pdata['state']} (granted: {pdata['is_granted']})")

    print("\n--- 2. Direct Bridge Requests ---")
    bat_resp = bridge.execute("battery", "get_status")
    print(f"Battery Execute Success: {bat_resp.success}")
    print(f"Battery Data: {bat_resp.data}")

    th_resp = bridge.execute("thermals", "get_status")
    print(f"Thermal Execute Success: {th_resp.success}")
    print(f"Thermal Data: {th_resp.data}")

    print("\n--- 3. Typed Android Providers ---")
    bat_provider = AndroidBatteryProvider(bridge=bridge)
    bat_data = bat_provider.get_battery_status()
    print(f"BatteryData -> percentage: {bat_data.percentage}%, status: {bat_data.status}, charging: {bat_data.charging}, plugged: {bat_data.plugged_source}, temp: {bat_data.temperature_c}°C, voltage: {bat_data.voltage_v}V, current: {bat_data.current_ma}mA, health: {bat_data.health}, tech: {bat_data.technology}")

    th_provider = AndroidThermalProvider(bridge=bridge)
    th_data = th_provider.get_thermal_status()
    print(f"ThermalData -> status: {th_data.thermal_status}, severity: {th_data.severity_code}, throttling: {th_data.throttling_active}, battery_temp: {th_data.battery_temperature_c}°C, cpu_temp: {th_data.cpu_temperature_c}°C")

    print("\n--- 4. Integrated PowerProvider Discovery & Ops ---")
    power_prov = PowerProvider(bridge=bridge)
    desc = power_prov.discover()
    print(f"PowerProvider Discovery State: {desc.state.value} (error={desc.error})")
    
    bat_op = power_prov.execute("GET_BATTERY_STATUS")
    print(f"PowerProvider GET_BATTERY_STATUS: success={bat_op.get('success')}, percent={bat_op.get('battery_percent')}%, charging={bat_op.get('is_charging')}")
    
    th_op = power_prov.execute("GET_THERMAL_STATUS")
    print(f"PowerProvider GET_THERMAL_STATUS: success={th_op.get('success')}, temp={th_op.get('thermal_celsius')}°C")

    print("\n--- 5. Integrated ResourceMonitor Sampling ---")
    monitor = ResourceMonitor()
    metrics = monitor.sample()
    print(f"ResourceMonitor Sample: cpu={metrics.cpu_percent}%, ram={metrics.ram_percent}%, battery={metrics.battery_percent}%, charging={metrics.is_charging}, temp={metrics.thermal_celsius}°C")

    print("\n--- 6. Bridge Metrics ---")
    print(json.dumps(bridge.get_metrics(), indent=2))

    print("\n" + "=" * 65)
    if bat_resp.success and bat_data.percentage is not None:
        print("VERIFICATION RESULT: SUCCESS (Real hardware telemetry acquired)")
    else:
        print("VERIFICATION RESULT: DEGRADED (Check permissions or companion tools)")
    print("=" * 65)

if __name__ == "__main__":
    main()

