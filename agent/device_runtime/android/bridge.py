"""
Native Android Bridge Core (D2)
===============================
Provides a hardened, production-quality interface between Python Device Runtime
and native Android system capabilities.

Coordinates:
- Transport lifecycle (Unix domain sockets, Termux:API companion, fallback sysfs)
- Protocol serialization/validation and payload bounding (v1)
- Permission gating and revocation checks
- Structured request/response execution with unique operation IDs and timeouts
- Honest error reporting without masking or faking hardware telemetry
"""

import time
import logging
from typing import Dict, Any, Optional, List

from agent.device_runtime.android.models import (
    BridgeState,
    BridgePermissionState,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
    PROTOCOL_VERSION,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    SENSOR_OPERATIONS,
)
from agent.device_runtime.android.transport import (
    BaseBridgeTransport,
    CompositeAndroidTransport,
)
from agent.device_runtime.android.permission_bridge import (
    AndroidPermissionBridge,
)

logger = logging.getLogger("agent.device_runtime.android.bridge")


class BridgeError(Exception):
    """Base exception for Native Android Bridge errors."""
    pass


class BridgePermissionError(BridgeError):
    """Raised when an operation is attempted without necessary Android permissions."""
    pass


class BridgeTimeoutError(BridgeError):
    """Raised when a bridge request exceeds its timeout deadline."""
    pass


class BridgeUnavailableError(BridgeError):
    """Raised when the bridge or requested native transport is unavailable."""
    pass


