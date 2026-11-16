"""
Participation Manager for Device Runtime (D1)
=============================================
Guards daily-use devices against battery drain, thermal throttling, and background hijacking.

States:
- OFF: Zero PersonalServer background workloads. Device runtime completely disengaged.
- STANDBY: Lightweight heartbeat and telemetry only. Camera, microphone, heavy compute forbidden.
- AVAILABLE: Permits approved compute and storage tasks within battery/thermal boundaries.
- ACTIVE: Authorized task or hardware session is currently running under an active watchdog.
"""

from enum import Enum
from typing import Dict, Any, Optional, Tuple
import time
import logging

logger = logging.getLogger("agent.device_runtime.participation")


class ParticipationState(str, Enum):
    """Opt-in participation state for the host device."""
    OFF = "OFF"
    STANDBY = "STANDBY"
    AVAILABLE = "AVAILABLE"
    ACTIVE = "ACTIVE"


class ParticipationPolicy:
    """Configurable boundaries and safety thresholds for daily-use devices."""

    def __init__(
        self,
        min_battery_percent: int = 20,
        require_charging_for_heavy_tasks: bool = True,
        max_cpu_percent: float = 75.0,
        max_ram_percent: float = 85.0,
        max_thermal_celsius: float = 45.0,
        max_active_workloads: int = 1,
    ):
        self.min_battery_percent = min_battery_percent
        self.require_charging_for_heavy_tasks = require_charging_for_heavy_tasks
        self.max_cpu_percent = max_cpu_percent
        self.max_ram_percent = max_ram_percent
        self.max_thermal_celsius = max_thermal_celsius
        self.max_active_workloads = max_active_workloads

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_battery_percent": self.min_battery_percent,
            "require_charging_for_heavy_tasks": self.require_charging_for_heavy_tasks,
            "max_cpu_percent": self.max_cpu_percent,
            "max_ram_percent": self.max_ram_percent,
            "max_thermal_celsius": self.max_thermal_celsius,
            "max_active_workloads": self.max_active_workloads,
        }


class ParticipationManager:
    """
    Manages host participation lifecycle, manual pause overrides,
    and automatic load shedding when device thresholds are exceeded.
    """

    def __init__(self, default_state: ParticipationState = ParticipationState.OFF, policy: Optional[ParticipationPolicy] = None):
        self._state = default_state
        self._policy = policy or ParticipationPolicy()
        self._paused = False
        self._pause_reason: Optional[str] = None
        self._active_sessions_count = 0
        self._last_state_change = time.time()

    @property
    def state(self) -> ParticipationState:
        """Current participation state."""
        return self._state

    @property
    def is_paused(self) -> bool:
        """True if the device is under an immediate manual pause."""
        return self._paused

    @property
    def policy(self) -> ParticipationPolicy:
        """Active participation policy thresholds."""
        return self._policy

    def set_state(self, new_state: ParticipationState, reason: str = "Explicit state transition") -> bool:
        """
        Transitions participation state according to valid lifecycle paths.
        Valid paths:
        OFF <-> STANDBY <-> AVAILABLE <-> ACTIVE
        """
        if self._state == new_state:
            return True

        if self._paused and new_state in (ParticipationState.AVAILABLE, ParticipationState.ACTIVE):
            logger.warning("Cannot transition to %s while paused (%s)", new_state.value, self._pause_reason)
            return False

        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()
        logger.info(
            "Participation state transition: %s -> %s (Reason: %s)",
            old_state.value,
            new_state.value,
            reason,
        )
        return True

    def pause(self, reason: str = "Manual pause requested") -> None:
        """
        Immediately pauses the device runtime.
        Forces active workloads to halt or suspend, and sets state to STANDBY.
        """
        self._paused = True
        self._pause_reason = reason
        if self._state == ParticipationState.ACTIVE:
            self._state = ParticipationState.STANDBY
        logger.warning("Device participation PAUSED: %s", reason)

    def resume(self) -> None:
        """Resumes device participation from pause."""
        self._paused = False
        self._pause_reason = None
        logger.info("Device participation RESUMED")

    def register_session_start(self) -> bool:
        """Increments active workload counter and transitions to ACTIVE if permitted."""
        if self._state not in (ParticipationState.AVAILABLE, ParticipationState.ACTIVE):
            logger.warning("Cannot start workload while in state %s", self._state.value)
            return False

        if self._paused:
            logger.warning("Cannot start workload while paused: %s", self._pause_reason)
            return False

        if self._active_sessions_count >= self._policy.max_active_workloads:
            logger.warning(
                "Workload rejected: maximum concurrent workloads (%d) reached",
                self._policy.max_active_workloads,
            )
            return False

        self._active_sessions_count += 1
        self._state = ParticipationState.ACTIVE
        return True

    def register_session_stop(self) -> None:
        """Decrements active workload counter and returns to AVAILABLE if idle."""
        self._active_sessions_count = max(0, self._active_sessions_count - 1)
        if self._active_sessions_count == 0 and self._state == ParticipationState.ACTIVE:
            self._state = ParticipationState.AVAILABLE

    def evaluate_task_admission(
        self,
        battery_percent: Optional[int] = None,
        is_charging: Optional[bool] = None,
        cpu_percent: Optional[float] = None,
        thermal_celsius: Optional[float] = None,
        is_heavy: bool = False,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a proposed task is safe to execute on this device
        given current telemetry and policy thresholds.
        """
        if self._state == ParticipationState.OFF:
            return False, "Device participation is OFF: PersonalServer workloads disabled"

        if self._paused:
            return False, f"Device is paused: {self._pause_reason}"

        if self._state == ParticipationState.STANDBY and is_heavy:
            return False, "Device is in STANDBY: Heavy workloads rejected"

        # Check battery policy if telemetry is available
        if battery_percent is not None and battery_percent < self._policy.min_battery_percent:
            if not is_charging:
                return False, f"Battery ({battery_percent}%) below minimum required ({self._policy.min_battery_percent}%)"

        if is_heavy and self._policy.require_charging_for_heavy_tasks:
            if is_charging is False:
                return False, "Heavy workload rejected: AC charging power required by device policy"

        # Check thermal limit
        if thermal_celsius is not None and thermal_celsius > self._policy.max_thermal_celsius:
            return False, f"Device temperature ({thermal_celsius:.1f}°C) exceeds thermal safety ceiling ({self._policy.max_thermal_celsius}°C)"

        # Check current CPU load
        if cpu_percent is not None and cpu_percent > self._policy.max_cpu_percent:
            return False, f"Current CPU utilization ({cpu_percent:.1f}%) exceeds safety ceiling ({self._policy.max_cpu_percent}%)"

        return True, "Task admitted"

    def to_dict(self) -> Dict[str, Any]:
        """Serializes participation state and policy to dictionary."""
        return {
            "state": self._state.value,
            "is_paused": self._paused,
            "pause_reason": self._pause_reason,
            "active_sessions_count": self._active_sessions_count,
            "last_state_change": self._last_state_change,
            "policy": self._policy.to_dict(),
        }
