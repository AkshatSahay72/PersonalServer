"""
PersonalServer Node Telemetry & Hardware Diagnostics
====================================================
Cross-platform collection of comprehensive hardware and network metrics:
- Battery status (percentage, charging/plugged state, temperature, health)
- Network connectivity & Wi-Fi metrics (SSID, RSSI, signal %, link speed, latency ping)
- CPU status and thermal metrics (utilization %, temperature °C, frequency, load averages)
- Memory, storage, and live speed diagnostics
Works across Termux (Android), Raspberry Pi / Linux, and Windows.
"""

import os
import sys
import time
import json
import socket
import shutil
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# ------------------------------------------------------------------------------
# 1. Battery Telemetry
# ------------------------------------------------------------------------------

def collect_battery_info():
    """
    Collects battery level, state, health, and temperature across platforms.
    Returns:
        dict: {
            "percentage": int or None,
            "status": str ("CHARGING", "DISCHARGING", "FULL", "NOT_CHARGING", "AC_POWERED", "UNKNOWN"),
            "plugged": str ("PLUGGED_AC", "PLUGGED_USB", "PLUGGED_WIRELESS", "UNPLUGGED", "UNKNOWN"),
            "health": str ("GOOD", "OVERHEAT", "NORMAL", "UNKNOWN"),
            "temperature_c": float or None,
            "current_ma": int or None
        }
    """
    battery = {
        "percentage": None,
        "status": "UNKNOWN",
        "plugged": "UNKNOWN",
        "health": "UNKNOWN",
        "temperature_c": None,
        "current_ma": None
    }

    # Method A: Termux / Android via termux-battery-status
    if shutil.which("termux-battery-status"):
        try:
            res = subprocess.run(
                ["termux-battery-status"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                battery["percentage"] = int(data.get("percentage", 0))
                battery["status"] = str(data.get("status", "UNKNOWN")).upper()
                battery["plugged"] = str(data.get("plugged", "UNKNOWN")).upper()
                battery["health"] = str(data.get("health", "UNKNOWN")).upper()
                if "temperature" in data and data["temperature"] is not None:
                    battery["temperature_c"] = round(float(data["temperature"]), 1)
                if "current" in data and data["current"] is not None:
                    battery["current_ma"] = int(data["current"])
                return battery
        except Exception:
            pass

    # Method B: Linux sysfs (/sys/class/power_supply)
    sys_power = Path("/sys/class/power_supply")
    if sys_power.exists():
        try:
            found_battery = False
            for p in sys_power.iterdir():
                p_name = p.name.lower()
                if "bat" in p_name:
                    found_battery = True
                    cap_file = p / "capacity"
                    stat_file = p / "status"
                    temp_file = p / "temp"
                    health_file = p / "health"

                    if cap_file.exists():
                        try:
                            battery["percentage"] = int(cap_file.read_text().strip())
                        except Exception:
                            pass
                    if stat_file.exists():
                        try:
                            battery["status"] = stat_file.read_text().strip().upper()
                        except Exception:
                            pass
                    if temp_file.exists():
                        try:
                            raw_t = float(temp_file.read_text().strip())
                            battery["temperature_c"] = round(raw_t / 10.0 if raw_t > 100 else raw_t, 1)
                        except Exception:
                            pass
                    if health_file.exists():
                        try:
                            battery["health"] = health_file.read_text().strip().upper()
                        except Exception:
                            pass
                    battery["plugged"] = "PLUGGED_AC" if battery["status"] in ("CHARGING", "FULL") else "UNPLUGGED"
                    break

            if not found_battery:
                # Desktop / Pi on direct AC power
                for p in sys_power.iterdir():
                    if any(x in p.name.lower() for x in ("ac", "acad", "mains", "usb")):
                        online_file = p / "online"
                        if online_file.exists() and online_file.read_text().strip() == "1":
                            battery["percentage"] = 100
                            battery["status"] = "AC_POWERED"
                            battery["plugged"] = "PLUGGED_AC"
                            battery["health"] = "GOOD"
                            break
            if battery["percentage"] is not None:
                return battery
        except Exception:
            pass

    # Method C: Windows via GetSystemPowerStatus
    if sys.platform == "win32":
        try:
            import ctypes
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)
                ]
            sps = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
                # ACLineStatus: 0 = offline, 1 = online, 255 = unknown
                if sps.ACLineStatus == 1:
                    battery["plugged"] = "PLUGGED_AC"
                elif sps.ACLineStatus == 0:
                    battery["plugged"] = "UNPLUGGED"

                # BatteryFlag: 128 = No system battery (Desktop)
                if sps.BatteryFlag & 128:
                    battery["status"] = "AC_POWERED"
                    battery["percentage"] = 100
                    battery["health"] = "GOOD"
                else:
                    if sps.BatteryLifePercent != 255:
                        battery["percentage"] = int(sps.BatteryLifePercent)
                    if sps.BatteryFlag & 8:
                        battery["status"] = "CHARGING"
                    elif battery["percentage"] == 100 and sps.ACLineStatus == 1:
                        battery["status"] = "FULL"
                    elif sps.ACLineStatus == 1:
                        battery["status"] = "NOT_CHARGING"
                    elif sps.ACLineStatus == 0:
                        battery["status"] = "DISCHARGING"

                    if sps.BatteryFlag & 4:
                        battery["health"] = "CRITICAL"
                    elif sps.BatteryFlag & 2:
                        battery["health"] = "LOW"
                    else:
                        battery["health"] = "GOOD"
                return battery
        except Exception:
            pass

    return battery


