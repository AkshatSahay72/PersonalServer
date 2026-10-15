#!/usr/bin/env python3
"""
Lightweight Structured Logger & Correlation Tracing for PersonalServer
=====================================================================
Provides zero-dependency structured logging, correlation ID propagation,
sensitive credential redaction, and in-memory diagnostic event tracing
for registration, scheduling, job attempts, worker failures, and recovery.
"""

import os
import re
import json
import secrets
from datetime import datetime, timezone
from collections import deque
from typing import Any, Dict, List, Optional
from threading import Lock

# Patterns for scrubbing tokens and credentials from free text
SENSITIVE_TEXT_PATTERNS = [
    (re.compile(r'(?i)(token[\s:=]+\s*bearer\s+)([a-zA-Z0-9_\-\.]{6,})'), r'\1[REDACTED]'),
    (re.compile(r'(?i)(bearer\s+)([a-zA-Z0-9_\-\.]{6,})'), r'\1[REDACTED]'),
    (re.compile(r'(?i)(token[\s:=]+["\']?)(?!bearer\s+)([a-zA-Z0-9_\-\.]{6,})(["\']?)'), r'\1[REDACTED]\3'),
    (re.compile(r'(?i)(password[\s:=]+["\']?)([^\s"\'&,;]+)(["\']?)'), r'\1[REDACTED]\3'),
    (re.compile(r'(?i)(secret[\s:=]+["\']?)([^\s"\'&,;]+)(["\']?)'), r'\1[REDACTED]\3'),
    (re.compile(r'(?i)(x-auth-token[\s:=]+["\']?)([a-zA-Z0-9_\-\.]{6,})(["\']?)'), r'\1[REDACTED]\3'),
    (re.compile(r'(?i)(auth_token[\s:=]+["\']?)([a-zA-Z0-9_\-\.]{6,})(["\']?)'), r'\1[REDACTED]\3'),
]

SENSITIVE_KEY_NAMES = {"token", "secret", "password", "key", "authorization", "cookie", "auth", "credential"}


def redact_sensitive_text(text: Any) -> str:
    """Scrub sensitive tokens and passwords from strings."""
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_sensitive_data(data: Any, max_depth: int = 10) -> Any:
    """
    Recursively redacts sensitive values and dictionary keys
    for safe structured logging and diagnostics.
    """
    if max_depth <= 0:
        return "[MAX_DEPTH]"
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(pat in k_lower for pat in SENSITIVE_KEY_NAMES):
                if v is None:
                    redacted[k] = None
                elif isinstance(v, str) and len(v) > 8:
                    redacted[k] = f"{v[:3]}...[REDACTED]...{v[-3:]}"
                else:
                    redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v, max_depth - 1)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item, max_depth - 1) for item in data]
    elif isinstance(data, str):
        return redact_sensitive_text(data)
    return data


def generate_correlation_id(prefix: str = "corr") -> str:
    """Generates an opaque, unique correlation identifier."""
    return f"{prefix}-{secrets.token_hex(6)}"


def extract_correlation_id(headers: Any = None, body: Any = None) -> str:
    """Extract correlation ID from incoming HTTP headers or JSON body, or generate a new one."""
    if headers:
        for header_name in ("X-Correlation-ID", "x-correlation-id", "X-Request-ID", "x-request-id"):
            val = headers.get(header_name)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    if isinstance(body, dict):
        val = body.get("correlation_id") or body.get("correlationId")
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return generate_correlation_id()


class StructuredLogger:
    """
    Lightweight, thread-safe structured logger with correlation tracing
    and in-memory ring buffer for failure diagnostics.
    """

    def __init__(self, subsystem: str = "PersonalServer", max_history: int = 200):
        self.subsystem = subsystem
        self.history: deque = deque(maxlen=max_history)
        self.lock = Lock()
        self.json_output = os.environ.get("PERSONALSERVER_LOG_JSON", "0").lower() in ("1", "true", "yes")

    def log(
        self,
        event: str,
        message: str = "",
        level: str = "INFO",
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        node_id: Optional[str] = None,
        **details
    ) -> Dict[str, Any]:
        """Record and output a structured diagnostic event."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        corr_id = correlation_id or generate_correlation_id()

        clean_message = redact_sensitive_text(message)
        clean_details = redact_sensitive_data(details)

        entry = {
            "timestamp": now,
            "subsystem": self.subsystem,
            "level": level.upper(),
            "event": event,
            "correlation_id": corr_id,
            "message": clean_message
        }
        if job_id:
            entry["job_id"] = str(job_id)
        if node_id:
            entry["node_id"] = str(node_id)
        if clean_details:
            entry["details"] = clean_details

        with self.lock:
            self.history.append(entry)

        # Console output
        if self.json_output:
            print(json.dumps(entry))
        else:
            context_parts = [f"[{entry['level']}]", f"[{self.subsystem}]", f"[{entry['event']}]"]
            if entry.get("job_id"):
                context_parts.append(f"job={entry['job_id']}")
            if entry.get("node_id"):
                context_parts.append(f"node={entry['node_id']}")
            context_parts.append(f"corr={corr_id}")
            context_str = " ".join(context_parts)
            print(f"{context_str} - {clean_message}" if clean_message else context_str)

        return entry

    def info(self, event: str, message: str = "", *args, **kwargs) -> Dict[str, Any]:
        if args and message:
            try:
                message = message % args
            except Exception:
                message = f"{message} {' '.join(str(a) for a in args)}"
        return self.log(event, message=message, level="INFO", **kwargs)

    def warning(self, event: str, message: str = "", *args, **kwargs) -> Dict[str, Any]:
        if args and message:
            try:
                message = message % args
            except Exception:
                message = f"{message} {' '.join(str(a) for a in args)}"
        return self.log(event, message=message, level="WARNING", **kwargs)

    def error(self, event: str, message: str = "", *args, **kwargs) -> Dict[str, Any]:
        if args and message:
            try:
                message = message % args
            except Exception:
                message = f"{message} {' '.join(str(a) for a in args)}"
        return self.log(event, message=message, level="ERROR", **kwargs)

    def get_recent_events(
        self,
        limit: int = 50,
        event: Optional[str] = None,
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        level: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Query recent in-memory events for diagnostics and API health responses."""
        with self.lock:
            items = list(self.history)

        filtered = []
        for item in reversed(items):
            if event and item.get("event") != event:
                continue
            if correlation_id and item.get("correlation_id") != correlation_id:
                continue
            if job_id and item.get("job_id") != job_id:
                continue
            if level and item.get("level") != level.upper():
                continue
            filtered.append(item)
            if len(filtered) >= limit:
                break

        return filtered


# Centralized controller logger instance
controller_logger = StructuredLogger(subsystem="Controller")
# Centralized node agent logger instance
agent_logger = StructuredLogger(subsystem="NodeAgent")