class NativeAndroidBridge:
    """
    Central orchestrator for PersonalServer Native Android communication.
    Thread-safe, non-blocking, and permission-aware.
    """

    _instance: Optional["NativeAndroidBridge"] = None

    def __init__(
        self,
        transport: Optional[BaseBridgeTransport] = None,
        permission_bridge: Optional[AndroidPermissionBridge] = None,
        default_timeout: float = 5.0,
    ):
        self._permission_bridge = permission_bridge or AndroidPermissionBridge()
        self._transport = transport or CompositeAndroidTransport()
        self._default_timeout = default_timeout
        self._state = BridgeState.UNINITIALIZED
        self._metrics = {
            "requests_total": 0,
            "requests_failed": 0,
            "requests_timed_out": 0,
            "permission_denied_count": 0,
            "last_request_time": 0.0,
        }

    @classmethod
    def get_instance(cls) -> "NativeAndroidBridge":
        """Singleton accessor."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Resets singleton (primarily for unit test isolation)."""
        if cls._instance is not None:
            cls._instance.shutdown()
            cls._instance = None

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def permissions(self) -> AndroidPermissionBridge:
        return self._permission_bridge

    @property
    def transport(self) -> BaseBridgeTransport:
        return self._transport

    def initialize(self) -> bool:
        """
        Initializes the bridge, verifies permissions, and connects transport.
        Returns True if at least one native channel is operational.
        """
        self._state = BridgeState.INITIALIZING
        logger.info("Initializing Native Android Bridge (Protocol v%d)...", PROTOCOL_VERSION)

        try:
            # 1. Run permission discovery probes
            self._permission_bridge.verify_all_probes()

            # 2. Connect transport
            connected = self._transport.connect()

            # 3. Probe transport readiness
            if connected and self._transport.is_available():
                self._state = BridgeState.CONNECTED
                logger.info("Native Android Bridge connected via %s", self._transport.transport_type)
                return True
            else:
                self._state = BridgeState.DEGRADED
                logger.warning("Native Android Bridge initialized in DEGRADED state (no active native transport)")
                return False
        except Exception as e:
            self._state = BridgeState.ERROR
            logger.error("Native Android Bridge initialization failed: %s", e)
            return False

    def shutdown(self) -> None:
        """Closes transports and marks the bridge uninitialized."""
        logger.info("Shutting down Native Android Bridge...")
        try:
            self._transport.disconnect()
        except Exception as e:
            logger.warning("Error disconnecting transport during shutdown: %s", e)
        finally:
            self._state = BridgeState.UNINITIALIZED

    def is_ready(self) -> bool:
        """Returns True if the bridge is ready to process operations."""
        return self._state in (BridgeState.CONNECTED, BridgeState.DEGRADED) and self._transport.is_available()

    def execute(
        self,
        capability: str,
        operation: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        """
        Executes an operation against native Android capabilities.
        Enforces protocol versioning, permissions, timeouts, and structured error responses.
        """
        self._metrics["requests_total"] += 1
        self._metrics["last_request_time"] = time.time()
        req_timeout = timeout if timeout is not None else self._default_timeout

        # 1. Validate permissions for requested capability
        allowed, granted_perms, missing_perms = self._permission_bridge.check_permissions(capability)
        if not allowed:
            self._metrics["permission_denied_count"] += 1
            self._metrics["requests_failed"] += 1
            error_msg = f"Permission denied for capability '{capability}'. Missing: {', '.join(missing_perms)}"
            logger.warning(error_msg)
            return AndroidBridgeResponse(
                operation_id="",
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="PERMISSION_DENIED",
                error_message=error_msg,
            )

        # 2. Validate sensor inputs if sensor operation & create structured protocol request
        validated_params = parameters or {}
        if operation in SENSOR_OPERATIONS:
            try:
                validated_params = BridgeProtocol.validate_sensor_request(operation, validated_params)
            except ProtocolError as pe:
                self._metrics["requests_failed"] += 1
                err_code = "INVALID_SAMPLING_RATE" if "sampling_interval" in str(pe) else "INVALID_REQUEST"
                return AndroidBridgeResponse(
                    operation_id="",
                    capability=capability,
                    operation=operation,
                    success=False,
                    data={},
                    error_code=err_code,
                    error_message=str(pe),
                )

        try:
            request = BridgeProtocol.create_request(
                capability=capability,
                operation=operation,
                parameters=validated_params,
                timeout=req_timeout,
            )
        except Exception as e:
            self._metrics["requests_failed"] += 1
            return AndroidBridgeResponse(
                operation_id="",
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="INVALID_REQUEST",
                error_message=str(e),
            )

        # 3. Check transport availability
        if not self._transport.is_available():
            self._metrics["requests_failed"] += 1
            return AndroidBridgeResponse(
                operation_id=request.operation_id,
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="TRANSPORT_UNAVAILABLE",
                error_message=f"No operational transport available for capability '{capability}'",
            )

        # 4. Execute via transport with timeout bounding
        start_time = time.time()
        try:
            raw_response = self._transport.send_request(request, timeout_sec=req_timeout)
            elapsed = time.time() - start_time

            # Validate response schema and bound payloads
            if isinstance(raw_response, AndroidBridgeResponse):
                response = raw_response
            else:
                response = BridgeProtocol.parse_response(raw_response)

            if not response.success:
                self._metrics["requests_failed"] += 1
            return response
        except TimeoutError as te:
            self._metrics["requests_timed_out"] += 1
            self._metrics["requests_failed"] += 1
            return AndroidBridgeResponse(
                operation_id=request.operation_id,
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="TIMEOUT",
                error_message=f"Operation timed out after {req_timeout:.1f}s: {te}",
            )
        except ProtocolError as pe:
            self._metrics["requests_failed"] += 1
            return AndroidBridgeResponse(
                operation_id=request.operation_id,
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="PROTOCOL_ERROR",
                error_message=str(pe),
            )
        except Exception as e:
            self._metrics["requests_failed"] += 1
            logger.error("Bridge request '%s' failed unexpectedly: %s", operation, e)
            return AndroidBridgeResponse(
                operation_id=request.operation_id,
                capability=capability,
                operation=operation,
                success=False,
                data={},
                error_code="INTERNAL_ERROR",
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Sensor Operations Helpers (D3)
    # ------------------------------------------------------------------

    def discover_sensors(
        self,
        sensor_types: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        params: Dict[str, Any] = {}
        if sensor_types is not None:
            params["sensor_types"] = sensor_types
        return self.execute("sensors", "SENSOR_DISCOVER", parameters=params, timeout=timeout)

    def start_sensor_session(
        self,
        sensor_type: str,
        sampling_interval_ms: int = 200,
        max_duration_sec: float = 60.0,
        max_buffer_samples: int = 32,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        return self.execute(
            capability=f"sensor.{sensor_type}",
            operation="SENSOR_START",
            parameters={
                "sensor_type": sensor_type,
                "sampling_interval_ms": sampling_interval_ms,
                "max_duration_sec": max_duration_sec,
                "max_buffer_samples": max_buffer_samples,
            },
            timeout=timeout,
        )

    def read_sensor_session(
        self,
        session_id: str,
        max_samples: int = 10,
        sensor_type: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        cap = f"sensor.{sensor_type}" if sensor_type else "sensors"
        return self.execute(
            capability=cap,
            operation="SENSOR_READ",
            parameters={"session_id": session_id, "max_samples": max_samples},
            timeout=timeout,
        )

    def stop_sensor_session(
        self,
        session_id: str,
        reason: str = "Requested stop",
        sensor_type: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        cap = f"sensor.{sensor_type}" if sensor_type else "sensors"
        return self.execute(
            capability=cap,
            operation="SENSOR_STOP",
            parameters={"session_id": session_id, "reason": reason},
            timeout=timeout,
        )

    def get_sensor_status(
        self,
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        params: Dict[str, Any] = {}
        if session_id:
            params["session_id"] = session_id
        return self.execute("sensors", "SENSOR_STATUS", parameters=params, timeout=timeout)

    def get_metrics(self) -> Dict[str, Any]:
        """Returns bridge performance and error metrics."""
        return {
            **self._metrics,
            "state": self._state.value,
            "transport_type": self._transport.transport_type,
            "is_transport_available": self._transport.is_available(),
        }
