"""
Power Provider for Device Runtime (D1 & D2)
============================================
Exposes battery status, charging state, and thermal measurements.
Integrates with NativeAndroidBridge on mobile/Android nodes while preserving
direct sysfs inspection on Linux hosts.

Enforces the strict PersonalServer policy:
DO NOT MOCK OR FAKE MEASUREMENTS.
If hardware telemetry is inaccessible due to missing companion apps or SELinux
restrictions, honestly report UNAVAILABLE or PERMISSION_REQUIRED.
"""

from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import logging

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider

logger = logging.getLogger("agent.device_runtime.providers.power")


class PowerProvider(BaseDeviceProvider):
    """Inspects battery and thermal power metrics with honest degradation."""

    def __init__(self, bridge: Optional[Any] = None):
        super().__init__(
            capability_id="power",
            name="Host Power & Thermal Subsystem",
            subsystem="power",
        )
        self._supported_operations = [
            "GET_BATTERY_STATUS",
            "GET_THERMAL_STATUS",
        ]
        self._requires_permission = True
        self._permission_name = "termux.api"
        self._battery_accessible = False
        self._thermals_accessible = False
        self._bridge = bridge

    def _get_bridge(self) -> Optional[Any]:
        if self._bridge is not None:
            return self._bridge
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            return NativeAndroidBridge.get_instance()
        except Exception:
            return None

    def discover(self) -> CapabilityDescriptor:
        """
        Discovers whether battery sysfs, Termux:API, or Native Android Bridge is accessible.
        Never fakes availability.
        """
        # 1. Probe direct sysfs
        self._battery_accessible = self._check_battery_readable()
        self._thermals_accessible = self._check_thermals_readable()

        # 2. If sysfs is blocked (e.g., Android SELinux), probe through Native Android Bridge
        if not (self._battery_accessible and self._thermals_accessible):
            bridge = self._get_bridge()
            if bridge and bridge.is_ready():
                # Probe battery via bridge
                if not self._battery_accessible:
                    try:
                        b_res = bridge.execute("battery", "get_status", timeout=2.0)
                        if b_res.success and b_res.data and b_res.data.get("percentage") is not None:
                            self._battery_accessible = True
                    except Exception:
                        pass

                # Probe thermal via bridge
                if not self._thermals_accessible:
                    try:
                        t_res = bridge.execute("thermals", "get_status", timeout=2.0)
                        if t_res.success and t_res.data:
                            self._thermals_accessible = True
                    except Exception:
                        pass

        details = {
            "battery_accessible": self._battery_accessible,
            "thermals_accessible": self._thermals_accessible,
        }

        if self._battery_accessible and self._thermals_accessible:
            self.state = CapabilityState.READY
            self.error = None
        elif self._battery_accessible or self._thermals_accessible:
            self.state = CapabilityState.AVAILABLE
            self.error = "Partial telemetry accessible"
        else:
            self.state = CapabilityState.PERMISSION_REQUIRED
            self.error = "Battery and thermal telemetry restricted by Android SELinux; companion API required"

        return self.build_descriptor(details)

    def _check_battery_readable(self) -> bool:
        """Checks if battery capacity file is readable."""
        f = Path("/sys/class/power_supply/battery/capacity")
        if f.exists():
            try:
                f.read_text()
                return True
            except Exception:
                return False
        return False

    def _check_thermals_readable(self) -> bool:
        """Checks if any thermal zone is readable."""
        base = Path("/sys/class/thermal")
        if base.exists():
            try:
                for z in base.glob("thermal_zone*"):
                    tf = z / "temp"
                    if tf.exists():
                        try:
                            tf.read_text()
                            return True
                        except Exception:
                            pass
            except Exception:
                pass
        return False

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if operation == "GET_BATTERY_STATUS":
            if not self._battery_accessible:
                return {
                    "success": False,
                    "operation": operation,
                    "status": "UNAVAILABLE",
                    "error": "Battery metrics unavailable in current user context (SELinux/missing Termux:API companion APK)",
                    "battery_percent": None,
                    "is_charging": None,
                }

            # Try reading through bridge first
            bridge = self._get_bridge()
            if bridge and bridge.is_ready():
                try:
                    resp = bridge.execute("battery", "get_status", timeout=3.0)
                    if resp.success and resp.data:
                        pct = resp.data.get("percentage")
                        st = str(resp.data.get("status", "")).upper()
                        charging = None
                        if st:
                            if "DISCHARG" in st or "NOT" in st:
                                charging = False
                            elif "CHARG" in st or "FULL" in st:
                                charging = True
                        return {
                            "success": True,
                            "operation": operation,
                            "status": "AVAILABLE",
                            "battery_percent": pct,
                            "is_charging": charging,
                            "temperature_c": resp.data.get("temperature"),
                            "health": resp.data.get("health"),
                            "plugged": resp.data.get("plugged"),
                        }
                except Exception:
                    pass

            pct, charging = self._read_battery_metrics()
            return {
                "success": True,
                "operation": operation,
                "status": "AVAILABLE",
                "battery_percent": pct,
                "is_charging": charging,
            }

        if operation == "GET_THERMAL_STATUS":
            if not self._thermals_accessible:
                return {
                    "success": False,
                    "operation": operation,
                    "status": "UNAVAILABLE",
                    "error": "Thermal telemetry unavailable in current user context",
                    "thermal_celsius": None,
                }

            # Try reading through bridge first
            bridge = self._get_bridge()
            if bridge and bridge.is_ready():
                try:
                    resp = bridge.execute("thermals", "get_status", timeout=3.0)
                    if resp.success and resp.data:
                        t_c = resp.data.get("battery_temperature_c") or resp.data.get("cpu_temperature_c")
                        return {
                            "success": True,
                            "operation": operation,
                            "status": "AVAILABLE",
                            "thermal_celsius": t_c,
                            "severity_code": resp.data.get("severity_code", 0),
                            "throttling_active": resp.data.get("throttling_active", False),
                        }
                except Exception:
                    pass

            t = self._read_thermal_metrics()
            return {
                "success": True,
                "operation": operation,
                "status": "AVAILABLE",
                "thermal_celsius": t,
            }

        return super()._handle_operation(operation, parameters)

    def _read_battery_metrics(self) -> Tuple[Optional[int], Optional[bool]]:
        cap_f = Path("/sys/class/power_supply/battery/capacity")
        st_f = Path("/sys/class/power_supply/battery/status")
        pct, charging = None, None
        try:
            if cap_f.exists():
                pct = int(cap_f.read_text().strip())
        except Exception:
            pass
        try:
            if st_f.exists():
                val = st_f.read_text().strip().upper()
                charging = ("CHARG" in val or "FULL" in val)
        except Exception:
            pass
        return pct, charging

    def _read_thermal_metrics(self) -> Optional[float]:
        base = Path("/sys/class/thermal")
        temps: List[float] = []
        try:
            for z in base.glob("thermal_zone*"):
                tf = z / "temp"
                if tf.exists():
                    try:
                        v = float(tf.read_text().strip())
                        if v > 1000.0:
                            v /= 1000.0
                        if 10.0 <= v <= 110.0:
                            temps.append(v)
                    except Exception:
                        pass
            return max(temps) if temps else None
        except Exception:
            return None
