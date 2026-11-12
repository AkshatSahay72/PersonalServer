"""
Data Models and Types for Native Android Bridge (D2)
====================================================
Defines structured request/response objects, data classes, and lifecycle states
for Android native hardware operations.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, Optional, List, Union
import time
from datetime import datetime, timezone


PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 65536  # 64 KB safety bound on requests/responses

# Sensor Runtime Protocol Constants & Limits (D3)
SENSOR_MIN_INTERVAL_MS = 20
SENSOR_MAX_INTERVAL_MS = 10000
SENSOR_DEFAULT_INTERVAL_MS = 200
SENSOR_MAX_SESSION_DURATION_SEC = 300.0
SENSOR_MAX_BUFFER_SAMPLES = 32
SENSOR_MAX_CONCURRENT_SESSIONS = 4


class SensorType(str, Enum):
    """Canonical sensor categories supported by the Native Android Sensor Runtime."""
    ACCELEROMETER = "accelerometer"
    GYROSCOPE = "gyroscope"
    MAGNETOMETER = "magnetometer"
    AMBIENT_LIGHT = "ambient_light"
    PROXIMITY = "proximity"


SENSOR_UNITS: Dict[str, str] = {
    SensorType.ACCELEROMETER.value: "m/s^2",
    SensorType.GYROSCOPE.value: "rad/s",
    SensorType.MAGNETOMETER.value: "uT",
    SensorType.AMBIENT_LIGHT.value: "lux",
    SensorType.PROXIMITY.value: "cm",
}

SENSOR_ANDROID_TYPES: Dict[str, int] = {
    SensorType.ACCELEROMETER.value: 1,   # Sensor.TYPE_ACCELEROMETER
    SensorType.MAGNETOMETER.value: 2,    # Sensor.TYPE_MAGNETIC_FIELD
    SensorType.GYROSCOPE.value: 4,       # Sensor.TYPE_GYROSCOPE
    SensorType.AMBIENT_LIGHT.value: 5,   # Sensor.TYPE_LIGHT
    SensorType.PROXIMITY.value: 8,       # Sensor.TYPE_PROXIMITY
}


class BridgeState(str, Enum):
    """Lifecycle state of the native Android bridge transport and subsystem."""
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZING = "INITIALIZING"
    CONNECTED = "CONNECTED"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


class BridgePermissionState(str, Enum):
    """
    Seven explicit capability permission states for Android subsystems.
    Enforces the fundamental architectural boundary:
    Detection does NOT imply permission, and permission does NOT imply availability.
    """
    DETECTED = "DETECTED"                       # Subsystem hardware physically exists, uninitialized
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED" # Interface detected, but missing user or OS authorization
    PERMISSION_GRANTED = "PERMISSION_GRANTED"   # Permission active and confirmed by Android OS
    PERMISSION_DENIED = "PERMISSION_DENIED"     # User or OS explicitly refused permission
    UNAVAILABLE = "UNAVAILABLE"                 # Subsystem unsupported on hardware or platform
    READY = "READY"                             # Configured, authorized, and operational
    ERROR = "ERROR"                             # Unexpected error during state evaluation


class AndroidThermalStatus(int, Enum):
    """
    Standard Android PowerManager.THERMAL_STATUS_* constants (API 29+).
    """
    THERMAL_STATUS_NONE = 0
    THERMAL_STATUS_LIGHT = 1
    THERMAL_STATUS_MODERATE = 2
    THERMAL_STATUS_SEVERE = 3
    THERMAL_STATUS_CRITICAL = 4
    THERMAL_STATUS_EMERGENCY = 5
    THERMAL_STATUS_SHUTDOWN = 6

    # Aliases
    NONE = 0
    LIGHT = 1
    MODERATE = 2
    SEVERE = 3
    CRITICAL = 4
    EMERGENCY = 5
    SHUTDOWN = 6

    @property
    def label(self) -> str:
        labels = {
            0: "None",
            1: "Light",
            2: "Moderate",
            3: "Severe",
            4: "Critical",
            5: "Emergency",
            6: "Shutdown",
        }
        return labels.get(self.value, "Unknown")

    @property
    def is_throttling(self) -> bool:
        return self.value >= AndroidThermalStatus.THERMAL_STATUS_MODERATE.value


@dataclass
class BatteryData:
    """Standardized battery telemetry from Android BatteryManager."""
    percentage: Optional[int] = None
    status: Optional[str] = None         # "CHARGING", "DISCHARGING", "FULL", "NOT_CHARGING", "UNKNOWN"
    plugged: Optional[str] = None        # "PLUGGED_AC", "PLUGGED_USB", "PLUGGED_WIRELESS", "UNPLUGGED", "UNKNOWN"
    health: Optional[str] = None         # "GOOD", "OVERHEAT", "DEAD", "OVER_VOLTAGE", "UNKNOWN"
    temperature_c: Optional[float] = None
    voltage_mv: Optional[int] = None
    voltage_v: Optional[float] = None
    current_ma: Optional[float] = None
    current_average_ma: Optional[float] = None
    technology: Optional[str] = "Li-ion"
    charging: Optional[bool] = None
    plugged_source: Optional[str] = None
    charge_counter_uah: Optional[int] = None
    cycle_count: Optional[int] = None
    present: Optional[bool] = None

    def __post_init__(self):
        if self.charging is None and self.status:
            st = str(self.status).upper()
            if "DISCHARG" in st or "NOT" in st:
                self.charging = False
            elif "CHARG" in st or "FULL" in st:
                self.charging = True
        if self.plugged_source is None and self.plugged:
            self.plugged_source = self.plugged
        elif self.plugged is None and self.plugged_source:
            self.plugged = self.plugged_source

        if self.voltage_v is None and self.voltage_mv is not None:
            self.voltage_v = round(self.voltage_mv / 1000.0, 2)
        elif self.voltage_mv is None and self.voltage_v is not None:
            self.voltage_mv = int(self.voltage_v * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ThermalData:
    """Standardized thermal telemetry from Android PowerManager and SoC sensors."""
    thermal_status: Any = AndroidThermalStatus.THERMAL_STATUS_NONE
    severity_code: int = 0                    # 0 (None) to 6 (Shutdown)
    throttling_active: bool = False
    battery_temperature_c: Optional[float] = None
    cpu_temperature_c: Optional[float] = None
    headroom_forecast: Optional[float] = None
    temperature_c: Optional[float] = None     # Alias
    severity: int = 0                         # Alias
    is_throttling: bool = False               # Alias
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.severity and self.severity_code:
            self.severity = self.severity_code
        elif not self.severity_code and self.severity:
            self.severity_code = self.severity

        if not self.is_throttling and self.throttling_active:
            self.is_throttling = self.throttling_active
        elif not self.throttling_active and self.is_throttling:
            self.throttling_active = self.is_throttling

        if self.temperature_c is None:
            self.temperature_c = self.battery_temperature_c if self.battery_temperature_c is not None else self.cpu_temperature_c

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if isinstance(self.thermal_status, Enum):
            d["thermal_status"] = self.thermal_status.name
        return d


@dataclass
class AndroidBridgeRequest:
    """Versioned request envelope for native Android operations."""
    capability: str
    operation: str
    request_id: str = ""
    operation_id: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION
    version: int = PROTOCOL_VERSION
    timestamp: float = field(default_factory=time.time)
    timeout_sec: float = 5.0

    def __post_init__(self):
        if not self.request_id and self.operation_id:
            self.request_id = self.operation_id
        elif not self.operation_id and self.request_id:
            self.operation_id = self.request_id

        if not self.parameters and self.arguments:
            self.parameters = self.arguments
        elif not self.arguments and self.parameters:
            self.arguments = self.parameters

        if self.version != self.protocol_version:
            self.version = self.protocol_version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "capability": self.capability,
            "operation": self.operation,
            "parameters": self.parameters,
            "arguments": self.arguments,
            "timestamp": self.timestamp,
            "timeout_sec": self.timeout_sec,
        }


@dataclass
class AndroidBridgeResponse:
    """Versioned response envelope from native Android operations."""
    capability: str
    operation: str
    request_id: str = ""
    operation_id: str = ""
    success: bool = False
    status: str = "READY"
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    protocol_version: int = PROTOCOL_VERSION
    version: int = PROTOCOL_VERSION
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        if not self.request_id and self.operation_id:
            self.request_id = self.operation_id
        elif not self.operation_id and self.request_id:
            self.operation_id = self.request_id

        if not self.error_message and self.error:
            self.error_message = self.error
        elif not self.error and self.error_message:
            self.error = self.error_message

        if self.version != self.protocol_version:
            self.version = self.protocol_version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "capability": self.capability,
            "operation": self.operation,
            "success": self.success,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "timestamp": self.timestamp,
        }


@dataclass
class SensorMetadata:
    """Hardware metadata for a physical Android sensor discovered via SensorManager."""
    sensor_type: str
    available: bool = False
    name: Optional[str] = None
    vendor: Optional[str] = None
    android_type: Optional[int] = None
    version: Optional[int] = None
    resolution: Optional[float] = None
    max_range: Optional[float] = None
    min_delay_us: Optional[int] = None
    power_ma: Optional[float] = None
    units: str = ""
    source: Optional[str] = None  # "native_bridge_apk" or "termux_sensor"
    error: Optional[str] = None

    def __post_init__(self):
        if not self.units and self.sensor_type in SENSOR_UNITS:
            self.units = SENSOR_UNITS[self.sensor_type]
        if self.android_type is None and self.sensor_type in SENSOR_ANDROID_TYPES:
            self.android_type = SENSOR_ANDROID_TYPES[self.sensor_type]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SensorReading:
    """Typed, structured reading from an active Android sensor session."""
    sensor_type: str
    available: bool = False
    timestamp: float = field(default_factory=time.time)
    sensor_timestamp_ns: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    value: Optional[float] = None
    accuracy: Optional[int] = None
    units: str = ""
    state: str = "UNAVAILABLE"  # READY, ACTIVE, UNAVAILABLE, PERMISSION_REQUIRED, ERROR
    sensor_name: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        if not self.units and self.sensor_type in SENSOR_UNITS:
            self.units = SENSOR_UNITS[self.sensor_type]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SensorSessionInfo:
    """Bounded acquisition session descriptor for native sensor streaming/polling."""
    session_id: str
    sensor_type: str
    sensor_name: Optional[str] = None
    requested_interval_ms: int = SENSOR_DEFAULT_INTERVAL_MS
    actual_interval_ms: int = SENSOR_DEFAULT_INTERVAL_MS
    started_at: float = field(default_factory=time.time)
    max_duration_sec: float = SENSOR_MAX_SESSION_DURATION_SEC
    state: str = "REQUESTED"
    samples_read: int = 0
    max_buffer_size: int = SENSOR_MAX_BUFFER_SAMPLES
    rate_clamped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
