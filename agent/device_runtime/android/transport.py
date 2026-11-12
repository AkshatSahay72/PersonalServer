"""
Android Bridge Transports (D2 & D3)
===================================
Secure local communication between Python Device Runtime and Android native components.
Strictly non-public, zero network exposure, bounds-checked, and process-death aware.
Supports Battery, Thermal, and Bounded Native Sensor operations (Protocol v1).
"""

import abc
from collections import deque
import os
import re
import socket
import subprocess
import json
import threading
import time
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List, Union

from agent.device_runtime.android.models import (
    MAX_PAYLOAD_BYTES,
    SENSOR_MIN_INTERVAL_MS,
    SENSOR_MAX_INTERVAL_MS,
    SENSOR_DEFAULT_INTERVAL_MS,
    SENSOR_MAX_SESSION_DURATION_SEC,
    SENSOR_MAX_BUFFER_SAMPLES,
    SENSOR_MAX_CONCURRENT_SESSIONS,
    SENSOR_UNITS,
    SENSOR_ANDROID_TYPES,
    SensorType,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
    BridgeState,
    BridgePermissionState,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    SENSOR_OPERATIONS,
)

logger = logging.getLogger("agent.device_runtime.android.transport")


def resolve_android_sensor_name(raw_names: List[str], sensor_type: str) -> Optional[str]:
    """
    Maps a canonical SensorType to the most appropriate raw physical sensor name
    reported by Android SensorManager. Never substitutes one sensor category for another.
    """
    if not raw_names:
        return None

    candidates = [str(n) for n in raw_names if n]

    if sensor_type == SensorType.ACCELEROMETER.value:
        # Exclude uncalibrated, linear acceleration, or synthetic wake/gesture sensors
        filtered = [
            n for n in candidates
            if "uncal" not in n.lower() and "linear" not in n.lower() and "wake" not in n.lower()
        ]
        for pattern in (r"^accelerometer$", r"_acc$", r"accelerometer", r"\bacc\b"):
            for n in filtered:
                if re.search(pattern, n, re.IGNORECASE):
                    return n
        return None

    if sensor_type == SensorType.GYROSCOPE.value:
        filtered = [
            n for n in candidates
            if "uncal" not in n.lower() and "wake" not in n.lower()
        ]
        for pattern in (r"^gyroscope$", r"_gyro$", r"gyroscope", r"gyro"):
            for n in filtered:
                if re.search(pattern, n, re.IGNORECASE):
                    return n
        return None

    if sensor_type == SensorType.MAGNETOMETER.value:
        filtered = [
            n for n in candidates
            if "uncal" not in n.lower() and "wake" not in n.lower() and "mag" not in ("image",)
        ]
        for pattern in (
            r"^magnetometer$",
            r"magnetic[_\s]?field",
            r"^qmc\d+",
            r"^ak\d+",
            r"^mmc\d+",
            r"^ist\d+",
            r"_mag$",
            r"magnet",
        ):
            for n in filtered:
                if re.search(pattern, n, re.IGNORECASE):
                    return n
        return None

    if sensor_type == SensorType.AMBIENT_LIGHT.value:
        filtered = [
            n for n in candidates
            if "policy" not in n.lower() and "scene" not in n.lower() and "wake" not in n.lower()
        ]
        for pattern in (r"^light$", r"_als$", r"ambient[_\s]?light", r"light"):
            for n in filtered:
                if re.search(pattern, n, re.IGNORECASE):
                    return n
        return None

    if sensor_type == SensorType.PROXIMITY.value:
        # Prefer standard Android "Proximity Sensor" first, avoid vendor algo wrappers if standard exists
        for pattern in (r"^proximity sensor$", r"^proximity$", r"proximity", r"_prox$"):
            for n in candidates:
                nl = n.lower()
                if "algo" in nl or "smart" in nl:
                    continue
                if re.search(pattern, n, re.IGNORECASE):
                    return n
        for n in candidates:
            if "prox" in n.lower() and "algo" not in n.lower():
                return n
        return None

    return None


