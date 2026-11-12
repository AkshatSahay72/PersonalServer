"""
Permission Manager for Device Runtime (D1)
==========================================
Explicitly manages Termux, Android runtime, and system privileges.
Separates capability detection from authorization and ensures operations fail fast
with clear actionable diagnostic messages when authorization is missing.
"""

from enum import Enum
from typing import Dict, Any, Optional, List, Tuple
import time
import logging

logger = logging.getLogger("agent.device_runtime.permission")


class PermissionStatus(str, Enum):
    """Explicit authorization status."""
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    PROMPT_REQUIRED = "PROMPT_REQUIRED"
    NOT_REQUESTED = "NOT_REQUESTED"
    UNAVAILABLE = "UNAVAILABLE"


class PermissionDescriptor:
    """Metadata for an individual Android or Termux privilege."""

    def __init__(
        self,
        permission_id: str,
        name: str,
        category: str, # "android.runtime", "termux.storage", "termux.api", "system.shell"
        status: PermissionStatus = PermissionStatus.NOT_REQUESTED,
        description: str = "",
        required_for: Optional[List[str]] = None,
    ):
        self.permission_id = permission_id
        self.name = name
        self.category = category
        self.status = status
        self.description = description
        self.required_for = required_for or []
        self.last_checked = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "name": self.name,
            "category": self.category,
            "status": self.status.value,
            "description": self.description,
            "required_for": list(self.required_for),
            "last_checked": self.last_checked,
        }


class PermissionManager:
    """
    Central permission enforcement and tracking engine.
    Ensures no hardware capability executes without verified permission status.
    """

    def __init__(self):
        self._permissions: Dict[str, PermissionDescriptor] = {}
        self._initialize_standard_descriptors()

    def _initialize_standard_descriptors(self) -> None:
        """Populates known Android and Termux security descriptors."""
        self.register(PermissionDescriptor(
            permission_id="android.permission.CAMERA",
            name="Camera Access",
            category="android.runtime",
            status=PermissionStatus.PROMPT_REQUIRED,
            description="Required for continuous video and still photo capture",
            required_for=["camera", "live_camera", "cctv"]
        ))
        self.register(PermissionDescriptor(
            permission_id="android.permission.RECORD_AUDIO",
            name="Microphone Recording",
            category="android.runtime",
            status=PermissionStatus.PROMPT_REQUIRED,
            description="Required for audio recording and ambient streaming",
            required_for=["microphone", "audio_record"]
        ))
        self.register(PermissionDescriptor(
            permission_id="android.permission.ACCESS_FINE_LOCATION",
            name="Precise Location",
            category="android.runtime",
            status=PermissionStatus.PROMPT_REQUIRED,
            description="Required for GPS reading and Wi-Fi network SSID detection",
            required_for=["gps", "wifi_info"]
        ))
        self.register(PermissionDescriptor(
            permission_id="termux.storage",
            name="Termux Shared Storage",
            category="termux.storage",
            status=PermissionStatus.NOT_REQUESTED,
            description="Access to /storage/emulated/0 (internal shared flash)",
            required_for=["storage_shared"]
        ))
        self.register(PermissionDescriptor(
            permission_id="termux.api",
            name="Termux:API IPC App",
            category="termux.api",
            status=PermissionStatus.UNAVAILABLE,
            description="IPC bridge to com.termux.api companion application",
            required_for=["battery_telemetry", "tts", "sensors"]
        ))

    def register(self, desc: PermissionDescriptor) -> None:
        """Registers a permission descriptor."""
        self._permissions[desc.permission_id] = desc

    def get(self, permission_id: str) -> Optional[PermissionDescriptor]:
        """Gets descriptor by ID."""
        return self._permissions.get(permission_id)

    def set_status(self, permission_id: str, status: PermissionStatus) -> None:
        """Updates the status of a permission."""
        if permission_id in self._permissions:
            self._permissions[permission_id].status = status
            self._permissions[permission_id].last_checked = time.time()
            logger.debug("Permission %s status updated to %s", permission_id, status.value)

    def is_granted(self, permission_id: str) -> bool:
        """True if the permission is verified GRANTED."""
        desc = self._permissions.get(permission_id)
        return desc is not None and desc.status == PermissionStatus.GRANTED

    def check_capability_permissions(self, capability_id: str) -> Tuple[bool, List[str]]:
        """
        Checks if all required permissions for a capability are granted.
        Returns (all_granted: bool, missing_permissions: List[str]).
        """
        missing = []
        for desc in self._permissions.values():
            if capability_id in desc.required_for and desc.status != PermissionStatus.GRANTED:
                missing.append(desc.permission_id)
        return len(missing) == 0, missing

    def to_dict(self) -> Dict[str, Any]:
        """Serializes all permission statuses."""
        return {
            pid: desc.to_dict()
            for pid, desc in self._permissions.items()
        }
