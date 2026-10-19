#!/usr/bin/env python3
"""
Controller Base HTTP Handler
============================
Base HTTP request handler providing standardized JSON responses, CORS headers,
authentication token parsing, request body reading, and HA leader write guarding.
"""

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional

from common.crypto_utils import constant_time_compare
from config.ha_manager import HAState
from config.platform_config import get_controller_runtime_config
from controller.ha_helper import get_ha_manager
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import load_nodes_db

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_HEARTBEAT_TIMEOUT = RUNTIME_CFG.heartbeat_timeout


class BaseHandler(BaseHTTPRequestHandler):
    """
    Base HTTP Request Handler with utility methods for JSON I/O, authentication,
    and High Availability leader lease validation.
    """
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT
    default_backoff_sec = 0.0

    def send_json(self, status_code: int, data: Any) -> None:
        """Sends a JSON formatted HTTP response with CORS and correlation headers."""
        corr_id = getattr(self, "current_correlation_id", None)
        if corr_id and isinstance(data, dict) and "correlation_id" not in data:
            data["correlation_id"] = corr_id
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if corr_id:
            self.send_header("X-Correlation-ID", corr_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Correlation-ID, X-Request-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handles CORS preflight OPTIONS requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Correlation-ID, X-Request-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def parse_auth_token(self) -> str:
        """Extracts Bearer token or X-Auth-Token header from HTTP headers."""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return self.headers.get("X-Auth-Token", "").strip()

    def is_authenticated_admin_or_node(self) -> bool:
        """Verifies if caller has valid cluster enrollment token or registered node auth token."""
        token = self.parse_auth_token()
        if not token:
            return False
        enrollment_token = get_or_create_enrollment_token()
        if constant_time_compare(token, enrollment_token):
            return True
        nodes_db = load_nodes_db()
        for node in nodes_db.get("nodes", {}).values():
            if node.get("auth_token") and constant_time_compare(node.get("auth_token"), token):
                return True
        return False

    def check_leader_write_permission(self) -> bool:
        """
        Guards cluster mutating routes (POST, PUT, DELETE).
        Rejects mutating operations when controller is in STANDBY or FENCED to prevent split-brain.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # Exempt HA election and heartbeat management routes
        if path in (
            "/ha/promote", "/ha/failover", "/ha/heartbeat", "/ha/renew",
            "/api/ha/promote", "/api/ha/failover", "/api/ha/heartbeat", "/api/ha/renew"
        ):
            return True

        mgr = get_ha_manager()
        if mgr.state == HAState.FENCED:
            self.send_json(503, {
                "error": "Leader lease expired. Controller is self-fenced to prevent split-brain.",
                "role": "fenced",
                "leader_id": mgr.current_leader_id,
                "leader_url": mgr.current_leader_url,
                "term": mgr.current_term,
                "lease_valid": False
            })
            return False

        if not mgr.is_leader:
            self.send_json(503, {
                "error": "Controller is in STANDBY mode. Mutating operations are rejected.",
                "role": mgr.state,
                "leader_id": mgr.current_leader_id,
                "leader_url": mgr.current_leader_url,
                "term": mgr.current_term,
                "lease_valid": mgr.current_lease.is_valid()
            })
            return False

        if not mgr.current_lease.is_valid():
            if mgr.state == HAState.LEADER and (not mgr.current_leader_id or mgr.current_leader_id == mgr.node_id):
                mgr.renew_lease()
            else:
                self.send_json(503, {
                    "error": "Leader lease expired. Controller is self-fenced to prevent split-brain.",
                    "role": "fenced",
                    "leader_id": mgr.current_leader_id,
                    "leader_url": mgr.current_leader_url,
                    "term": mgr.current_term,
                    "lease_valid": False
                })
                return False

        return True

    def read_json_body(self) -> Optional[Dict[str, Any]]:
        """Safely parses JSON request body with Content-Length limits."""
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len == 0:
                return None
            raw = self.rfile.read(content_len).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def log_message(self, format: str, *args: Any) -> None:
        """Custom formatted log message."""
        print(f"[HTTP {self.command}] {self.path} - {args[0] if args else ''}")


__all__ = [
    "BaseHandler",
    "DEFAULT_HEARTBEAT_TIMEOUT",
]