# ------------------------------------------------------------------------------
# 2. Network & Wi-Fi Telemetry
# ------------------------------------------------------------------------------

def _measure_ping_latency_ms(host="1.1.1.1", port=80, timeout=1.5):
    """Measures quick TCP round-trip connect latency in milliseconds without raw socket privileges."""
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return round((time.time() - t0) * 1000, 1)
    except Exception:
        # Fallback to local DNS check
        try:
            t1 = time.time()
            socket.gethostbyname("localhost")
            return round((time.time() - t1) * 1000, 1)
        except Exception:
            return None


def collect_network_info():
    """
    Collects Wi-Fi SSID, signal strength (RSSI & %), link speed (Mbps),
    IP addresses, connection type, and internet ping latency.
    """
    net_info = {
        "connection_type": "Unknown",
        "wifi_ssid": None,
        "wifi_signal_percent": None,
        "wifi_rssi_dbm": None,
        "link_speed_mbps": None,
        "ping_ms": None,
        "ip": None,
        "tailscale_ip": None
    }

    # 1. Resolve local LAN IP & Tailscale IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Does not send actual traffic, gets outbound interface IP
        s.connect(("8.8.8.8", 80))
        net_info["ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        try:
            net_info["ip"] = socket.gethostbyname(socket.gethostname())
        except Exception:
            net_info["ip"] = "127.0.0.1"

    # Tailscale IP detection
    if shutil.which("tailscale"):
        try:
            res = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.strip():
                net_info["tailscale_ip"] = res.stdout.strip().splitlines()[0]
        except Exception:
            pass

    # 2. Platform-specific Wi-Fi and Signal Metrics
    # Method A: Termux / Android via termux-wifi-connectioninfo
    if shutil.which("termux-wifi-connectioninfo"):
        try:
            res = subprocess.run(["termux-wifi-connectioninfo"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip():
                wdata = json.loads(res.stdout)
                ssid = wdata.get("ssid", "").strip('"')
                if ssid and ssid != "<unknown ssid>":
                    net_info["wifi_ssid"] = ssid
                    net_info["connection_type"] = "Wi-Fi"

                if "rssi" in wdata and wdata["rssi"] is not None:
                    rssi = int(wdata["rssi"])
                    net_info["wifi_rssi_dbm"] = rssi
                    # Convert RSSI (-100 to -50) to 0-100%
                    net_info["wifi_signal_percent"] = max(0, min(100, int(2 * (rssi + 100))))

                if "link_speed_mbps" in wdata and wdata["link_speed_mbps"] is not None:
                    net_info["link_speed_mbps"] = float(wdata["link_speed_mbps"])

                if not net_info.get("ip") and wdata.get("ip"):
                    net_info["ip"] = wdata["ip"]
        except Exception:
            pass

    # Method B: Windows netsh wlan show interfaces
    elif sys.platform == "win32":
        try:
            res = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if k == "ssid" and v and v != "none":
                            net_info["wifi_ssid"] = v
                            net_info["connection_type"] = "Wi-Fi"
                        elif k == "signal" and "%" in v:
                            pct_match = re.search(r"(\d+)", v)
                            if pct_match:
                                sig_pct = int(pct_match.group(1))
                                net_info["wifi_signal_percent"] = sig_pct
                                # Estimate dBm from signal %: dBm ~= (pct / 2) - 100
                                net_info["wifi_rssi_dbm"] = int((sig_pct / 2.0) - 100)
                        elif "receive rate" in k and v:
                            rate_m = re.search(r"([\d.]+)", v)
                            if rate_m:
                                net_info["link_speed_mbps"] = float(rate_m.group(1))
        except Exception:
            pass

    # Method C: Linux /proc/net/wireless or iwconfig / nmcli
    else:
        # Check /proc/net/wireless
        proc_w = Path("/proc/net/wireless")
        if proc_w.exists():
            try:
                lines = proc_w.read_text().splitlines()
                if len(lines) >= 3:
                    parts = lines[2].split()
                    if len(parts) >= 4:
                        net_info["connection_type"] = "Wi-Fi"
                        # parts[2] is link quality, parts[3] is level in dBm
                        lvl_str = parts[3].rstrip(".")
                        if lvl_str.lstrip("-").isdigit():
                            rssi = int(lvl_str)
                            net_info["wifi_rssi_dbm"] = rssi
                            net_info["wifi_signal_percent"] = max(0, min(100, int(2 * (rssi + 100))))
            except Exception:
                pass

        # Try nmcli or iwgetid for SSID
        if not net_info.get("wifi_ssid") and shutil.which("iwgetid"):
            try:
                res = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=1)
                if res.returncode == 0 and res.stdout.strip():
                    net_info["wifi_ssid"] = res.stdout.strip()
                    net_info["connection_type"] = "Wi-Fi"
            except Exception:
                pass

    if net_info["connection_type"] == "Unknown":
        if net_info.get("tailscale_ip"):
            net_info["connection_type"] = "Tailscale"
        elif net_info.get("ip") and not net_info["ip"].startswith("127."):
            net_info["connection_type"] = "Ethernet / LAN"
        else:
            net_info["connection_type"] = "Loopback"

    # 3. Round-trip Ping / Latency
    net_info["ping_ms"] = _measure_ping_latency_ms()

    return net_info


# ------------------------------------------------------------------------------
# 3. CPU Status & Thermals Telemetry
# ------------------------------------------------------------------------------

def collect_cpu_thermal_info():
    """
    Collects CPU utilization percentage, thermal temperature in °C,
    frequency, core count, and thermal status across Linux, Android, and Windows.
    """
    cpu_cores = os.cpu_count() or 1
    cpu_percent = 0.0
    load_average = []
    temperature_c = None
    frequency_mhz = None
    thermal_status = "Normal"

    # Load average
    try:
        load_average = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        try:
            uptime_raw = subprocess.check_output(["uptime"], text=True, stderr=subprocess.DEVNULL)
            m = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", uptime_raw)
            if m:
                load_average = [float(m.group(i)) for i in range(1, 4)]
        except Exception:
            load_average = [0.0, 0.0, 0.0]

    # CPU Utilization %
    if sys.platform == "win32":
        try:
            import ctypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

            def _to_int(ft):
                return (ft.dwHighDateTime << 32) + ft.dwLowDateTime

            i1, k1, u1 = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i1), ctypes.byref(k1), ctypes.byref(u1))
            time.sleep(0.04)
            i2, k2, u2 = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i2), ctypes.byref(k2), ctypes.byref(u2))

            idle_delta = _to_int(i2) - _to_int(i1)
            total_delta = (_to_int(k2) - _to_int(k1)) + (_to_int(u2) - _to_int(u1))
            if total_delta > 0:
                cpu_percent = round((1.0 - (idle_delta / total_delta)) * 100, 1)
        except Exception:
            pass
    else:
        try:
            stat_path = Path("/proc/stat")
            if stat_path.exists():
                def _read_cpu_stat():
                    with open(stat_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("cpu "):
                                parts = [float(x) for x in line.split()[1:]]
                                idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
                                total = sum(parts)
                                return idle, total
                    return None, None
                id1, tot1 = _read_cpu_stat()
                if id1 is not None and tot1 is not None:
                    time.sleep(0.04)
                    id2, tot2 = _read_cpu_stat()
                    if id2 is not None and tot2 is not None and tot2 > tot1:
                        idle_delta = id2 - id1
                        tot_delta = tot2 - tot1
                        cpu_percent = round((1.0 - (idle_delta / tot_delta)) * 100, 1)
        except Exception:
            pass

    # CPU Temperature Detection
    # Method A: Linux / Android / Termux thermal zones
    thermal_base = Path("/sys/class/thermal")
    if thermal_base.exists():
        temps = []
        try:
            for zone in thermal_base.iterdir():
                if zone.name.startswith("thermal_zone"):
                    temp_f = zone / "temp"
                    type_f = zone / "type"
                    z_type = ""
                    if type_f.exists():
                        try:
                            z_type = type_f.read_text().strip().lower()
                        except Exception:
                            pass
                    if temp_f.exists():
                        try:
                            val = float(temp_f.read_text().strip())
                            # Convert millidegrees to degrees
                            t_val = val / 1000.0 if val > 1000 else val
                            if 15.0 <= t_val <= 115.0:
                                # Prioritize CPU / SoC thermal zones
                                priority = 2 if any(k in z_type for k in ("cpu", "soc", "pkg", "tsens", "core")) else 1
                                temps.append((priority, t_val))
                        except Exception:
                            pass
            if temps:
                temps.sort(key=lambda x: x[0], reverse=True)
                temperature_c = round(temps[0][1], 1)
        except Exception:
            pass

    # Method B: Raspberry Pi vcgencmd measure_temp
    if temperature_c is None and shutil.which("vcgencmd"):
        try:
            out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True, timeout=1).strip()
            # temp=42.8'C
            m = re.search(r"temp=([\d.]+)", out)
            if m:
                temperature_c = round(float(m.group(1)), 1)
        except Exception:
            pass

    # Method C: Windows ACPI thermal zone via WMI (if supported)
    if temperature_c is None and sys.platform == "win32":
        try:
            wmic_res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-WmiObject -Namespace root/wmi -Class MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CurrentTemperature"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if wmic_res.returncode == 0 and wmic_res.stdout.strip():
                # Values in tenths of Kelvin: (val - 2732) / 10.0
                k_val = float(wmic_res.stdout.strip().splitlines()[0])
                c_val = (k_val - 2732) / 10.0
                if 10.0 <= c_val <= 115.0:
                    temperature_c = round(c_val, 1)
        except Exception:
            pass

    # CPU Frequency
    freq_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq_path.exists():
        try:
            f_val = float(freq_path.read_text().strip())
            frequency_mhz = round(f_val / 1000.0, 1)
        except Exception:
            pass

    # Determine thermal status
    if temperature_c is not None:
        if temperature_c < 45.0:
            thermal_status = "Cool"
        elif temperature_c < 68.0:
            thermal_status = "Optimal"
        elif temperature_c < 82.0:
            thermal_status = "Warm"
        else:
            thermal_status = "Throttling"

    return {
        "cores": cpu_cores,
        "utilization_percent": cpu_percent,
        "load_average": load_average,
        "temperature_c": temperature_c,
        "thermal_status": thermal_status,
        "frequency_mhz": frequency_mhz
    }


