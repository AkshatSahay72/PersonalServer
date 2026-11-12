"""
Connectivity Provider for Device Runtime (D1)
=============================================
Exposes network interfaces, Tailscale mesh connectivity, and local reachability.
"""

import socket
import subprocess
from typing import Dict, Any, Optional, List

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider


class ConnectivityProvider(BaseDeviceProvider):
    """Provides network interface inspection and Tailscale reachability verification."""

    def __init__(self):
        super().__init__(
            capability_id="connectivity",
            name="Network & Tailscale Subsystem",
            subsystem="connectivity",
        )
        self._supported_operations = [
            "GET_NETWORK_STATUS",
            "VERIFY_CONTROLLER_REACHABLE",
        ]
        self._requires_permission = False

    def discover(self) -> CapabilityDescriptor:
        """Discovers network interfaces and Tailscale IP."""
        try:
            tailscale_ip = self._detect_tailscale_ip()
            details = {
                "tailscale_ip": tailscale_ip,
                "tailscale_connected": bool(tailscale_ip),
            }
            self.state = CapabilityState.READY
            return self.build_descriptor(details)
        except Exception as e:
            self.state = CapabilityState.ERROR
            self.error = str(e)
            return self.build_descriptor({"error": str(e)})

    def _detect_tailscale_ip(self) -> Optional[str]:
        """Detects Tailscale 100.x.y.z IP from interfaces or command."""
        try:
            res = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2.0)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

        # Try socket probe or interface inspection
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("100.100.100.100", 53))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("100."):
                return ip
        except Exception:
            pass

        return None

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if operation == "GET_NETWORK_STATUS":
            ts_ip = self._detect_tailscale_ip()
            return {
                "success": True,
                "tailscale_ip": ts_ip,
                "tailscale_connected": bool(ts_ip),
                "hostname": socket.gethostname(),
            }

        if operation == "VERIFY_CONTROLLER_REACHABLE":
            host = parameters.get("host", "100.85.108.5")
            port = int(parameters.get("port", 8000))
            timeout = float(parameters.get("timeout_sec", 3.0))

            reachable = False
            err = None
            try:
                s = socket.create_connection((host, port), timeout=timeout)
                s.close()
                reachable = True
            except Exception as e:
                err = str(e)

            return {
                "success": True,
                "host": host,
                "port": port,
                "reachable": reachable,
                "error": err,
            }

        return super()._handle_operation(operation, parameters)

