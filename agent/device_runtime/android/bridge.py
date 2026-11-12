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

import base64
import hashlib
import os
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from agent.device_runtime.android.models import (
    BridgeState,
    BridgePermissionState,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
    PROTOCOL_VERSION,
    MIC_DEFAULT_DURATION_SEC,
    MIC_DEFAULT_SAMPLE_RATE_HZ,
    MIC_MAX_FILE_BYTES,
    MIC_MAX_CHUNK_BYTES,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    SENSOR_OPERATIONS,
    RECORDING_OPERATIONS,
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

        # 1. Validate permissions for requested capability.
        # Only explicit RECORDING_OPERATIONS defer live RECORD_AUDIO state sync to the bridge
        # unless permission is already explicitly DENIED locally.
        is_recording_op = operation in RECORDING_OPERATIONS
        if not is_recording_op:
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
        elif operation in ("START_RECORDING", "RECORDING_READ"):
            if self._permission_bridge.get_state("android.permission.RECORD_AUDIO") == BridgePermissionState.PERMISSION_DENIED:
                self._metrics["permission_denied_count"] += 1
                self._metrics["requests_failed"] += 1
                return AndroidBridgeResponse(
                    operation_id="",
                    capability=capability,
                    operation=operation,
                    success=False,
                    data={},
                    error_code="PERMISSION_DENIED",
                    error_message="Permission denied for capability 'microphone' (android.permission.RECORD_AUDIO)",
                )

        # 2. Validate sensor / recording inputs & create structured protocol request
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
        elif operation in RECORDING_OPERATIONS:
            try:
                validated_params = BridgeProtocol.validate_recording_request(operation, validated_params)
            except ProtocolError as pe:
                self._metrics["requests_failed"] += 1
                err_code = "UNAUTHORIZED" if "authorization" in str(pe).lower() else "INVALID_REQUEST"
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
                if is_recording_op and response.error_code == "PERMISSION_DENIED":
                    self._metrics["permission_denied_count"] += 1
                    self._permission_bridge.set_state(
                        "android.permission.RECORD_AUDIO",
                        BridgePermissionState.PERMISSION_DENIED,
                    )
            elif is_recording_op and operation in ("RECORDING_DISCOVER", "RECORDING_STATUS"):
                perm_str = str((response.data or {}).get("permission", "")).upper()
                if perm_str == "GRANTED":
                    self._permission_bridge.set_state(
                        "android.permission.RECORD_AUDIO",
                        BridgePermissionState.READY,
                    )
                elif perm_str in ("PERMISSION_DENIED", "DENIED"):
                    self._permission_bridge.set_state(
                        "android.permission.RECORD_AUDIO",
                        BridgePermissionState.PERMISSION_DENIED,
                    )
                elif perm_str:
                    self._permission_bridge.set_state(
                        "android.permission.RECORD_AUDIO",
                        BridgePermissionState.PERMISSION_REQUIRED,
                    )
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

    # ------------------------------------------------------------------
    # Native Microphone Capture Helpers (D4-E02)
    # ------------------------------------------------------------------

    def discover_microphone(self, timeout: Optional[float] = None) -> AndroidBridgeResponse:
        return self.execute("microphone", "RECORDING_DISCOVER", parameters={}, timeout=timeout)

    def start_recording(
        self,
        duration_sec: float = MIC_DEFAULT_DURATION_SEC,
        sample_rate_hz: int = MIC_DEFAULT_SAMPLE_RATE_HZ,
        authorized: bool = False,
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        params: Dict[str, Any] = {
            "authorized": bool(authorized),
            "duration_sec": int(round(float(duration_sec))),
            "sample_rate_hz": int(sample_rate_hz),
        }
        if session_id:
            params["session_id"] = session_id
        return self.execute("microphone", "START_RECORDING", parameters=params, timeout=timeout)

    def get_recording_status(
        self,
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AndroidBridgeResponse:
        params: Dict[str, Any] = {}
        if session_id:
            params["session_id"] = session_id
        return self.execute("microphone", "RECORDING_STATUS", parameters=params, timeout=timeout)

    def stop_recording(
        self,
        session_id: Optional[str] = None,
        reason: str = "Requested stop",
        timeout: Optional[float] = 6.0,
    ) -> AndroidBridgeResponse:
        params: Dict[str, Any] = {"reason": reason}
        if session_id:
            params["session_id"] = session_id
        return self.execute("microphone", "STOP_RECORDING", parameters=params, timeout=timeout)

    def read_recording_chunk(
        self,
        session_id: str,
        offset: int = 0,
        length: int = MIC_MAX_CHUNK_BYTES,
        timeout: Optional[float] = 5.0,
    ) -> AndroidBridgeResponse:
        return self.execute(
            "microphone",
            "RECORDING_READ",
            parameters={
                "session_id": session_id,
                "offset": int(offset),
                "length": int(length),
            },
            timeout=timeout,
        )

    def cleanup_recording(self, session_id: str, timeout: Optional[float] = 4.0) -> AndroidBridgeResponse:
        return self.execute(
            "microphone",
            "RECORDING_CLEANUP",
            parameters={"session_id": session_id},
            timeout=timeout,
        )

    def transfer_recording(
        self,
        session_id: str,
        dest_path: Path,
        cleanup_remote: bool = True,
        max_bytes: int = MIC_MAX_FILE_BYTES,
    ) -> Dict[str, Any]:
        """
        Transfers a completed recording over bounded loopback IPC chunks into dest_path,
        verifies RIFF/WAVE headers and SHA-256 checksum, then removes the app-private
        temporary copy on success.
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")

        offset = 0
        expected_sha256: Optional[str] = None
        expected_total: Optional[int] = None
        hasher = hashlib.sha256()

        try:
            with open(part, "wb") as fh:
                while True:
                    chunk_resp = self.read_recording_chunk(
                        session_id=session_id,
                        offset=offset,
                        length=MIC_MAX_CHUNK_BYTES,
                    )
                    if not chunk_resp.success:
                        raise RuntimeError(chunk_resp.error_message or "Recording chunk transfer failed")

                    cdata = chunk_resp.data or {}
                    b64 = cdata.get("chunk_base64") or ""
                    raw = base64.b64decode(b64.encode("ascii")) if b64 else b""
                    if expected_sha256 is None and cdata.get("sha256"):
                        expected_sha256 = str(cdata["sha256"]).lower()
                    if expected_total is None and cdata.get("total_bytes") is not None:
                        expected_total = int(cdata["total_bytes"])
                        if expected_total > max_bytes:
                            raise ValueError(f"Recording size {expected_total} exceeds bound {max_bytes}")

                    if offset + len(raw) > max_bytes:
                        raise ValueError(f"Transferred recording exceeds max_bytes ({max_bytes})")

                    if raw:
                        fh.write(raw)
                        hasher.update(raw)
                        offset += len(raw)

                    eof = bool(cdata.get("eof", False))
                    if eof:
                        break
                    if len(raw) == 0:
                        raise RuntimeError("Zero-length chunk returned before EOF during recording transfer")

            actual_sha256 = hasher.hexdigest().lower()
            if expected_total is not None and offset != expected_total:
                raise RuntimeError(f"Transferred byte count mismatch: got {offset}, expected {expected_total}")
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise RuntimeError(f"SHA-256 verification failed: got {actual_sha256}, expected {expected_sha256}")

            # Basic WAV container integrity validation
            with open(part, "rb") as chk:
                hdr = chk.read(12)
            if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                raise RuntimeError("Transferred file failed RIFF/WAVE header integrity check")

            os.replace(part, dest)

            remote_cleaned = False
            if cleanup_remote:
                try:
                    c_resp = self.cleanup_recording(session_id)
                    remote_cleaned = bool(c_resp.success and (c_resp.data or {}).get("deleted", False))
                except Exception:
                    remote_cleaned = False

            return {
                "success": True,
                "session_id": session_id,
                "file_path": str(dest),
                "file_size_bytes": offset,
                "sha256": actual_sha256,
                "remote_cleaned": remote_cleaned,
                "mime_type": "audio/wav",
            }
        except Exception:
            if part.exists():
                try:
                    part.unlink()
                except Exception:
                    pass
            raise

    def get_metrics(self) -> Dict[str, Any]:
        """Returns bridge performance and error metrics."""
        return {
            **self._metrics,
            "state": self._state.value,
            "transport_type": self._transport.transport_type,
            "is_transport_available": self._transport.is_available(),
        }
