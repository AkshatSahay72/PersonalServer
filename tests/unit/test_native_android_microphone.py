"""
Unit tests for D4-E02: Native Android Bridge Microphone Capture, Protocol,
DeviceRuntime provider, HardwareCapabilityManager integration, and authenticated
HTTP routes (node-api + controller).
"""

import base64
import hashlib
import importlib.util
import io
import json
import struct
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import (
    AndroidBridgeRequest,
    AndroidBridgeResponse,
    BridgeState,
    MIC_MAX_CHUNK_BYTES,
    MIC_MAX_FILE_BYTES,
)
from agent.device_runtime.android.permission_bridge import AndroidPermissionBridge
from agent.device_runtime.android.protocol import BridgeProtocol, ProtocolError
from agent.device_runtime.android.providers.microphone import (
    AndroidMicrophoneProvider,
    validate_wav_file,
)
from agent.device_runtime.android.transport import BaseBridgeTransport
from agent.device_runtime.capability_registry import CapabilityState
from agent.device_runtime.permission_manager import PermissionManager, PermissionStatus
from agent.device_runtime.runtime import DeviceRuntime
from agent.hardware.manager import HardwareCapabilityManager
from agent.hardware.microphone import MicrophoneProvider


def _build_wav_bytes(sample_rate: int = 16000, duration_sec: float = 0.25) -> bytes:
    """Create valid PCM 16-bit mono WAV bytes in memory."""
    num_frames = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = b"".join(struct.pack("<h", (i * 123) % 3000 - 1500) for i in range(num_frames))
        wf.writeframes(frames)
    return buf.getvalue()


class _FakeBridgeTransport(BaseBridgeTransport):
    """Deterministic in-memory stand-in for BaseBridgeTransport."""

    def __init__(self):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.wav_bytes = _build_wav_bytes(16000, 0.3)
        self.wav_sha256 = hashlib.sha256(self.wav_bytes).hexdigest()
        self.permission = "GRANTED"
        self.recording_state = "IDLE"
        self.active_session: Optional[Dict[str, Any]] = None
        self.completed_recording: Optional[Dict[str, Any]] = None
        self.corrupt_chunk_sha = False
        self.corrupt_header = False

    def is_available(self) -> bool:
        return True

    def check_readiness(self) -> Tuple[bool, Optional[str]]:
        return True, None

    def shutdown(self) -> None:
        pass

    def send_request(self, request: AndroidBridgeRequest, timeout_sec: float = 3.0) -> AndroidBridgeResponse:
        operation = request.operation
        params = dict(request.parameters or {})
        self.calls.append((operation, params))

        if operation == "RECORDING_DISCOVER":
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={
                    "mic_available": True,
                    "permission": self.permission,
                    "state": self.recording_state,
                    "visible_indication": self.recording_state == "RECORDING",
                    "active_session": self.active_session,
                    "limits": {
                        "min_duration_sec": 1.0,
                        "max_duration_sec": 60.0,
                        "max_file_bytes": MIC_MAX_FILE_BYTES,
                    },
                },
            )

        if operation == "START_RECORDING":
            if self.permission != "GRANTED":
                return AndroidBridgeResponse(
                    capability=request.capability,
                    operation=operation,
                    request_id=request.request_id,
                    success=False,
                    error_code="PERMISSION_DENIED",
                    error_message="RECORD_AUDIO permission denied on Android device",
                )
            sid = params.get("session_id") or "rec_test_001"
            self.recording_state = "RECORDING"
            self.active_session = {
                "session_id": sid,
                "state": "RECORDING",
                "max_duration_sec": float(params.get("duration_sec", 15.0)),
                "sample_rate_hz": int(params.get("sample_rate_hz", 16000)),
                "elapsed_sec": 0.1,
                "remaining_sec": float(params.get("duration_sec", 15.0)) - 0.1,
            }
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={
                    "status": "RECORDING",
                    "visible_indication": True,
                    "session": dict(self.active_session),
                    "limits": {"max_duration_sec": 60.0, "max_file_bytes": MIC_MAX_FILE_BYTES},
                },
            )

        if operation == "RECORDING_STATUS":
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={
                    "mic_available": True,
                    "permission": self.permission,
                    "state": self.recording_state,
                    "visible_indication": self.recording_state == "RECORDING",
                    "active_session": self.active_session,
                    "recording": self.completed_recording,
                },
            )

        if operation == "STOP_RECORDING":
            sid = (self.active_session or {}).get("session_id") or params.get("session_id") or "rec_test_001"
            self.recording_state = "COMPLETED"
            self.active_session = None
            self.completed_recording = {
                "session_id": sid,
                "state": "COMPLETED",
                "available_for_transfer": True,
                "file_size_bytes": len(self.wav_bytes),
                "sha256": self.wav_sha256,
                "duration_sec": 0.3,
                "sample_rate_hz": 16000,
            }
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={
                    "status": "COMPLETED",
                    "visible_indication": False,
                    "stop_reason": params.get("reason", "Stopped"),
                    "recording": dict(self.completed_recording),
                },
            )

        if operation == "RECORDING_READ":
            payload = self.wav_bytes
            if self.corrupt_header:
                payload = b"NOPE" + payload[4:]
            offset = int(params.get("offset", 0))
            chunk_sz = int(params.get("length", MIC_MAX_CHUNK_BYTES))
            chunk = payload[offset: offset + chunk_sz]
            sha = "0" * 64 if self.corrupt_chunk_sha else hashlib.sha256(payload).hexdigest()
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={
                    "session_id": params.get("session_id"),
                    "offset": offset,
                    "bytes_read": len(chunk),
                    "total_bytes": len(payload),
                    "eof": (offset + len(chunk)) >= len(payload),
                    "sha256": sha,
                    "chunk_base64": base64.b64encode(chunk).decode("ascii"),
                },
            )

        if operation == "RECORDING_CLEANUP":
            sid = params.get("session_id")
            if sid and self.completed_recording and self.completed_recording.get("session_id") == sid:
                self.completed_recording = None
                self.recording_state = "IDLE"
            elif not sid:
                self.completed_recording = None
                self.active_session = None
                self.recording_state = "IDLE"
            return AndroidBridgeResponse(
                capability=request.capability,
                operation=operation,
                request_id=request.request_id,
                success=True,
                data={"deleted": True},
            )

        return AndroidBridgeResponse(
            capability=request.capability,
            operation=operation,
            request_id=request.request_id,
            success=False,
            error_code="UNKNOWN",
            error_message=operation,
        )