class _CompanionSensorSession:
    """Bounded, thread-safe companion sensor acquisition session for AndroidCompanionTransport."""

    def __init__(
        self,
        session_id: str,
        sensor_type: str,
        sensor_name: str,
        termux_sensor_exe: str,
        requested_interval_ms: int,
        actual_interval_ms: int,
        max_duration_sec: float,
        max_buffer_samples: int,
        cleanup_exe: Optional[str] = None,
    ):
        self.session_id = session_id
        self.sensor_type = sensor_type
        self.sensor_name = sensor_name
        self.termux_sensor_exe = termux_sensor_exe
        self.cleanup_exe = cleanup_exe or termux_sensor_exe
        self.requested_interval_ms = requested_interval_ms
        self.actual_interval_ms = actual_interval_ms
        self.max_duration_sec = max_duration_sec
        self.max_buffer_samples = max_buffer_samples

        self.started_at = time.time()
        self.stopped_at: Optional[float] = None
        self.state = "ACTIVE"
        self.error: Optional[str] = None
        self.samples_read = 0
        self.buffer: deque = deque(maxlen=max_buffer_samples)

        self._lock = threading.Lock()
        self._data_event = threading.Event()
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._acquisition_loop,
            daemon=True,
            name=f"SensorAcq-{self.session_id}",
        )
        self._thread.start()

    def is_expired(self) -> bool:
        return self.state == "ACTIVE" and (time.time() - self.started_at) >= self.max_duration_sec

    def _push_reading(self, raw_values: List[Any]) -> None:
        now = time.time()
        entry: Dict[str, Any] = {
            "sensor_type": self.sensor_type,
            "sensor_name": self.sensor_name,
            "timestamp": now,
            "accuracy": None,  # Not exposed by termux-sensor CLI
            "units": SENSOR_UNITS.get(self.sensor_type, ""),
            "session_id": self.session_id,
            "available": True,
            "state": "ACTIVE",
        }
        if self.sensor_type in (
            SensorType.ACCELEROMETER.value,
            SensorType.GYROSCOPE.value,
            SensorType.MAGNETOMETER.value,
        ):
            entry["x"] = float(raw_values[0]) if len(raw_values) > 0 else None
            entry["y"] = float(raw_values[1]) if len(raw_values) > 1 else None
            entry["z"] = float(raw_values[2]) if len(raw_values) > 2 else None
        else:
            entry["value"] = float(raw_values[0]) if len(raw_values) > 0 else None

        with self._lock:
            self.buffer.append(entry)
            self.samples_read += 1
        self._data_event.set()

    def _acquisition_loop(self) -> None:
        deadline = self.started_at + self.max_duration_sec
        is_onchange = self.sensor_type == SensorType.PROXIMITY.value

        try:
            if is_onchange:
                # On-change sensors emit one initial event and block until physical change;
                # poll cleanly with bounded -n 1 bursts at requested interval.
                while not self._stop_event.is_set() and time.time() < deadline:
                    try:
                        res = subprocess.run(
                            [self.termux_sensor_exe, "-s", self.sensor_name, "-n", "1"],
                            capture_output=True,
                            text=True,
                            timeout=3.0,
                        )
                        if res.returncode == 0 and res.stdout.strip():
                            parsed = json.loads(res.stdout.strip())
                            sdata = parsed.get(self.sensor_name)
                            if isinstance(sdata, dict) and isinstance(sdata.get("values"), list):
                                self._push_reading(sdata["values"])
                    except Exception as e:
                        logger.debug("On-change sensor read %s error: %s", self.sensor_name, e)
                    if self._stop_event.wait(timeout=self.actual_interval_ms / 1000.0):
                        break
            else:
                # Streaming sensor: compute hard sample cap from max_duration_sec & interval
                sample_cap = max(
                    1,
                    min(int((self.max_duration_sec * 1000.0) / max(1, self.actual_interval_ms)) + 2, 500),
                )
                cmd = [
                    self.termux_sensor_exe,
                    "-s", self.sensor_name,
                    "-d", str(self.actual_interval_ms),
                    "-n", str(sample_cap),
                ]
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                json_buf: List[str] = []
                brace_depth = 0
                in_obj = False

                while not self._stop_event.is_set() and time.time() < deadline:
                    if self._proc.stdout is None:
                        break
                    line = self._proc.stdout.readline()
                    if not line:
                        break
                    for ch in line:
                        if ch == "{":
                            if brace_depth == 0:
                                json_buf = []
                                in_obj = True
                            brace_depth += 1
                        if in_obj:
                            json_buf.append(ch)
                        if ch == "}":
                            brace_depth -= 1
                            if in_obj and brace_depth == 0:
                                in_obj = False
                                raw_chunk = "".join(json_buf)
                                try:
                                    parsed = json.loads(raw_chunk)
                                    sdata = parsed.get(self.sensor_name)
                                    if isinstance(sdata, dict) and isinstance(sdata.get("values"), list):
                                        self._push_reading(sdata["values"])
                                except Exception:
                                    pass
        except Exception as e:
            self.error = str(e)
            if self.state == "ACTIVE":
                self.state = "ERROR"
        finally:
            self._terminate_proc()
            if self.state == "ACTIVE":
                if time.time() >= deadline and not self._stop_event.is_set():
                    self.state = "TIMED_OUT"
                else:
                    self.state = "STOPPED"
            if self.stopped_at is None:
                self.stopped_at = time.time()
            self._data_event.set()

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        proc.kill()
            except Exception:
                pass
        try:
            subprocess.run([self.cleanup_exe, "-c"], capture_output=True, timeout=2.0)
        except Exception:
            pass

    def read_samples(self, max_samples: int = 10, wait_timeout: float = 2.0) -> List[Dict[str, Any]]:
        """Reads buffered samples (waiting briefly if buffer is empty on newly started active session)."""
        if self.state == "ACTIVE" and len(self.buffer) == 0:
            self._data_event.wait(timeout=wait_timeout)

        with self._lock:
            out: List[Dict[str, Any]] = []
            while self.buffer and len(out) < max_samples:
                out.append(self.buffer.popleft())
            if not self.buffer:
                self._data_event.clear()
            return out

    def stop(self, reason: str = "Requested stop") -> None:
        self._stop_event.set()
        self._terminate_proc()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        if self.state == "ACTIVE":
            self.state = "STOPPED"
        if self.stopped_at is None:
            self.stopped_at = time.time()
        self._data_event.set()

    def to_status_dict(self) -> Dict[str, Any]:
        with self._lock:
            buffered = len(self.buffer)
        return {
            "session_id": self.session_id,
            "sensor_type": self.sensor_type,
            "sensor_name": self.sensor_name,
            "requested_interval_ms": self.requested_interval_ms,
            "actual_interval_ms": self.actual_interval_ms,
            "rate_clamped": self.actual_interval_ms != self.requested_interval_ms,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "max_duration_sec": self.max_duration_sec,
            "state": self.state,
            "samples_read": self.samples_read,
            "buffered_samples": buffered,
            "max_buffer_size": self.max_buffer_samples,
            "error": self.error,
        }


