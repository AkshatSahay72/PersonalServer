"""
Base Device Provider Interface (D1)
===================================
Common contract for hardware, system, and compute providers in Device Runtime.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
import logging

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.session_manager import DeviceSession

logger = logging.getLogger("agent.device_runtime.provider")


class BaseDeviceProvider(ABC):
    """
    Abstract base class for all device providers.
    Provides uniform lifecycle, capability descriptor generation, and safe execution.
    """

    def __init__(self, capability_id: str, name: str, subsystem: str):
        self.capability_id = capability_id
        self.name = name
        self.subsystem = subsystem
        self.state = CapabilityState.DETECTED
        self.error: Optional[str] = None
        self._supported_operations: List[str] = []
        self._requires_permission = False
        self._permission_name: Optional[str] = None

    @abstractmethod
    def discover(self) -> CapabilityDescriptor:
        """
        Discovers subsystem presence, inspects system boundaries, and updates state.
        Must NOT attempt unauthorized operations.
        """
        pass

    def get_status(self) -> Dict[str, Any]:
        """Returns operational status and telemetry for this capability."""
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "subsystem": self.subsystem,
            "state": self.state.value,
            "supported_operations": list(self._supported_operations),
            "requires_permission": self._requires_permission,
            "permission_name": self._permission_name,
            "error": self.error,
        }

    def execute(self, operation: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Executes an operation specific to this capability.
        Subclasses override to implement supported operations.
        """
        if operation not in self._supported_operations:
            return {
                "success": False,
                "operation": operation,
                "error": f"Operation '{operation}' not supported by capability '{self.capability_id}'",
                "supported_operations": list(self._supported_operations),
            }

        if self.state not in (CapabilityState.READY, CapabilityState.ACTIVE, CapabilityState.AVAILABLE):
            return {
                "success": False,
                "operation": operation,
                "error": f"Capability '{self.capability_id}' is not in an executable state (current: {self.state.value})",
            }

        try:
            return self._handle_operation(operation, parameters or {})
        except Exception as e:
            logger.error("Error executing %s on %s: %s", operation, self.capability_id, e)
            return {
                "success": False,
                "operation": operation,
                "error": str(e),
            }

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Internal dispatch for capability-specific operations."""
        return {"success": False, "error": f"Operation '{operation}' handler not implemented"}

    def start_session(self, parameters: Optional[Dict[str, Any]] = None, timeout_sec: float = 300.0) -> Dict[str, Any]:
        """Starts a persistent or streaming session if supported by provider."""
        return {"success": False, "error": f"Sessions not supported for capability '{self.capability_id}'"}

    def stop_session(self, session_id: Optional[str] = None, reason: str = "Requested stop") -> Dict[str, Any]:
        """Stops an active session if supported."""
        return {"success": True, "message": "No active session"}

    def health_check(self) -> Tuple[bool, Optional[str]]:
        """Verifies health of the provider."""
        if self.state == CapabilityState.ERROR:
            return False, self.error
        return True, None

    def build_descriptor(self, details: Optional[Dict[str, Any]] = None) -> CapabilityDescriptor:
        """Constructs an up-to-date CapabilityDescriptor."""
        return CapabilityDescriptor(
            capability_id=self.capability_id,
            name=self.name,
            subsystem=self.subsystem,
            state=self.state,
            supported_operations=list(self._supported_operations),
            requires_permission=self._requires_permission,
            permission_name=self._permission_name,
            details=details or {},
            error=self.error,
        )
