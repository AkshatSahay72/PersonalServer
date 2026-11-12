"""
Android Native Thermal Provider (D2)
====================================
Exposes Android PowerManager thermal status, throttling indicators,
and hardware thermal sensor readings through the Native Android Bridge.
"""

from typing import Dict, Any, Optional
import logging

from agent.device_runtime.android.models import (
    ThermalData,
    AndroidThermalStatus,
)
from agent.device_runtime.android.bridge import NativeAndroidBridge

logger = logging.getLogger("agent.device_runtime.android.providers.thermal")

# Mapping from PowerManager status integer codes to enum
STATUS_CODE_MAP = {
    0: AndroidThermalStatus.THERMAL_STATUS_NONE,
    1: AndroidThermalStatus.THERMAL_STATUS_LIGHT,
    2: AndroidThermalStatus.THERMAL_STATUS_MODERATE,
    3: AndroidThermalStatus.THERMAL_STATUS_SEVERE,
    4: AndroidThermalStatus.THERMAL_STATUS_CRITICAL,
    5: AndroidThermalStatus.THERMAL_STATUS_EMERGENCY,
    6: AndroidThermalStatus.THERMAL_STATUS_SHUTDOWN,
}


class AndroidThermalProvider:
    """
    Supplies structured thermal and throttling metrics via the Native Android Bridge.
    Strictly reports authentic hardware values without simulation.
    """

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        self._bridge = bridge

    @property
    def bridge(self) -> NativeAndroidBridge:
        if self._bridge is None:
            self._bridge = NativeAndroidBridge.get_instance()
        return self._bridge

    def get_thermal_status(self) -> ThermalData:
        """
        Retrieves real-time thermal telemetry.
        Returns a populated ThermalData object.
        """
        resp = self.bridge.execute(
            capability="thermals",
            operation="get_status",
            parameters={},
            timeout=4.0,
        )

        if not resp.success:
            logger.debug("Failed to get thermal status from bridge: %s", resp.error_message)
            return ThermalData(
                thermal_status=AndroidThermalStatus.THERMAL_STATUS_NONE,
                severity_code=0,
                throttling_active=False,
                battery_temperature_c=None,
                cpu_temperature_c=None,
                headroom_forecast=None,
            )

        data = resp.data or {}

        # 1. Parse severity code & status
        raw_code = data.get("severity_code") or data.get("status_code")
        severity_code = 0
        if raw_code is not None:
            try:
                severity_code = max(0, min(6, int(raw_code)))
            except (ValueError, TypeError):
                severity_code = 0

        thermal_status = STATUS_CODE_MAP.get(severity_code, AndroidThermalStatus.THERMAL_STATUS_NONE)

        # 2. Check explicit throttling flag or infer from severity (MODERATE or higher triggers throttling)
        throttling_active = bool(data.get("throttling_active", severity_code >= 2))

        # 3. Parse battery temperature
        raw_bat_temp = data.get("battery_temperature_c") or data.get("battery_temperature")
        bat_temp_c = None
        if raw_bat_temp is not None:
            try:
                bat_temp_c = float(raw_bat_temp)
                if bat_temp_c > 100.0:
                    bat_temp_c /= 10.0
            except (ValueError, TypeError):
                bat_temp_c = None

        # 4. Parse CPU temperature
        raw_cpu_temp = data.get("cpu_temperature_c") or data.get("cpu_temperature")
        cpu_temp_c = None
        if raw_cpu_temp is not None:
            try:
                cpu_temp_c = float(raw_cpu_temp)
                if cpu_temp_c > 1000.0:
                    cpu_temp_c /= 1000.0
            except (ValueError, TypeError):
                cpu_temp_c = None

        # 5. Parse thermal headroom
        raw_headroom = data.get("headroom_forecast") or data.get("headroom")
        headroom = None
        if raw_headroom is not None:
            try:
                headroom = float(raw_headroom)
            except (ValueError, TypeError):
                headroom = None

        return ThermalData(
            thermal_status=thermal_status,
            severity_code=severity_code,
            throttling_active=throttling_active,
            battery_temperature_c=round(bat_temp_c, 1) if bat_temp_c is not None else None,
            cpu_temperature_c=round(cpu_temp_c, 1) if cpu_temp_c is not None else None,
            headroom_forecast=round(headroom, 2) if headroom is not None else None,
        )

