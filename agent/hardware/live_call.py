"""
LiveCallProvider for PersonalServer (E07).
Unified on-demand camera, microphone, and speaker two-way communication system.
Interfaces with Android Native Bridge via loopback IPC (127.0.0.1:8765) and
separate binary media stream channel (127.0.0.1:8766).
"""

import os
import sys
import time
import json
import uuid
import struct
import socket
import base64
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List, Callable

from agent.hardware.base import SessionState, HardwareSession, StreamQuality, find_termux_tool

logger = logging.getLogger("agent.hardware.live_call")

MAGIC_PSMS = b"PSMS"
TYPE_VIDEO_FRAME = 0x01
TYPE_AUDIO_UPSTREAM = 0x02
TYPE_AUDIO_DOWNSTREAM = 0x03
TYPE_HEARTBEAT = 0x04
TYPE_CONTROL = 0x05

DEFAULT_MEDIA_PORT = 8766


class LiveCallResult(dict):
    """Result dictionary that allows unpacking as (ok, dict) or indexing as dict."""
    def __iter__(self):
        yield self.get("success", False)
        yield dict(self)


class LiveCallSession(HardwareSession):
    """Tracks an active bidirectional video/audio live call session."""

    def __init__(
        self,
        session_id: str,
        camera_id: str = "0",
        quality: Optional[StreamQuality] = None,
        timeout_sec: float = 300.0,
        audio_enabled: bool = True,
        speaker_enabled: bool = True
    ):
        super().__init__(
            session_id=session_id,
            capability="live_call",
            parameters={
                "camera_id": camera_id,
                "audio_enabled": audio_enabled,
                "speaker_enabled": speaker_enabled
            },
            timeout_sec=timeout_sec
        )
        self.camera_id = camera_id
        self.quality = quality or StreamQuality()
        self.audio_enabled = audio_enabled
        self.speaker_enabled = speaker_enabled
        self.webrtc_state = "IDLE"
        self.ice_candidates: List[str] = []
        self.tailscale_ip: Optional[str] = None
        self.frames_received = 0
        self.audio_packets_received = 0
        self.speaker_packets_sent = 0
        self.speaker_bytes_sent = 0
        self.mic_muted = False
        self.speaker_muted = False

    def check_watchdog_expiration(self) -> bool:
        return self.is_expired()

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "camera_id": self.camera_id,
            "quality": self.quality.to_dict(),
            "audio_enabled": self.audio_enabled,
            "speaker_enabled": self.speaker_enabled,
            "webrtc_state": self.webrtc_state,
            "ice_candidates": self.ice_candidates,
            "tailscale_ip": self.tailscale_ip,
            "frames_received": self.frames_received,
            "audio_packets_received": self.audio_packets_received,
            "speaker_packets_sent": self.speaker_packets_sent,
            "mic_muted": self.mic_muted,
            "speaker_muted": self.speaker_muted
        })
        return d