def _make_ready_bridge(transport: _FakeBridgeTransport) -> Tuple[NativeAndroidBridge, PermissionManager]:
    perm_mgr = PermissionManager()
    perm_bridge = AndroidPermissionBridge(permission_manager=perm_mgr)
    bridge = NativeAndroidBridge(transport=transport, permission_bridge=perm_bridge)
    bridge.initialize()
    return bridge, perm_mgr


# ---------------------------------------------------------------------------
# 1. Protocol Validation Tests
# ---------------------------------------------------------------------------

def test_protocol_validate_start_recording_requires_explicit_authorization():
    for bad_params in (
        {},
        {"authorized": False},
        {"authorized": "true"},
        {"authorized": True, "duration_sec": 0.5},
        {"authorized": True, "duration_sec": 120},
        {"authorized": True, "sample_rate_hz": 8000},
    ):
        with pytest.raises(ProtocolError):
            BridgeProtocol.validate_recording_request("START_RECORDING", bad_params)

    BridgeProtocol.validate_recording_request(
        "START_RECORDING",
        {"authorized": True, "duration_sec": 15, "sample_rate_hz": 16000, "session_id": "rec_valid_1"},
    )


def test_protocol_validate_recording_read_bounds_and_session_id():
    for bad_params in (
        {},
        {"session_id": "../evil"},
        {"session_id": "rec_1", "offset": -1},
        {"session_id": "rec_1", "offset": MIC_MAX_FILE_BYTES + 1},
        {"session_id": "rec_1", "offset": 0, "length": 0},
    ):
        with pytest.raises(ProtocolError):
            BridgeProtocol.validate_recording_request("RECORDING_READ", bad_params)

    validated = BridgeProtocol.validate_recording_request(
        "RECORDING_READ",
        {"session_id": "rec_1", "offset": 0, "length": MIC_MAX_CHUNK_BYTES + 5000},
    )
    assert validated["length"] == MIC_MAX_CHUNK_BYTES


