"""
Android Native Battery Provider (D2)
====================================
Exposes battery capacity, health, temperature, and charging metrics
through the Native Android Bridge.
"""

from typing import Dict, Any, Optional
import logging

from agent.device_runtime.android.models import BatteryData
from agent.device_runtime.android.bridge import NativeAndroidBridge

logger = logging.getLogger("agent.device_runtime.android.providers.battery")


class AndroidBatteryProvider:
    """
    Supplies structured battery metrics by issuing requests across the Native Android Bridge.
    Strictly reports authentic hardware values without simulation.
    """

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        self._bridge = bridge

    @property
    def bridge(self) -> NativeAndroidBridge:
        if self._bridge is None:
            self._bridge = NativeAndroidBridge.get_instance()
        return self._bridge

    def get_battery_status(self) -> BatteryData:
        """
        Retrieves real-time battery status.
        Returns a populated BatteryData object or a safe zero/unknown representation if restricted.
        """
        resp = self.bridge.execute(
            capability="battery",
            operation="get_status",
            parameters={},
            timeout=4.0,
        )

        if not resp.success:
            logger.debug("Failed to get battery status from bridge: %s", resp.error_message)
            return BatteryData(
                percentage=None,
                charging=None,
                plugged_source="UNKNOWN",
                temperature_c=None,
                voltage_v=None,
                health="UNKNOWN",
                current_ma=None,
                technology="UNKNOWN",
            )

        data = resp.data or {}

        # Parse percentage safely
        percentage = data.get("percentage")
        if percentage is not None:
            try:
                percentage = int(percentage)
            except (ValueError, TypeError):
                percentage = None

        # Parse charging status
        status_str = str(data.get("status", "")).upper()
        charging = None
        if status_str:
            if "DISCHARG" in status_str or "NOT" in status_str:
                charging = False
            elif "CHARG" in status_str or "FULL" in status_str:
                charging = True

        # Parse plugged source
        plugged_str = str(data.get("plugged", "UNPLUGGED")).upper()

        # Parse temperature (Termux:API reports temperature in Celsius or tenths of Celsius)
        raw_temp = data.get("temperature") or data.get("temperature_c")
        temp_c = None
        if raw_temp is not None:
            try:
                temp_c = float(raw_temp)
                if temp_c > 100.0:  # e.g., 398 -> 39.8
                    temp_c /= 10.0
            except (ValueError, TypeError):
                temp_c = None

        # Parse voltage (millivolts to volts if > 100)
        raw_volt = data.get("voltage")
        voltage_v = None
        if raw_volt is not None:
            try:
                voltage_v = float(raw_volt)
                if voltage_v > 100.0:
                    voltage_v /= 1000.0
            except (ValueError, TypeError):
                voltage_v = None

        # Parse current (microamperes or milliamperes)
        raw_current = data.get("current") or data.get("current_ma")
        current_ma = None
        if raw_current is not None:
            try:
                current_ma = float(raw_current)
                # If in microamperes (> 100000), scale to mA
                if abs(current_ma) > 50000:
                    current_ma /= 1000.0
            except (ValueError, TypeError):
                current_ma = None

        health = str(data.get("health", "UNKNOWN")).upper()
        technology = str(data.get("technology", "Li-ion"))

        return BatteryData(
            percentage=percentage,
            status=status_str,
            charging=charging,
            plugged_source=plugged_str,
            temperature_c=round(temp_c, 1) if temp_c is not None else None,
            voltage_v=round(voltage_v, 2) if voltage_v is not None else None,
            health=health,
            current_ma=round(current_ma, 1) if current_ma is not None else None,
            technology=technology,
        )
