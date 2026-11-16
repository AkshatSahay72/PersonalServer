"""
Android Native Sensor Providers (D3)
====================================
Typed, permission-aware, session-bounded Python providers for physical Android sensors:
- Accelerometer (x, y, z in m/s^2)
- Gyroscope (x, y, z in rad/s)
- Magnetometer (x, y, z in uT)
- Ambient Light (scalar value in lux)
- Proximity (scalar value in cm)

Reuses BaseDeviceProvider, CapabilityRegistry states, and DeviceSessionManager.
Strict rules:
- Discovery NEVER activates sensors.
- Missing sensors are reported as UNAVAILABLE (never fabricated or substituted).
- Every active acquisition MUST use a bounded session with explicit stop & watchdog cleanup.
"""

import os
import time
import logging
from typing import Dict, Any, Optional, List, Type

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider
from agent.device_runtime.android.models import (
    BridgeState,
    BridgePermissionState,
    SensorType,
    SensorMetadata,
    SensorReading,
    SensorSessionInfo,
    SENSOR_UNITS,
    SENSOR_DEFAULT_INTERVAL_MS,
)
from agent.device_runtime.android.bridge import NativeAndroidBridge

logger = logging.getLogger("agent.device_runtime.android.providers.sensors")


class BaseAndroidSensorProvider(BaseDeviceProvider):
    """Base provider class integrating a single Android sensor type with Device Runtime."""

    sensor_type: str = ""
    default_name: str = "Android Sensor"

    def __init__(
        self,
        sensor_type: Optional[str] = None,
        name: Optional[str] = None,
        bridge: Optional[NativeAndroidBridge] = None,
    ):
        st = sensor_type or self.sensor_type
        if not st:
            raise ValueError("sensor_type must be specified")
        self.sensor_type = st
        super().__init__(
            capability_id=f"sensor.{st}",
            name=name or self.default_name,
            subsystem="sensors",
        )
        self._supported_operations = [
            "SENSOR_DISCOVER",
            "SENSOR_START",
            "SENSOR_READ",
            "SENSOR_STOP",
            "SENSOR_STATUS",
        ]
        self._requires_permission = True
        self._permission_name = "termux.api"
        self._bridge = bridge
        self._metadata: Optional[SensorMetadata] = None
        self._active_native_session_id: Optional[str] = None
        self._active_session_info: Optional[SensorSessionInfo] = None

    def _get_bridge(self) -> NativeAndroidBridge:
        if self._bridge is None:
            self._bridge = NativeAndroidBridge.get_instance()
        if self._bridge.state == BridgeState.UNINITIALIZED and (
            os.path.exists("/data/data/com.termux") or os.path.exists("/system/build.prop")
        ):
            self._bridge.initialize()
        return self._bridge

    @property
    def metadata(self) -> Optional[SensorMetadata]:
        return self._metadata

    @property
    def active_session_id(self) -> Optional[str]:
        return self._active_native_session_id

    def discover(self) -> CapabilityDescriptor:
        """
        Discovers sensor presence & metadata via Android SensorManager/bridge.
        Strictly read-only metadata inspection: NEVER activates hardware sensor acquisition.
        """
        bridge = self._get_bridge()

        # 1. Check permission boundary first
        perm_state = bridge.permissions.get_state("termux.api")
        if perm_state == BridgePermissionState.PERMISSION_DENIED:
            self.state = CapabilityState.PERMISSION_DENIED
            self.error = "Companion IPC permission denied by OS/user"
            self._metadata = SensorMetadata(sensor_type=self.sensor_type, available=False, error=self.error)
            return self.build_descriptor(self._metadata.to_dict())

        if perm_state in (BridgePermissionState.PERMISSION_REQUIRED, BridgePermissionState.DETECTED):
            self.state = CapabilityState.PERMISSION_REQUIRED
            self.error = "Companion IPC permission required before accessing sensors"
            self._metadata = SensorMetadata(sensor_type=self.sensor_type, available=False, error=self.error)
            return self.build_descriptor(self._metadata.to_dict())

        if not bridge.is_ready():
            self.state = CapabilityState.UNAVAILABLE
            self.error = "Native Android bridge transport is unavailable"
            self._metadata = SensorMetadata(sensor_type=self.sensor_type, available=False, error=self.error)
            return self.build_descriptor(self._metadata.to_dict())

        # 2. Query bridge SENSOR_DISCOVER (bounded to this sensor_type only)
        resp = bridge.discover_sensors(sensor_types=[self.sensor_type], timeout=4.0)
        if not resp.success:
            if resp.error_code == "PERMISSION_DENIED":
                self.state = CapabilityState.PERMISSION_REQUIRED
            else:
                self.state = CapabilityState.UNAVAILABLE
            self.error = resp.error_message or "Sensor discovery failed"
            self._metadata = SensorMetadata(sensor_type=self.sensor_type, available=False, error=self.error)
            return self.build_descriptor(self._metadata.to_dict())

        sensors_dict = (resp.data or {}).get("sensors", {})
        info = sensors_dict.get(self.sensor_type)
        if not isinstance(info, dict) or not info.get("available", False):
            self.state = CapabilityState.UNAVAILABLE
            self.error = (info or {}).get("error") or f"Physical '{self.sensor_type}' sensor not present on device"
            self._metadata = SensorMetadata(sensor_type=self.sensor_type, available=False, error=self.error)
            return self.build_descriptor(self._metadata.to_dict())

        self._metadata = SensorMetadata(
            sensor_type=self.sensor_type,
            available=True,
            name=info.get("name"),
            vendor=info.get("vendor"),
            android_type=info.get("android_type"),
            version=info.get("version"),
            resolution=info.get("resolution"),
            max_range=info.get("max_range"),
            min_delay_us=info.get("min_delay_us"),
            power_ma=info.get("power_ma"),
            units=info.get("units") or SENSOR_UNITS.get(self.sensor_type, ""),
            source=info.get("source"),
            error=None,
        )

        # Preserve ACTIVE state if already running an authorized session; otherwise READY
        if self._active_native_session_id:
            self.state = CapabilityState.ACTIVE
        else:
            self.state = CapabilityState.READY
        self.error = None
        return self.build_descriptor(self._metadata.to_dict())

    def start_session(self, parameters: Optional[Dict[str, Any]] = None, timeout_sec: float = 60.0) -> Dict[str, Any]:
        """Begins bounded sensor acquisition on the native bridge."""
        params = dict(parameters or {})

        # Ensure metadata was discovered and available
        if self.state == CapabilityState.DETECTED:
            self.discover()

        if self.state == CapabilityState.ACTIVE and self._active_native_session_id:
            return {
                "success": False,
                "error": f"Sensor '{self.sensor_type}' already has an active session ({self._active_native_session_id})",
                "session_id": self._active_native_session_id,
            }

        if self.state not in (CapabilityState.READY, CapabilityState.AVAILABLE):
            return {
                "success": False,
                "error": f"Cannot start '{self.sensor_type}' sensor in state {self.state.value}: {self.error}",
                "status": self.state.value,
            }

        sampling_interval_ms = int(params.get("sampling_interval_ms", SENSOR_DEFAULT_INTERVAL_MS))
        max_buffer_samples = int(params.get("max_buffer_samples", 32))
        max_duration_sec = float(params.get("max_duration_sec", timeout_sec))

        bridge = self._get_bridge()
        resp = bridge.start_sensor_session(
            sensor_type=self.sensor_type,
            sampling_interval_ms=sampling_interval_ms,
            max_duration_sec=max_duration_sec,
            max_buffer_samples=max_buffer_samples,
            timeout=5.0,
        )

        if not resp.success:
            if resp.error_code == "PERMISSION_DENIED":
                self.state = CapabilityState.PERMISSION_REQUIRED
            return {
                "success": False,
                "error": resp.error_message or "Failed to start native sensor session",
                "error_code": resp.error_code,
            }

        sess_data = (resp.data or {}).get("session", {})
        native_sid = sess_data.get("session_id")
        if not native_sid:
            return {
                "success": False,
                "error": "Native bridge did not return a valid session_id",
            }

        self._active_native_session_id = native_sid
        self._active_session_info = SensorSessionInfo(
            session_id=native_sid,
            sensor_type=self.sensor_type,
            sensor_name=sess_data.get("sensor_name"),
            requested_interval_ms=int(sess_data.get("requested_interval_ms", sampling_interval_ms)),
            actual_interval_ms=int(sess_data.get("actual_interval_ms", sampling_interval_ms)),
            started_at=float(sess_data.get("started_at", time.time())),
            max_duration_sec=float(sess_data.get("max_duration_sec", max_duration_sec)),
            state=str(sess_data.get("state", "ACTIVE")),
            max_buffer_size=int(sess_data.get("max_buffer_size", max_buffer_samples)),
            rate_clamped=bool(sess_data.get("rate_clamped", False)),
        )
        self.state = CapabilityState.ACTIVE

        return {
            "success": True,
            "native_session_id": native_sid,
            "session": self._active_session_info.to_dict(),
        }

    def read_readings(self, session_id: Optional[str] = None, max_samples: int = 10, timeout: float = 4.0) -> Dict[str, Any]:
        """Reads typed structured readings from the active native sensor session."""
        target_sid = session_id or self._active_native_session_id
        if not target_sid:
            return {
                "success": False,
                "error": f"No active session for '{self.sensor_type}'",
                "error_code": "NO_ACTIVE_SESSION",
                "readings": [],
            }

        bridge = self._get_bridge()
        resp = bridge.read_sensor_session(
            session_id=target_sid,
            max_samples=max_samples,
            sensor_type=self.sensor_type,
            timeout=timeout,
        )

        if not resp.success:
            if resp.error_code in ("SESSION_EXPIRED", "SESSION_NOT_FOUND", "TRANSPORT_UNAVAILABLE"):
                self._active_native_session_id = None
                self._active_session_info = None
                self.state = CapabilityState.READY if bridge.is_ready() else CapabilityState.UNAVAILABLE
            return {
                "success": False,
                "error": resp.error_message,
                "error_code": resp.error_code,
                "readings": [],
            }

        raw_readings = (resp.data or {}).get("readings", [])
        typed_readings = [self._parse_reading(r, target_sid) for r in raw_readings]

        return {
            "success": True,
            "session_id": target_sid,
            "sensor_type": self.sensor_type,
            "count": len(typed_readings),
            "readings": [tr.to_dict() for tr in typed_readings],
            "typed_readings": typed_readings,
            "session": (resp.data or {}).get("session", {}),
        }

    def stop_session(self, session_id: Optional[str] = None, reason: str = "Requested stop") -> Dict[str, Any]:
        """Stops the active sensor session cleanly and restores READY state."""
        target_sid = self._active_native_session_id or session_id
        if not target_sid:
            if self.state == CapabilityState.ACTIVE:
                self.state = CapabilityState.READY
            return {"success": True, "message": "No active native sensor session"}

        bridge = self._get_bridge()
        try:
            resp = bridge.stop_sensor_session(
                session_id=target_sid,
                reason=reason,
                sensor_type=self.sensor_type,
                timeout=4.0,
            )
            return {
                "success": resp.success,
                "session": (resp.data or {}).get("session", {}),
                "error": resp.error_message,
            }
        finally:
            self._active_native_session_id = None
            self._active_session_info = None
            if self._metadata and self._metadata.available and bridge.is_ready():
                self.state = CapabilityState.READY
            elif self.state == CapabilityState.ACTIVE:
                self.state = CapabilityState.UNAVAILABLE

    def _parse_reading(self, raw: Dict[str, Any], session_id: str) -> SensorReading:
        """Converts raw bridge sample payload into a strictly typed SensorReading."""
        return SensorReading(
            sensor_type=self.sensor_type,
            available=bool(raw.get("available", True)),
            timestamp=float(raw.get("timestamp", time.time())),
            sensor_timestamp_ns=raw.get("sensor_timestamp_ns"),
            x=raw.get("x"),
            y=raw.get("y"),
            z=raw.get("z"),
            value=raw.get("value"),
            accuracy=raw.get("accuracy"),
            units=raw.get("units") or SENSOR_UNITS.get(self.sensor_type, ""),
            state=str(raw.get("state", "ACTIVE")),
            sensor_name=raw.get("sensor_name") or (self._metadata.name if self._metadata else None),
            session_id=session_id,
            error=raw.get("error"),
        )

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if operation == "SENSOR_DISCOVER":
            desc = self.discover()
            return {"success": desc.state in (CapabilityState.READY, CapabilityState.ACTIVE), "descriptor": desc.to_dict()}

        if operation == "SENSOR_START":
            return self.start_session(
                parameters=parameters,
                timeout_sec=float(parameters.get("max_duration_sec", 60.0)),
            )

        if operation == "SENSOR_READ":
            res = self.read_readings(
                session_id=parameters.get("session_id"),
                max_samples=int(parameters.get("max_samples", 10)),
            )
            # Exclude non-JSON-serializable dataclass instances for execute() dict compatibility
            clean = dict(res)
            clean.pop("typed_readings", None)
            return clean

        if operation == "SENSOR_STOP":
            return self.stop_session(
                session_id=parameters.get("session_id"),
                reason=str(parameters.get("reason", "Requested stop")),
            )

        if operation == "SENSOR_STATUS":
            bridge = self._get_bridge()
            resp = bridge.get_sensor_status(session_id=parameters.get("session_id") or self._active_native_session_id)
            return {
                "success": resp.success,
                "data": resp.data,
                "error": resp.error_message,
            }

        return super()._handle_operation(operation, parameters)