# ---------------------------------------------------------------------------
# 2. NativeAndroidBridge Microphone Transfer & Permission Sync Tests
# ---------------------------------------------------------------------------

def test_bridge_discover_syncs_record_audio_permission():
    transport = _FakeBridgeTransport()
    bridge, perm_mgr = _make_ready_bridge(transport)

    transport.permission = "PERMISSION_REQUIRED"
    resp1 = bridge.discover_microphone()
    assert resp1.success
    assert perm_mgr.get("android.permission.RECORD_AUDIO").status == PermissionStatus.PROMPT_REQUIRED

    transport.permission = "PERMISSION_DENIED"
    bridge.discover_microphone()
    assert perm_mgr.get("android.permission.RECORD_AUDIO").status == PermissionStatus.DENIED

    transport.permission = "GRANTED"
    bridge.discover_microphone()
    assert perm_mgr.get("android.permission.RECORD_AUDIO").status == PermissionStatus.GRANTED


def test_bridge_transfer_recording_multichunk_and_integrity_checks(tmp_path: Path):
    transport = _FakeBridgeTransport()
    # Force multi-chunk transfer (> 24 KB)
    transport.wav_bytes = _build_wav_bytes(16000, 1.0)  # ~32,044 bytes
    transport.wav_sha256 = hashlib.sha256(transport.wav_bytes).hexdigest()
    bridge, _ = _make_ready_bridge(transport)

    dest = tmp_path / "rec_multi.wav"
    res = bridge.transfer_recording("rec_multi", dest, cleanup_remote=True)
    assert res["file_size_bytes"] == len(transport.wav_bytes)
    assert res["sha256"] == transport.wav_sha256
    assert res["remote_cleaned"] is True
    assert dest.read_bytes() == transport.wav_bytes

    # SHA-256 mismatch must fail and not write dest file
    transport.corrupt_chunk_sha = True
    bad_dest = tmp_path / "rec_bad_sha.wav"
    with pytest.raises(RuntimeError, match="SHA-256"):
        bridge.transfer_recording("rec_bad_sha", bad_dest)
    assert not bad_dest.exists()

    # Corrupted RIFF header must fail even if hash matches
    transport.corrupt_chunk_sha = False
    transport.corrupt_header = True
    bad_hdr_dest = tmp_path / "rec_bad_hdr.wav"
    with pytest.raises(RuntimeError, match="RIFF/WAVE"):
        bridge.transfer_recording("rec_bad_hdr", bad_hdr_dest)
    assert not bad_hdr_dest.exists()


# ---------------------------------------------------------------------------
# 3. AndroidMicrophoneProvider & DeviceRuntime Tests
# ---------------------------------------------------------------------------

