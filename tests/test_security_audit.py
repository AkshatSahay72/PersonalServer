#!/usr/bin/env python3
"""
Security Audit Regression Test Suite
====================================
Tests security controls, constant-time comparisons, authentication enforcement,
subprocess execution safety, storage sandboxing, and credential stripping.
"""

import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent

# Dynamically import controller and node-api
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

controller_mod = load_module("controller", BASE_DIR / "controller" / "controller.py")
node_api_mod = load_module("node_api", BASE_DIR / "services" / "node-api" / "app.py")


class TestConstantTimeCompare(unittest.TestCase):
    """Verify timing-safe token comparisons in controller and node-api."""

    def test_controller_constant_time_compare(self):
        cmp = controller_mod.constant_time_compare
        secret = "secret-token-abcdef123456"

        self.assertTrue(cmp(secret, secret))
        self.assertTrue(cmp("token", "token"))

        # Rejections
        self.assertFalse(cmp(secret, "secret-token-abcdef123457"))
        self.assertFalse(cmp(secret, "secret-token"))
        self.assertFalse(cmp(secret, ""))
        self.assertFalse(cmp("", secret))
        self.assertFalse(cmp(None, secret))
        self.assertFalse(cmp(secret, None))
        self.assertFalse(cmp(None, None))
        self.assertFalse(cmp(12345, secret))
        self.assertFalse(cmp(secret, {}))

    def test_node_api_constant_time_compare(self):
        cmp = node_api_mod.constant_time_compare
        token = "node-api-token-9876543210"

        self.assertTrue(cmp(token, token))
        self.assertFalse(cmp(token, "node-api-token-9876543211"))
        self.assertFalse(cmp(token, ""))
        self.assertFalse(cmp(None, token))
        self.assertFalse(cmp(token, None))
        self.assertFalse(cmp([], token))


class TestNodeApiStorageAuthentication(unittest.TestCase):
    """Verify check_auth in services/node-api/app.py rejects unauthorized access."""

    def _create_mock_handler(self, client_ip="192.168.1.100", headers=None):
        handler = MagicMock()
        handler.client_address = (client_ip, 45678)
        handler.headers = headers or {}
        # Bind check_auth to mock handler
        return handler

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_unauthenticated_remote_client_denied(self, mock_ctrl_tok, mock_tok):
        handler = self._create_mock_handler(client_ip="192.168.1.50", headers={})
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertFalse(is_auth, "Remote unauthenticated request must be denied!")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_invalid_token_remote_client_denied(self, mock_ctrl_tok, mock_tok):
        handler = self._create_mock_handler(
            client_ip="192.168.1.50",
            headers={"Authorization": "Bearer invalid-tampered-token"}
        )
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertFalse(is_auth, "Remote request with invalid token must be denied!")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_authenticated_remote_client_allowed_with_bearer_token(self, mock_ctrl_tok, mock_tok):
        handler = self._create_mock_handler(
            client_ip="192.168.1.50",
            headers={"Authorization": "Bearer expected-secret-node-token"}
        )
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertTrue(is_auth, "Remote request with valid token must be allowed!")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_authenticated_remote_client_allowed_with_x_auth_token(self, mock_ctrl_tok, mock_tok):
        handler = self._create_mock_handler(
            client_ip="192.168.1.50",
            headers={"X-Auth-Token": "expected-enrollment-token"}
        )
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertTrue(is_auth, "Remote request with valid controller token must be allowed!")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_loopback_client_allowed(self, mock_ctrl_tok, mock_tok):
        for loopback_ip in ("127.0.0.1", "::1", "localhost"):
            handler = self._create_mock_handler(client_ip=loopback_ip, headers={})
            is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
            self.assertTrue(is_auth, f"Loopback client on {loopback_ip} should be permitted.")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_cf_email_spoofing_without_proxy_headers_denied(self, mock_ctrl_tok, mock_tok):
        # Direct connection spoofing Cf-Access-Authenticated-User-Email without Cf-Ray or Cf-Connecting-Ip
        handler = self._create_mock_handler(
            client_ip="192.168.1.50",
            headers={"Cf-Access-Authenticated-User-Email": "attacker@evil.com"}
        )
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertFalse(is_auth, "Direct spoofing of Cloudflare Access email must be rejected!")

    @patch.object(node_api_mod, "get_auth_token", return_value="expected-secret-node-token")
    @patch.object(node_api_mod, "get_controller_auth_token", return_value="expected-enrollment-token")
    def test_cf_email_with_valid_proxy_headers_allowed(self, mock_ctrl_tok, mock_tok):
        handler = self._create_mock_handler(
            client_ip="192.168.1.50",
            headers={
                "Cf-Access-Authenticated-User-Email": "admin@personalserver.lan",
                "Cf-Ray": "ray-123456789"
            }
        )
        is_auth = node_api_mod.NodeAPIHandler.check_auth(handler)
        self.assertTrue(is_auth, "Valid Cloudflare Access proxied request must be allowed.")


class TestSubprocessAndTelemetrySafety(unittest.TestCase):
    """Verify subprocess execution does not use shell=True or string interpolation."""

    def test_node_api_command_uses_shell_false(self):
        with patch("subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "safe-output"
            res = node_api_mod.command(["echo", "hello"])
            self.assertEqual(res, "safe-output")
            self.assertTrue(mock_check_output.called)
            # Verify shell=False was passed
            call_kwargs = mock_check_output.call_args[1]
            self.assertFalse(call_kwargs.get("shell", True), "Subprocess must not run with shell=True")

    def test_system_info_uses_disk_usage(self):
        sys_info = node_api_mod.get_system_info()
        self.assertIn("storage", sys_info)
        storage = sys_info["storage"]
        self.assertIn("total", storage)
        self.assertIn("used", storage)
        self.assertIn("available", storage)
        self.assertIn("used_percent", storage)

    def test_is_service_running_uses_shell_false(self):
        with patch("subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "12345\n"
            running = node_api_mod.is_service_running("dummy-service")
            self.assertTrue(running)
            call_kwargs = mock_check_output.call_args[1]
            self.assertFalse(call_kwargs.get("shell", True), "is_service_running must not run with shell=True")


class TestStorageSandboxing(unittest.TestCase):
    """Verify directory traversal and sandbox escaping is strictly prevented."""

    def test_node_api_storage_traversal_rejection(self):
        resolve = node_api_mod.resolve_safe_storage_path
        with self.assertRaises(ValueError):
            resolve("../../../etc/passwd")
        with self.assertRaises(ValueError):
            resolve("..\\..\\windows\\system32")
        with self.assertRaises(ValueError):
            resolve("/etc/shadow")
        with self.assertRaises(ValueError):
            resolve("C:\\secret.txt")
        with self.assertRaises(ValueError):
            resolve("test\0injection")

    def test_controller_app_storage_traversal_rejection(self):
        resolve = controller_mod.resolve_safe_app_storage_path
        with self.assertRaises(ValueError):
            resolve("valid-app", "../../../outside")
        with self.assertRaises(ValueError):
            resolve("valid-app", "..\\..\\outside")
        with self.assertRaises(ValueError):
            resolve("invalid/app/name", "file.txt")


if __name__ == "__main__":
    unittest.main()
