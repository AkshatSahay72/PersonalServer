"""
Android Native Microphone Provider (D4-E02)
===========================================
Permission-gated, time- and size-bounded microphone capture provider backed by
the PersonalServer Native Android Bridge (loopback IPC).
"""

import os
import re
import time
import shutil
import logging
import threading
import wave
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider
from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import (
    BridgeState,
    MIC_DEFAULT_DURATION_SEC,
    MIC_DEFAULT_SAMPLE_RATE_HZ,
    MIC_MAX_FILE_BYTES,
)

logger = logging.getLogger("agent.device_runtime.android.providers.microphone")

SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
DEFAULT_TEMP_RETENTION_SEC = 600.0
DEFAULT_MAX_TEMP_FILES = 5


class AndroidMicrophoneProvider(BaseDeviceProvider):
    """Device Runtime provider for Native Android Bridge microphone capture."""

    def __init__(
        self,
        bridge: Optional[NativeAndroidBridge] = None,
        staging_dir: Optional[Path] = None,
        retention_sec: float = DEFAULT_TEMP_RETENTION_SEC,
        max_retained_files: int = DEFAULT_MAX_TEMP_FILES,
    ):
        super().__init__(
            capability_id="microphone",
            name="Android Native Microphone",
            subsystem="audio",
        )
        self._supported_operations = [
            "RECORDING_DISCOVER",
            "START_RECORDING",
            "RECORDING_STATUS",
            "STOP_RECORDING",
            "RECORDING_READ",
            "RECORDING_CLEANUP",
        ]
        self._requires_permission = True
        self._permission_name = "android.permission.RECORD_AUDIO"
        self._bridge = bridge
        self._staging_dir = Path(
            staging_dir
            or Path(os.environ.get("HOME", ".")) / ".personalserver" / "tmp_recordings"
        )
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._retention_sec = float(retention_sec)
        self._max_retained_files = int(max_retained_files)
        self._lock = threading.RLock()
        self._recordings: Dict[str, Dict[str, Any]] = {}
        self._latest_session_id: Optional[str] = None
        self._active_session_id: Optional[str] = None
        self._last_status_details: Dict[str, Any] = {}

    def _get_bridge(self) -> NativeAndroidBridge:
        if self._bridge is None:
            self._bridge = NativeAndroidBridge.get_instance()
        if self._bridge.state == BridgeState.UNINITIALIZED and (
            os.path.exists("/data/data/com.termux") or os.path.exists("/system/build.prop")
        ):
            self._bridge.initialize()
        return self._bridge

    def _purge_expired_staged(self) -> None:
        now = time.time()
        with self._lock:
            expired_ids = [
                sid
                for sid, meta in self._recordings.items()
                if now >= float(meta.get("expires_at", 0.0)) or not Path(meta.get("file_path", "")).exists()
            ]
            for sid in expired_ids:
                meta = self._recordings.pop(sid, None)
                if meta:
                    fp = Path(meta.get("file_path", ""))
                    if fp.exists():
                        try:
                            fp.unlink()
                        except Exception:
                            pass
                if self._latest_session_id == sid:
                    self._latest_session_id = None

            if len(self._recordings) > self._max_retained_files:
                ordered = sorted(
                    self._recordings.items(),
                    key=lambda kv: float(kv[1].get("created_at", 0.0)),
                )
                while len(self._recordings) > self._max_retained_files and ordered:
                    old_sid, old_meta = ordered.pop(0)
                    self._recordings.pop(old_sid, None)
                    fp = Path(old_meta.get("file_path", ""))
                    if fp.exists():
                        try:
                            fp.unlink()
                        except Exception:
                            pass
                    if self._latest_session_id == old_sid:
                        self._latest_session_id = None

    def discover(self) -> CapabilityDescriptor:
        """Read-only discovery of Android microphone hardware & RECORD_AUDIO permission."""
        self._purge_expired_staged()
        bridge = self._get_bridge()
        resp = bridge.discover_microphone(timeout=4.0)
        if not resp.success:
            if resp.error_code == "PERMISSION_DENIED":
                self.state = CapabilityState.PERMISSION_DENIED
            else:
                self.state = CapabilityState.UNAVAILABLE
            self.error = resp.error_message or "Native microphone bridge unavailable"
            self._last_status_details = {"error": self.error, "error_code": resp.error_code}
            return self.build_descriptor(self._last_status_details)

        data = resp.data or {}
        mic_avail = bool(data.get("mic_available", False))
        perm = str(data.get("permission", "PERMISSION_REQUIRED")).upper()
        rec_state = str(data.get("state", "IDLE")).upper()

        if not mic_avail:
            self.state = CapabilityState.UNAVAILABLE
            self.error = "Device reports no microphone hardware"
        elif perm == "GRANTED":
            self.state = CapabilityState.ACTIVE if rec_state == "RECORDING" else CapabilityState.READY
            self.error = None
        elif perm == "PERMISSION_DENIED":
            self.state = CapabilityState.PERMISSION_DENIED
            self.error = "RECORD_AUDIO runtime permission denied"
        else:
            self.state = CapabilityState.PERMISSION_REQUIRED
            self.error = "RECORD_AUDIO runtime permission not granted on device"

        self._last_status_details = {
            "backend": "native_android_bridge",
            "mic_available": mic_avail,
            "permission": perm,
            "recording_state": rec_state,
            "limits": data.get("limits", {}),
        }
        return self.build_descriptor(self._last_status_details)

    def start_recording(
        self,
        duration_sec: float = MIC_DEFAULT_DURATION_SEC,
        sample_rate_hz: int = MIC_DEFAULT_SAMPLE_RATE_HZ,
        authorized: bool = False,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Starts an explicitly authorized, bounded recording session via NativeAndroidBridge."""
        self._purge_expired_staged()
        if authorized is not True:
            return {
                "success": False,
                "error": "Explicit user authorization ('authorized': True) is required to start recording",
                "error_code": "UNAUTHORIZED",
            }

        bridge = self._get_bridge()
        resp = bridge.start_recording(
            duration_sec=duration_sec,
            sample_rate_hz=sample_rate_hz,
            authorized=True,
            session_id=session_id,
            timeout=5.0,
        )
        if not resp.success:
            if resp.error_code == "PERMISSION_DENIED":
                self.state = CapabilityState.PERMISSION_DENIED
            elif resp.error_code == "TRANSPORT_UNAVAILABLE":
                self.state = CapabilityState.UNAVAILABLE
            self.error = resp.error_message
            return {
                "success": False,
                "error": resp.error_message or "Failed to start native recording",
                "error_code": resp.error_code,
            }

        data = resp.data or {}
        sess = data.get("session") or {}
        sid = sess.get("session_id")
        with self._lock:
            self._active_session_id = sid
            if sid:
                self._latest_session_id = sid
        self.state = CapabilityState.ACTIVE
        self.error = None

        return {
            "success": True,
            "status": data.get("status", "RECORDING"),
            "visible_indication": bool(data.get("visible_indication", True)),
            "session": sess,
            "limits": data.get("limits", {}),
        }

    def get_recording_status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Retrieves live recording session state + staged completed recording metadata."""
        self._purge_expired_staged()
        bridge = self._get_bridge()
        resp = bridge.get_recording_status(session_id=session_id, timeout=4.0)
        if not resp.success:
            avail = self.state == CapabilityState.READY
            return {
                "success": False,
                "available": avail,
                "backend": "native_android_bridge",
                "state": "UNAVAILABLE",
                "error": resp.error_message,
                "error_code": resp.error_code,
            }

        data = dict(resp.data or {})
        rec_state = str(data.get("state", "IDLE")).upper()
        perm = str(data.get("permission", "PERMISSION_REQUIRED")).upper()
        mic_avail = bool(data.get("mic_available", False))

        if rec_state == "RECORDING":
            self.state = CapabilityState.ACTIVE
            active_sess = data.get("active_session") or {}
            self._active_session_id = active_sess.get("session_id")
        else:
            self._active_session_id = None
            if mic_avail and perm == "GRANTED":
                self.state = CapabilityState.READY
            elif mic_avail:
                self.state = CapabilityState.PERMISSION_REQUIRED
            else:
                self.state = CapabilityState.UNAVAILABLE

        rec_obj = data.get("recording")
        if (
            isinstance(rec_obj, dict)
            and rec_obj.get("available_for_transfer")
            and rec_obj.get("session_id")
            and SAFE_SESSION_ID_RE.match(str(rec_obj["session_id"]))
        ):
            rec_sid = str(rec_obj["session_id"])
            with self._lock:
                already_staged = rec_sid in self._recordings
            if not already_staged:
                try:
                    self._stage_recording_from_bridge(rec_sid, rec_obj)
                except Exception as exc:
                    logger.warning("Auto-staging completed recording '%s' failed: %s", rec_sid, exc)

        target_sid = session_id or self._latest_session_id
        staged_meta = None
        with self._lock:
            if target_sid and target_sid in self._recordings:
                staged_meta = dict(self._recordings[target_sid])
                staged_meta.pop("file_path", None)

        return {
            "success": True,
            "available": mic_avail and perm == "GRANTED",
            "mic_available": mic_avail,
            "permission": perm,
            "backend": "native_android_bridge",
            "state": rec_state,
            "visible_indication": bool(data.get("visible_indication", False)),
            "active_session": data.get("active_session"),
            "recording": data.get("recording"),
            "staged_recording": staged_meta,
            "limits": data.get("limits", {}),
        }

    def stop_recording(
        self,
        session_id: Optional[str] = None,
        reason: str = "Requested stop",
        auto_transfer: bool = True,
    ) -> Dict[str, Any]:
        """Stops active capture cleanly and optionally pulls completed WAV into bounded staging."""
        self._purge_expired_staged()
        bridge = self._get_bridge()
        resp = bridge.stop_recording(session_id=session_id, reason=reason, timeout=6.0)
        with self._lock:
            self._active_session_id = None

        if not resp.success:
            return {
                "success": False,
                "error": resp.error_message or "Failed to stop recording",
                "error_code": resp.error_code,
            }

        data = dict(resp.data or {})
        rec_info = data.get("recording") or data.get("session") or {}
        sid = rec_info.get("session_id") or session_id
        state_str = str(rec_info.get("state") or data.get("status") or "IDLE").upper()

        self.state = CapabilityState.READY if bridge.is_ready() else CapabilityState.UNAVAILABLE

        transfer_info: Optional[Dict[str, Any]] = None
        if (
            auto_transfer
            and sid
            and SAFE_SESSION_ID_RE.match(str(sid))
            and rec_info.get("available_for_transfer")
        ):
            try:
                transfer_info = self._stage_recording_from_bridge(sid, rec_info)
            except Exception as exc:
                logger.warning("Failed staging recording '%s': %s", sid, exc)
                transfer_info = {"success": False, "error": str(exc)}

        return {
            "success": True,
            "status": state_str,
            "visible_indication": bool(data.get("visible_indication", False)),
            "stop_reason": data.get("stop_reason", reason),
            "session": data.get("session"),
            "recording": rec_info,
            "transfer": transfer_info,
        }

    def _stage_recording_from_bridge(self, session_id: str, rec_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not SAFE_SESSION_ID_RE.match(session_id):
            raise ValueError(f"Unsafe session_id: {session_id}")
        bridge = self._get_bridge()
        dest_file = (self._staging_dir / f"{session_id}.wav").resolve()
        if dest_file.parent != self._staging_dir.resolve():
            raise ValueError("Path traversal blocked")

        res = bridge.transfer_recording(
            session_id=session_id,
            dest_path=dest_file,
            cleanup_remote=True,
            max_bytes=MIC_MAX_FILE_BYTES,
        )

        # Verify WAV audio integrity using Python stdlib wave
        wav_meta = validate_wav_file(dest_file)
        now = time.time()
        entry = {
            "session_id": session_id,
            "file_path": str(dest_file),
            "file_name": dest_file.name,
            "mime_type": "audio/wav",
            "file_size_bytes": res["file_size_bytes"],
            "sha256": res["sha256"],
            "duration_sec": wav_meta["duration_sec"],
            "sample_rate_hz": wav_meta["sample_rate_hz"],
            "channels": wav_meta["channels"],
            "bits_per_sample": wav_meta["bits_per_sample"],
            "frames": wav_meta["frames"],
            "created_at": now,
            "expires_at": now + self._retention_sec,
            "remote_cleaned": res.get("remote_cleaned", False),
            "download_path": f"/hardware/microphone/recording?session_id={session_id}",
        }
        with self._lock:
            self._recordings[session_id] = entry
            self._latest_session_id = session_id
            self._purge_expired_staged()
        return {k: v for k, v in entry.items() if k != "file_path"}

    def get_staged_recording_file(self, session_id: Optional[str] = None) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
        """Returns (local_wav_path, metadata) for an authenticated download, pulling from bridge if needed."""
        self._purge_expired_staged()
        sid = (session_id or self._latest_session_id or "").strip()
        if not sid or not SAFE_SESSION_ID_RE.match(sid):
            return None, None

        with self._lock:
            meta = self._recordings.get(sid)
            if meta:
                fp = Path(meta["file_path"])
                if fp.exists():
                    return fp, dict(meta)

        # If not staged yet (e.g. session auto-expired by duration timer), pull cleanly from bridge
        try:
            st = self.get_recording_status(session_id=sid)
            rec = st.get("recording") or {}
            if st.get("success") and rec.get("session_id") == sid and rec.get("available_for_transfer"):
                self._stage_recording_from_bridge(sid, rec)
                with self._lock:
                    meta = self._recordings.get(sid)
                    if meta and Path(meta["file_path"]).exists():
                        return Path(meta["file_path"]), dict(meta)
        except Exception as exc:
            logger.warning("On-demand transfer failed for '%s': %s", sid, exc)
        return None, None

    def cleanup_all(self, reason: str = "Shutdown cleanup") -> None:
        if self._active_session_id:
            try:
                self.stop_recording(session_id=self._active_session_id, reason=reason, auto_transfer=False)
            except Exception:
                pass
        with self._lock:
            for meta in list(self._recordings.values()):
                fp = Path(meta.get("file_path", ""))
                if fp.exists():
                    try:
                        fp.unlink()
                    except Exception:
                        pass
            self._recordings.clear()
            self._latest_session_id = None
            if self._staging_dir.exists():
                for p in self._staging_dir.glob("*.wav*"):
                    try:
                        p.unlink()
                    except Exception:
                        pass

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        op = str(operation).upper().strip()
        if op == "RECORDING_DISCOVER":
            desc = self.discover()
            return {"success": desc.state in (CapabilityState.READY, CapabilityState.ACTIVE), "descriptor": desc.to_dict()}
        if op == "START_RECORDING":
            return self.start_recording(
                duration_sec=float(parameters.get("duration_sec", MIC_DEFAULT_DURATION_SEC)),
                sample_rate_hz=int(parameters.get("sample_rate_hz", MIC_DEFAULT_SAMPLE_RATE_HZ)),
                authorized=bool(parameters.get("authorized", False)),
                session_id=parameters.get("session_id"),
            )
        if op == "RECORDING_STATUS":
            return self.get_recording_status(session_id=parameters.get("session_id"))
        if op == "STOP_RECORDING":
            return self.stop_recording(
                session_id=parameters.get("session_id"),
                reason=str(parameters.get("reason", "Requested stop")),
                auto_transfer=bool(parameters.get("auto_transfer", True)),
            )
        if op == "RECORDING_READ":
            bridge = self._get_bridge()
            resp = bridge.read_recording_chunk(
                session_id=str(parameters.get("session_id", "")),
                offset=int(parameters.get("offset", 0)),
                length=int(parameters.get("length", 24576)),
            )
            return {"success": resp.success, "data": resp.data, "error": resp.error_message, "error_code": resp.error_code}
        if op == "RECORDING_CLEANUP":
            sid = str(parameters.get("session_id", ""))
            bridge = self._get_bridge()
            resp = bridge.cleanup_recording(sid)
            with self._lock:
                meta = self._recordings.pop(sid, None)
                if meta and Path(meta.get("file_path", "")).exists():
                    Path(meta["file_path"]).unlink(missing_ok=True)
            return {"success": resp.success, "data": resp.data, "error": resp.error_message}
        return {"success": False, "error": f"Unsupported microphone operation: {operation}"}


def validate_wav_file(path: Path) -> Dict[str, Any]:
    """Validates RIFF/WAVE header & PCM parameters via Python wave module."""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        duration_sec = round(nframes / float(framerate), 3) if framerate > 0 else 0.0
    return {
        "channels": channels,
        "bits_per_sample": sampwidth * 8,
        "sample_rate_hz": framerate,
        "frames": nframes,
        "duration_sec": duration_sec,
    }