class LiveCallProvider:
    """
    On-demand provider for real-time bidirectional audio/video communication.
    Strictly on-demand: camera, mic, and speaker are never initialized at startup
    and are cleanly released upon session termination.
    """

    def __init__(self, bridge_host: str = "127.0.0.1", bridge_port: int = 8765, media_port: int = 8766, **kwargs):
        self.bridge_host = bridge_host
        self.bridge_port = bridge_port
        self.media_port = media_port
        self.current_session: Optional[LiveCallSession] = None
        self._lock = threading.RLock()

        # Media streaming state
        self._media_socket: Optional[socket.socket] = None
        self._media_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._latest_jpeg_frame: Optional[bytes] = None
        self._frame_timestamp: float = 0.0
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._audio_buffer = bytearray()
        self._audio_buffer_lock = threading.Lock()
        self._audio_sample_rate: int = 16000
        self._audio_channels: int = 1
        self._frame_listeners: List[Callable[[bytes], None]] = []
        self._audio_listeners: List[Callable[[bytes, int, int], None]] = []
        self._last_frame_consumed_time: float = 0.0
        self._watchdog_thread: Optional[threading.Thread] = None

    def mark_frame_consumed(self):
        """Records client frame consumption activity to prevent inactivity watchdog timeout."""
        self._last_frame_consumed_time = time.time()

    def is_active(self) -> bool:
        with self._lock:
            if not (self.current_session and self.current_session.state == SessionState.ACTIVE):
                return False
            if self._media_thread and not self._media_thread.is_alive():
                return False
            return True

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "cameras": ["front", "back"],
            "supported_resolutions": ["480p", "720p"],
            "supported_fps": [15, 24, 30],
            "audio_supported": True,
            "speaker_supported": True,
            "two_way_audio": True
        }

    def _check_session_watchdog(self):
        with self._lock:
            if self.current_session and self.current_session.is_expired():
                self.stop_session("Session timeout reached")

    def is_available(self) -> Tuple[bool, Optional[str]]:
        """Checks if native bridge or host simulation is available."""
        # Probe loopback native bridge socket
        try:
            with socket.create_connection((self.bridge_host, self.bridge_port), timeout=0.2):
                return True, None
        except Exception:
            pass

        # In CI or host development, simulated live call is supported
        return True, "Simulated host provider available"

    def _ensure_bridge_awake(self):
        """Probes port 8765. If unreachable on Android, triggers BridgeActivity via 'am' to wake the service."""
        try:
            with socket.create_connection((self.bridge_host, self.bridge_port), timeout=0.3):
                return
        except Exception:
            pass

        # Try to wake up BridgeActivity via Android 'am' command if on Android
        try:
            import subprocess
            am_bin = find_termux_tool("am") or "am"
            subprocess.run(
                [am_bin, "start", "-n", "com.personalserver.bridge/.BridgeActivity"],
                capture_output=True,
                timeout=4
            )
            for _ in range(12):
                time.sleep(0.25)
                try:
                    with socket.create_connection((self.bridge_host, self.bridge_port), timeout=0.2):
                        logger.info("Successfully woke Native Bridge on %s:%s", self.bridge_host, self.bridge_port)
                        return
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Failed waking native bridge: %s", e)

    def _call_native_bridge(self, operation: str, parameters: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
        """Sends a JSON control request to LocalBridgeServer on 127.0.0.1:8765."""
        self._ensure_bridge_awake()
        req = {
            "version": 1,
            "protocol_version": 1,
            "capability": "live_call",
            "operation": operation,
            "operation_id": f"call-op-{uuid.uuid4().hex[:8]}",
            "request_id": f"call-req-{uuid.uuid4().hex[:8]}",
            "parameters": parameters or {},
            "data": parameters or {}
        }
        try:
            with socket.create_connection((self.bridge_host, self.bridge_port), timeout=6.0) as s:
                s.settimeout(6.0)
                payload = (json.dumps(req) + "\n").encode("utf-8")
                s.sendall(payload)
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        break
                if not buf:
                    return False, {"error": "Empty response from Native Bridge"}
                res = json.loads(buf.decode("utf-8").strip())
                return bool(res.get("success", False)), res.get("data", res)
        except Exception as e:
            return False, {"error": f"Native bridge IPC error: {e}"}

    def start_session(
        self,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0,
        **kwargs
    ) -> LiveCallResult:
        """
        Starts an on-demand live call session.
        Parameters:
            - camera_id: '0' (back) or '1' (front), default '0'
            - audio_enabled: boolean, default True
            - speaker_enabled: boolean, default True
            - quality: preset ('720p', '480p', '360p'), width, height, fps
            - sdp_offer: WebRTC SDP offer from client browser
        """
        with self._lock:
            params = dict(parameters or {})
            if kwargs:
                params.update(kwargs)
            if "camera" in params and "camera_id" not in params:
                params["camera_id"] = "1" if params["camera"] == "front" else "0"
            if "with_mic" in params and "audio_enabled" not in params:
                params["audio_enabled"] = bool(params["with_mic"])
            if "with_speaker" in params and "speaker_enabled" not in params:
                params["speaker_enabled"] = bool(params["with_speaker"])

            camera_id = str(params.get("camera_id", "0"))
            quality = StreamQuality.from_dict(params)
            audio_enabled = bool(params.get("audio_enabled", True))
            speaker_enabled = bool(params.get("speaker_enabled", True))

            # 1. Prevent duplicate active sessions
            if self.current_session and self.current_session.state == SessionState.ACTIVE:
                if not self.current_session.is_expired():
                    return LiveCallResult({
                        "success": False,
                        "error": "SESSION_ALREADY_ACTIVE",
                        "error_code": "SESSION_ALREADY_ACTIVE",
                        "session": self.current_session.to_dict()
                    })
                else:
                    self.stop_session("Prior session timed out")

            session_id = f"call-{uuid.uuid4().hex[:8]}"

            session = LiveCallSession(
                session_id=session_id,
                camera_id=camera_id,
                quality=quality,
                timeout_sec=timeout_sec,
                audio_enabled=audio_enabled,
                speaker_enabled=speaker_enabled
            )
            session.state = SessionState.REQUESTED
            self.current_session = session
            with self._audio_buffer_lock:
                self._audio_buffer.clear()
            self._frame_event.clear()

            # 2. Try native Android bridge
            jpeg_quality = int(params.get("jpeg_quality", 50))
            bridge_params = {
                "session_id": session_id,
                "camera_id": camera_id,
                "width": quality.width,
                "height": quality.height,
                "fps": quality.fps,
                "jpeg_quality": jpeg_quality,
                "quality_percent": jpeg_quality,
                "audio_enabled": audio_enabled,
                "speaker_enabled": speaker_enabled,
                "timeout_sec": timeout_sec
            }

            ok, res = self._call_native_bridge("START_LIVE_CALL", bridge_params)
            err_code = str(res.get("error_code") or res.get("error") or "").upper()
            if not ok and "SESSION_ALREADY_ACTIVE" in err_code:
                logger.info("Prior bridge session active; sending STOP_LIVE_CALL reset")
                self._call_native_bridge("STOP_LIVE_CALL", {"reason": "Re-syncing"})
                time.sleep(0.3)
                ok, res = self._call_native_bridge("START_LIVE_CALL", bridge_params)

            is_native = ok and (res.get("status") == "ACTIVE" or res.get("success") is True)
            is_android = os.path.exists("/data/data/com.termux") or "com.termux" in os.environ.get("PREFIX", "")

            if is_native:
                self.media_port = int(res.get("media_port", DEFAULT_MEDIA_PORT))
                self._start_media_stream_client(self.media_port)
            elif not is_android:
                # Host/mock fallback for development or simulated tests (CI / Windows / Linux PC)
                logger.info("Using simulated live media generator (Native bridge not active: %s)", res.get("error"))
                self._start_simulated_stream_generator(quality)
            else:
                logger.error("Native bridge failed on Android device: %s", res.get("error"))
                session.state = SessionState.ERROR
                self.current_session = None
                return LiveCallResult({
                    "success": False,
                    "status": "ERROR",
                    "error": f"Native bridge failed to start camera: {res.get('error', 'unknown error')}",
                    "backend": "none"
                })

            session.state = SessionState.ACTIVE
            session.started_at = time.time()
            session.webrtc_state = "CONNECTED"
            self._last_frame_consumed_time = time.time()
            self._start_inactivity_watchdog()

            # 3. Generate WebRTC SDP answer and ICE candidates
            offer_sdp = params.get("sdp_offer", "")
            webrtc_data = self._generate_webrtc_negotiation(session_id, offer_sdp, camera_id, quality)
            session.tailscale_ip = webrtc_data.get("primary_ip")
            session.ice_candidates = webrtc_data.get("candidates", [])

            return LiveCallResult({
                "success": True,
                "status": "ACTIVE",
                "session_id": session_id,
                "session": session.to_dict(),
                "webrtc": webrtc_data,
                "backend": "android-native-bridge" if is_native else "simulated-test-stream"
            })

    def _start_inactivity_watchdog(self):
        """Starts a background monitor that auto-releases camera if no clients consume frames for >15s."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._inactivity_watchdog_loop,
            daemon=True,
            name="LiveCallInactivityWatchdog"
        )
        self._watchdog_thread.start()

    def _inactivity_watchdog_loop(self):
        while not self._stop_event.is_set():
            time.sleep(2.0)
            with self._lock:
                if not self.current_session or self.current_session.state != SessionState.ACTIVE:
                    break
                if self._last_frame_consumed_time > 0 and (time.time() - self._last_frame_consumed_time > 15.0):
                    logger.info("Inactivity watchdog: no consumers for 15s; auto-releasing camera to IDLE")
                    self.stop_session("Inactivity watchdog: no consumers for 15s")
                    break

    def stop_session(self, reason: str = "Requested by user/controller") -> LiveCallResult:
        """Terminates the live call session and guarantees release of all hardware resources."""
        with self._lock:
            self._last_frame_consumed_time = 0.0
            self._stop_event.set()

            # 1. ALWAYS stop native bridge session and release Camera2 hardware
            self._call_native_bridge("STOP_LIVE_CALL", {"reason": reason})

            # 2. Close media socket connection
            if self._media_socket:
                try:
                    self._media_socket.close()
                except Exception:
                    pass
                self._media_socket = None

            if self._media_thread and self._media_thread.is_alive():
                self._media_thread.join(timeout=1.0)
                self._media_thread = None

            with self._frame_lock:
                self._latest_jpeg_frame = None

            if not self.current_session:
                return LiveCallResult({"success": True, "status": "IDLE", "message": "No live call session was active"})

            session = self.current_session
            session.state = SessionState.IDLE
            session.stopped_at = time.time()
            session.webrtc_state = "CLOSED"
            data = session.to_dict()
            data["stop_reason"] = reason
            self.current_session = None

            logger.info("Live call session %s stopped cleanly (%s)", session.session_id, reason)
            return LiveCallResult({"success": True, "status": "STOPPED", "session": data})

    def get_status(self) -> Dict[str, Any]:
        """Returns the current live call session state, telemetry, and capabilities."""
        with self._lock:
            available, reason = self.is_available()
            if self.current_session and self.current_session.is_expired():
                self.stop_session("Session timeout reached")

            session_active = bool(self.current_session and self.current_session.state == SessionState.ACTIVE)
            facing = "none"
            mic_active = False
            speaker_active = False
            sid = None
            if self.current_session:
                sid = self.current_session.session_id
                if session_active:
                    facing = "front" if self.current_session.camera_id == "1" else "back"
                    mic_active = not self.current_session.mic_muted
                    speaker_active = not self.current_session.speaker_muted

            return {
                "available": available,
                "reason": reason,
                "session_active": session_active,
                "session_id": sid,
                "camera_facing": facing,
                "mic_active": mic_active,
                "speaker_active": speaker_active,
                "session": self.current_session.to_dict() if self.current_session else None,
                "has_frame": self._latest_jpeg_frame is not None,
                "frame_timestamp": self._frame_timestamp,
                "capabilities": self.get_capabilities()
            }

    def update_session(self, parameters: Optional[Dict[str, Any]] = None, **kwargs) -> LiveCallResult:
        """Toggles mute or switches camera."""
        with self._lock:
            if not self.current_session or self.current_session.state != SessionState.ACTIVE:
                return LiveCallResult({"success": False, "error": "No active live call session"})

            params = dict(parameters or {})
            if kwargs:
                params.update(kwargs)

            session = self.current_session
            if "camera" in params:
                session.camera_id = "1" if params["camera"] == "front" else "0"
                params["camera_id"] = session.camera_id
            if "camera_id" in params:
                session.camera_id = str(params["camera_id"])
                params["camera_id"] = session.camera_id
            if "with_mic" in params:
                session.mic_muted = not bool(params["with_mic"])
                params["mute_mic"] = session.mic_muted
            if "mute_mic" in params:
                session.mic_muted = bool(params["mute_mic"])
                params["mute_mic"] = session.mic_muted
            if "with_speaker" in params:
                session.speaker_muted = not bool(params["with_speaker"])
                params["mute_speaker"] = session.speaker_muted
            if "mute_speaker" in params:
                session.speaker_muted = bool(params["mute_speaker"])
                params["mute_speaker"] = session.speaker_muted

            # Propagate to native bridge
            self._call_native_bridge("UPDATE_LIVE_CALL", params)
            return LiveCallResult({"success": True, "session": session.to_dict()})

    def transmit_speaker_audio(self, pcm_base64: str, sample_rate: int = 16000, channels: int = 1) -> LiveCallResult:
        """Helper to decode base64 audio and send to speaker with accounting."""
        with self._lock:
            if not self.current_session or self.current_session.state != SessionState.ACTIVE:
                return LiveCallResult({"success": False, "error": "No active live call session"})
            try:
                raw_pcm = base64.b64decode(pcm_base64)
                ok = self.send_speaker_audio(raw_pcm, sample_rate=sample_rate, channels=channels)
                if ok and self.current_session:
                    self.current_session.speaker_bytes_sent += len(raw_pcm)
                return LiveCallResult({"success": ok, "bytes_received": len(raw_pcm)})
            except Exception as e:
                return LiveCallResult({"success": False, "error": str(e)})

    def send_speaker_audio(self, pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bool:
        """Sends downstream audio from browser microphone to phone speaker via loopback media channel."""
        with self._lock:
            if not self.current_session or self.current_session.state != SessionState.ACTIVE:
                return False
            if self.current_session.speaker_muted:
                return True

            self.current_session.speaker_packets_sent += 1

            if self._media_socket:
                try:
                    # Packet structure: MAGIC (4), Type (1), Length (4), Timestamp (8), Payload (2 bytes sr, 1 byte ch, pcm)
                    now_ms = int(time.time() * 1000)
                    meta = struct.pack(">HB", sample_rate, channels)
                    payload = meta + pcm_data
                    header = MAGIC_PSMS + bytes([TYPE_AUDIO_DOWNSTREAM]) + struct.pack(">IQ", len(payload), now_ms)
                    self._media_socket.sendall(header + payload)
                    return True
                except Exception as e:
                    logger.debug("Failed sending speaker audio to media socket: %s", e)
                    return False
            return True

    def get_latest_frame(self) -> Optional[bytes]:
        """Returns the most recent JPEG video frame."""
        self.mark_frame_consumed()
        with self._frame_lock:
            return self._latest_jpeg_frame

    def wait_for_next_frame(self, timeout: float = 0.5) -> Optional[bytes]:
        """Waits for next newly captured video frame or returns the latest available frame."""
        self._frame_event.wait(timeout=timeout)
        self._frame_event.clear()
        self.mark_frame_consumed()
        with self._frame_lock:
            return self._latest_jpeg_frame

    def get_latest_frame_with_timestamp(self) -> Tuple[Optional[bytes], float]:
        """Returns the most recent JPEG video frame and its arrival timestamp."""
        self.mark_frame_consumed()
        with self._frame_lock:
            return self._latest_jpeg_frame, self._frame_timestamp

    def get_audio_chunk(self, max_bytes: int = 16000) -> Tuple[bytes, int, int]:
        """Retrieves and clears pending microphone PCM audio chunks for browser playback."""
        with self._audio_buffer_lock:
            if not self._audio_buffer:
                return b"", self._audio_sample_rate, self._audio_channels
            chunk = bytes(self._audio_buffer[:max_bytes])
            self._audio_buffer = self._audio_buffer[len(chunk):]
            return chunk, self._audio_sample_rate, self._audio_channels

    # =========================================================================
    # Media Transport Reader (Client for 127.0.0.1:8766)
    # =========================================================================

    def _start_media_stream_client(self, port: int):
        self._stop_event.clear()
        self._media_thread = threading.Thread(
            target=self._media_client_loop,
            args=(port,),
            daemon=True,
            name="MediaStreamClientReader"
        )
        self._media_thread.start()

    def _media_client_loop(self, port: int):
        retries = 0
        sock = None
        while not self._stop_event.is_set() and retries < 15:
            try:
                sock = socket.create_connection((self.bridge_host, port), timeout=2.0)
                sock.settimeout(15.0)
                self._media_socket = sock
                logger.info("Connected to Native MediaStreamServer on port %d", port)
                break
            except Exception:
                retries += 1
                time.sleep(0.2)

        if not sock:
            logger.warning("Could not connect to MediaStreamServer on port %d; using fallback", port)
            return

        try:
            while not self._stop_event.is_set():
                magic = self._recv_exact(sock, 4)
                if magic != MAGIC_PSMS:
                    break

                type_byte = self._recv_exact(sock, 1)[0]
                length = struct.unpack(">I", self._recv_exact(sock, 4))[0]
                timestamp_ms = struct.unpack(">Q", self._recv_exact(sock, 8))[0]

                if length > 2 * 1024 * 1024:
                    break

                payload = self._recv_exact(sock, length)

                if type_byte == TYPE_VIDEO_FRAME and len(payload) >= 5:
                    width = struct.unpack(">H", payload[0:2])[0]
                    height = struct.unpack(">H", payload[2:4])[0]
                    codec = payload[4]
                    frame_bytes = payload[5:]

                    with self._frame_lock:
                        self._latest_jpeg_frame = frame_bytes
                        self._frame_timestamp = timestamp_ms / 1000.0
                    self._frame_event.set()

                    if self.current_session:
                        self.current_session.frames_received += 1

                elif type_byte == TYPE_AUDIO_UPSTREAM and len(payload) >= 3:
                    sample_rate = struct.unpack(">H", payload[0:2])[0]
                    channels = payload[2]
                    pcm_bytes = payload[3:]

                    if self.current_session:
                        self.current_session.audio_packets_received += 1

                    with self._audio_buffer_lock:
                        self._audio_sample_rate = sample_rate
                        self._audio_channels = channels
                        self._audio_buffer.extend(pcm_bytes)
                        if len(self._audio_buffer) > 96000:
                            self._audio_buffer = self._audio_buffer[-96000:]

                    for listener in self._audio_listeners:
                        try:
                            listener(pcm_bytes, sample_rate, channels)
                        except Exception:
                            pass
        except Exception as e:
            if not self._stop_event.is_set():
                logger.debug("Media client socket stream ended: %s", e)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
            self._media_socket = None
            if not self._stop_event.is_set() and self.current_session:
                logger.warning("Media stream connection lost unexpectedly; resetting session state")
                self.current_session.state = SessionState.IDLE

    def _recv_exact(self, sock: socket.socket, num_bytes: int) -> bytes:
        data = bytearray()
        while len(data) < num_bytes:
            packet = sock.recv(num_bytes - len(data))
            if not packet:
                raise ConnectionResetError("Connection closed while receiving media stream")
            data.extend(packet)
        return bytes(data)

    # =========================================================================
    # Simulated Frame Generator (Host / CI fallback)
    # =========================================================================

    def _start_simulated_stream_generator(self, quality: StreamQuality):
        self._stop_event.clear()
        self._media_thread = threading.Thread(
            target=self._simulated_generator_loop,
            args=(quality,),
            daemon=True,
            name="SimulatedMediaGenerator"
        )
        self._media_thread.start()

    def _simulated_generator_loop(self, quality: StreamQuality):
        # Generates a minimal valid JPEG header and payload for test verification
        # 1x1 or 2x2 solid color JPEG
        minimal_jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
            b"\xff\xc0\x00\x0b\x08\x00\x10\x00\x10\x01\x01\x11\x00\xff\xc4\x00\x1f"
            b"\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01"
            b"\x00\x00?\x00\xbf\x80\xff\xd9"
        )

        interval = 1.0 / max(1, quality.fps)
        while not self._stop_event.is_set():
            time.sleep(interval)
            now = time.time()
            with self._frame_lock:
                self._latest_jpeg_frame = minimal_jpeg
                self._frame_timestamp = now
            self._frame_event.set()

            with self._audio_buffer_lock:
                # Provide dummy PCM chunk for simulated audio testing
                chunk_len = int(interval * 16000 * 2)
                self._audio_buffer.extend(b"\x00" * chunk_len)
                if len(self._audio_buffer) > 96000:
                    self._audio_buffer = self._audio_buffer[-96000:]

            if self.current_session:
                self.current_session.frames_received += 1
                self.current_session.audio_packets_received += 1

    # =========================================================================
    # WebRTC Negotiation Helpers
    # =========================================================================

    def _generate_webrtc_negotiation(
        self,
        session_id: str,
        offer_sdp: str,
        camera_id: str,
        quality: StreamQuality
    ) -> Dict[str, Any]:
        """Generates a compliant WebRTC SDP answer and ICE candidates."""
        candidate_ips = []
        try:
            ts_bin = find_termux_tool("tailscale")
            if ts_bin:
                ts_proc = subprocess.run([ts_bin, "ip", "-4"], capture_output=True, text=True, timeout=2)
                if ts_proc.returncode == 0 and ts_proc.stdout.strip():
                    candidate_ips.append(ts_proc.stdout.strip().splitlines()[0])
        except Exception:
            pass

        try:
            h_name = socket.gethostname()
            for ip in socket.gethostbyname_ex(h_name)[2]:
                if ip not in candidate_ips and not ip.startswith("127."):
                    candidate_ips.append(ip)
        except Exception:
            pass

        if not candidate_ips:
            candidate_ips.append("127.0.0.1")

        primary_ip = candidate_ips[0]
        rtp_port = 8554
        candidates = []
        priority = 2122260223
        for ip in candidate_ips:
            candidates.append(f"candidate:1 1 UDP {priority} {ip} {rtp_port} typ host")
            priority -= 1000

        sdp_lines = [
            "v=0",
            f"o=- {int(time.time() * 1000)} 2 IN IP4 {primary_ip}",
            "s=PersonalServer Live Call",
            "t=0 0",
            "a=group:BUNDLE video audio",
            f"m=video {rtp_port} UDP/TLS/RTP/SAVPF 96",
            f"c=IN IP4 {primary_ip}",
            "a=rtpmap:96 H264/90000",
            "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
            "a=sendonly",
            f"m=audio {rtp_port + 2} UDP/TLS/RTP/SAVPF 111",
            f"c=IN IP4 {primary_ip}",
            "a=rtpmap:111 opus/48000/2",
            "a=sendrecv"
        ]
        for c in candidates:
            sdp_lines.append(f"a={c}")
        sdp_lines.append("")

        return {
            "type": "answer",
            "sdp": "\r\n".join(sdp_lines),
            "candidates": candidates,
            "primary_ip": primary_ip,
            "port": rtp_port,
            "session_id": session_id,
            "video_codec": "H.264 / JPEG",
            "audio_codec": "Opus / PCM",
            "camera_id": camera_id,
            "resolution": f"{quality.width}x{quality.height}",
            "fps": quality.fps
        }