class BaseBridgeTransport(abc.ABC):
    """Abstract communication channel to native Android subsystems."""

    @property
    def transport_type(self) -> str:
        return self.__class__.__name__

    def connect(self) -> bool:
        """Connects or initializes transport resources."""
        return self.is_available()

    def disconnect(self) -> None:
        """Disconnects transport resources."""
        self.shutdown()

    @abc.abstractmethod
    def send_request(self, request: AndroidBridgeRequest, timeout_sec: float = 3.0) -> AndroidBridgeResponse:
        """Dispatches request envelope and awaits validated response envelope."""
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Returns True if the underlying native component or transport exists on this system."""
        pass

    @abc.abstractmethod
    def check_readiness(self) -> Tuple[bool, Optional[str]]:
        """Verifies transport is operational and accepting requests."""
        pass

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Terminates transport connections and background threads cleanly."""
        pass


class AndroidCompanionTransport(BaseBridgeTransport):
    """
    Direct local transport using Android's native Termux companion / IPC bridge.
    Executes native operations through validated IPC commands without exposing
    any network ports or socket listeners.
    """

    def __init__(self, timeout_sec: float = 4.0):
        self._state = BridgeState.UNINITIALIZED
        self._fail_until = 0.0
        self._backoff_sec = 15.0
        self._default_timeout = timeout_sec
        self._sensor_sessions: Dict[str, _CompanionSensorSession] = {}
        self._sensor_lock = threading.Lock()
        self._cached_raw_sensors: Optional[List[str]] = None

    def is_available(self) -> bool:
        """Checks if Android environment and companion tools exist."""
        is_android = os.path.exists("/system/build.prop") or os.path.exists("/data/data/com.termux")
        has_tool = bool(
            self._resolve_executable("termux-battery-status")
            or self._resolve_executable("termux-sensor")
        )
        return is_android and has_tool

    def check_readiness(self) -> Tuple[bool, Optional[str]]:
        """Pings native layer to verify execution without hanging."""
        if not self.is_available():
            return False, "Not running in Android Termux environment with companion API installed"

        if time.time() < self._fail_until:
            return False, "Companion transport in backoff recovery"

        try:
            exe = self._resolve_executable("termux-battery-status")
            cmd = [exe] if exe else None
            if not cmd:
                sensor_exe = self._resolve_executable("termux-sensor")
                cmd = [sensor_exe, "-l"] if sensor_exe else None
            if not cmd:
                return False, "Native companion tools not found in PATH"

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
            if res.returncode == 0 and res.stdout.strip():
                self._state = BridgeState.READY
                return True, None
            return False, f"Companion tool returned non-zero exit code: {res.returncode}"
        except subprocess.TimeoutExpired:
            self._fail_until = time.time() + self._backoff_sec
            self._kill_orphaned_helpers()
            return False, "Companion tool timed out (Android API companion unresponsive)"
        except Exception as e:
            return False, f"Readiness check failed: {e}"

    def send_request(self, request: AndroidBridgeRequest, timeout_sec: Optional[float] = None) -> AndroidBridgeResponse:
        """Executes a validated bridge request via the companion layer."""
        t_limit = timeout_sec if timeout_sec is not None else getattr(request, "timeout_sec", self._default_timeout)

        if time.time() < self._fail_until:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.UNAVAILABLE.value,
                error="Transport currently in backoff recovery after timeout",
                error_code="TRANSPORT_UNAVAILABLE",
            )

        # Dispatch Sensor operations (D3)
        if request.operation in SENSOR_OPERATIONS:
            return self._handle_sensor_request(request, t_limit)

        cmd = self._resolve_command_for_operation(request.capability, request.operation, request.arguments)
        if not cmd:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.UNAVAILABLE.value,
                error=f"No native companion handler for operation '{request.operation}' on '{request.capability}'",
                error_code="OPERATION_UNSUPPORTED",
            )

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=t_limit)
            if proc.returncode != 0:
                err_text = proc.stderr.strip() or f"Process exited with code {proc.returncode}"
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.ERROR.value,
                    error=f"Native command failed: {err_text}",
                    error_code="NATIVE_ERROR",
                )

            raw_out = proc.stdout.strip()
            if not raw_out:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error="Native command returned empty output",
                    error_code="EMPTY_RESPONSE",
                )

            try:
                parsed_data = json.loads(raw_out)
            except Exception as e:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.ERROR.value,
                    error=f"Malformed JSON returned by native component: {e}",
                    error_code="MALFORMED_JSON",
                )

            # Normalization for thermal requests using battery status
            if request.capability in ("thermals", "thermal", "power.thermal"):
                if isinstance(parsed_data, dict):
                    raw_temp = parsed_data.get("temperature")
                    temp_c = float(raw_temp) if raw_temp is not None else None
                    if temp_c is not None and temp_c > 100.0:
                        temp_c /= 10.0
                    parsed_data = {
                        "severity_code": 0,
                        "throttling_active": False,
                        "battery_temperature_c": temp_c,
                        "cpu_temperature_c": None,
                        "raw_battery": parsed_data,
                    }

            return BridgeProtocol.create_response(
                request=request,
                success=True,
                status=BridgePermissionState.READY.value,
                data=parsed_data if isinstance(parsed_data, dict) else {"raw": parsed_data},
            )

        except subprocess.TimeoutExpired:
            self._fail_until = time.time() + self._backoff_sec
            self._kill_orphaned_helpers()
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.UNAVAILABLE.value,
                error=f"Native operation timed out after {t_limit:.1f}s",
                error_code="TIMEOUT",
            )
        except Exception as e:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.ERROR.value,
                error=f"Native transport error: {e}",
                error_code="TRANSPORT_ERROR",
            )

    # ------------------------------------------------------------------
    # Sensor Operations via Companion IPC (D3)
    # ------------------------------------------------------------------

    def _discover_raw_sensors(self, timeout_sec: float = 4.0) -> Tuple[Optional[List[str]], Optional[str]]:
        exe = self._resolve_executable("termux-sensor")
        if not exe:
            return None, "termux-sensor binary not installed"

        try:
            proc = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode != 0:
                return None, proc.stderr.strip() or f"termux-sensor -l failed (rc={proc.returncode})"
            payload = json.loads(proc.stdout.strip())
            sensors = payload.get("sensors", [])
            if isinstance(sensors, list):
                self._cached_raw_sensors = [str(s) for s in sensors]
                return self._cached_raw_sensors, None
            return None, "Malformed response from termux-sensor -l"
        except subprocess.TimeoutExpired:
            self._fail_until = time.time() + self._backoff_sec
            self._kill_orphaned_helpers()
            return None, "termux-sensor -l timed out"
        except Exception as e:
            return None, str(e)

    def _handle_sensor_request(self, request: AndroidBridgeRequest, timeout_sec: float) -> AndroidBridgeResponse:
        op = request.operation
        params = request.parameters or request.arguments or {}

        if op == "SENSOR_DISCOVER":
            raw_list, err = self._discover_raw_sensors(timeout_sec=timeout_sec)
            if raw_list is None:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=err or "Unable to query sensor list",
                    error_code="SENSOR_DISCOVERY_FAILED",
                )

            target_types = params.get("sensor_types") or [st.value for st in SensorType]
            sensors_info: Dict[str, Any] = {}
            for st in target_types:
                matched = resolve_android_sensor_name(raw_list, st)
                sensors_info[st] = {
                    "sensor_type": st,
                    "available": matched is not None,
                    "name": matched,
                    "vendor": None,         # Companion CLI exposes name only
                    "android_type": SENSOR_ANDROID_TYPES.get(st),
                    "version": None,
                    "resolution": None,
                    "max_range": None,
                    "min_delay_us": None,
                    "power_ma": None,
                    "units": SENSOR_UNITS.get(st, ""),
                    "source": "termux_sensor" if matched else None,
                    "error": None if matched else f"No physical '{st}' sensor detected on device",
                }

            return BridgeProtocol.create_response(
                request=request,
                success=True,
                status=BridgePermissionState.READY.value,
                data={
                    "sensors": sensors_info,
                    "raw_sensor_count": len(raw_list),
                    "source": "termux_sensor",
                },
            )

        if op == "SENSOR_START":
            sensor_type = params.get("sensor_type", "")
            req_ms = int(params.get("requested_interval_ms", params.get("sampling_interval_ms", SENSOR_DEFAULT_INTERVAL_MS)))
            actual_ms = max(SENSOR_MIN_INTERVAL_MS, min(int(params.get("actual_interval_ms", req_ms)), SENSOR_MAX_INTERVAL_MS))
            max_dur = float(params.get("max_duration_sec", 60.0))
            max_buf = int(params.get("max_buffer_samples", SENSOR_MAX_BUFFER_SAMPLES))

            exe = self._resolve_executable("termux-sensor")
            if not exe:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error="termux-sensor companion binary not available",
                    error_code="TRANSPORT_UNAVAILABLE",
                )

            raw_list = self._cached_raw_sensors
            if raw_list is None:
                raw_list, err = self._discover_raw_sensors(timeout_sec=min(3.0, timeout_sec))
                if raw_list is None:
                    return BridgeProtocol.create_response(
                        request=request,
                        success=False,
                        status=BridgePermissionState.UNAVAILABLE.value,
                        error=err or "Sensor discovery failed before start",
                        error_code="SENSOR_DISCOVERY_FAILED",
                    )

            sensor_name = resolve_android_sensor_name(raw_list, sensor_type)
            if not sensor_name:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Sensor '{sensor_type}' is not physically available on this device",
                    error_code="SENSOR_UNAVAILABLE",
                )

            with self._sensor_lock:
                # Reap any expired sessions first
                for sid, sess in list(self._sensor_sessions.items()):
                    if sess.is_expired():
                        sess.stop("Maximum duration exceeded")

                active_sessions = [s for s in self._sensor_sessions.values() if s.state == "ACTIVE"]
                if len(active_sessions) >= SENSOR_MAX_CONCURRENT_SESSIONS:
                    return BridgeProtocol.create_response(
                        request=request,
                        success=False,
                        status=BridgePermissionState.ERROR.value,
                        error=f"Concurrent sensor session limit reached ({SENSOR_MAX_CONCURRENT_SESSIONS})",
                        error_code="RESOURCE_LIMIT_EXCEEDED",
                    )

                session_id = f"nsess-{uuid.uuid4().hex[:10]}"
                sess = _CompanionSensorSession(
                    session_id=session_id,
                    sensor_type=sensor_type,
                    sensor_name=sensor_name,
                    termux_sensor_exe=exe,
                    requested_interval_ms=req_ms,
                    actual_interval_ms=actual_ms,
                    max_duration_sec=max_dur,
                    max_buffer_samples=max_buf,
                )
                self._sensor_sessions[session_id] = sess

            sess.start()
            return BridgeProtocol.create_response(
                request=request,
                success=True,
                status=BridgePermissionState.READY.value,
                data={"session": sess.to_status_dict()},
            )

        if op == "SENSOR_READ":
            session_id = params.get("session_id", "")
            max_samples = int(params.get("max_samples", 10))
            with self._sensor_lock:
                sess = self._sensor_sessions.get(session_id)

            if not sess:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Sensor session '{session_id}' not found",
                    error_code="SESSION_NOT_FOUND",
                )

            if sess.is_expired():
                sess.stop("Maximum duration exceeded")
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Sensor session '{session_id}' expired",
                    error_code="SESSION_EXPIRED",
                )

            readings = sess.read_samples(max_samples=max_samples, wait_timeout=min(2.5, timeout_sec))
            return BridgeProtocol.create_response(
                request=request,
                success=True,
                status=BridgePermissionState.READY.value,
                data={
                    "session_id": session_id,
                    "sensor_type": sess.sensor_type,
                    "readings": readings,
                    "session": sess.to_status_dict(),
                },
            )

        if op == "SENSOR_STOP":
            session_id = params.get("session_id", "")
            reason = str(params.get("reason", "Requested stop"))
            with self._sensor_lock:
                sess = self._sensor_sessions.get(session_id)

            if not sess:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Sensor session '{session_id}' not found",
                    error_code="SESSION_NOT_FOUND",
                )

            sess.stop(reason)
            return BridgeProtocol.create_response(
                request=request,
                success=True,
                status=BridgePermissionState.READY.value,
                data={"session": sess.to_status_dict()},
            )

        if op == "SENSOR_STATUS":
            session_id = params.get("session_id")
            with self._sensor_lock:
                if session_id:
                    sess = self._sensor_sessions.get(session_id)
                    if not sess:
                        return BridgeProtocol.create_response(
                            request=request,
                            success=False,
                            status=BridgePermissionState.UNAVAILABLE.value,
                            error=f"Sensor session '{session_id}' not found",
                            error_code="SESSION_NOT_FOUND",
                        )
                    return BridgeProtocol.create_response(
                        request=request,
                        success=True,
                        status=BridgePermissionState.READY.value,
                        data={"session": sess.to_status_dict()},
                    )
                sessions = [s.to_status_dict() for s in self._sensor_sessions.values()]
                return BridgeProtocol.create_response(
                    request=request,
                    success=True,
                    status=BridgePermissionState.READY.value,
                    data={"sessions": sessions},
                )

        return BridgeProtocol.create_response(
            request=request,
            success=False,
            status=BridgePermissionState.ERROR.value,
            error=f"Unhandled sensor operation: {op}",
            error_code="OPERATION_UNSUPPORTED",
        )

    def shutdown(self) -> None:
        self._state = BridgeState.SHUTDOWN
        with self._sensor_lock:
            sessions = list(self._sensor_sessions.values())
        for sess in sessions:
            try:
                sess.stop("Transport shutdown")
            except Exception:
                pass
        self._kill_orphaned_helpers()

    def _resolve_executable(self, name: str) -> Optional[str]:
        """Finds executable in PATH or standard Termux bin."""
        import shutil
        found = shutil.which(name)
        if found:
            return found
        direct = Path("/data/data/com.termux/files/usr/bin") / name
        if direct.exists() and os.access(str(direct), os.X_OK):
            return str(direct)
        return None

    def _resolve_command_for_operation(self, capability: str, operation: str, args: Dict[str, Any]) -> Optional[list]:
        """Translates capability operation to corresponding native companion execution."""
        cap_l = capability.lower()
        op_l = operation.lower()

        if cap_l in ("battery", "power.battery", "power"):
            if op_l in ("get_status", "capability.read", "get_battery_status"):
                exe = self._resolve_executable("termux-battery-status")
                return [exe] if exe else None

        if cap_l in ("thermals", "thermal", "power.thermal"):
            if op_l in ("get_status", "capability.read", "get_thermal_status"):
                exe = self._resolve_executable("termux-battery-status")
                return [exe] if exe else None

        return None

    def _kill_orphaned_helpers(self) -> None:
        """Terminates any hung background termux-api/sensor helper processes and triggers cleanup."""
        try:
            exe = self._resolve_executable("termux-sensor")
            if exe:
                subprocess.run([exe, "-c"], capture_output=True, timeout=1.5)
        except Exception:
            pass
        try:
            subprocess.run(["pkill", "-9", "-f", "termux-api"], capture_output=True, timeout=1.0)
        except Exception:
            pass