def test_android_microphone_provider_lifecycle_staging_and_retention(tmp_path: Path):
    transport = _FakeBridgeTransport()
    bridge, _ = _make_ready_bridge(transport)
    provider = AndroidMicrophoneProvider(
        bridge=bridge,
        staging_dir=tmp_path / "staging",
        retention_sec=60.0,
        max_retained_files=2,
    )

    # Discover permission denied vs ready
    transport.permission = "PERMISSION_DENIED"
    desc_denied = provider.discover()
    assert desc_denied.state == CapabilityState.PERMISSION_DENIED

    transport.permission = "GRANTED"
    desc_ready = provider.discover()
    assert desc_ready.state == CapabilityState.READY

    # Unauthorized start blocked cleanly
    unauth = provider.start_recording(authorized=False)
    assert unauth["success"] is False

    # Start -> Status -> Stop -> Staged WAV validation
    started = provider.start_recording(duration_sec=10.0, sample_rate_hz=16000, authorized=True, session_id="rec_s1")
    assert started["success"] is True
    assert started["visible_indication"] is True

    st = provider.get_recording_status("rec_s1")
    assert st["state"] == "RECORDING"
    assert st["visible_indication"] is True

    stopped = provider.stop_recording("rec_s1", reason="Unit test complete")
    assert stopped["success"] is True
    assert stopped["visible_indication"] is False
    assert stopped["transfer"]["file_size_bytes"] == len(transport.wav_bytes)

    wav_path, meta = provider.get_staged_recording_file("rec_s1")
    assert wav_path is not None and wav_path.exists()
    wav_info = validate_wav_file(wav_path)
    assert wav_info["sample_rate_hz"] == 16000
    assert wav_info["channels"] == 1
    assert meta["sha256"] == transport.wav_sha256

    # Retention max_retained_files=2 prunes oldest when 3rd recording is staged
    for idx in (2, 3):
        sid = f"rec_s{idx}"
        provider.start_recording(duration_sec=5.0, authorized=True, session_id=sid)
        provider.stop_recording(sid)
        time.sleep(0.01)

    p1, _ = provider.get_staged_recording_file("rec_s1")
    p3, _ = provider.get_staged_recording_file("rec_s3")
    assert p1 is None
    assert p3 is not None and p3.exists()

    # Auto-staging during get_recording_status if Android timed out capture on its own
    transport.recording_state = "COMPLETED"
    transport.active_session = None
    transport.completed_recording = {
        "session_id": "rec_auto_timeout",
        "state": "COMPLETED",
        "available_for_transfer": True,
        "file_size_bytes": len(transport.wav_bytes),
        "sha256": transport.wav_sha256,
    }
    auto_st = provider.get_recording_status("rec_auto_timeout")
    assert auto_st["staged_recording"] is not None
    assert auto_st["staged_recording"]["session_id"] == "rec_auto_timeout"

    # cleanup_all clears staging and triggers remote cleanup
    provider.cleanup_all("Shutdown")
    assert any(op == "RECORDING_CLEANUP" for op, _ in transport.calls)
    assert provider.get_staged_recording_file("rec_auto_timeout")[0] is None


def test_device_runtime_registers_microphone_provider(tmp_path: Path):
    transport = _FakeBridgeTransport()
    bridge, perm_mgr = _make_ready_bridge(transport)

    runtime = DeviceRuntime()
    runtime._permissions = perm_mgr
    runtime._android_bridge = bridge
    mic_provider = AndroidMicrophoneProvider(bridge=bridge, staging_dir=tmp_path / "rt_staging")
    runtime.register_provider(mic_provider)

    assert runtime.get_provider("microphone") is mic_provider


# ---------------------------------------------------------------------------
# 4. HardwareCapabilityManager & MicrophoneProvider Integration Tests
# ---------------------------------------------------------------------------

def test_hardware_capability_manager_microphone_operations_and_stop_all(tmp_path: Path):
    transport = _FakeBridgeTransport()
    bridge, _ = _make_ready_bridge(transport)
    android_mic = AndroidMicrophoneProvider(bridge=bridge, staging_dir=tmp_path / "hw_staging")
    android_mic.discover()

    mic = MicrophoneProvider()
    with patch.object(mic, "_get_native_provider", return_value=android_mic):
        avail, err = mic.is_available()
        assert avail is True and err is None
        mgr = HardwareCapabilityManager()
        mgr.microphone = mic

        res_start = mgr.execute_operation(
            "START_RECORDING",
            {"authorized": True, "duration_sec": 12, "sample_rate_hz": 16000, "session_id": "rec_mgr_1"},
        )
        assert res_start["success"] is True

        res_status = mgr.execute_operation("RECORDING_STATUS", {"session_id": "rec_mgr_1"})
        assert res_status["status"]["state"] == "RECORDING"

        res_stop = mgr.execute_operation("STOP_RECORDING", {"session_id": "rec_mgr_1", "reason": "Test"})
        assert res_stop["success"] is True
        stop_inner = res_stop["result"]
        assert stop_inner["recording"]["session_id"] == "rec_mgr_1" or stop_inner["transfer"]["session_id"] == "rec_mgr_1"

        wav_path, meta = mic.get_recording_file("rec_mgr_1")
        assert wav_path is not None and wav_path.exists()
        assert meta["sha256"] == transport.wav_sha256

        # stop_all triggers native cleanup
        mgr.stop_all(reason="Emergency stop")
        assert mic.get_recording_file("rec_mgr_1")[0] is None