class AccelerometerProvider(BaseAndroidSensorProvider):
    """Typed provider for 3-axis accelerometer (m/s^2)."""
    sensor_type = SensorType.ACCELEROMETER.value
    default_name = "Android Accelerometer"

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        super().__init__(sensor_type=SensorType.ACCELEROMETER.value, name="Android Accelerometer", bridge=bridge)


class GyroscopeProvider(BaseAndroidSensorProvider):
    """Typed provider for 3-axis gyroscope (rad/s)."""
    sensor_type = SensorType.GYROSCOPE.value
    default_name = "Android Gyroscope"

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        super().__init__(sensor_type=SensorType.GYROSCOPE.value, name="Android Gyroscope", bridge=bridge)


class MagnetometerProvider(BaseAndroidSensorProvider):
    """Typed provider for 3-axis geomagnetic field sensor (uT)."""
    sensor_type = SensorType.MAGNETOMETER.value
    default_name = "Android Magnetometer"

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        super().__init__(sensor_type=SensorType.MAGNETOMETER.value, name="Android Magnetometer", bridge=bridge)


class AmbientLightProvider(BaseAndroidSensorProvider):
    """Typed provider for scalar ambient light sensor (lux)."""
    sensor_type = SensorType.AMBIENT_LIGHT.value
    default_name = "Android Ambient Light Sensor"

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        super().__init__(sensor_type=SensorType.AMBIENT_LIGHT.value, name="Android Ambient Light Sensor", bridge=bridge)


class ProximityProvider(BaseAndroidSensorProvider):
    """Typed provider for scalar proximity sensor (cm)."""
    sensor_type = SensorType.PROXIMITY.value
    default_name = "Android Proximity Sensor"

    def __init__(self, bridge: Optional[NativeAndroidBridge] = None):
        super().__init__(sensor_type=SensorType.PROXIMITY.value, name="Android Proximity Sensor", bridge=bridge)


SENSOR_PROVIDER_CLASSES: Dict[str, Type[BaseAndroidSensorProvider]] = {
    SensorType.ACCELEROMETER.value: AccelerometerProvider,
    SensorType.GYROSCOPE.value: GyroscopeProvider,
    SensorType.MAGNETOMETER.value: MagnetometerProvider,
    SensorType.AMBIENT_LIGHT.value: AmbientLightProvider,
    SensorType.PROXIMITY.value: ProximityProvider,
}


def create_all_sensor_providers(bridge: Optional[NativeAndroidBridge] = None) -> List[BaseAndroidSensorProvider]:
    """Instantiates all 5 standard Android sensor providers sharing one bridge."""
    return [cls(bridge=bridge) for cls in SENSOR_PROVIDER_CLASSES.values()]