class LocalSocketTransport(BaseBridgeTransport):
    """
    Loopback IPC transport to a dedicated native Android bridge service on 127.0.0.1.
    Strictly bound to 127.0.0.1 (loopback only, zero public exposure).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout_sec: float = 3.0):
        self.host = host
        self.port = port
        self.timeout_sec = timeout_sec

    def is_available(self) -> bool:
        """Tests whether local bridge port is listening on loopback."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                res = s.connect_ex((self.host, self.port))
                return res == 0
        except Exception:
            return False

    def check_readiness(self) -> Tuple[bool, Optional[str]]:
        if not self.is_available():
            return False, f"Local socket bridge not reachable on {self.host}:{self.port}"
        return True, None

    def send_request(self, request: AndroidBridgeRequest, timeout_sec: Optional[float] = None) -> AndroidBridgeResponse:
        t_limit = timeout_sec if timeout_sec is not None else self.timeout_sec
        try:
            raw_bytes = BridgeProtocol.serialize_request(request)
        except Exception as pe:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.ERROR.value,
                error=f"Serialization failure: {pe}",
                error_code="SERIALIZATION_ERROR",
            )

        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(t_limit)
            s.connect((self.host, self.port))

            # Send payload with newline framing
            s.sendall(raw_bytes + b"\n")

            # Receive bounded response
            chunks = []
            total_bytes = 0
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > MAX_PAYLOAD_BYTES:
                    return BridgeProtocol.create_response(
                        request=request,
                        success=False,
                        status=BridgePermissionState.ERROR.value,
                        error="Response payload exceeded size limit",
                        error_code="PAYLOAD_TOO_LARGE",
                    )
                if b"\n" in chunk:
                    break

            raw_response = b"".join(chunks).decode("utf-8").strip()
            if not raw_response:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.ERROR.value,
                    error="Empty response from native socket service",
                    error_code="EMPTY_RESPONSE",
                )

            resp = BridgeProtocol.parse_response(raw_response)
            resp.transport_used = "local_socket"
            resp.fallback_used = False
            return resp

        except socket.timeout:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.UNAVAILABLE.value,
                error=f"Local socket request timed out after {t_limit:.1f}s",
                error_code="TIMEOUT",
            )
        except Exception as e:
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.ERROR.value,
                error=f"Local socket communication failure: {e}",
                error_code="SOCKET_ERROR",
            )
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def shutdown(self) -> None:
        pass