# ---------------------------------------------------------------------------
# 5. Controller & Node-API Authenticated Recording Routes
# ---------------------------------------------------------------------------

class _MockControllerHandler:
    """Minimal HTTP handler stub for exercising controller/http/node_routes.py."""

    def __init__(self, authenticated: bool = False, token: str = ""):
        self._authenticated = authenticated
        self._token = token
        self.headers = {"Host": "127.0.0.1:8080"}
        self.client_address = ("127.0.0.1", 55555)
        self.responses: List[Tuple[int, Dict[str, Any]]] = []
        self.raw_status: Optional[int] = None
        self.raw_headers: Dict[str, str] = {}
        self.wfile = io.BytesIO()

    def is_authenticated_admin_or_node(self) -> bool:
        return self._authenticated

    def parse_auth_token(self) -> str:
        return self._token

    def get_auth_token(self) -> str:
        return self._token

    def is_leader_or_standalone(self) -> bool:
        return True

    def send_json(self, code: int, payload: Dict[str, Any]) -> None:
        self.responses.append((code, payload))

    def send_response(self, code: int) -> None:
        self.raw_status = code

    def send_header(self, k: str, v: str) -> None:
        self.raw_headers[k] = v

    def end_headers(self) -> None:
        pass


def test_controller_microphone_routes_auth_permission_and_download(tmp_path: Path):
    from controller.http.node_routes import handle_node_get, handle_node_post

    wav_bytes = _build_wav_bytes(16000, 0.2)
    wav_sha = hashlib.sha256(wav_bytes).hexdigest()
    wav_file = tmp_path / "rec_ctrl_1.wav"
    wav_file.write_bytes(wav_bytes)
    meta = {"session_id": "rec_ctrl_1", "file_name": "rec_ctrl_1.wav", "sha256": wav_sha}

    mock_mgr = MagicMock()
    mock_mgr.microphone.get_recording_file.return_value = (wav_file, meta)
    mock_mgr.execute_operation.return_value = {"success": True, "status": "RECORDING"}

    nodes_db = {
        "nodes": {
            "node-v2334": {
                "node_id": "node-v2334",
                "ip": "100.64.0.10",
                "status": "ONLINE",
                "capability_permissions": {"microphone": "PENDING"},
            }
        }
    }

    with patch("controller.http.node_routes.load_nodes_db", return_value=nodes_db), \
         patch("controller.http.node_routes.get_or_create_enrollment_token", return_value="enroll-secret"), \
         patch("agent.hardware.manager.HardwareCapabilityManager.get_instance", return_value=mock_mgr):

        # 1. Unauthenticated recording download blocked -> 401
        h_unauth = _MockControllerHandler(authenticated=False, token="wrong")
        assert handle_node_get(h_unauth, "/api/nodes/local/hardware/microphone/recording", {"session_id": ["rec_ctrl_1"]})
        assert h_unauth.responses[0][0] == 401

        # 2. Path traversal session_id rejected -> 400
        h_bad_sid = _MockControllerHandler(authenticated=True)
        assert handle_node_get(h_bad_sid, "/api/nodes/local/hardware/microphone/recording", {"session_id": ["../evil"]})
        assert h_bad_sid.responses[0][0] == 400

        # 3. Authenticated local recording download -> 200 audio/wav + SHA256
        h_ok = _MockControllerHandler(authenticated=True)
        assert handle_node_get(h_ok, "/api/nodes/local/hardware/microphone/recording", {"session_id": ["rec_ctrl_1"], "download": ["1"]})
        assert h_ok.raw_status == 200
        assert h_ok.raw_headers["Content-Type"] == "audio/wav"
        assert h_ok.raw_headers["X-Recording-Sha256"] == wav_sha
        assert h_ok.raw_headers["Cache-Control"] == "no-store"
        assert "attachment" in h_ok.raw_headers["Content-Disposition"]
        assert h_ok.wfile.getvalue() == wav_bytes

        # 4. Oversized recording blocked -> 413
        oversized = tmp_path / "huge.wav"
        oversized.write_bytes(b"RIFF" + b"\x00" * (MIC_MAX_FILE_BYTES + 16))
        mock_mgr.microphone.get_recording_file.return_value = (oversized, {"session_id": "huge", "sha256": "x"})
        h_huge = _MockControllerHandler(authenticated=True)
        assert handle_node_get(h_huge, "/api/nodes/local/hardware/microphone/recording", {"session_id": ["huge"]})
        assert h_huge.responses[0][0] == 413

        # 5. Controller capability_permissions["microphone"] != APPROVED blocked -> 403
        h_perm = _MockControllerHandler(authenticated=True)
        assert handle_node_post(
            h_perm,
            "/api/nodes/node-v2334/hardware",
            {"operation": "START_RECORDING", "params": {"authorized": True, "duration_sec": 10}},
            token="",
        )
        assert h_perm.responses[0][0] == 403

        # 6. START_RECORDING without authorized=True rejected -> 400
        h_no_authz = _MockControllerHandler(authenticated=True)
        assert handle_node_post(
            h_no_authz,
            "/api/nodes/local/hardware",
            {"operation": "START_RECORDING", "params": {"duration_sec": 10}},
            token="",
        )
        assert h_no_authz.responses[0][0] == 400


