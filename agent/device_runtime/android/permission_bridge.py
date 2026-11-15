r"""
Android Permission Bridge (D2)
==============================
Manages native Android and Termux runtime privileges, capability discovery,
and authorization state transitions.

Enforces clear lifecycle states:
  DETECTED -> PERMISSION_REQUIRED -> PERMISSION_GRANTED -> READY
          \-> PERMISSION_DENIED / UNAVAILABLE / ERROR

Integrates with PermissionManager and enforces fast-fail behavior with
actionable diagnostic guidance when permissions are missing or revoked.
"""

import os
import shutil
import subprocess
import time
import logging
from typing import Dict, Any, Optional, List, Tuple, Set

from agent.device_runtime.android.models import (
    BridgePermissionState,
    BridgeState,
)
from agent.device_runtime.permission_manager import (
    PermissionManager,
    PermissionStatus,
    PermissionDescriptor,
)

logger = logging.getLogger("agent.device_runtime.android.permission")


# Standard Android permissions mapped to internal capabilities
ANDROID_PERMISSION_MAP: Dict[str, Dict[str, Any]] = {
    "android.permission.CAMERA": {
        "name": "Camera Hardware Access",
        "category": "android.runtime",
        "required_for": ["camera", "live_camera", "cctv"],
        "description": "Required for capturing video frames and photos through Android Camera2 API",
    },
    "android.permission.RECORD_AUDIO": {
        "name": "Audio Recording",
        "category": "android.runtime",
        "required_for": ["microphone", "audio_record"],
        "description": "Required for ambient microphone sampling and streaming",
    },
    "termux.api": {
        "name": "Termux:API IPC App",
        "category": "termux.api",
        "required_for": [
            "battery", "power", "thermals", "battery_telemetry",
            "sensors", "sensor",
            "sensor.accelerometer", "sensor.gyroscope", "sensor.magnetometer",
            "sensor.ambient_light", "sensor.proximity",
        ],
        "description": "Required for non-root BatteryManager, PowerManager, and SensorManager telemetry",
    },
    "termux.storage": {
        "name": "Shared Storage Access",
        "category": "termux.storage",
        "required_for": ["storage_shared"],
        "description": "Access to /storage/emulated/0 via termux-setup-storage",
    },
    "android.permission.BODY_SENSORS": {
        "name": "Body & Environmental Sensors",
        "category": "android.runtime",
        "required_for": ["body_sensors", "heart_rate", "ambient_temperature"],
        "description": "Required for biometric and body sensor hardware managers",
    },
}


