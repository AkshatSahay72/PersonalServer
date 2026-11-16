"""
Capability Registry for Device Runtime (D1)
===========================================
Tracks hardware, system, and compute capabilities using explicit lifecycle states.
Enforces the fundamental architectural boundary: Detection does NOT imply authorization.
"""

from enum import Enum
from typing import Dict, Any, Optional, List, Set
import time
import logging

logger = logging.getLogger("agent.device_runtime.registry")


class CapabilityState(str, Enum):
    """Explicit capability states for device providers and operations."""
    DETECTED = "DETECTED"                       # Hardware or interface physically exists, uninitialized
    AVAILABLE = "AVAILABLE"                     # Discovered, operational, but not yet initialized or engaged
    READY = "READY"                             # Configured, authorized, and ready to accept operations
    ACTIVE = "ACTIVE"                           # Currently executing an authorized workload or hardware session
    UNAVAILABLE = "UNAVAILABLE"                 # Hardware missing, platform unsupported, or interface offline
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED" # Interface detected, but missing user or OS authorization
    PERMISSION_DENIED = "PERMISSION_DENIED"     # User or OS explicitly refused permission
    ERROR = "ERROR"                             # Hardware or adapter initialization error


class CapabilityDescriptor:
    """Detailed metadata and operational contract for a registered capability."""

    def __init__(
        self,
        capability_id: str,
        name: str,
        subsystem: str,
        state: CapabilityState = CapabilityState.DETECTED,
        supported_operations: Optional[List[str]] = None,
        requires_permission: bool = False,
        permission_name: Optional[str] = None,
        resource_requirements: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        self.capability_id = capability_id
        self.name = name
        self.subsystem = subsystem
        self.state = state
        self.supported_operations = supported_operations or []
        self.requires_permission = requires_permission
        self.permission_name = permission_name
        self.resource_requirements = resource_requirements or {}
        self.details = details or {}
        self.error = error
        self.last_updated = time.time()

    def update_state(self, new_state: CapabilityState, error: Optional[str] = None) -> None:
        """Transitions state and records timestamp."""
        self.state = new_state
        if error is not None:
            self.error = error
        self.last_updated = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes capability descriptor to dictionary."""
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "subsystem": self.subsystem,
            "state": self.state.value,
            "supported_operations": list(self.supported_operations),
            "requires_permission": self.requires_permission,
            "permission_name": self.permission_name,
            "resource_requirements": dict(self.resource_requirements),
            "details": dict(self.details),
            "error": self.error,
            "last_updated": self.last_updated,
        }


class CapabilityRegistry:
    """
    Central registry for device capabilities.
    Maintains validated inventory of subsystems and guards unauthorized execution.
    """

    def __init__(self):
        self._capabilities: Dict[str, CapabilityDescriptor] = {}

    def register(self, descriptor: CapabilityDescriptor) -> None:
        """Registers or updates a capability descriptor."""
        self._capabilities[descriptor.capability_id] = descriptor
        logger.debug(
            "Registered capability: %s (subsystem: %s, state: %s)",
            descriptor.capability_id,
            descriptor.subsystem,
            descriptor.state.value,
        )

    def unregister(self, capability_id: str) -> bool:
        """Removes a capability descriptor."""
        return self._capabilities.pop(capability_id, None) is not None

    def get(self, capability_id: str) -> Optional[CapabilityDescriptor]:
        """Retrieves a capability descriptor by ID."""
        return self._capabilities.get(capability_id)

    def get_state(self, capability_id: str) -> CapabilityState:
        """Returns the current state of a capability, defaulting to UNAVAILABLE."""
        cap = self._capabilities.get(capability_id)
        return cap.state if cap else CapabilityState.UNAVAILABLE

    def is_operational(self, capability_id: str) -> bool:
        """True if capability is READY or ACTIVE."""
        cap = self._capabilities.get(capability_id)
        return cap is not None and cap.state in (CapabilityState.READY, CapabilityState.ACTIVE)

    def is_authorized(self, capability_id: str) -> bool:
        """True if capability does not require permission or is already READY/ACTIVE."""
        cap = self._capabilities.get(capability_id)
        if not cap:
            return False
        if not cap.requires_permission:
            return True
        return cap.state in (CapabilityState.READY, CapabilityState.ACTIVE)

    def list_all(self) -> List[CapabilityDescriptor]:
        """Returns all registered capability descriptors."""
        return list(self._capabilities.values())

    def list_by_subsystem(self, subsystem: str) -> List[CapabilityDescriptor]:
        """Filters capabilities by subsystem category."""
        return [c for c in self._capabilities.values() if c.subsystem == subsystem]

    def to_dict(self) -> Dict[str, Any]:
        """Returns full capability catalog serialized for telemetry and controller sync."""
        return {
            cid: desc.to_dict()
            for cid, desc in self._capabilities.items()
        }

