"""
Resource Monitor for Device Runtime (D1)
========================================
Collects real-time, non-blocking hardware resource telemetry (CPU, RAM, storage,
thermals, battery) on Linux/Android platforms.
Handles SELinux restrictions gracefully without throwing or blocking.
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import logging

logger = logging.getLogger("agent.device_runtime.resources")


class SystemResourceMetrics:
    """Snapshot of device resource telemetry."""

    def __init__(
        self,
        cpu_cores: int,
        cpu_percent: float,
        ram_total_mb: int,
        ram_available_mb: int,
        ram_used_mb: int,
        ram_percent: float,
        storage_total_gb: float,
        storage_available_gb: float,
        storage_percent: float,
        thermal_celsius: Optional[float] = None,
        battery_percent: Optional[int] = None,
        is_charging: Optional[bool] = None,
        load_average: Optional[List[float]] = None,
    ):
        self.timestamp = time.time()
        self.cpu_cores = cpu_cores
        self.cpu_percent = cpu_percent
        self.ram_total_mb = ram_total_mb
        self.ram_available_mb = ram_available_mb
        self.ram_used_mb = ram_used_mb
        self.ram_percent = ram_percent
        self.storage_total_gb = storage_total_gb
        self.storage_available_gb = storage_available_gb
        self.storage_percent = storage_percent
        self.thermal_celsius = thermal_celsius
        self.battery_percent = battery_percent
        self.is_charging = is_charging
        self.load_average = load_average or [0.0, 0.0, 0.0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cpu_cores": self.cpu_cores,
            "cpu_percent": round(self.cpu_percent, 1),
            "ram_total_mb": self.ram_total_mb,
            "ram_available_mb": self.ram_available_mb,
            "ram_used_mb": self.ram_used_mb,
            "ram_percent": round(self.ram_percent, 1),
            "storage_total_gb": round(self.storage_total_gb, 1),
            "storage_available_gb": round(self.storage_available_gb, 1),
            "storage_percent": round(self.storage_percent, 1),
            "thermal_celsius": round(self.thermal_celsius, 1) if self.thermal_celsius is not None else None,
            "battery_percent": self.battery_percent,
            "is_charging": self.is_charging,
            "load_average": [round(x, 2) for x in self.load_average],
        }


class ResourceMonitor:
    """Non-blocking system resource telemetry collector."""

    def __init__(self, storage_path: Optional[str] = None):
        self._storage_path = storage_path or (
            "/storage/emulated/0" if os.path.exists("/storage/emulated/0") else os.path.expanduser("~")
        )
        self._last_cpu_times: Optional[Tuple[float, float]] = None

    def sample(self) -> SystemResourceMetrics:
        """Takes a fresh non-blocking sample of all system resources."""
        cpu_cores = self._get_cpu_cores()
        cpu_percent = self._sample_cpu_percent()
        ram_total, ram_avail, ram_used, ram_pct = self._sample_ram()
        st_total, st_avail, st_pct = self._sample_storage()
        thermals = self._sample_thermals()
        battery_pct, is_charging = self._sample_battery()
        load_avg = self._sample_load_avg()

        return SystemResourceMetrics(
            cpu_cores=cpu_cores,
            cpu_percent=cpu_percent,
            ram_total_mb=ram_total,
            ram_available_mb=ram_avail,
            ram_used_mb=ram_used,
            ram_percent=ram_pct,
            storage_total_gb=st_total,
            storage_available_gb=st_avail,
            storage_percent=st_pct,
            thermal_celsius=thermals,
            battery_percent=battery_pct,
            is_charging=is_charging,
            load_average=load_avg,
        )

    def _get_cpu_cores(self) -> int:
        try:
            return os.cpu_count() or 1
        except Exception:
            return 1

    def _sample_cpu_percent(self) -> float:
        """Computes CPU utilization percentage using delta from /proc/stat if readable."""
        stat_path = Path("/proc/stat")
        if not stat_path.exists():
            return 0.0

        try:
            with open(stat_path, "r", encoding="utf-8") as f:
                line = f.readline()
            if not line.startswith("cpu "):
                return 0.0

            fields = [float(x) for x in line.strip().split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0) # idle + iowait
            total = sum(fields)

            if self._last_cpu_times is None:
                self._last_cpu_times = (idle, total)
                return 0.0

            last_idle, last_total = self._last_cpu_times
            self._last_cpu_times = (idle, total)

            delta_idle = idle - last_idle
            delta_total = total - last_total

            if delta_total <= 0:
                return 0.0

            util = 100.0 * (1.0 - (delta_idle / delta_total))
            return max(0.0, min(100.0, util))
        except Exception:
            return 0.0

    def _sample_ram(self) -> Tuple[int, int, int, float]:
        """Reads RAM metrics from /proc/meminfo."""
        mem_path = Path("/proc/meminfo")
        if not mem_path.exists():
            return 0, 0, 0, 0.0

        total_kb = 0
        avail_kb = 0
        try:
            with open(mem_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb and avail_kb:
                        break

            total_mb = total_kb // 1024
            avail_mb = avail_kb // 1024
            used_mb = max(0, total_mb - avail_mb)
            pct = (used_mb / total_mb * 100.0) if total_mb > 0 else 0.0
            return total_mb, avail_mb, used_mb, pct
        except Exception:
            return 0, 0, 0, 0.0

    def _sample_storage(self) -> Tuple[float, float, float]:
        """Reads filesystem capacity using os.statvfs or shutil.disk_usage."""
        try:
            if hasattr(os, "statvfs"):
                st = os.statvfs(self._storage_path)
                total_b = st.f_blocks * st.f_frsize
                avail_b = st.f_bavail * st.f_frsize
                used_b = total_b - avail_b
            else:
                import shutil
                total_b, used_b, avail_b = shutil.disk_usage(self._storage_path)

            total_gb = total_b / (1024 ** 3)
            avail_gb = avail_b / (1024 ** 3)
            pct = (used_b / total_b * 100.0) if total_b > 0 else 0.0
            return total_gb, avail_gb, pct
        except Exception:
            return 0.0, 0.0, 0.0

    def _sample_thermals(self) -> Optional[float]:
        """Reads highest thermal zone temperature or PowerManager thermal status."""
        # Try native Android bridge first if available
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            bridge = NativeAndroidBridge.get_instance()
            if bridge.is_ready():
                resp = bridge.execute("thermals", "get_status", timeout=1.5)
                if resp.success and resp.data:
                    t_c = resp.data.get("battery_temperature_c") or resp.data.get("cpu_temperature_c")
                    if t_c is not None:
                        return float(t_c)
        except Exception:
            pass

        thermal_base = Path("/sys/class/thermal")
        if not thermal_base.exists():
            return None

        temps: List[float] = []
        try:
            for zone_dir in thermal_base.glob("thermal_zone*"):
                temp_file = zone_dir / "temp"
                if temp_file.exists():
                    try:
                        raw = temp_file.read_text().strip()
                        val = float(raw)
                        # Many thermal drivers report in millidegrees C
                        if val > 1000.0:
                            val /= 1000.0
                        if 10.0 <= val <= 110.0:
                            temps.append(val)
                    except Exception:
                        pass
            return max(temps) if temps else None
        except Exception:
            return None

    def _sample_battery(self) -> Tuple[Optional[int], Optional[bool]]:
        """
        Attempts reading battery status safely via Native Android Bridge or sysfs.
        Returns (battery_percent, is_charging).
        Does NOT fake values if unavailable.
        """
        # Try native Android bridge first if available
        try:
            from agent.device_runtime.android.bridge import NativeAndroidBridge
            bridge = NativeAndroidBridge.get_instance()
            if bridge.is_ready():
                resp = bridge.execute("battery", "get_status", timeout=1.5)
                if resp.success and resp.data:
                    pct = resp.data.get("percentage")
                    st = str(resp.data.get("status", "")).upper()
                    charging = None
                    if st:
                        if "DISCHARG" in st or "NOT" in st:
                            charging = False
                        elif "CHARG" in st or "FULL" in st:
                            charging = True
                    if pct is not None:
                        return int(pct), charging
        except Exception:
            pass

        bat_dir = Path("/sys/class/power_supply/battery")
        if not bat_dir.exists():
            # Alternative: check any power_supply
            ps_base = Path("/sys/class/power_supply")
            if ps_base.exists():
                try:
                    for d in ps_base.iterdir():
                        if "bat" in d.name.lower():
                            bat_dir = d
                            break
                except Exception:
                    pass

        pct = None
        charging = None

        if bat_dir.exists():
            try:
                cap_f = bat_dir / "capacity"
                if cap_f.exists():
                    pct = int(cap_f.read_text().strip())
            except Exception:
                pass

            try:
                st_f = bat_dir / "status"
                if st_f.exists():
                    st_val = st_f.read_text().strip().upper()
                    charging = ("CHARG" in st_val or "FULL" in st_val)
            except Exception:
                pass

        return pct, charging

    def _sample_load_avg(self) -> List[float]:
        try:
            return list(os.getloadavg())
        except Exception:
            return [0.0, 0.0, 0.0]
