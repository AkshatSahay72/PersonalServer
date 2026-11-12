"""
Device Runtime Coordinator (D1)
===============================
Unified controller and hardware abstraction layer for Android/Linux/mobile nodes.
Coordinates capability discovery, permission verification, daily-use resource protection,
and bounded hardware sessions.
"""

from typing import Dict, Any, Optional, List, Tuple
import time
import logging

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityState,
)
from agent.device_runtime.participation_manager import (
    ParticipationManager,
    ParticipationPolicy,
    ParticipationState,
)
from agent.device_runtime.permission_manager import (
    PermissionManager,
    PermissionStatus,
)
from agent.device_runtime.session_manager import (
    DeviceSession,
    DeviceSessionManager,
    DeviceSessionState,
)
from agent.device_runtime.resource_monitor import (
    ResourceMonitor,
    SystemResourceMetrics,
)
from agent.device_runtime.providers.base import BaseDeviceProvider
from agent.device_runtime.providers.compute_provider import ComputeProvider
from agent.device_runtime.providers.storage_provider import StorageProvider
from agent.device_runtime.providers.connectivity_provider import ConnectivityProvider
from agent.device_runtime.providers.power_provider import PowerProvider

logger = logging.getLogger("agent.device_runtime")


class DeviceRuntime:
    """
    Main runtime coordinator for experimental and mobile nodes.
    Integrates capability discovery, resource monitoring, and bounded execution.
    """

    _instance: Optional["DeviceRuntime"] = None

    def __init__(
        self,
        node_id: str = "server-v2334-a531",
        default_participation: ParticipationState = ParticipationState.OFF,
        storage_path: Optional[str] = None,
    ):
        self.node_id = node_id
        self.registry = CapabilityRegistry()
        self.participation = ParticipationManager(default_state=default_participation)
        self.permissions = PermissionManager()
        self.sessions = DeviceSessionManager()
        self.monitor = ResourceMonitor(storage_path=storage_path)

        self._providers: Dict[str, BaseDeviceProvider] = {}
        self._register_default_providers()

    @classmethod
    def get_instance(cls, node_id: str = "server-v2334-a531") -> "DeviceRuntime":
        if cls._instance is None:
            cls._instance = cls(node_id=node_id)
        return cls._instance

    def _register_default_providers(self) -> None:
        """Initializes standard D1 & D3 providers."""
        self.register_provider(ComputeProvider())
        self.register_provider(StorageProvider())
        self.register_provider(ConnectivityProvider())
        self.register_provider(PowerProvider())

        # D3 Native Android Sensor providers & D4-E02 Microphone provider (discovery only; never auto-activated)
        try:
            from agent.device_runtime.android.providers.sensors import create_all_sensor_providers
            from agent.device_runtime.android.providers.microphone import AndroidMicrophoneProvider
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            bridge = NativeAndroidBridge.get_instance()
            termux_perm = self.permissions.get("termux.api")
            for sp in create_all_sensor_providers(bridge=bridge):
                self.register_provider(sp)
                if termux_perm and sp.capability_id not in termux_perm.required_for:
                    termux_perm.required_for.append(sp.capability_id)
            self.register_provider(AndroidMicrophoneProvider(bridge=bridge))
            # Sync bridge permission status into DeviceRuntime PermissionManager
            if bridge.permissions.is_granted("termux.api"):
                self.permissions.set_status("termux.api", PermissionStatus.GRANTED)
            if bridge.permissions.is_granted("android.permission.RECORD_AUDIO"):
                self.permissions.set_status("android.permission.RECORD_AUDIO", PermissionStatus.GRANTED)
        except Exception as e:
            logger.debug("Optional Android hardware providers not loaded: %s", e)

    def register_provider(self, provider: BaseDeviceProvider) -> None:
        """Registers a device provider and executes discovery."""
        self._providers[provider.capability_id] = provider
        desc = provider.discover()
        self.registry.register(desc)

    def _sync_android_permissions(self) -> None:
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            bridge = NativeAndroidBridge.get_instance()
            if bridge.permissions.is_granted("termux.api"):
                self.permissions.set_status("termux.api", PermissionStatus.GRANTED)
            if bridge.permissions.is_granted("android.permission.RECORD_AUDIO"):
                self.permissions.set_status("android.permission.RECORD_AUDIO", PermissionStatus.GRANTED)
        except Exception:
            pass

    def discover_all(self) -> Dict[str, CapabilityDescriptor]:
        """Runs discovery across all registered providers."""
        results = {}
        for cid, provider in self._providers.items():
            desc = provider.discover()
            self.registry.register(desc)
            results[cid] = desc
        self._sync_android_permissions()
        return results

    def get_provider(self, capability_id: str) -> Optional[BaseDeviceProvider]:
        """Gets provider by capability ID."""
        return self._providers.get(capability_id)

    def execute_operation(
        self,
        capability_id: str,
        operation: str,
        parameters: Optional[Dict[str, Any]] = None,
        is_heavy: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes an operation against a registered capability provider.
        Enforces:
        1. Participation admission (OFF/STANDBY checks).
        2. Permission verification (guards unprivileged calls).
        3. Capability state verification.
        """
        # 1. Participation admission check
        metrics = self.monitor.sample()
        admitted, reason = self.participation.evaluate_task_admission(
            battery_percent=metrics.battery_percent,
            is_charging=metrics.is_charging,
            cpu_percent=metrics.cpu_percent,
            thermal_celsius=metrics.thermal_celsius,
            is_heavy=is_heavy,
        )
        if not admitted:
            logger.warning("Operation %s on %s rejected by participation policy: %s", operation, capability_id, reason)
            return {
                "success": False,
                "operation": operation,
                "capability_id": capability_id,
                "error": f"Device participation rejected operation: {reason}",
                "participation_state": self.participation.state.value,
            }

        # 2. Permission check
        all_granted, missing = self.permissions.check_capability_permissions(capability_id)
        if not all_granted:
            return {
                "success": False,
                "operation": operation,
                "capability_id": capability_id,
                "error": f"Missing required permissions: {', '.join(missing)}",
                "missing_permissions": missing,
                "status": "PERMISSION_REQUIRED",
            }

        # 3. Provider dispatch
        provider = self._providers.get(capability_id)
        if not provider:
            return {
                "success": False,
                "operation": operation,
                "capability_id": capability_id,
                "error": f"Provider for capability '{capability_id}' not found",
            }

        return provider.execute(operation, parameters)

    def start_session(
        self,
        capability_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0,
    ) -> Dict[str, Any]:
        """
        Initiates a time-bounded hardware session under participation & permission guards.
        Transitions participation and capability states to ACTIVE.
        """
        if not self.participation.register_session_start():
            return {
                "success": False,
                "error": f"Cannot start session in participation state {self.participation.state.value} (is_paused={self.participation.is_paused})",
            }

        provider = self._providers.get(capability_id)
        if not provider:
            self.participation.register_session_stop()
            return {"success": False, "error": f"Provider '{capability_id}' not found"}

        all_granted, missing = self.permissions.check_capability_permissions(capability_id)
        if not all_granted:
            self.participation.register_session_stop()
            return {
                "success": False,
                "error": f"Missing required permissions: {', '.join(missing)}",
                "missing_permissions": missing,
                "status": "PERMISSION_REQUIRED",
            }

        session = self.sessions.create_session(
            capability_id=capability_id,
            parameters=parameters,
            timeout_sec=timeout_sec,
            cleanup_callback=lambda s: self._on_session_cleanup(s, provider),
        )

        res = provider.start_session(parameters, timeout_sec)
        if not res.get("success", False):
            session.close(f"Provider start failed: {res.get('error')}")
            return res

        # Record provider session metadata (intervals, native IDs, resource limits)
        if isinstance(res.get("session"), dict):
            session.extra_data.update(res["session"])
        elif res.get("native_session_id"):
            session.extra_data["native_session_id"] = res["native_session_id"]

        session.start()
        desc = self.registry.get(capability_id)
        if desc:
            desc.update_state(provider.state)

        return {
            "success": True,
            "session": session.to_dict(),
            "provider_result": res,
        }

    def read_sensor_session(
        self,
        session_id: str,
        max_samples: int = 10,
        timeout: float = 4.0,
    ) -> Dict[str, Any]:
        """Reads structured sensor telemetry from an active DeviceSession."""
        session = self.sessions.get_session(session_id)
        if not session:
            return {"success": False, "error": f"Session '{session_id}' not found", "readings": []}
        if session.state != DeviceSessionState.ACTIVE:
            return {
                "success": False,
                "error": f"Session '{session_id}' is not ACTIVE (state={session.state.value})",
                "readings": [],
            }
        provider = self._providers.get(session.capability_id)
        if not provider or not hasattr(provider, "read_readings"):
            return {"success": False, "error": f"Provider '{session.capability_id}' does not support sensor reads", "readings": []}

        native_sid = session.extra_data.get("native_session_id")
        return provider.read_readings(session_id=native_sid, max_samples=max_samples, timeout=timeout)

    def _on_session_cleanup(self, session: DeviceSession, provider: BaseDeviceProvider) -> None:
        """Invoked when session ends or watchdog fires."""
        try:
            native_sid = session.extra_data.get("native_session_id")
            provider.stop_session(native_sid or session.session_id, reason=session.stop_reason or "Session ended")
            desc = self.registry.get(provider.capability_id)
            if desc:
                desc.update_state(provider.state)
        finally:
            self.participation.register_session_stop()

    def stop_session(self, session_id: str, reason: str = "Requested stop") -> Dict[str, Any]:
        """Stops an active session cleanly."""
        session = self.sessions.close_session(session_id, reason)
        if not session:
            return {"success": False, "error": f"Session '{session_id}' not found"}
        return {"success": True, "session": session.to_dict()}

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Generates the synchronized telemetry payload for controller heartbeats:
        combines resources, participation state, capabilities, and active sessions.
        """
        metrics = self.monitor.sample()
        return {
            "node_id": self.node_id,
            "timestamp": time.time(),
            "participation": self.participation.to_dict(),
            "system": metrics.to_dict(),
            "capabilities": self.registry.to_dict(),
            "permissions": self.permissions.to_dict(),
            "active_sessions": [s.to_dict() for s in self.sessions.list_active()],
        }

    @property
    def android_bridge(self) -> Optional[Any]:
        """Provides access to the Native Android Bridge if present."""
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            return NativeAndroidBridge.get_instance()
        except Exception:
            return None

    def shutdown(self) -> None:
        """Gracefully shuts down the runtime."""
        self.participation.set_state(ParticipationState.OFF, "Runtime shutdown")
        self.sessions.shutdown()
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            NativeAndroidBridge.reset_instance()
        except Exception:
            pass