def test_node_api_microphone_endpoints_require_explicit_auth(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "node_api_app_mic_test",
        str(Path(__file__).resolve().parents[2] / "services" / "node-api" / "app.py"),
    )
    node_api_mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(node_api_mod)

    wav_bytes = _build_wav_bytes(16000, 0.2)
    wav_sha = hashlib.sha256(wav_bytes).hexdigest()
    wav_file = tmp_path / "rec_nodeapi.wav"
    wav_file.write_bytes(wav_bytes)

    mock_mgr = MagicMock()
    mock_mgr.microphone.get_recording_file.return_value = (
        wav_file,
        {"session_id": "rec_nodeapi", "file_name": "rec_nodeapi.wav", "sha256": wav_sha},
    )
    mock_mgr.execute_operation.return_value = {"success": True, "status": "RECORDING"}

    server = node_api_mod.create_node_api_server(host="127.0.0.1", port=0)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        with patch.object(node_api_mod, "get_auth_token", return_value="node-secret-token"), \
             patch.object(node_api_mod, "get_controller_auth_token", return_value="ctrl-secret-token"), \
             patch.object(HardwareCapabilityManager, "get_instance", return_value=mock_mgr):

            # Unauthenticated GET recording on loopback MUST fail with 401 (require_explicit=True)
            with pytest.raises(urllib.error.HTTPError) as exc_get:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/hardware/microphone/recording?session_id=rec_nodeapi", timeout=5)
            assert exc_get.value.code == 401

            # Unauthenticated POST START_RECORDING on loopback MUST fail with 401
            req_post = urllib.request.Request(
                f"http://127.0.0.1:{port}/hardware",
                data=json.dumps({"operation": "START_RECORDING", "params": {"authorized": True, "duration_sec": 5}}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_post:
                urllib.request.urlopen(req_post, timeout=5)
            assert exc_post.value.code == 401

            # Authenticated GET recording returns WAV + SHA256
            req_auth_get = urllib.request.Request(
                f"http://127.0.0.1:{port}/hardware/microphone/recording?session_id=rec_nodeapi",
                headers={"Authorization": "Bearer node-secret-token"},
            )
            with urllib.request.urlopen(req_auth_get, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Content-Type") == "audio/wav"
                assert resp.headers.get("X-Recording-Sha256") == wav_sha
                assert resp.read() == wav_bytes

            # Authenticated POST START_RECORDING succeeds
            req_auth_post = urllib.request.Request(
                f"http://127.0.0.1:{port}/hardware",
                data=json.dumps({"operation": "START_RECORDING", "params": {"authorized": True, "duration_sec": 5}}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer node-secret-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(req_auth_post, timeout=5) as resp:
                body = json.loads(resp.read().decode())
                assert resp.status == 200
                assert body["success"] is True
    finally:
        server.shutdown()
        server.server_close()
