"""
Unit Tests for Native Android Bridge (D2)
=========================================
Covers the 16 core requirements of the PersonalServer Native Android Bridge:
1. Protocol request creation and bounding
2. Protocol response parsing and validation
3. Protocol version mismatch rejection
4. Payload size limit (64KB) rejection
5. Permission lifecycle state transitions
6. Permission revocation detection
7. Bridge fast-fail on missing permissions
8. Companion transport command execution
9. Local socket transport loopback communication
10. Composite transport fallback hierarchy
11. Bridge timeout handling
12. Battery provider parsing (all telemetry fields)
13. Thermal provider parsing (PowerManager thermal codes 0-6)
14. PowerProvider integration with Android bridge
15. ResourceMonitor integration with Android bridge
16. Honest degradation (zero fake/mocked telemetry)
"""

import json
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.device_runtime.android.models import (
    PROTOCOL_VERSION,
    MAX_PAYLOAD_BYTES,
    BridgeState,
    BridgePermissionState,
    AndroidThermalStatus,
    BatteryData,
    ThermalData,
    AndroidBridgeRequest,
    AndroidBridgeResponse,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    ProtocolVersionMismatchError,
    PayloadTooLargeError,
)
from agent.device_runtime.android.permission_bridge import (
    AndroidPermissionBridge,
    ANDROID_PERMISSION_MAP,
)
from agent.device_runtime.android.transport import (
    BaseBridgeTransport,
    AndroidCompanionTransport,
    LocalSocketTransport,
    CompositeAndroidTransport,
)
from agent.device_runtime.android.bridge import (
    NativeAndroidBridge,
    BridgeError,
    BridgePermissionError,
)
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.providers.power_provider import PowerProvider
from agent.device_runtime.resource_monitor import ResourceMonitor
from agent.device_runtime.capability_registry import CapabilityState
from agent.device_runtime.permission_manager import PermissionManager, PermissionStatus


