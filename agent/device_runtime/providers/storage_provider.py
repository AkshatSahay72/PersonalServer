"""
Storage Provider for Device Runtime (D1)
========================================
Exposes storage capacity, mount points, and safe file operations.
Operates within designated PersonalServer roots and verified storage paths.
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from agent.device_runtime.capability_registry import (
    CapabilityDescriptor,
    CapabilityState,
)
from agent.device_runtime.providers.base import BaseDeviceProvider


class StorageProvider(BaseDeviceProvider):
    """Provides non-destructive storage metrics and validation."""

    def __init__(self, primary_storage_path: Optional[str] = None):
        super().__init__(
            capability_id="storage",
            name="Host Storage Subsystem",
            subsystem="storage",
        )
        self._storage_path = primary_storage_path or (
            "/storage/emulated/0" if os.path.exists("/storage/emulated/0") else os.path.expanduser("~")
        )
        self._supported_operations = [
            "GET_STORAGE_METRICS",
            "VERIFY_PATH_ACCESSIBLE",
        ]
        self._requires_permission = False

    def discover(self) -> CapabilityDescriptor:
        """Discovers primary storage capacity and accessibility."""
        try:
            total_gb, avail_gb, used_gb = self._read_capacity()
            details = {
                "primary_path": self._storage_path,
                "storage_total_gb": round(total_gb, 1),
                "storage_available_gb": round(avail_gb, 1),
                "storage_used_gb": round(used_gb, 1),
                "shared_storage_accessible": os.path.exists("/storage/emulated/0"),
            }
            self.state = CapabilityState.READY
            return self.build_descriptor(details)
        except Exception as e:
            self.state = CapabilityState.ERROR
            self.error = str(e)
            return self.build_descriptor({"error": str(e)})

    def _read_capacity(self) -> Tuple[float, float, float]:
        if hasattr(os, "statvfs"):
            st = os.statvfs(self._storage_path)
            total_b = st.f_blocks * st.f_frsize
            avail_b = st.f_bavail * st.f_frsize
            used_b = total_b - avail_b
            return total_b / (1024 ** 3), avail_b / (1024 ** 3), used_b / (1024 ** 3)
        import shutil
        total, used, free = shutil.disk_usage(self._storage_path)
        return total / (1024 ** 3), free / (1024 ** 3), used / (1024 ** 3)

    def _handle_operation(self, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if operation == "GET_STORAGE_METRICS":
            total_gb, avail_gb, used_gb = self._read_capacity()
            pct = (used_gb / total_gb * 100.0) if total_gb > 0 else 0.0
            return {
                "success": True,
                "primary_path": self._storage_path,
                "storage_total_gb": round(total_gb, 2),
                "storage_available_gb": round(avail_gb, 2),
                "storage_used_gb": round(used_gb, 2),
                "used_percent": round(pct, 1),
            }

        if operation == "VERIFY_PATH_ACCESSIBLE":
            check_path = parameters.get("path", self._storage_path)
            p = Path(check_path)
            exists = p.exists()
            readable = os.access(check_path, os.R_OK) if exists else False
            writable = os.access(check_path, os.W_OK) if exists else False
            return {
                "success": True,
                "path": check_path,
                "exists": exists,
                "is_dir": p.is_dir() if exists else False,
                "readable": readable,
                "writable": writable,
            }

        return super()._handle_operation(operation, parameters)
