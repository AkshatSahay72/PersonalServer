"""
Session Manager for Device Runtime (D1)
=======================================
Manages bounded operational and hardware sessions on mobile nodes.
Guarantees automatic watchdog termination so devices never drain power or leak sensors.
"""

from enum import Enum
from typing import Dict, Any, Optional, List, Callable
import time
import uuid
import threading
import logging

logger = logging.getLogger("agent.device_runtime.session")


class DeviceSessionState(str, Enum):
    REQUESTED = "REQUESTED"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


class DeviceSession:
    """Represents an active, time-bounded hardware or operational session."""

    def __init__(
        self,
        session_id: str,
        capability_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0,
        cleanup_callback: Optional[Callable[["DeviceSession"], None]] = None,
    ):
        self.session_id = session_id
        self.capability_id = capability_id
        self.parameters = parameters or {}
        self.timeout_sec = max(5.0, min(timeout_sec, 3600.0)) # Bounded between 5s and 1hr
        self.cleanup_callback = cleanup_callback

        self.state = DeviceSessionState.REQUESTED
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.stop_reason: Optional[str] = None
        self.error: Optional[str] = None
        self.extra_data: Dict[str, Any] = {}

    def start(self) -> None:
        """Transitions to ACTIVE."""
        self.state = DeviceSessionState.ACTIVE
        self.started_at = time.time()

    def close(self, reason: str = "Requested stop") -> None:
        """Closes session cleanly and executes cleanup callback."""
        if self.state in (DeviceSessionState.CLOSED, DeviceSessionState.TIMED_OUT):
            return
        self.state = DeviceSessionState.CLOSED
        self.stopped_at = time.time()
        self.stop_reason = reason
        if self.cleanup_callback:
            try:
                self.cleanup_callback(self)
            except Exception as e:
                logger.error("Error in session %s cleanup callback: %s", self.session_id, e)

    def mark_timed_out(self) -> None:
        """Marks session as timed out and fires cleanup."""
        if self.state in (DeviceSessionState.CLOSED, DeviceSessionState.TIMED_OUT):
            return
        self.state = DeviceSessionState.TIMED_OUT
        self.stopped_at = time.time()
        self.stop_reason = "Safety watchdog timeout expired"
        if self.cleanup_callback:
            try:
                self.cleanup_callback(self)
            except Exception as e:
                logger.error("Error in session %s timeout cleanup: %s", self.session_id, e)

    def is_expired(self) -> bool:
        """Checks if session has exceeded its configured duration."""
        if self.state != DeviceSessionState.ACTIVE or not self.started_at:
            return False
        return (time.time() - self.started_at) >= self.timeout_sec

    @property
    def remaining_sec(self) -> float:
        """Remaining duration in seconds before watchdog termination."""
        if self.state != DeviceSessionState.ACTIVE or not self.started_at:
            return 0.0
        elapsed = time.time() - self.started_at
        return max(0.0, self.timeout_sec - elapsed)

    @property
    def duration_sec(self) -> float:
        """Total duration session was active."""
        if not self.started_at:
            return 0.0
        end = self.stopped_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes session state for telemetry and controller sync."""
        return {
            "session_id": self.session_id,
            "capability_id": self.capability_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "timeout_sec": self.timeout_sec,
            "remaining_sec": round(self.remaining_sec, 2),
            "duration_sec": round(self.duration_sec, 2),
            "stop_reason": self.stop_reason,
            "error": self.error,
            "parameters": self.parameters,
            "extra_data": self.extra_data,
        }


class DeviceSessionManager:
    """Manages active sessions and background watchdog sweeping."""

    def __init__(self, sweep_interval_sec: float = 2.0):
        self._sessions: Dict[str, DeviceSession] = {}
        self._lock = threading.Lock()
        self._sweep_interval = sweep_interval_sec
        self._running = True
        self._sweeper_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="DeviceSessionSweeper")
        self._sweeper_thread.start()

    def create_session(
        self,
        capability_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 300.0,
        cleanup_callback: Optional[Callable[[DeviceSession], None]] = None,
    ) -> DeviceSession:
        """Creates and stores a new bounded session."""
        session_id = f"dsess-{uuid.uuid4().hex[:12]}"
        session = DeviceSession(
            session_id=session_id,
            capability_id=capability_id,
            parameters=parameters,
            timeout_sec=timeout_sec,
            cleanup_callback=cleanup_callback,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[DeviceSession]:
        """Retrieves session by ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str, reason: str = "Requested stop") -> Optional[DeviceSession]:
        """Closes an active session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.close(reason)
                return session
        return None

    def close_all_sessions(self, reason: str = "Emergency pause / shutdown") -> List[DeviceSession]:
        """Closes all active sessions immediately."""
        closed = []
        with self._lock:
            for session in self._sessions.values():
                if session.state in (DeviceSessionState.ACTIVE, DeviceSessionState.REQUESTED):
                    session.close(reason)
                    closed.append(session)
        return closed

    def list_active(self) -> List[DeviceSession]:
        """Returns all currently active sessions."""
        with self._lock:
            return [s for s in self._sessions.values() if s.state == DeviceSessionState.ACTIVE]

    def _watchdog_loop(self) -> None:
        """Periodic background thread checking for expired sessions."""
        while self._running:
            time.sleep(self._sweep_interval)
            expired = []
            with self._lock:
                for s in self._sessions.values():
                    if s.is_expired():
                        expired.append(s)

            for s in expired:
                logger.warning(
                    "Watchdog terminating expired session %s (capability: %s, duration: %.1fs)",
                    s.session_id,
                    s.capability_id,
                    s.duration_sec,
                )
                s.mark_timed_out()

    def shutdown(self) -> None:
        """Stops the watchdog sweeper and terminates all sessions."""
        self._running = False
        self.close_all_sessions("Session manager shutdown")