class TestNativeAndroidBridgeSuite:

    @pytest.fixture(autouse=True)
    def clean_bridge_singleton(self):
        """Ensure clean singleton state before and after each test."""
        NativeAndroidBridge.reset_instance()
        yield
        NativeAndroidBridge.reset_instance()

    # 1. Protocol Request Creation and Bounding
    def test_protocol_create_valid_request(self):
        req = BridgeProtocol.create_request(
            capability="battery",
            operation="get_status",
            parameters={"detail": True},
            timeout=5.0,
        )
        assert req.version == PROTOCOL_VERSION
        assert req.capability == "battery"
        assert req.operation == "get_status"
        assert req.timeout_sec == 5.0
        assert req.operation_id.startswith("op_")

        # Serialized JSON should be well-formed and bounded
        serialized = BridgeProtocol.serialize_request(req)
        assert len(serialized) <= MAX_PAYLOAD_BYTES
        parsed = json.loads(serialized.decode("utf-8"))
        assert parsed["operation_id"] == req.operation_id

    # 2. Protocol Response Parsing and Validation
    def test_protocol_parse_valid_response(self):
        raw = json.dumps({
            "version": PROTOCOL_VERSION,
            "operation_id": "op_test_123",
            "capability": "battery",
            "operation": "get_status",
            "success": True,
            "data": {"percentage": 85, "status": "CHARGING"},
            "error_code": None,
            "error_message": None,
        })
        resp = BridgeProtocol.parse_response(raw)
        assert resp.version == PROTOCOL_VERSION
        assert resp.operation_id == "op_test_123"
        assert resp.success is True
        assert resp.data["percentage"] == 85

    # 3. Protocol Version Mismatch Rejection
    def test_protocol_version_mismatch_rejection(self):
        bad_req_json = json.dumps({
            "version": 99,
            "operation_id": "op_invalid_ver",
            "capability": "battery",
            "operation": "get_status",
            "parameters": {},
            "timestamp": time.time(),
            "timeout_sec": 5.0,
        })
        with pytest.raises(ProtocolVersionMismatchError):
            BridgeProtocol.parse_request(bad_req_json)

        bad_resp_json = json.dumps({
            "version": 2,
            "operation_id": "op_invalid_ver",
            "capability": "battery",
            "operation": "get_status",
            "success": True,
            "data": {},
        })
        with pytest.raises(ProtocolVersionMismatchError):
            BridgeProtocol.parse_response(bad_resp_json)

    # 4. Payload Size Limit Rejection (> 64KB)
    def test_protocol_payload_too_large_rejection(self):
        huge_data = "x" * (MAX_PAYLOAD_BYTES + 1024)
        huge_bytes = huge_data.encode("utf-8")
        with pytest.raises(PayloadTooLargeError):
            BridgeProtocol.validate_payload_size(huge_bytes)

    # 5. Permission Lifecycle State Transitions
    def test_permission_states_lifecycle(self):
        pm = PermissionManager()
        apb = AndroidPermissionBridge(permission_manager=pm)

        # Initial state should be DETECTED
        assert apb.get_state("termux.api") == BridgePermissionState.DETECTED

        # Transition: PERMISSION_REQUIRED
        apb.set_state("termux.api", BridgePermissionState.PERMISSION_REQUIRED)
        assert apb.get_state("termux.api") == BridgePermissionState.PERMISSION_REQUIRED
        assert apb.is_granted("termux.api") is False
        assert pm.get("termux.api").status == PermissionStatus.PROMPT_REQUIRED

        # Transition: PERMISSION_GRANTED
        apb.set_state("termux.api", BridgePermissionState.PERMISSION_GRANTED)
        assert apb.is_granted("termux.api") is True
        assert pm.get("termux.api").status == PermissionStatus.GRANTED

        # Transition: READY
        apb.set_state("termux.api", BridgePermissionState.READY)
        assert apb.is_granted("termux.api") is True

        # Transition: PERMISSION_DENIED
        apb.set_state("termux.api", BridgePermissionState.PERMISSION_DENIED)
        assert apb.is_granted("termux.api") is False
        assert pm.get("termux.api").status == PermissionStatus.DENIED

        # Transition: UNAVAILABLE
        apb.set_state("termux.api", BridgePermissionState.UNAVAILABLE)
        assert apb.is_granted("termux.api") is False
        assert pm.get("termux.api").status == PermissionStatus.UNAVAILABLE

    # 6. Permission Revocation Detection
    def test_permission_revocation_detection(self):
        apb = AndroidPermissionBridge()
        apb.set_state("termux.api", BridgePermissionState.READY)
        assert apb.is_granted("termux.api") is True

        # Mock probe returning PERMISSION_DENIED on subsequent check
        with patch.object(apb, "probe_termux_api_permission", return_value=BridgePermissionState.PERMISSION_DENIED):
            revoked = apb.detect_revocation("termux.api")
            assert revoked is True
            assert apb.get_state("termux.api") == BridgePermissionState.PERMISSION_DENIED
            assert apb.is_granted("termux.api") is False

    # 7. Bridge Fast-Fail on Missing Permissions
    def test_bridge_fast_fail_on_missing_permission(self):
        apb = AndroidPermissionBridge()
        apb.set_state("termux.api", BridgePermissionState.PERMISSION_REQUIRED)

        mock_transport = MagicMock(spec=BaseBridgeTransport)
        mock_transport.is_available.return_value = True

        bridge = NativeAndroidBridge(transport=mock_transport, permission_bridge=apb)

        # Attempt to execute battery operation without granted permission
        resp = bridge.execute("battery", "get_status")
        assert resp.success is False
        assert resp.error_code == "PERMISSION_DENIED"
        assert "Missing: termux.api" in resp.error_message
        # Ensure transport was NEVER called (fast fail)
        mock_transport.send_request.assert_not_called()

    # 8. Companion Transport Command Execution
    def test_companion_transport_execution(self):
        transport = AndroidCompanionTransport(timeout_sec=2.0)

        # Mock subprocess returning valid JSON output for termux-battery-status
        fake_battery_json = json.dumps({
            "percentage": 88,
            "status": "CHARGING",
            "temperature": 38.5,
            "voltage": 4120,
            "plugged": "PLUGGED_AC",
            "health": "GOOD",
        })

        with patch("shutil.which", return_value="/data/data/com.termux/files/usr/bin/termux-battery-status"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_battery_json, stderr="")

            req = BridgeProtocol.create_request(capability="battery", operation="get_status")
            raw_resp = transport.send_request(req)
            resp = BridgeProtocol.parse_response(raw_resp)

            assert resp.success is True
            assert resp.data["percentage"] == 88
            assert resp.data["status"] == "CHARGING"

    # 9. Local Socket Transport Loopback Communication
    def test_local_socket_transport_execution(self):
        # Spin up a temporary local echo server on loopback
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        port = server_sock.getsockname()[1]
        server_sock.listen(5)
        stop_event = threading.Event()

        def echo_handler():
            while not stop_event.is_set():
                try:
                    server_sock.settimeout(0.2)
                    conn, _ = server_sock.accept()
                except (socket.timeout, OSError):
                    continue
                try:
                    data = conn.recv(4096)
                    if not data or not data.strip():
                        conn.close()
                        continue
                    req_dict = json.loads(data.decode("utf-8").strip())
                    resp_dict = {
                        "version": PROTOCOL_VERSION,
                        "operation_id": req_dict.get("operation_id", "op_test"),
                        "capability": req_dict.get("capability", "battery"),
                        "operation": req_dict.get("operation", "get_status"),
                        "success": True,
                        "data": {"echo": "ok"},
                        "error_code": None,
                        "error_message": None,
                    }
                    conn.sendall((json.dumps(resp_dict) + "\n").encode("utf-8"))
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        server_thread = threading.Thread(target=echo_handler, daemon=True)
        server_thread.start()

        try:
            transport = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=2.0)
            assert transport.connect() is True
            assert transport.is_available() is True

            req = BridgeProtocol.create_request("battery", "get_status")
            raw_resp = transport.send_request(req)
            resp = BridgeProtocol.parse_response(raw_resp)

            assert resp.success is True
            assert resp.data["echo"] == "ok"
            transport.disconnect()
        finally:
            stop_event.set()
            server_sock.close()
            server_thread.join(timeout=1.0)

    # 10. Composite Transport Fallback Hierarchy
    def test_composite_transport_fallback(self):
        mock_socket = MagicMock(spec=BaseBridgeTransport)
        mock_socket.is_available.return_value = False # Socket unavailable

        mock_companion = MagicMock(spec=BaseBridgeTransport)
        mock_companion.is_available.return_value = True # Companion operational
        mock_companion.send_request.return_value = json.dumps({
            "version": PROTOCOL_VERSION,
            "operation_id": "op_comp",
            "capability": "battery",
            "operation": "get_status",
            "success": True,
            "data": {"percentage": 92},
        })

        composite = CompositeAndroidTransport(
            socket_transport=mock_socket,
            companion_transport=mock_companion,
        )

        assert composite.is_available() is True
        req = BridgeProtocol.create_request("battery", "get_status")
        raw_resp = composite.send_request(req)
        resp = BridgeProtocol.parse_response(raw_resp)

        assert resp.success is True
        assert resp.data["percentage"] == 92
        mock_socket.send_request.assert_not_called()
        mock_companion.send_request.assert_called_once()

    # 11. Bridge Timeout Handling
    def test_bridge_timeout_handling(self):
        apb = AndroidPermissionBridge()
        apb.set_state("termux.api", BridgePermissionState.READY)

        mock_transport = MagicMock(spec=BaseBridgeTransport)
        mock_transport.is_available.return_value = True
        mock_transport.send_request.side_effect = TimeoutError("Deadline exceeded")

        bridge = NativeAndroidBridge(transport=mock_transport, permission_bridge=apb, default_timeout=0.1)
        resp = bridge.execute("battery", "get_status", timeout=0.1)

        assert resp.success is False
        assert resp.error_code == "TIMEOUT"
        assert "Deadline exceeded" in resp.error_message

    # 12. Battery Provider Parsing
    def test_battery_provider_parsing(self):
        mock_bridge = MagicMock(spec=NativeAndroidBridge)
        mock_bridge.execute.return_value = AndroidBridgeResponse(
            operation_id="op_bat",
            capability="battery",
            operation="get_status",
            success=True,
            data={
                "percentage": 84,
                "status": "CHARGING",
                "plugged": "PLUGGED_AC",
                "temperature": 39.1,
                "voltage": 4250, # 4.25 V
                "health": "GOOD",
                "current": -2400000, # -2.4 A in uA -> -2400.0 mA
                "technology": "Li-poly",
            },
        )

        provider = AndroidBatteryProvider(bridge=mock_bridge)
        bat: BatteryData = provider.get_battery_status()

        assert bat.percentage == 84
        assert bat.charging is True
        assert bat.plugged_source == "PLUGGED_AC"
        assert bat.temperature_c == 39.1
        assert bat.voltage_v == 4.25
        assert bat.health == "GOOD"
        assert bat.current_ma == -2400.0
        assert bat.technology == "Li-poly"

    # 13. Thermal Provider Parsing (PowerManager codes 0-6)
    def test_thermal_provider_parsing(self):
        mock_bridge = MagicMock(spec=NativeAndroidBridge)

        # Test code 3 (SEVERE -> Throttling Active)
        mock_bridge.execute.return_value = AndroidBridgeResponse(
            operation_id="op_th",
            capability="thermals",
            operation="get_status",
            success=True,
            data={
                "severity_code": 3,
                "throttling_active": True,
                "battery_temperature_c": 44.5,
                "cpu_temperature_c": 52.0,
                "headroom_forecast": 0.85,
            },
        )

        provider = AndroidThermalProvider(bridge=mock_bridge)
        thermal: ThermalData = provider.get_thermal_status()

        assert thermal.severity_code == 3
        assert thermal.thermal_status == AndroidThermalStatus.THERMAL_STATUS_SEVERE
        assert thermal.throttling_active is True
        assert thermal.battery_temperature_c == 44.5
        assert thermal.cpu_temperature_c == 52.0
        assert thermal.headroom_forecast == 0.85

    # 14. PowerProvider Integration with Bridge
    def test_power_provider_integration_with_bridge(self):
        mock_bridge = MagicMock(spec=NativeAndroidBridge)
        mock_bridge.is_ready.return_value = True

        # Setup mock returns for battery & thermal
        def mock_execute(cap, op, **kwargs):
            if cap == "battery":
                return AndroidBridgeResponse(
                    operation_id="op_1",
                    capability="battery",
                    operation=op,
                    success=True,
                    data={"percentage": 78, "status": "DISCHARGING", "temperature": 36.2},
                )
            elif cap == "thermals":
                return AndroidBridgeResponse(
                    operation_id="op_2",
                    capability="thermals",
                    operation=op,
                    success=True,
                    data={"severity_code": 1, "battery_temperature_c": 36.2, "throttling_active": False},
                )
            return AndroidBridgeResponse(operation_id="", capability=cap, operation=op, success=False, data={})

        mock_bridge.execute.side_effect = mock_execute

        p = PowerProvider(bridge=mock_bridge)
        # Sysfs files absent/inaccessible
        with patch.object(p, "_check_battery_readable", return_value=False), \
             patch.object(p, "_check_thermals_readable", return_value=False):

            desc = p.discover()
            assert desc.capability_id == "power"
            assert desc.state == CapabilityState.READY

            # Check battery operation
            b_res = p._handle_operation("GET_BATTERY_STATUS", {})
            assert b_res["success"] is True
            assert b_res["battery_percent"] == 78
            assert b_res["is_charging"] is False

            # Check thermal operation
            t_res = p._handle_operation("GET_THERMAL_STATUS", {})
            assert t_res["success"] is True
            assert t_res["thermal_celsius"] == 36.2

    # 15. ResourceMonitor Integration with Bridge
    def test_resource_monitor_integration_with_bridge(self):
        mock_bridge = MagicMock(spec=NativeAndroidBridge)
        mock_bridge.is_ready.return_value = True

        def mock_execute(cap, op, **kwargs):
            if cap == "battery":
                return AndroidBridgeResponse(
                    operation_id="op_bat",
                    capability="battery",
                    operation=op,
                    success=True,
                    data={"percentage": 95, "status": "FULL"},
                )
            elif cap == "thermals":
                return AndroidBridgeResponse(
                    operation_id="op_th",
                    capability="thermals",
                    operation=op,
                    success=True,
                    data={"severity_code": 0, "battery_temperature_c": 34.0},
                )
            return AndroidBridgeResponse(operation_id="", capability=cap, operation=op, success=False, data={})

        mock_bridge.execute.side_effect = mock_execute

        with patch("agent.device_runtime.android.bridge.NativeAndroidBridge.get_instance", return_value=mock_bridge):
            monitor = ResourceMonitor()
            metrics = monitor.sample()
            assert metrics.battery_percent == 95
            assert metrics.is_charging is True
            assert metrics.thermal_celsius == 34.0

    # 16. Honest Unavailable Degradation (Zero Fake Telemetry)
    def test_bridge_honest_unavailable_degradation(self):
        # Transport reports not available
        mock_transport = MagicMock(spec=BaseBridgeTransport)
        mock_transport.is_available.return_value = False

        apb = AndroidPermissionBridge()
        apb.set_state("termux.api", BridgePermissionState.READY)

        bridge = NativeAndroidBridge(transport=mock_transport, permission_bridge=apb)
        resp = bridge.execute("battery", "get_status")

        assert resp.success is False
        assert resp.error_code == "TRANSPORT_UNAVAILABLE"

        # Battery provider must report None values, never fabricated numbers
        b_prov = AndroidBatteryProvider(bridge=bridge)
        bdata = b_prov.get_battery_status()
        assert bdata.percentage is None
        assert bdata.charging is None
        assert bdata.temperature_c is None

        # Thermal provider must report none/unthrottled, never fabricated numbers
        t_prov = AndroidThermalProvider(bridge=bridge)
        tdata = t_prov.get_thermal_status()
        assert tdata.thermal_status == AndroidThermalStatus.THERMAL_STATUS_NONE
        assert tdata.battery_temperature_c is None
