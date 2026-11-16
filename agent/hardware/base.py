"""
Hardware Capability Manager and Provider Interfaces for PersonalServer.
Supports on-demand Camera, Microphone, and Speaker capabilities across Android/Termux and host platforms.
"""

import os
import sys
import time
import json
import uuid
import shutil
import logging
import threading
import subprocess
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("agent.hardware")

TERMUX_BIN_DIR = Path("/data/data/com.termux/files/usr/bin")


def find_termux_tool(name: str) -> Optional[str]:
    """
    Locates a tool in PATH or within the Termux default binary directory.
    Ensures commands are found even when PATH is minimally defined in daemon contexts.
    """
    found = shutil.which(name)
    if found:
        return found
    termux_candidate = TERMUX_BIN_DIR / name
    if termux_candidate.exists() and os.access(str(termux_candidate), os.X_OK):
        return str(termux_candidate)
    return None



class SessionState(str, Enum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"


class HardwareSession:
    """Represents an active or transitioning hardware session."""
    def __init__(
        self,
        session_id: str,
        capability: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0
    ):
        self.session_id = session_id
        self.capability = capability
        self.parameters = parameters or {}
        self.state = SessionState.REQUESTED
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.timeout_sec = float(timeout_sec)
        self.process: Optional[subprocess.Popen] = None
        self.resource_paths: List[str] = []
        self.extra_data: Dict[str, Any] = {}
        self.error: Optional[str] = None

    def is_expired(self) -> bool:
        if self.state in (SessionState.IDLE,):
            return False
        elapsed = time.time() - (self.started_at or self.created_at)
        return elapsed > self.timeout_sec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "capability": self.capability,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "timeout_sec": self.timeout_sec,
            "duration_sec": round(time.time() - self.started_at, 2) if self.started_at else 0.0,
            "extra_data": self.extra_data,
            "error": self.error
        }


class StreamQuality:
    """Configurable live video stream quality settings."""
    PRESETS = {
        "720p": {"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 1500},
        "480p": {"width": 640, "height": 480, "fps": 30, "bitrate_kbps": 800},
        "360p": {"width": 480, "height": 360, "fps": 30, "bitrate_kbps": 400},
        "vga": {"width": 640, "height": 480, "fps": 30, "bitrate_kbps": 800},
    }

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        bitrate_kbps: int = 800
    ):
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.bitrate_kbps = int(bitrate_kbps)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]] = None) -> "StreamQuality":
        if not data:
            return cls()
        preset = (data.get("preset") or data.get("quality") or "").lower()
        if preset in cls.PRESETS:
            p = cls.PRESETS[preset]
            return cls(width=p["width"], height=p["height"], fps=p["fps"], bitrate_kbps=p["bitrate_kbps"])
        return cls(
            width=data.get("width", 640),
            height=data.get("height", 480),
            fps=data.get("fps", 30),
            bitrate_kbps=data.get("bitrate_kbps", 800)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate_kbps,
            "codec": "H.264 (AVC)",
            "encoder": "Android MediaCodec"
        }


class LiveCameraSession(HardwareSession):
    """Represents a continuous WebRTC live camera streaming session."""
    def __init__(
        self,
        session_id: str,
        camera_id: str = "0",
        quality: Optional[StreamQuality] = None,
        timeout_sec: float = 300.0,
        reconnection_authorized: bool = False
    ):
        super().__init__(
            session_id=session_id,
            capability="live_camera",
            parameters={"camera_id": camera_id},
            timeout_sec=timeout_sec
        )
        self.camera_id = str(camera_id)
        self.quality = quality or StreamQuality()
        self.webrtc_state = "IDLE"
        self.reconnection_authorized = reconnection_authorized
        self.viewer_ip: Optional[str] = None
        self.tailscale_ip: Optional[str] = None
        self.ice_candidates: List[str] = []
        self.frame_count: int = 0
        self.last_keepalive: float = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "camera_id": self.camera_id,
            "quality": self.quality.to_dict(),
            "webrtc_state": self.webrtc_state,
            "reconnection_authorized": self.reconnection_authorized,
            "viewer_ip": self.viewer_ip,
            "tailscale_ip": self.tailscale_ip,
            "ice_candidates_count": len(self.ice_candidates),
            "frame_count": self.frame_count,
            "timeout_remaining_sec": max(0, int(self.timeout_sec - (time.time() - (self.started_at or self.created_at))))
        })
        return d