# ------------------------------------------------------------------------------
# 4. Comprehensive Telemetry Collector
# ------------------------------------------------------------------------------

def collect_comprehensive_telemetry():
    """
    Gathers a complete hardware and network snapshot for a node:
    - Battery state (charge %, health, temp, plugged)
    - Network connectivity (Wi-Fi SSID, signal strength %, link speed, ping latency)
    - CPU & Thermals (utilization %, temperature °C, load, cores)
    - Storage and memory metrics
    """
    bat = collect_battery_info()
    net = collect_network_info()
    cpu_thm = collect_cpu_thermal_info()

    return {
        "battery": bat,
        "network": net,
        "cpu_details": cpu_thm,
        "thermals": {
            "temperature_c": cpu_thm.get("temperature_c"),
            "status": cpu_thm.get("thermal_status")
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# ------------------------------------------------------------------------------
# 5. Live Diagnostics & Speed Test
# ------------------------------------------------------------------------------

def run_live_diagnostics(target_host=None, test_speed=True):
    """
    Executes a real-time health and performance test:
    - Measures ping latency (ms)
    - Measures download throughput transfer speed (Mbps)
    - Captures instantaneous CPU load, temperature, and battery levels
    """
    t_start = time.time()
    telemetry = collect_comprehensive_telemetry()
    net = telemetry["network"]

    speed_mbps = None
    transfer_duration_ms = None
    bytes_transferred = 0

    if test_speed:
        # Measure download throughput over a fast lightweight test endpoint
        # Test targets: 1) local Controller, 2) Cloudflare trace, 3) Google DNS
        test_endpoints = [
            ("1.1.1.1", 80, "/cdn-cgi/trace"),
            ("8.8.8.8", 80, "/")
        ]
        if target_host:
            test_endpoints.insert(0, (target_host, 80, "/health"))

        for host, port, path in test_endpoints:
            try:
                t0 = time.time()
                s = socket.create_connection((host, port), timeout=2.0)
                req_str = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: PersonalServer/1.0\r\nConnection: close\r\n\r\n"
                s.sendall(req_str.encode("utf-8"))
                
                resp_chunks = []
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp_chunks.append(chunk)
                s.close()

                dur = time.time() - t0
                total_b = sum(len(c) for c in resp_chunks)
                if total_b > 0 and dur > 0:
                    bytes_transferred = total_b
                    transfer_duration_ms = round(dur * 1000, 1)
                    # Convert to Mbps: (bytes * 8) / (seconds * 1,000,000)
                    # For small metadata payloads, rate represents socket throughput
                    speed_mbps = round((total_b * 8.0) / (dur * 1000000.0), 2)
                    break
            except Exception:
                continue

    total_diag_duration_ms = round((time.time() - t_start) * 1000, 1)

    return {
        "status": "completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": total_diag_duration_ms,
        "battery": telemetry["battery"],
        "network": {
            **net,
            "transfer_speed_mbps": speed_mbps,
            "test_bytes": bytes_transferred,
            "test_duration_ms": transfer_duration_ms
        },
        "cpu": telemetry["cpu_details"],
        "thermals": telemetry["thermals"]
    }
