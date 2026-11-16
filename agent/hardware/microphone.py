"""
MicrophoneProvider implementation using Termux:API.
Supports on-demand bounded audio recording, status inspection, and explicit stopping.
"""

import os
import sys
import time
import json
import uuid
import shutil
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from agent.hardware.base import SessionState, HardwareSession, find_termux_tool

logger = logging.getLogger("agent.hardware.microphone")


class MicrophoneProvider:
    """
    On-demand microphone provider supporting audio recording with configurable
    encoders (aac, amr_wb, opus), sample rates, bitrates, and explicit session stop.
    """
    def __init__(self, storage_dir: Optional[Path] = None, native_provider: Optional[Any] = None):
        self.storage_dir = storage_dir or Path(os.environ.get("HOME", ".")) / "storage" / "hardware" / "audio"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.current_session: Optional[HardwareSession] = None
        self._native_provider = native_provider
        self._lock = threading.RLock()

    def _get_native_provider(self):
        if self._native_provider is None:
            try:
                from agent.device_runtime.android.providers.microphone import AndroidMicrophoneProvider
                self._native_provider = AndroidMicrophoneProvider(staging_dir=self.storage_dir / "tmp_native")
            except Exception:
                return None
        return self._native_provider

    def _is_native_bridge_active(self) -> bool:
        prov = self._get_native_provider()
        if prov is None:
            return False
        try:
            bridge = prov._get_bridge()
            return bool(bridge.transport.is_available())
        except Exception:
            return False

    def is_available(self) -> Tuple[bool, Optional[str]]:
        if self._is_native_bridge_active():
            prov = self._get_native_provider()
            st = prov.get_recording_status()
            if st.get("success"):
                if st.get("mic_available") and st.get("permission") == "GRANTED":
                    return True, None
                if st.get("mic_available"):
                    return False, "RECORD_AUDIO runtime permission not granted on device"
                return False, "Device reports no microphone hardware"
        mic_bin = find_termux_tool("termux-microphone-record")
        if not mic_bin:
            return False, "Native Android Bridge unavailable and termux-microphone-record not found"
        return True, None

    def start_native_recording(self, parameters: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
        prov = self._get_native_provider()
        if prov is None:
            return False, {"error": "Native Android Microphone provider unavailable", "error_code": "UNAVAILABLE"}
        params = dict(parameters or {})
        res = prov.start_recording(
            duration_sec=float(params.get("duration_sec", 15.0)),
            sample_rate_hz=int(params.get("sample_rate_hz", 16000)),
            authorized=bool(params.get("authorized", False)),
            session_id=params.get("session_id"),
        )
        return bool(res.get("success")), res

    def get_native_recording_status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        prov = self._get_native_provider()
        if prov is None:
            return {"success": False, "available": False, "state": "UNAVAILABLE", "error": "Provider unavailable"}
        return prov.get_recording_status(session_id=session_id)

    def stop_native_recording(self, parameters: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
        prov = self._get_native_provider()
        if prov is None:
            return False, {"error": "Native Android Microphone provider unavailable", "error_code": "UNAVAILABLE"}
        params = dict(parameters or {})
        res = prov.stop_recording(
            session_id=params.get("session_id"),
            reason=str(params.get("reason", "Requested by controller")),
            auto_transfer=bool(params.get("auto_transfer", True)),
        )
        return bool(res.get("success")), res

    def get_recording_file(self, session_id: Optional[str] = None) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
        prov = self._get_native_provider()
        if prov is not None:
            return prov.get_staged_recording_file(session_id=session_id)
        return None, None

    def cleanup_native(self, reason: str = "Shutdown cleanup") -> None:
        prov = self._get_native_provider()
        if prov is not None:
            prov.cleanup_all(reason=reason)

    def start_session(
        self,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Starts an on-demand microphone recording session.
        Parameters:
            - duration_sec: maximum recording duration (default bounded by timeout)
            - encoder: "aac" (default), "amr_wb", "opus"
            - bitrate: kbps (default 128)
            - rate: sampling rate in Hz (default 44100)
            - output_path: optional target file path
        """
        with self._lock:
            # Check duplicate session
            if self.current_session and self.current_session.state == SessionState.ACTIVE:
                if not self.current_session.is_expired():
                    return False, {
                        "error": "A microphone session is already active",
                        "session": self.current_session.to_dict()
                    }
                else:
                    self.stop_session("Session timed out")

            available, reason = self.is_available()
            if not available:
                return False, {"error": f"Microphone not available: {reason}"}

            params = parameters or {}
            duration_sec = int(params.get("duration_sec", timeout_sec))
            duration_sec = min(duration_sec, int(timeout_sec))
            encoder = str(params.get("encoder", "aac"))
            bitrate = int(params.get("bitrate", 128))
            rate = int(params.get("rate", 44100))

            session_id = f"mic-{uuid.uuid4().hex[:8]}"
            out_file = params.get("output_path")
            if not out_file:
                ext = "m4a" if encoder == "aac" else ("opus" if encoder == "opus" else "amr")
                out_file = str(self.storage_dir / f"rec_{session_id}_{int(time.time())}.{ext}")

            Path(out_file).parent.mkdir(parents=True, exist_ok=True)

            mic_bin = find_termux_tool("termux-microphone-record") or "termux-microphone-record"
            cmd = [
                mic_bin,
                "-f", out_file,
                "-l", str(duration_sec),
                "-e", encoder,
                "-b", str(bitrate),
                "-r", str(rate)
            ]

            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if res.returncode != 0:
                    err = res.stderr.strip() or f"Process failed with exit code {res.returncode}"
                    if "Permission" in err or "denied" in err.lower():
                        err = "ANDROID_PERMISSION_DENIED: Audio recording permission required"
                    return False, {"error": err, "session_id": session_id}

                session = HardwareSession(
                    session_id=session_id,
                    capability="microphone",
                    parameters=params,
                    timeout_sec=float(duration_sec)
                )
                session.state = SessionState.ACTIVE
                session.started_at = time.time()
                session.resource_paths.append(out_file)
                session.extra_data = {
                    "output_file": out_file,
                    "encoder": encoder,
                    "bitrate": bitrate,
                    "rate": rate,
                    "duration_limit_sec": duration_sec
                }
                self.current_session = session
                return True, {"status": "ACTIVE", "session": session.to_dict()}
            except Exception as e:
                return False, {"error": str(e), "session_id": session_id}

    def stop_session(self, reason: str = "Requested by controller") -> Dict[str, Any]:
        with self._lock:
            mic_bin = find_termux_tool("termux-microphone-record") or "termux-microphone-record"
            if not self.current_session:
                # Still attempt to signal termux-microphone-record -q to ensure device is not recording
                try:
                    subprocess.run([mic_bin, "-q"], capture_output=True, timeout=5)
                except Exception:
                    pass
                return {"status": "IDLE", "message": "No active microphone session"}

            session = self.current_session
            session.state = SessionState.STOPPING

            try:
                subprocess.run([mic_bin, "-q"], capture_output=True, timeout=5)
            except Exception as e:
                logger.warning("Error stopping termux-microphone-record: %s", e)

            session.state = SessionState.IDLE
            session.stopped_at = time.time()
            out_file = session.extra_data.get("output_file")
            if out_file and os.path.exists(out_file):
                session.extra_data["file_size_bytes"] = os.path.getsize(out_file)

            data = session.to_dict()
            data["stop_reason"] = reason
            self.current_session = None
            return {"status": "STOPPED", "session": data}

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            if self._is_native_bridge_active():
                native_st = self.get_native_recording_status()
                if native_st.get("success"):
                    return {
                        "available": bool(native_st.get("available")),
                        "mic_available": bool(native_st.get("mic_available")),
                        "permission": native_st.get("permission"),
                        "backend": "native_android_bridge",
                        "state": native_st.get("state"),
                        "visible_indication": bool(native_st.get("visible_indication", False)),
                        "session": native_st.get("active_session") or native_st.get("recording"),
                        "recording": native_st.get("recording"),
                        "staged_recording": native_st.get("staged_recording"),
                        "limits": native_st.get("limits", {}),
                    }
            available, reason = self.is_available()
            if self.current_session and self.current_session.is_expired():
                self.stop_session("Session timeout reached")

            recording_info = None
            if available:
                mic_bin = find_termux_tool("termux-microphone-record") or "termux-microphone-record"
                try:
                    res = subprocess.run([mic_bin, "-i"], capture_output=True, text=True, timeout=5)
                    if res.returncode == 0 and res.stdout.strip():
                        recording_info = json.loads(res.stdout)
                except Exception:
                    pass

            return {
                "available": available,
                "reason": reason,
                "session": self.current_session.to_dict() if self.current_session else None,
                "recording_info": recording_info
            }
