#!/usr/bin/env python3
"""
Controller Failure Notification & Alert Deduplication Subsystem
================================================================
Prevents duplicate failure notifications and email spam during repeated
job retry cycles, transient worker disconnections, and lease recovery sweeps.

Deduplication Strategy:
- Deduplication key: (job_id, attempt, error_hash)
- Cooldown window: prevents repeated identical errors within cooldown_sec
- Persistent tracking inside job record: job["notifications"]
"""

import hashlib
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from common.time_utils import get_current_iso_timestamp
from config.structured_logger import controller_logger, generate_correlation_id


class FailureNotificationDeduplicator:
    """
    Thread-safe alert and notification deduplicator for job failure events.
    Tracks sent notifications in memory and within the persistent job record.
    """

    def __init__(self, default_cooldown_sec: float = 300.0):
        self.default_cooldown_sec = default_cooldown_sec
        self._lock = threading.Lock()
        # In-memory cache: dedup_key -> timestamp
        self._sent_cache: Dict[str, float] = {}

    @staticmethod
    def compute_error_hash(error_message: str) -> str:
        """Computes a deterministic 16-character hash of normalized error message."""
        normalized = (error_message or "").strip().lower()
        return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]

    def is_duplicate(
        self,
        job_id: str,
        attempt: int,
        error_message: str,
        cooldown_sec: Optional[float] = None,
        job_dict: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, str]:
        """
        Determines whether a failure notification for this job and error is duplicate.
        Returns (is_duplicate: bool, error_hash: str, reason: str).
        """
        cooldown = cooldown_sec if cooldown_sec is not None else self.default_cooldown_sec
        error_hash = self.compute_error_hash(error_message)
        attempt_key = f"{job_id}:{attempt}:{error_hash}"
        job_error_key = f"{job_id}:{error_hash}"
        now = time.time()

        with self._lock:
            # 1. Check exact attempt + error in-memory
            if attempt_key in self._sent_cache:
                return True, error_hash, f"Exact attempt {attempt} with error hash {error_hash} already notified."

            # 2. Check cooldown for same error on same job in-memory
            if job_error_key in self._sent_cache:
                last_time = self._sent_cache[job_error_key]
                elapsed = now - last_time
                if elapsed < cooldown:
                    return True, error_hash, f"Identical error notified {elapsed:.1f}s ago (cooldown {cooldown}s)."

            # 3. Check persistent record in job_dict if provided
            if job_dict and "notifications" in job_dict:
                notifs = job_dict["notifications"]
                sent_history = notifs.get("history", [])
                for entry in sent_history:
                    # Check exact attempt match
                    if entry.get("attempt") == attempt and entry.get("error_hash") == error_hash:
                        return True, error_hash, f"Persistent history: attempt {attempt} already notified."
                    # Check cooldown
                    if entry.get("error_hash") == error_hash:
                        entry_epoch = entry.get("timestamp_epoch", 0)
                        if (now - entry_epoch) < cooldown:
                            return True, error_hash, f"Persistent history: identical error in cooldown window."

            return False, error_hash, "New notification eligible."

    def record_notification(
        self,
        job_id: str,
        attempt: int,
        error_hash: str,
        job_dict: Optional[Dict[str, Any]] = None,
        is_terminal: bool = False,
        channel: str = "alert"
    ) -> None:
        """Records dispatched notification in memory and persistent job record."""
        now = time.time()
        now_iso = get_current_iso_timestamp()
        attempt_key = f"{job_id}:{attempt}:{error_hash}"
        job_error_key = f"{job_id}:{error_hash}"

        with self._lock:
            self._sent_cache[attempt_key] = now
            self._sent_cache[job_error_key] = now

            if job_dict is not None:
                notifs = job_dict.setdefault("notifications", {
                    "history": [],
                    "last_notified_at": None,
                    "last_error_hash": None,
                    "total_sent": 0,
                    "total_suppressed": 0
                })
                notifs["last_notified_at"] = now_iso
                notifs["last_error_hash"] = error_hash
                notifs["total_sent"] = notifs.get("total_sent", 0) + 1
                notifs.setdefault("history", []).append({
                    "timestamp": now_iso,
                    "timestamp_epoch": now,
                    "attempt": attempt,
                    "error_hash": error_hash,
                    "is_terminal": is_terminal,
                    "channel": channel
                })

    def record_suppression(
        self,
        job_id: str,
        attempt: int,
        error_hash: str,
        job_dict: Optional[Dict[str, Any]] = None
    ) -> None:
        """Increments suppression counter for duplicate notifications."""
        with self._lock:
            if job_dict is not None:
                notifs = job_dict.setdefault("notifications", {
                    "history": [],
                    "last_notified_at": None,
                    "last_error_hash": None,
                    "total_sent": 0,
                    "total_suppressed": 0
                })
                notifs["total_suppressed"] = notifs.get("total_suppressed", 0) + 1

    def notify_job_failure(
        self,
        job: Dict[str, Any],
        failure_reason: str,
        is_terminal: bool = False,
        cooldown_sec: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Processes a potential job failure notification with deduplication.
        Returns a dictionary summary: {'sent': bool, 'error_hash': str, 'reason': str}.
        """
        job_id = job.get("job_id", "unknown")
        attempt = job.get("attempt", 1)
        corr_id = job.get("correlation_id") or generate_correlation_id()

        is_dup, error_hash, reason = self.is_duplicate(
            job_id, attempt, failure_reason, cooldown_sec=cooldown_sec, job_dict=job
        )

        if is_dup:
            self.record_suppression(job_id, attempt, error_hash, job_dict=job)
            controller_logger.info(
                "notification.suppressed",
                f"Duplicate failure notification suppressed for job '{job_id}': {reason}",
                correlation_id=corr_id,
                job_id=job_id,
                attempt=attempt,
                error_hash=error_hash,
                is_terminal=is_terminal,
                suppression_reason=reason
            )
            return {
                "sent": False,
                "suppressed": True,
                "error_hash": error_hash,
                "reason": reason
            }

        # Dispatched alert
        self.record_notification(
            job_id, attempt, error_hash, job_dict=job, is_terminal=is_terminal
        )
        controller_logger.warning(
            "job.failure_alert",
            f"Failure alert emitted for job '{job_id}' (attempt {attempt}, terminal={is_terminal}): {failure_reason}",
            correlation_id=corr_id,
            job_id=job_id,
            attempt=attempt,
            error_hash=error_hash,
            is_terminal=is_terminal,
            target_node=job.get("target_node")
        )

        return {
            "sent": True,
            "suppressed": False,
            "error_hash": error_hash,
            "reason": reason,
            "is_terminal": is_terminal
        }


# Global shared deduplicator instance
GLOBAL_DEDUPLICATOR = FailureNotificationDeduplicator()


def notify_job_failure(
    job: Dict[str, Any],
    failure_reason: str,
    is_terminal: bool = False,
    cooldown_sec: Optional[float] = None
) -> Dict[str, Any]:
    """Convenience helper to notify job failures via GLOBAL_DEDUPLICATOR."""
    return GLOBAL_DEDUPLICATOR.notify_job_failure(
        job, failure_reason, is_terminal=is_terminal, cooldown_sec=cooldown_sec
    )


__all__ = [
    "FailureNotificationDeduplicator",
    "GLOBAL_DEDUPLICATOR",
    "notify_job_failure",
]
