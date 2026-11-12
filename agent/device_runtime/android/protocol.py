"""
Bridge Protocol Validator and Serializer (D2)
============================================
Handles version negotiation, payload validation, size bounds checking,
and structured error propagation between Python and Native Android components.
"""

import json
import uuid
import time
from typing import Dict, Any, Optional, Tuple, Union

from agent.device_runtime.android.models import (
    PROTOCOL_VERSION,
    MAX_PAYLOAD_BYTES,
    SENSOR_MIN_INTERVAL_MS,
    SENSOR_MAX_INTERVAL_MS,
    SENSOR_DEFAULT_INTERVAL_MS,
    SENSOR_MAX_SESSION_DURATION_SEC,
    SENSOR_MAX_BUFFER_SAMPLES,
    SensorType,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
    BridgePermissionState,
)

SENSOR_OPERATIONS = frozenset({
    "SENSOR_DISCOVER",
    "SENSOR_START",
    "SENSOR_READ",
    "SENSOR_STOP",
    "SENSOR_STATUS",
})

VALID_SENSOR_TYPES = frozenset(st.value for st in SensorType)


class ProtocolError(Exception):
    """Raised when request or response fails protocol specification."""
    pass


class ProtocolVersionMismatchError(ProtocolError):
    """Raised when an incompatible protocol version is encountered."""
    pass


class PayloadTooLargeError(ProtocolError):
    """Raised when payload exceeds MAX_PAYLOAD_BYTES (64KB)."""
    pass


