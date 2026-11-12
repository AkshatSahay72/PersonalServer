"""
Android Runtime Integration Bridge (D2 & D3)
============================================
Integrates the Native Android Bridge with the PersonalServer Device Runtime.
Bridges capability discovery, permission state, battery, thermal, and sensor telemetry.
"""

from typing import Optional, Dict, Any, List
import logging

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import (
    BridgeState,
    BridgePermissionState,
    BatteryData,
    ThermalData,
    SensorMetadata,
)
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.android.providers.sensors import (
    BaseAndroidSensorProvider,
    create_all_sensor_providers,
)

logger = logging.getLogger("agent.device_runtime.android.runtime")


class AndroidRuntimeBridge:
    """
    Adapter between NativeAndroidBridge and DeviceRuntime.
    Provides a clean, uniform interface for device coordinators to access Android capabilities.
    """

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        self._bridge = bridge or NativeAndroidBridge.get_instance()
        self._battery_provider = AndroidBatteryProvider(self._bridge)
        self._thermal_provider = AndroidThermalProvider(self._bridge)
        self._sensor_providers: Dict[str, BaseAndroidSensorProvider] = {
            p.sensor_type: p for p in create_all_sensor_providers(self._bridge)
        }

    @property
    def bridge(self) -> NativeAndroidBridge:
        return self._bridge

    @property
    def battery(self) -> AndroidBatteryProvider:
        return self._battery_provider

    @property
    def thermal(self) -> AndroidThermalProvider:
        return self._thermal_provider

    @property
    def sensors(self) -> Dict[str, BaseAndroidSensorProvider]:
        return self._sensor_providers

    def initialize(self) -> bool:
        """Initializes the underlying bridge."""
        return self._bridge.initialize()

    def shutdown(self) -> None:
        """Shuts down all active sensor sessions and the underlying bridge."""
        for sp in self._sensor_providers.values():
            try:
                if sp.active_session_id:
                    sp.stop_session(reason="AndroidRuntimeBridge shutdown")
            except Exception:
                pass
        self._bridge.shutdown()

    def is_available(self) -> bool:
        """Returns True if the bridge is initialized and ready."""
        return self._bridge.is_ready()

    def get_battery_metrics(self) -> BatteryData:
        """Convenience method to retrieve structured battery telemetry."""
        return self._battery_provider.get_battery_status()

    def get_thermal_metrics(self) -> ThermalData:
        """Convenience method to retrieve structured thermal telemetry."""
        return self._thermal_provider.get_thermal_status()

    def discover_sensors(self) -> Dict[str, SensorMetadata]:
        """Discovers metadata for all supported sensors without activating them."""
        results: Dict[str, SensorMetadata] = {}
        for st, sp in self._sensor_providers.items():
            sp.discover()
            if sp.metadata:
                results[st] = sp.metadata
        return results

    def get_diagnostics(self) -> Dict[str, Any]:
        """Provides diagnostic information on bridge state, transports, permissions, and sensors."""
        return {
            "bridge_state": self._bridge.state.value,
            "transport_type": self._bridge.transport.transport_type,
            "is_ready": self._bridge.is_ready(),
            "metrics": self._bridge.get_metrics(),
            "permissions": self._bridge.permissions.to_dict(),
            "sensors": {st: sp.get_status() for st, sp in self._sensor_providers.items()},
        }
