"""
Compute Provider for Device Runtime (D1)
========================================
Exposes host CPU architecture, execution cores, system memory, and load averages.
Operates safely through standard POSIX and Linux /proc interfaces.
"""

import os
import platform
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider


class ComputeProvider(BaseDeviceProvider):
    """Provides non-intrusive compute inspection and status."""

    def __init__(self):
        super().__init__(
            capability_id="compute",
            name="Host Compute Subsystem",
            subsystem="compute",
        )
        self._supported_operations = [
            "GET_CPU_INFO",
            "GET_MEMORY_INFO",
            "GET_COMPUTE_METRICS",
        ]
        self._requires_permission = False

    def discover(self) -> CapabilityDescriptor:
        """Discovers CPU and memory architecture."""
        try:
            cores = os.cpu_count() or 1
            arch = platform.machine() or "unknown"
            details = {
                "architecture": arch,
                "cores": cores,
                "platform": platform.system(),
            }

            # Check SoC details if readable on Android
            soc = self._get_android_soc()
            if soc:
                details["soc"] = soc

            self.state = CapabilityState.READY
            return self.build_descriptor(details)
        except Exception as e:
            self.state = CapabilityState.ERROR
            self.error = str(e)
            return self.build_descriptor({"error": str(e)})

    def _get_android_soc(self) -> Optional[str]:
        """Attempts to read Android SoC model property."""
        try:
            import subprocess
            res = subprocess.run(["getprop", "ro.soc.model"], capture_output=True, text=True, timeout=2.0)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            res2 = subprocess.run(["getprop", "ro.board.platform"], capture_output=True, text=True, timeout=2.0)
            if res2.returncode == 0 and res2.stdout.strip():
                return res2.stdout.strip()
        except Exception:
            pass
        return None

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if operation == "GET_CPU_INFO":
            cores = os.cpu_count() or 1
            load = list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0]
            return {
                "success": True,
                "architecture": platform.machine(),
                "cores": cores,
                "load_average": load,
                "soc": self._get_android_soc(),
            }

        if operation == "GET_MEMORY_INFO":
            total_mb, avail_mb = self._read_meminfo()
            return {
                "success": True,
                "ram_total_mb": total_mb,
                "ram_available_mb": avail_mb,
                "ram_used_mb": max(0, total_mb - avail_mb),
            }

        if operation == "GET_COMPUTE_METRICS":
            cores = os.cpu_count() or 1
            total_mb, avail_mb = self._read_meminfo()
            load = list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0]
            return {
                "success": True,
                "cores": cores,
                "ram_total_mb": total_mb,
                "ram_available_mb": avail_mb,
                "load_average": load,
            }

        return super()._handle_operation(operation, parameters)

    def _read_meminfo(self) -> Tuple[int, int]:
        mem_path = Path("/proc/meminfo")
        if not mem_path.exists():
            return 0, 0
        total_kb, avail_kb = 0, 0
        try:
            with open(mem_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb and avail_kb:
                        break
            return total_kb // 1024, avail_kb // 1024
        except Exception:
            return 0, 0