class AndroidPermissionBridge:
    """
    Coordinates permission resolution, probe checks, and state caching
    between the Native Android Bridge and the DeviceRuntime PermissionManager.
    """

    def __init__(self, permission_manager: Optional[PermissionManager] = None):
        self._pm = permission_manager or PermissionManager()
        self._states: Dict[str, BridgePermissionState] = {}
        self._last_checked: Dict[str, float] = {}
        self._cache_ttl_sec: float = 30.0
        self._initialize_descriptors()

    def _initialize_descriptors(self) -> None:
        """Ensures all standard Android permissions are registered with PermissionManager."""
        for perm_id, meta in ANDROID_PERMISSION_MAP.items():
            if not self._pm.get(perm_id):
                desc = PermissionDescriptor(
                    permission_id=perm_id,
                    name=meta["name"],
                    category=meta["category"],
                    status=PermissionStatus.NOT_REQUESTED,
                    description=meta["description"],
                    required_for=meta["required_for"],
                )
                self._pm.register(desc)
            self._states[perm_id] = BridgePermissionState.DETECTED

    @property
    def permission_manager(self) -> PermissionManager:
        return self._pm

    def get_state(self, permission_id: str) -> BridgePermissionState:
        """Returns the current BridgePermissionState for a given permission."""
        return self._states.get(permission_id, BridgePermissionState.UNAVAILABLE)

    def set_state(self, permission_id: str, state: BridgePermissionState) -> None:
        """Explicitly sets the permission state and synchronizes with PermissionManager."""
        self._states[permission_id] = state
        self._last_checked[permission_id] = time.time()

        # Synchronize with legacy PermissionManager status
        pm_status = self._map_bridge_to_pm_status(state)
        self._pm.set_status(permission_id, pm_status)

    def is_granted(self, permission_id: str) -> bool:
        """Returns True if the permission state is GRANTED or READY."""
        state = self.get_state(permission_id)
        return state in (BridgePermissionState.PERMISSION_GRANTED, BridgePermissionState.READY)

    def check_permissions(self, capability_id: str) -> Tuple[bool, List[str], List[str]]:
        """
        Checks all permissions required for a capability.
        Returns:
            (all_granted: bool, granted_list: List[str], missing_or_denied_list: List[str])
        """
        granted = []
        missing = []
        for perm_id, meta in ANDROID_PERMISSION_MAP.items():
            if capability_id in meta.get("required_for", []):
                state = self.get_state(perm_id)
                if state in (BridgePermissionState.PERMISSION_GRANTED, BridgePermissionState.READY):
                    granted.append(perm_id)
                else:
                    missing.append(perm_id)
        return len(missing) == 0, granted, missing

    def probe_termux_api_permission(self) -> BridgePermissionState:
        """
        Actively checks if Termux:API companion APK and CLI tools are functional.
        Detects revocation or missing installation.
        """
        cmd_path = shutil.which("termux-battery-status") or shutil.which("termux-sensor")
        if not cmd_path:
            state = BridgePermissionState.UNAVAILABLE
            self.set_state("termux.api", state)
            return state

        probe_cmd = [cmd_path, "-l"] if cmd_path.endswith("termux-sensor") else [cmd_path]

        # Probe with short timeout
        try:
            res = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                timeout=4.0,
            )
            if res.returncode == 0 and res.stdout.strip().startswith("{"):
                state = BridgePermissionState.READY
            elif "permission" in res.stderr.lower() or "denied" in res.stderr.lower():
                state = BridgePermissionState.PERMISSION_DENIED
            else:
                state = BridgePermissionState.PERMISSION_REQUIRED
        except subprocess.TimeoutExpired:
            # Termux:API may hang if companion app is missing or not installed
            state = BridgePermissionState.PERMISSION_REQUIRED
        except Exception as e:
            logger.warning("Error probing Termux:API permission: %s", e)
            state = BridgePermissionState.ERROR

        self.set_state("termux.api", state)
        return state

    def probe_storage_permission(self) -> BridgePermissionState:
        """Probes shared internal storage accessibility."""
        shared_dir = "/storage/emulated/0"
        if not os.path.exists(shared_dir):
            state = BridgePermissionState.UNAVAILABLE
        else:
            try:
                # Test directory listing capability
                _ = os.listdir(shared_dir)
                state = BridgePermissionState.READY
            except PermissionError:
                state = BridgePermissionState.PERMISSION_DENIED
            except Exception:
                state = BridgePermissionState.ERROR

        self.set_state("termux.storage", state)
        return state

    def verify_all_probes(self) -> Dict[str, BridgePermissionState]:
        """Runs probes for all detectable permissions and returns status dictionary."""
        self.probe_termux_api_permission()
        self.probe_storage_permission()

        # Camera & Audio require companion native service or prompt
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=0.3):
                self._states["android.permission.CAMERA"] = BridgePermissionState.PERMISSION_GRANTED
                self._states["android.permission.RECORD_AUDIO"] = BridgePermissionState.PERMISSION_GRANTED
        except Exception:
            cam_path = self.find_tool("termux-camera-info")
            if cam_path:
                try:
                    cres = subprocess.run([cam_path], capture_output=True, text=True, timeout=2.0)
                    if cres.returncode == 0 and cres.stdout.strip().startswith("["):
                        self._states["android.permission.CAMERA"] = BridgePermissionState.PERMISSION_GRANTED
                except Exception:
                    pass

        if self._states.get("android.permission.CAMERA") == BridgePermissionState.DETECTED:
            self._states["android.permission.CAMERA"] = BridgePermissionState.PERMISSION_REQUIRED

        if self._states.get("android.permission.RECORD_AUDIO") == BridgePermissionState.DETECTED:
            self._states["android.permission.RECORD_AUDIO"] = BridgePermissionState.PERMISSION_REQUIRED

        if self._states.get("android.permission.BODY_SENSORS") == BridgePermissionState.DETECTED:
            self._states["android.permission.BODY_SENSORS"] = BridgePermissionState.PERMISSION_REQUIRED

        return dict(self._states)

    def detect_revocation(self, permission_id: str) -> bool:
        """
        Re-probes a permission that was previously granted to verify it wasn't revoked.
        Returns True if revocation was detected.
        """
        prev_state = self._states.get(permission_id)
        if prev_state not in (BridgePermissionState.PERMISSION_GRANTED, BridgePermissionState.READY):
            return False

        if permission_id == "termux.api":
            new_state = self.probe_termux_api_permission()
        elif permission_id == "termux.storage":
            new_state = self.probe_storage_permission()
        else:
            new_state = prev_state

        if new_state not in (BridgePermissionState.PERMISSION_GRANTED, BridgePermissionState.READY):
            logger.warning(
                "Permission %s was revoked! Transitioned from %s to %s",
                permission_id, prev_state.value, new_state.value
            )
            self.set_state(permission_id, new_state)
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Serializes current permission bridge state."""
        return {
            pid: {
                "state": state.value,
                "is_granted": state in (BridgePermissionState.PERMISSION_GRANTED, BridgePermissionState.READY),
                "last_checked": self._last_checked.get(pid, 0.0),
                "metadata": ANDROID_PERMISSION_MAP.get(pid, {}),
            }
            for pid, state in self._states.items()
        }

    @staticmethod
    def _map_bridge_to_pm_status(state: BridgePermissionState) -> PermissionStatus:
        if state in (BridgePermissionState.READY, BridgePermissionState.PERMISSION_GRANTED):
            return PermissionStatus.GRANTED
        elif state == BridgePermissionState.PERMISSION_DENIED:
            return PermissionStatus.DENIED
        elif state == BridgePermissionState.PERMISSION_REQUIRED:
            return PermissionStatus.PROMPT_REQUIRED
        elif state == BridgePermissionState.UNAVAILABLE:
            return PermissionStatus.UNAVAILABLE
        elif state == BridgePermissionState.DETECTED:
            return PermissionStatus.NOT_REQUESTED
        return PermissionStatus.UNAVAILABLE