class CompositeAndroidTransport(BaseBridgeTransport):
    """
    Tiered transport manager:
    Prefers high-performance LocalSocketTransport if native Android Bridge Service is active;
    uses AndroidCompanionTransport only for supported non-native-only operations with explicit
    fallback reporting.
    Tracks session affinity so SENSOR_READ/SENSOR_STOP route to the originating transport.
    """

    NATIVE_ONLY_OPERATIONS = frozenset({
        "PING",
        "GET_DIAGNOSTICS",
        "BRIDGE_DIAGNOSTICS",
        "RECORDING_DISCOVER",
        "START_RECORDING",
        "RECORDING_STATUS",
        "STOP_RECORDING",
        "RECORDING_READ",
        "RECORDING_CLEANUP",
    })

    def __init__(
        self,
        socket_transport: Optional[BaseBridgeTransport] = None,
        companion_transport: Optional[BaseBridgeTransport] = None,
        socket_port: int = 8765,
        allow_companion_fallback: bool = True,
        require_native: bool = False,
    ):
        self.socket_transport = socket_transport or LocalSocketTransport(port=socket_port)
        self.companion_transport = companion_transport or AndroidCompanionTransport()
        self.allow_companion_fallback = allow_companion_fallback
        self.require_native = require_native
        self._session_transports: Dict[str, BaseBridgeTransport] = {}
        self.last_transport_used: Optional[str] = None
        self.last_fallback_used: bool = False
        self.last_fallback_reason: Optional[str] = None
        self.fallback_count: int = 0

    def is_available(self) -> bool:
        if self.require_native or not self.allow_companion_fallback:
            return self.socket_transport.is_available()
        return self.socket_transport.is_available() or self.companion_transport.is_available()

    def check_readiness(self) -> Tuple[bool, Optional[str]]:
        if self.socket_transport.is_available():
            return self.socket_transport.check_readiness()
        if self.require_native or not self.allow_companion_fallback:
            return False, "Native LocalSocketTransport unavailable and Termux fallback disabled"
        return self.companion_transport.check_readiness()

    @staticmethod
    def _tag_response(resp: Any, transport_used: str, fallback_used: bool, fallback_reason: Optional[str]) -> Any:
        if isinstance(resp, AndroidBridgeResponse):
            resp.transport_used = transport_used
            resp.fallback_used = fallback_used
            resp.fallback_reason = fallback_reason
        return resp

    def send_request(self, request: AndroidBridgeRequest, timeout_sec: Optional[float] = None) -> AndroidBridgeResponse:
        op_upper = str(request.operation or "").upper()
        # Route existing sensor session operations to originating transport
        if request.operation in ("SENSOR_READ", "SENSOR_STOP", "SENSOR_STATUS"):
            params = request.parameters or request.arguments or {}
            sid = params.get("session_id")
            if sid and sid in self._session_transports:
                target = self._session_transports[sid]
                if target.is_available():
                    resp = target.send_request(request, timeout_sec=timeout_sec)
                    is_comp = target is self.companion_transport
                    t_name = "termux_companion" if is_comp else "local_socket"
                    self.last_transport_used = t_name
                    self.last_fallback_used = is_comp
                    self.last_fallback_reason = "session_affinity_companion" if is_comp else None
                    self._tag_response(resp, t_name, is_comp, self.last_fallback_reason)
                    if request.operation == "SENSOR_STOP" and (not isinstance(resp, AndroidBridgeResponse) or resp.success):
                        self._session_transports.pop(sid, None)
                    return resp
                if request.operation == "SENSOR_STOP":
                    self._session_transports.pop(sid, None)
                # If the originating socket died mid-session, report cleanly (never cross-route)
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Originating transport {target.transport_type} became unavailable for session '{sid}'",
                    error_code="TRANSPORT_UNAVAILABLE",
                )

        fallback_reason: Optional[str] = None
        if self.socket_transport.is_available():
            resp = self.socket_transport.send_request(request, timeout_sec=timeout_sec)
            resp_ok = resp.success if isinstance(resp, AndroidBridgeResponse) else True
            if resp_ok:
                self.last_transport_used = "local_socket"
                self.last_fallback_used = False
                self.last_fallback_reason = None
                self._tag_response(resp, "local_socket", False, None)
                if request.operation == "SENSOR_START":
                    sid = (resp.data or {}).get("session", {}).get("session_id")
                    if sid:
                        self._session_transports[sid] = self.socket_transport
                return resp

            # For explicit domain errors or native-only ops / disabled fallback, return directly
            err_code = resp.error_code if isinstance(resp, AndroidBridgeResponse) else None
            if (
                err_code not in ("SOCKET_ERROR", "TRANSPORT_UNAVAILABLE", "TIMEOUT", "EMPTY_RESPONSE")
                or op_upper in self.NATIVE_ONLY_OPERATIONS
                or self.require_native
                or not self.allow_companion_fallback
            ):
                self.last_transport_used = "local_socket"
                self.last_fallback_used = False
                self.last_fallback_reason = None
                self._tag_response(resp, "local_socket", False, None)
                return resp
            fallback_reason = f"native_socket:{err_code}"
            logger.warning("Native socket transport failed (%s); falling back to Termux companion for %s", err_code, request.operation)
        else:
            if op_upper in self.NATIVE_ONLY_OPERATIONS or self.require_native or not self.allow_companion_fallback:
                return BridgeProtocol.create_response(
                    request=request,
                    success=False,
                    status=BridgePermissionState.UNAVAILABLE.value,
                    error=f"Native local socket transport unavailable for '{request.operation}'",
                    error_code="TRANSPORT_UNAVAILABLE",
                )
            fallback_reason = "native_socket_unavailable"
            logger.warning("Native socket transport unavailable; using Termux companion fallback for %s", request.operation)

        if not self.companion_transport.is_available():
            return BridgeProtocol.create_response(
                request=request,
                success=False,
                status=BridgePermissionState.UNAVAILABLE.value,
                error="Neither native socket transport nor Termux companion transport is available",
                error_code="TRANSPORT_UNAVAILABLE",
            )

        chosen = self.companion_transport
        resp = chosen.send_request(request, timeout_sec=timeout_sec)
        self.last_transport_used = "termux_companion"
        self.last_fallback_used = True
        self.last_fallback_reason = fallback_reason
        self.fallback_count += 1
        self._tag_response(resp, "termux_companion", True, fallback_reason)
        if request.operation == "SENSOR_START" and isinstance(resp, AndroidBridgeResponse) and resp.success:
            sid = (resp.data or {}).get("session", {}).get("session_id")
            if sid:
                self._session_transports[sid] = chosen
        return resp

    def shutdown(self) -> None:
        self._session_transports.clear()
        self.socket_transport.shutdown()
        self.companion_transport.shutdown()
