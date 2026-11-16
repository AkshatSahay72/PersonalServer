"""
Native Android Bridge Package (D2 & D3)
=======================================
Reusable native integration layer between PersonalServer Device Runtime
and Android system capabilities (Power, Thermals, Sensors).
"""

from agent.device_runtime.android.models import (
    PROTOCOL_VERSION,
    MAX_PAYLOAD_BYTES,
    SENSOR_MIN_INTERVAL_MS,
    SENSOR_MAX_INTERVAL_MS,
    SENSOR_DEFAULT_INTERVAL_MS,
    SENSOR_MAX_SESSION_DURATION_SEC,
    SENSOR_MAX_BUFFER_SAMPLES,
    SENSOR_MAX_CONCURRENT_SESSIONS,
    SENSOR_UNITS,
    SENSOR_ANDROID_TYPES,
    BridgeState,
    BridgePermissionState,
    AndroidThermalStatus,
    SensorType,
    BatteryData,
    ThermalData,
    SensorMetadata,
    SensorReading,
    SensorSessionInfo,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    ProtocolVersionMismatchError,
    PayloadTooLargeError,
    SENSOR_OPERATIONS,
    VALID_SENSOR_TYPES,
)
from agent.device_runtime.android.transport import (
    BaseBridgeTransport,
    AndroidCompanionTransport,
    LocalSocketTransport,
    CompositeAndroidTransport,
    resolve_android_sensor_name,
)
from agent.device_runtime.android.permission_bridge import (
    AndroidPermissionBridge,
    ANDROID_PERMISSION_MAP,
)
from agent.device_runtime.android.bridge import (
    NativeAndroidBridge,
    BridgeError,
    BridgePermissionError,
    BridgeTimeoutError,
    BridgeUnavailableError,
)
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.android.providers.sensors import (
    BaseAndroidSensorProvider,
    AccelerometerProvider,
    GyroscopeProvider,
    MagnetometerProvider,
    AmbientLightProvider,
    ProximityProvider,
    SENSOR_PROVIDER_CLASSES,
    create_all_sensor_providers,
)
from agent.device_runtime.android.runtime import AndroidRuntimeBridge

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_PAYLOAD_BYTES",
    "SENSOR_MIN_INTERVAL_MS",
    "SENSOR_MAX_INTERVAL_MS",
    "SENSOR_DEFAULT_INTERVAL_MS",
    "SENSOR_MAX_SESSION_DURATION_SEC",
    "SENSOR_MAX_BUFFER_SAMPLES",
    "SENSOR_MAX_CONCURRENT_SESSIONS",
    "SENSOR_UNITS",
    "SENSOR_ANDROID_TYPES",
    "BridgeState",
    "BridgePermissionState",
    "AndroidThermalStatus",
    "SensorType",
    "BatteryData",
    "ThermalData",
    "SensorMetadata",
    "SensorReading",
    "SensorSessionInfo",
    "AndroidBridgeRequest",
    "AndroidBridgeResponse",
    "BridgeProtocol",
    "ProtocolError",
    "ProtocolVersionMismatchError",
    "PayloadTooLargeError",
    "SENSOR_OPERATIONS",
    "VALID_SENSOR_TYPES",
    "BaseBridgeTransport",
    "AndroidCompanionTransport",
    "LocalSocketTransport",
    "CompositeAndroidTransport",
    "resolve_android_sensor_name",
    "AndroidPermissionBridge",
    "ANDROID_PERMISSION_MAP",
    "NativeAndroidBridge",
    "BridgeError",
    "BridgePermissionError",
    "BridgeTimeoutError",
    "BridgeUnavailableError",
    "AndroidBatteryProvider",
    "AndroidThermalProvider",
    "BaseAndroidSensorProvider",
    "AccelerometerProvider",
    "GyroscopeProvider",
    "MagnetometerProvider",
    "AmbientLightProvider",
    "ProximityProvider",
    "SENSOR_PROVIDER_CLASSES",
    "create_all_sensor_providers",
    "AndroidRuntimeBridge",
]
