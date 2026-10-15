"""
Unit and Integration Tests for PersonalServer Node Telemetry & Hardware Diagnostics
===================================================================================
Tests battery, network strength/speed, CPU thermals, and live diagnostics across platforms.
"""

import unittest
import json
import sys
from unittest.mock import patch, MagicMock
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.node_telemetry import (
    collect_battery_info,
    collect_network_info,
    collect_cpu_thermal_info,
    collect_comprehensive_telemetry,
    run_live_diagnostics,
    _measure_ping_latency_ms
)


class TestNodeTelemetry(unittest.TestCase):

    def test_battery_info_structure(self):
        bat = collect_battery_info()
        self.assertIsInstance(bat, dict)
        self.assertIn("percentage", bat)
        self.assertIn("status", bat)
        self.assertIn("plugged", bat)
        self.assertIn("health", bat)
        self.assertIn("temperature_c", bat)

    def test_battery_termux_mock(self):
        fake_termux_json = json.dumps({
            "percentage": 82,
            "status": "CHARGING",
            "plugged": "PLUGGED_AC",
            "health": "GOOD",
            "temperature": 32.5,
            "current": 420
        })

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = fake_termux_json

        with patch("shutil.which", return_value="/data/data/com.termux/files/usr/bin/termux-battery-status"), \
             patch("subprocess.run", return_value=mock_res):
            bat = collect_battery_info()
            self.assertEqual(bat["percentage"], 82)
            self.assertEqual(bat["status"], "CHARGING")
            self.assertEqual(bat["plugged"], "PLUGGED_AC")
            self.assertEqual(bat["health"], "GOOD")
            self.assertEqual(bat["temperature_c"], 32.5)
            self.assertEqual(bat["current_ma"], 420)

    def test_network_info_structure(self):
        net = collect_network_info()
        self.assertIsInstance(net, dict)
        self.assertIn("connection_type", net)
        self.assertIn("wifi_ssid", net)
        self.assertIn("wifi_signal_percent", net)
        self.assertIn("wifi_rssi_dbm", net)
        self.assertIn("link_speed_mbps", net)
        self.assertIn("ping_ms", net)
        self.assertIn("ip", net)

    def test_network_termux_wifi_mock(self):
        fake_wifi_json = json.dumps({
            "bssid": "00:11:22:33:44:55",
            "frequency_mhz": 5180,
            "ip": "192.168.1.155",
            "link_speed_mbps": 866,
            "rssi": -52,
            "ssid": "NodeOne-5G",
            "supplicant_state": "COMPLETED"
        })

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = fake_wifi_json

        with patch("shutil.which") as mock_which, \
             patch("subprocess.run", return_value=mock_res):
            mock_which.side_effect = lambda cmd: "/usr/bin/termux-wifi-connectioninfo" if "wifi" in cmd else None
            net = collect_network_info()
            self.assertEqual(net["wifi_ssid"], "NodeOne-5G")
            self.assertEqual(net["wifi_rssi_dbm"], -52)
            self.assertEqual(net["link_speed_mbps"], 866.0)
            self.assertEqual(net["wifi_signal_percent"], 96) # 2 * (-52 + 100) = 96%
            self.assertEqual(net["connection_type"], "Wi-Fi")

    def test_cpu_thermal_info_structure(self):
        cpu = collect_cpu_thermal_info()
        self.assertIsInstance(cpu, dict)
        self.assertIn("cores", cpu)
        self.assertIn("utilization_percent", cpu)
        self.assertIn("load_average", cpu)
        self.assertIn("temperature_c", cpu)
        self.assertIn("thermal_status", cpu)
        self.assertGreaterEqual(cpu["cores"], 1)

    def test_thermal_status_classification(self):
        # Test thermal classification helper ranges
        from config.node_telemetry import collect_cpu_thermal_info
        with patch("config.node_telemetry.Path.exists", return_value=False):
            # Test direct logic checks
            info = collect_cpu_thermal_info()
            self.assertIn(info["thermal_status"], ("Normal", "Cool", "Optimal", "Warm", "Throttling"))

    def test_comprehensive_telemetry(self):
        telem = collect_comprehensive_telemetry()
        self.assertIn("battery", telem)
        self.assertIn("network", telem)
        self.assertIn("cpu_details", telem)
        self.assertIn("thermals", telem)
        self.assertIn("timestamp", telem)

    def test_run_live_diagnostics(self):
        diag = run_live_diagnostics(test_speed=False)
        self.assertEqual(diag["status"], "completed")
        self.assertIn("battery", diag)
        self.assertIn("network", diag)
        self.assertIn("cpu", diag)
        self.assertIn("thermals", diag)
        self.assertIn("duration_ms", diag)


if __name__ == "__main__":
    unittest.main()
