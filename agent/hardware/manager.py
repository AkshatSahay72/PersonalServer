"""
HardwareCapabilityManager unifying CameraProvider, MicrophoneProvider, and SpeakerProvider.
Enforces session bounds, duplicate session prevention, watchdog cleanup, and controller operations.
"""

import os
import sys
import time
import json
import atexit
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from agent.hardware.base import SessionState, HardwareSession
from agent.hardware.camera import CameraProvider
from agent.hardware.microphone import MicrophoneProvider
from agent.hardware.speaker import SpeakerProvider

logger = logging.getLogger("agent.hardware.manager")


class HardwareCapabilityManager:
    """
    Authoritative manager for on-demand hardware sessions on a node.
    Coordinates Camera, Microphone, and Speaker providers, enforcing timeout bounds,
    watchdogs, duplicate prevention, and clean resource release upon shutdown.
    """
    _instance: Optional["HardwareCapabilityManager"] = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls, storage_dir: Optional[Path] = None) -> "HardwareCapabilityManager":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(storage_dir=storage_dir)
            return cls._instance

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or Path(os.environ.get("HOME", ".")) / "storage" / "hardware"
        self.camera = CameraProvider(storage_dir=self.storage_dir / "camera")
        self.microphone = MicrophoneProvider(storage_dir=self.storage_dir / "audio")
        self.speaker = SpeakerProvider()
        self._lock = threading.RLock()
        self._watchdog_active = True
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="HardwareWatchdog")
        self._watchdog_thread.start()
        atexit.register(self.stop_all)

    def _watchdog_loop(self):
        """Background watchdog to automatically reclaim expired sessions."""
        while self._watchdog_active:
            try:
                time.sleep(2.0)
                # Camera expiration check
                if self.camera.current_session and self.camera.current_session.is_expired():
                    logger.info("Watchdog cleaning up expired camera session: %s", self.camera.current_session.session_id)
                    self.camera.stop_session("Session timeout reached")

                # Live camera streaming expiration check
                if self.camera.live_session and self.camera.live_session.is_expired():
                    logger.info("Watchdog cleaning up expired live camera session: %s", self.camera.live_session.session_id)
                    self.camera.stop_live_camera("Session timeout reached")

                # Microphone expiration check
                if self.microphone.current_session and self.microphone.current_session.is_expired():
                    logger.info("Watchdog cleaning up expired microphone session: %s", self.microphone.current_session.session_id)
                    self.microphone.stop_session("Session timeout reached")

                # Speaker expiration check
                if self.speaker.current_session and self.speaker.current_session.is_expired():
                    logger.info("Watchdog cleaning up expired speaker session: %s", self.speaker.current_session.session_id)
                    self.speaker.stop_session("Session timeout reached")
            except Exception as e:
                logger.debug("Error in hardware watchdog: %s", e)

    def execute_operation(self, operation: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Executes a controller-triggered hardware operation.
        Supported operations:
            - START_CAMERA
            - STOP_CAMERA
            - START_MIC
            - STOP_MIC
            - SPEAK_TEXT
            - PLAY_AUDIO
            - STOP_AUDIO
            - GET_HARDWARE_STATUS
        """
        op = str(operation).upper().strip()
        params = parameters or {}
        timeout_sec = float(params.get("timeout_sec", 300.0))

        if op == "START_CAMERA":
            ok, res = self.camera.start_session(parameters=params, timeout_sec=timeout_sec)
            return {"operation": op, "success": ok, "result": res}

        elif op == "STOP_CAMERA":
            reason = str(params.get("reason", "Requested by controller"))
            res = self.camera.stop_session(reason=reason)
            return {"operation": op, "success": True, "result": res}

        elif op == "START_MIC":
            ok, res = self.microphone.start_session(parameters=params, timeout_sec=timeout_sec)
            return {"operation": op, "success": ok, "result": res}

        elif op == "STOP_MIC":
            reason = str(params.get("reason", "Requested by controller"))
            res = self.microphone.stop_session(reason=reason)
            return {"operation": op, "success": True, "result": res}

        elif op == "START_RECORDING":
            ok, res = self.microphone.start_native_recording(parameters=params)
            return {"operation": op, "success": ok, "result": res}

        elif op == "RECORDING_STATUS":
            res = self.microphone.get_native_recording_status(session_id=params.get("session_id"))
            return {"operation": op, "success": bool(res.get("success")), "status": res}

        elif op == "STOP_RECORDING":
            ok, res = self.microphone.stop_native_recording(parameters=params)
            return {"operation": op, "success": ok, "result": res}

        elif op == "SPEAK_TEXT":
            text = str(params.get("text", ""))
            engine = params.get("engine")
            language = params.get("language")
            pitch = float(params.get("pitch", 1.0))
            rate = float(params.get("rate", 1.0))
            stream = str(params.get("stream", "NOTIFICATION"))
            ok, res = self.speaker.speak_text(
                text=text,
                engine=engine,
                language=language,
                pitch=pitch,
                rate=rate,
                stream=stream,
                timeout_sec=timeout_sec
            )
            return {"operation": op, "success": ok, "result": res}

        elif op == "PLAY_AUDIO":
            file_path = str(params.get("file_path", params.get("audio_file_path", "")))
            ok, res = self.speaker.play_audio(audio_file_path=file_path, timeout_sec=timeout_sec)
            return {"operation": op, "success": ok, "result": res}

        elif op == "STOP_AUDIO":
            reason = str(params.get("reason", "Requested by controller"))
            res = self.speaker.stop_session(reason=reason)
            return {"operation": op, "success": True, "result": res}

        elif op == "GET_HARDWARE_STATUS":
            return {
                "operation": op,
                "success": True,
                "status": self.get_full_status()
            }

        # Live Video Streaming Operations (WebRTC / Camera2 / MediaCodec H.264)
        elif op == "START_LIVE_CAMERA":
            ok, res = self.camera.start_live_camera(parameters=params, timeout_sec=timeout_sec)
            return {"operation": op, "success": ok, "result": res}

        elif op == "STOP_LIVE_CAMERA":
            reason = str(params.get("reason", "Requested by controller"))
            res = self.camera.stop_live_camera(reason=reason)
            return {"operation": op, "success": True, "result": res}

        elif op == "GET_LIVE_CAMERA_STATUS":
            res = self.camera.get_live_camera_status()
            return {"operation": op, "success": True, "status": res}

        elif op == "SELECT_CAMERA":
            cam_id = str(params.get("camera_id", "0"))
            res = self.camera.select_camera(camera_id=cam_id)
            return {"operation": op, "success": res.get("success", False), "result": res}

        elif op == "UPDATE_STREAM_QUALITY":
            res = self.camera.update_stream_quality(
                width=params.get("width"),
                height=params.get("height"),
                fps=params.get("fps"),
                bitrate_kbps=params.get("bitrate_kbps"),
                preset=params.get("preset")
            )
            return {"operation": op, "success": res.get("success", False), "result": res}

        else:
            return {
                "operation": op,
                "success": False,
                "error": f"Unsupported hardware operation: '{operation}'"
            }

    def get_full_status(self) -> Dict[str, Any]:
        """Returns the current state and active sessions across all providers."""
        return {
            "timestamp": time.time(),
            "camera": self.camera.get_status(),
            "microphone": self.microphone.get_status(),
            "speaker": self.speaker.get_status()
        }

    def stop_all(self, reason: str = "Controller connection lost or agent shutdown"):
        """Emergency stop of all active hardware sessions."""
        with self._lock:
            self.camera.stop_session(reason)
            self.camera.stop_live_camera(reason)
            self.microphone.stop_session(reason)
            self.microphone.cleanup_native(reason)
            self.speaker.stop_session(reason)
