#!/usr/bin/env python3
"""
Unit Tests for Leader-Aware Ingress & Gateway Follower Forwarding
================================================================
Verifies that:
1. When local node is the active cluster leader, ingress routes to local applications.
2. When local node is a follower, ingress proxies traffic to the current leader's gateway over private network.
3. When no valid leader lease exists across the cluster, ingress returns controlled HTTP 503 unavailable.
4. Gateway health checks (/health) always succeed locally without triggering forwarding.
"""

import os
import sys
import json
import time
import socket
import unittest
import threading
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from router.app_router import ApplicationRouter, resolve_leader_gateway, RouterHTTPHandler
from config.ha_manager import LeaderLease
from config.platform_config import override_controller_paths


class MockUpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-From-Leader", "true")
        self.end_headers()
        response = {
            "status": "ok",
            "path": self.path,
            "routed_by": self.headers.get("X-PersonalServer-Routed-By"),
            "host_header": self.headers.get("Host")
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "created", "echo": body}).encode("utf-8"))

    def log_message(self, format, *args):
        pass


class TestLeaderAwareIngress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start a mock leader gateway server
        cls.leader_server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.leader_port = cls.leader_server.server_address[1]
        cls.leader_thread = threading.Thread(target=cls.leader_server.serve_forever, daemon=True)
        cls.leader_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.leader_server.shutdown()
        cls.leader_server.server_close()

    def test_standalone_or_local_leader_state(self):
        """When no lease file exists or local node is leader, state is LOCAL_LEADER."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with override_controller_paths(data_dir=tmp_path, runtime_dir=tmp_path):
                # 1. No lease file exists
                is_leader, gw_url, state = resolve_leader_gateway()
                self.assertTrue(is_leader)
                self.assertIsNone(gw_url)
                self.assertEqual(state, "LOCAL_LEADER")

                # 2. Local node is active leader with valid lease
                lease = LeaderLease(
                    term=5,
                    leader_id="local-node-1",
                    leader_url="http://127.0.0.1:8000",
                    lease_expires_at=time.time() + 60.0
                )
                lease_file = tmp_path / "leader_lease.json"
                lease.save_to_file(lease_file)

                # Set environment to match local node ID
                os.environ["PERSONALSERVER_NODE_ID"] = "local-node-1"
                try:
                    is_leader, gw_url, state = resolve_leader_gateway()
                    self.assertTrue(is_leader)
                    self.assertEqual(state, "LOCAL_LEADER")
                finally:
                    os.environ.pop("PERSONALSERVER_NODE_ID", None)

    def test_follower_node_resolves_leader_gateway(self):
        """When local node is follower, resolves remote leader gateway URL."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with override_controller_paths(data_dir=tmp_path, runtime_dir=tmp_path):
                # Lease points to remote leader
                lease = LeaderLease(
                    term=5,
                    leader_id="vivo-y31-master",
                    leader_url="http://100.85.108.5:8000",
                    lease_expires_at=time.time() + 60.0
                )
                lease.save_to_file(tmp_path / "leader_lease.json")

                nodes_db = {
                    "nodes": {
                        "vivo-y31-master": {
                            "node_id": "vivo-y31-master",
                            "tailscale_ip": "100.85.108.5",
                            "status": "ONLINE"
                        }
                    }
                }

                os.environ["PERSONALSERVER_NODE_ID"] = "realme-follower"
                try:
                    is_leader, gw_url, state = resolve_leader_gateway(nodes_db=nodes_db)
                    self.assertFalse(is_leader)
                    self.assertEqual(state, "FOLLOWER")
                    self.assertEqual(gw_url, "http://100.85.108.5:8088")
                finally:
                    os.environ.pop("PERSONALSERVER_NODE_ID", None)

    def test_expired_lease_returns_no_valid_leader(self):
        """When leader lease is expired, state is NO_VALID_LEADER."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with override_controller_paths(data_dir=tmp_path, runtime_dir=tmp_path):
                # Expired lease
                lease = LeaderLease(
                    term=5,
                    leader_id="vivo-y31-master",
                    leader_url="http://100.85.108.5:8000",
                    lease_expires_at=time.time() - 10.0
                )
                lease.save_to_file(tmp_path / "leader_lease.json")

                is_leader, gw_url, state = resolve_leader_gateway()
                self.assertFalse(is_leader)
                self.assertEqual(state, "NO_VALID_LEADER")
                self.assertIsNone(gw_url)

    def test_follower_gateway_http_forwarding(self):
        """Follower router gateway proxies requests to leader and returns upstream response."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with override_controller_paths(data_dir=tmp_path, runtime_dir=tmp_path):
                # Lease points to mock leader server running on local port
                lease = LeaderLease(
                    term=2,
                    leader_id="mock-leader",
                    leader_url=f"http://127.0.0.1:{self.leader_port}",
                    lease_expires_at=time.time() + 60.0
                )
                lease.save_to_file(tmp_path / "leader_lease.json")

                nodes_db = {
                    "nodes": {
                        "mock-leader": {
                            "node_id": "mock-leader",
                            "ip": "127.0.0.1",
                            "tailscale_ip": "127.0.0.1",
                            "status": "ONLINE"
                        }
                    }
                }
                with open(tmp_path / "nodes.json", "w", encoding="utf-8") as f:
                    json.dump(nodes_db, f)

                os.environ["PERSONALSERVER_NODE_ID"] = "follower-node"
                try:
                    # Start router HTTP server on ephemeral port
                    router_srv = ThreadingHTTPServer(("127.0.0.1", 0), RouterHTTPHandler)
                    router_port = router_srv.server_address[1]
                    router_thread = threading.Thread(target=router_srv.serve_forever, daemon=True)
                    router_thread.start()

                    try:
                        # 1. Health check succeeds locally
                        health_req = urllib.request.Request(f"http://127.0.0.1:{router_port}/health")
                        with urllib.request.urlopen(health_req, timeout=2) as resp:
                            self.assertEqual(resp.status, 200)
                            data = json.loads(resp.read().decode("utf-8"))
                            self.assertEqual(data.get("service"), "PersonalServer-AppRouter")

                        # 2. Application request forwarded to leader
                        # We mock resolve_leader_gateway to point to our mock leader port
                        from router import app_router
                        orig_resolve = app_router.resolve_leader_gateway
                        app_router.resolve_leader_gateway = lambda **kw: (False, f"http://127.0.0.1:{self.leader_port}", "FOLLOWER")

                        try:
                            app_req = urllib.request.Request(
                                f"http://127.0.0.1:{router_port}/exambuddy/api/test?arg=1",
                                headers={"Host": "akshatsahay.space"}
                            )
                            with urllib.request.urlopen(app_req, timeout=2) as resp:
                                self.assertEqual(resp.status, 200)
                                self.assertEqual(resp.headers.get("X-From-Leader"), "true")
                                payload = json.loads(resp.read().decode("utf-8"))
                                self.assertEqual(payload["routed_by"], "follower-gateway")
                                self.assertEqual(payload["host_header"], "akshatsahay.space")
                                self.assertEqual(payload["path"], "/exambuddy/api/test?arg=1")

                            # 3. Test NO_VALID_LEADER returns 503
                            app_router.resolve_leader_gateway = lambda **kw: (False, None, "NO_VALID_LEADER")
                            with self.assertRaises(urllib.error.HTTPError) as ctx:
                                urllib.request.urlopen(f"http://127.0.0.1:{router_port}/exambuddy", timeout=2)
                            self.assertEqual(ctx.exception.code, 503)
                            self.assertEqual(ctx.exception.headers.get("Retry-After"), "5")
                            err_data = json.loads(ctx.exception.read().decode("utf-8"))
                            self.assertEqual(err_data["status"], 503)
                            self.assertEqual(err_data["leader_state"], "unavailable")

                        finally:
                            app_router.resolve_leader_gateway = orig_resolve

                    finally:
                        router_srv.shutdown()
                        router_srv.server_close()

                finally:
                    os.environ.pop("PERSONALSERVER_NODE_ID", None)


if __name__ == "__main__":
    unittest.main()