class BridgeProtocol:
    """Encoder, decoder, and validator for the PersonalServer Native Android Bridge."""

    @staticmethod
    def validate_payload_size(payload: Union[bytes, str]) -> None:
        """Enforces 64KB bound on payload size."""
        size = len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"Payload size ({size} bytes) exceeds maximum allowed ({MAX_PAYLOAD_BYTES} bytes)"
            )

    @staticmethod
    def create_request(
        capability: str,
        operation: str,
        parameters: Optional[Dict[str, Any]] = None,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
        request_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> AndroidBridgeRequest:
        """Constructs a validated request envelope."""
        if not operation or not isinstance(operation, str):
            raise ProtocolError("Operation must be a non-empty string")
        if not capability or not isinstance(capability, str):
            raise ProtocolError("Capability must be a non-empty string")

        op_id = operation_id or request_id or f"op_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
        params = parameters if parameters is not None else (arguments or {})

        req = AndroidBridgeRequest(
            capability=capability,
            operation=operation,
            request_id=op_id,
            operation_id=op_id,
            parameters=params,
            arguments=params,
            protocol_version=PROTOCOL_VERSION,
            version=PROTOCOL_VERSION,
            timeout_sec=timeout,
        )
        return req

    @staticmethod
    def serialize_request(request: AndroidBridgeRequest) -> bytes:
        """Serializes a request to UTF-8 JSON bytes with 64KB bound enforcement."""
        data_dict = request.to_dict()
        raw = json.dumps(data_dict, ensure_ascii=False)
        raw_bytes = raw.encode("utf-8")
        BridgeProtocol.validate_payload_size(raw_bytes)
        return raw_bytes

    @staticmethod
    def parse_request(raw: Union[str, bytes]) -> AndroidBridgeRequest:
        """Parses and strictly validates an incoming JSON request."""
        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8")
        else:
            raw_str = str(raw)

        if not raw_str or not raw_str.strip():
            raise ProtocolError("Request payload must be non-empty")

        BridgeProtocol.validate_payload_size(raw_str)

        try:
            d = json.loads(raw_str)
        except Exception as e:
            raise ProtocolError(f"Invalid JSON format: {e}") from e

        if not isinstance(d, dict):
            raise ProtocolError("Request root must be a JSON object")

        version = d.get("version") or d.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolVersionMismatchError(
                f"Protocol version mismatch: expected {PROTOCOL_VERSION}, received {version}"
            )

        operation = d.get("operation")
        capability = d.get("capability")
        op_id = d.get("operation_id") or d.get("request_id")

        if not operation or not isinstance(operation, str):
            raise ProtocolError("Missing or invalid 'operation' in request")
        if not capability or not isinstance(capability, str):
            raise ProtocolError("Missing or invalid 'capability' in request")
        if not op_id or not isinstance(op_id, str):
            raise ProtocolError("Missing or invalid 'operation_id' in request")

        params = d.get("parameters") or d.get("arguments") or {}
        if not isinstance(params, dict):
            raise ProtocolError("Parameters must be a JSON object")

        return AndroidBridgeRequest(
            capability=capability,
            operation=operation,
            request_id=op_id,
            operation_id=op_id,
            parameters=params,
            arguments=params,
            protocol_version=PROTOCOL_VERSION,
            version=PROTOCOL_VERSION,
            timeout_sec=float(d.get("timeout_sec", 5.0)),
            timestamp=float(d.get("timestamp", time.time())),
        )

    # Alias for deserialize_request
    deserialize_request = parse_request

    @staticmethod
    def create_response(
        request: AndroidBridgeRequest,
        success: bool,
        status: str = BridgePermissionState.READY.value,
        data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> AndroidBridgeResponse:
        """Constructs a validated response envelope matching the originating request."""
        return AndroidBridgeResponse(
            capability=request.capability,
            operation=request.operation,
            request_id=request.request_id,
            operation_id=request.operation_id,
            success=success,
            status=status,
            data=data or {},
            error=error,
            error_message=error,
            error_code=error_code,
            protocol_version=PROTOCOL_VERSION,
            version=PROTOCOL_VERSION,
        )

    @staticmethod
    def serialize_response(response: AndroidBridgeResponse) -> bytes:
        """Serializes a response to UTF-8 JSON bytes with 64KB bound enforcement."""
        data_dict = response.to_dict()
        raw = json.dumps(data_dict, ensure_ascii=False)
        raw_bytes = raw.encode("utf-8")
        BridgeProtocol.validate_payload_size(raw_bytes)
        return raw_bytes

    @staticmethod
    def parse_response(raw: Union[str, bytes, AndroidBridgeResponse]) -> AndroidBridgeResponse:
        """Parses and strictly validates a response payload."""
        if isinstance(raw, AndroidBridgeResponse):
            return raw

        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8")
        else:
            raw_str = str(raw)

        if not raw_str or not raw_str.strip():
            raise ProtocolError("Response payload must be non-empty")

        BridgeProtocol.validate_payload_size(raw_str)

        try:
            d = json.loads(raw_str)
        except Exception as e:
            raise ProtocolError(f"Invalid JSON response format: {e}") from e

        if not isinstance(d, dict):
            raise ProtocolError("Response root must be a JSON object")

        version = d.get("version") or d.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolVersionMismatchError(
                f"Protocol version mismatch: expected {PROTOCOL_VERSION}, received {version}"
            )

        op_id = d.get("operation_id") or d.get("request_id")
        if not op_id or not isinstance(op_id, str):
            raise ProtocolError("Missing or invalid 'operation_id' in response")

        capability = d.get("capability", "")
        operation = d.get("operation", "")
        success = bool(d.get("success", False))
        status = str(d.get("status", "READY"))
        data = d.get("data", {})
        if not isinstance(data, dict):
            raise ProtocolError("'data' field must be a JSON object")

        err = d.get("error") or d.get("error_message")
        err_code = d.get("error_code")

        return AndroidBridgeResponse(
            capability=capability,
            operation=operation,
            request_id=op_id,
            operation_id=op_id,
            success=success,
            status=status,
            data=data,
            error=err,
            error_message=err,
            error_code=err_code,
            protocol_version=PROTOCOL_VERSION,
            version=PROTOCOL_VERSION,
            timestamp=str(d.get("timestamp", "")),
        )

    # Alias for deserialize_response
    deserialize_response = parse_response

    @staticmethod
    def validate_sensor_request(
        operation: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validates parameters for protocol v1 sensor operations.
        Enforces sensor type allowlist, sampling rate bounds, max session duration,
        and bounded queue/sample limits.
        Raises ProtocolError on invalid input.
        """
        if operation not in SENSOR_OPERATIONS:
            raise ProtocolError(f"Unsupported sensor operation: {operation}")

        params = dict(parameters or {})

        if operation == "SENSOR_DISCOVER":
            sensor_types = params.get("sensor_types")
            if sensor_types is not None:
                if not isinstance(sensor_types, list) or not all(isinstance(s, str) for s in sensor_types):
                    raise ProtocolError("'sensor_types' must be a list of sensor type strings")
                invalid = [s for s in sensor_types if s not in VALID_SENSOR_TYPES]
                if invalid:
                    raise ProtocolError(f"Unsupported sensor types requested: {', '.join(invalid)}")
            return params

        if operation == "SENSOR_START":
            sensor_type = params.get("sensor_type")
            if not sensor_type or not isinstance(sensor_type, str) or sensor_type not in VALID_SENSOR_TYPES:
                raise ProtocolError(
                    f"Invalid or unsupported 'sensor_type': {sensor_type}. "
                    f"Supported: {', '.join(sorted(VALID_SENSOR_TYPES))}"
                )

            raw_interval = params.get("sampling_interval_ms", SENSOR_DEFAULT_INTERVAL_MS)
            try:
                req_interval = int(raw_interval)
            except (TypeError, ValueError) as e:
                raise ProtocolError(f"Invalid sampling_interval_ms: {raw_interval}") from e

            if req_interval <= 0 or req_interval > 60000:
                raise ProtocolError(
                    f"sampling_interval_ms ({req_interval}) out of valid bounds (1..60000 ms)"
                )

            actual_interval = max(SENSOR_MIN_INTERVAL_MS, min(req_interval, SENSOR_MAX_INTERVAL_MS))
            params["requested_interval_ms"] = req_interval
            params["sampling_interval_ms"] = actual_interval
            params["actual_interval_ms"] = actual_interval
            params["rate_clamped"] = (actual_interval != req_interval)

            raw_dur = params.get("max_duration_sec", 60.0)
            try:
                dur = float(raw_dur)
            except (TypeError, ValueError) as e:
                raise ProtocolError(f"Invalid max_duration_sec: {raw_dur}") from e
            if dur <= 0:
                raise ProtocolError("max_duration_sec must be positive")
            params["max_duration_sec"] = max(1.0, min(dur, SENSOR_MAX_SESSION_DURATION_SEC))

            raw_buf = params.get("max_buffer_samples", SENSOR_MAX_BUFFER_SAMPLES)
            try:
                buf_sz = int(raw_buf)
            except (TypeError, ValueError) as e:
                raise ProtocolError(f"Invalid max_buffer_samples: {raw_buf}") from e
            if buf_sz <= 0:
                raise ProtocolError("max_buffer_samples must be positive")
            params["max_buffer_samples"] = min(buf_sz, SENSOR_MAX_BUFFER_SAMPLES)

            return params

        if operation == "SENSOR_READ":
            session_id = params.get("session_id")
            if not session_id or not isinstance(session_id, str):
                raise ProtocolError("SENSOR_READ requires a valid 'session_id'")
            raw_max = params.get("max_samples", 10)
            try:
                max_samples = int(raw_max)
            except (TypeError, ValueError) as e:
                raise ProtocolError(f"Invalid max_samples: {raw_max}") from e
            if max_samples <= 0:
                raise ProtocolError("max_samples must be positive")
            params["max_samples"] = min(max_samples, SENSOR_MAX_BUFFER_SAMPLES)
            return params

        if operation == "SENSOR_STOP":
            session_id = params.get("session_id")
            if not session_id or not isinstance(session_id, str):
                raise ProtocolError("SENSOR_STOP requires a valid 'session_id'")
            return params

        if operation == "SENSOR_STATUS":
            session_id = params.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                raise ProtocolError("'session_id' must be a string when provided")
            return params

        return params
