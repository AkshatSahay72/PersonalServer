"""
Native Android Bridge Providers (D2 & D3)
=========================================
"""

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

__all__ = [
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
]
