#!/usr/bin/env python3
"""
Controller HTTP Handler
=======================
Main HTTP request handler for PersonalServer Controller.
Dispatches requests across HA, Node, Job, and Application routing domains.
"""

import urllib.parse
from typing import Any, Dict, Optional

from config.structured_logger import extract_correlation_id
from controller.http.app_routes import handle_app_delete, handle_app_get, handle_app_post
from controller.http.base_handler import BaseHandler, DEFAULT_HEARTBEAT_TIMEOUT
from controller.http.ha_routes import handle_ha_get, handle_ha_post
from controller.http.job_routes import handle_job_delete, handle_job_get, handle_job_post
from controller.http.node_routes import handle_node_delete, handle_node_get, handle_node_post


class ControllerHandler(BaseHandler):
    """
    HTTP request dispatcher for PersonalServer Controller.
    Implements REST endpoints for nodes, jobs, applications, and High Availability.
    """
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT
    default_backoff_sec = 0.0

    def do_GET(self) -> None:
        """Dispatches GET requests to domain route handlers."""
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        query = urllib.parse.parse_qs(parsed.query)

        # 1. HA & Health Routes
        if handle_ha_get(self, path):
            return

        # 2. Node & Cluster Routes
        if handle_node_get(self, path, query):
            return

        # 3. Job & Lease Routes
        if handle_job_get(self, path, query):
            return

        # 4. Application Routes
        if handle_app_get(self, path, query):
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_POST(self) -> None:
        """Dispatches POST requests to domain route handlers with leader write protection."""
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        query = urllib.parse.parse_qs(parsed.query)
        token = self.parse_auth_token()
        # For raw streaming artifact uploads, preserve rfile for stream consumption
        body = None if path == "/artifacts/upload/raw" else self.read_json_body()


        # 1. HA Routes (executed before leader write guard to allow leader election / heartbeats)
        if handle_ha_post(self, path, body, token):
            return

        # 2. Guard mutating routes against split-brain when controller is in STANDBY or FENCED mode
        if not self.check_leader_write_permission():
            return

        # 3. Node Registration & Heartbeat Routes
        if handle_node_post(self, path, body, token):
            return

        # 4. Job Submission & Result Routes
        if handle_job_post(self, path, body, token, query):
            return

        # 5. Application Management Routes
        if handle_app_post(self, path, body, token, query):
            return

        self.send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self) -> None:
        """Dispatches DELETE requests to domain route handlers with leader write protection."""
        self.current_correlation_id = extract_correlation_id(self.headers)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        query = urllib.parse.parse_qs(parsed.query)
        token = self.parse_auth_token()

        if not self.check_leader_write_permission():
            return

        # 1. Job Deletion Routes
        if handle_job_delete(self, path, token):
            return

        # 2. Application Deletion Routes
        if handle_app_delete(self, path, token, query):
            return

        # 3. Node Removal Routes
        if handle_node_delete(self, path, token):
            return

        self.send_json(404, {"error": "Endpoint not found"})


__all__ = [
    "ControllerHandler",
    "BaseHandler",
    "DEFAULT_HEARTBEAT_TIMEOUT",
]
