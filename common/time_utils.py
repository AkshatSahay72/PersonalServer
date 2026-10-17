#!/usr/bin/env python3
"""
Time and Timestamp Utilities
============================
Provides standardized, timezone-aware ISO-8601 UTC timestamp generation.
"""

from datetime import datetime, timezone


def get_current_iso_timestamp() -> str:
    """Returns the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
