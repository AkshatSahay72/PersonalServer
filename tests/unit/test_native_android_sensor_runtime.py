"""
Unit Tests for Native Android Sensor Runtime (D3)
=================================================
Validates:
- Versioned Protocol v1 sensor operations & bounds (SENSOR_DISCOVER, SENSOR_START, SENSOR_READ, SENSOR_STOP, SENSOR_STATUS)
- Physical sensor name mapping without cross-sensor substitution
- Full SensorManager metadata discovery & missing-sensor unavailability
- Sampling interval enforcement (rejection & floor clamping)
- Bounded queue / session duration limits and cleanup on stop/watchdog/shutdown
- Typed Python sensor providers (Accelerometer, Gyroscope, Magnetometer, AmbientLight, Proximity)
- DeviceRuntime, CapabilityRegistry, and DeviceSessionManager integration
- Native service restart / disconnect recovery
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.device_runtime.android.models import (
    PROTOCOL_VERSION,
    SENSOR_MIN_INTERVAL_MS,
    SENSOR_MAX_INTERVAL_MS,
    SENSOR_MAX_BUFFER_SAMPLES,
    SENSOR_MAX_SESSION_DURATION_SEC,
    BridgePermissionState,
    SensorType,
    AndroidBridgeResponse,
)
from agent.device_runtime.android.protocol import (
    BridgeProtocol,
    ProtocolError,
    SENSOR_OPERATIONS,
)
from agent.device_runtime.android.transport import (
    BaseBridgeTransport,
    AndroidCompanionTransport,
    CompositeAndroidTransport,
    resolve_android_sensor_name,
    _CompanionSensorSession,
)
from agent.device_runtime.android.permission_bridge import AndroidPermissionBridge
from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.providers.sensors import (
    AccelerometerProvider,
    GyroscopeProvider,
    MagnetometerProvider,
    AmbientLightProvider,
    ProximityProvider,
)
from agent.device_runtime.capability_registry import CapabilityState
from agent.device_runtime.participation_manager import ParticipationState
from agent.device_runtime.permission_manager import PermissionStatus
from agent.device_runtime.runtime import DeviceRuntime
from agent.device_runtime.session_manager import DeviceSessionState


class TestSensorProtocolAndValidation:

    def test_protocol_sensor_operations_valid(self):
        for op in ("SENSOR_DISCOVER", "SENSOR_START", "SENSOR_READ", "SENSOR_STOP", "SENSOR_STATUS"):
            assert op in SENSOR_OPERATIONS

        params = BridgeProtocol.validate_sensor_request(
            "SENSOR_START",
            {"sensor_type": "accelerometer", "sampling_interval_ms": 100, "max_duration_sec": 30.0},
        )
        assert params["requested_interval_ms"] == 100
        assert params["actual_interval_ms"] == 100
        assert params["rate_clamped"] is False
        assert params["max_duration_sec"] == 30.0
        assert params["max_buffer_samples"] == SENSOR_MAX_BUFFER_SAMPLES

    def test_protocol_sampling_rate_limits_and_clamping(self):
        # Too fast (5 ms) -> clamped to SENSOR_MIN_INTERVAL_MS (20 ms)
        clamped = BridgeProtocol.validate_sensor_request(
            "SENSOR_START",
            {"sensor_type": "gyroscope", "sampling_interval_ms": 5},
        )
        assert clamped["requested_interval_ms"] == 5
        assert clamped["actual_interval_ms"] == SENSOR_MIN_INTERVAL_MS
        assert clamped["rate_clamped"] is True

        # Extremely slow (25000 ms) -> clamped to SENSOR_MAX_INTERVAL_MS (10000 ms)
        clamped_max = BridgeProtocol.validate_sensor_request(
            "SENSOR_START",
            {"sensor_type": "ambient_light", "sampling_interval_ms": 25000},
        )
        assert clamped_max["actual_interval_ms"] == SENSOR_MAX_INTERVAL_MS
        assert clamped_max["rate_clamped"] is True

        # Zero / negative / >60s -> hard protocol error
        for bad_ms in (0, -10, 75000, "not-a-number"):
            with pytest.raises(ProtocolError):
                BridgeProtocol.validate_sensor_request(
                    "SENSOR_START",
                    {"sensor_type": "accelerometer", "sampling_interval_ms": bad_ms},
                )

    def test_protocol_queue_and_duration_bounds(self):
        bounded = BridgeProtocol.validate_sensor_request(
            "SENSOR_START",
            {
                "sensor_type": "magnetometer",
                "max_duration_sec": 99999.0,
                "max_buffer_samples": 99999,
            },
        )
        assert bounded["max_duration_sec"] == SENSOR_MAX_SESSION_DURATION_SEC
        assert bounded["max_buffer_samples"] == SENSOR_MAX_BUFFER_SAMPLES

    def test_protocol_rejects_unknown_sensor_or_missing_session_id(self):
        with pytest.raises(ProtocolError):
            BridgeProtocol.validate_sensor_request("SENSOR_START", {"sensor_type": "thermometer_fake"})

        with pytest.raises(ProtocolError):
            BridgeProtocol.validate_sensor_request("SENSOR_READ", {})

        with pytest.raises(ProtocolError):
            BridgeProtocol.validate_sensor_request("SENSOR_STOP", {"session_id": ""})


class TestSensorNameResolution:

    def test_v2334_hardware_names_resolve_accurately(self):
        v2334_names = [
            "lsm6dso_acc",
            "qmc6308",
            "orientation",
            "lsm6dso_gyro",
            "stk36c31_als",
            "PROXIMITY_A",
            "linear_acc",
            "qmc6308_uncali_mag",
            "lsm6dso_uncali_gyro",
            "lsm6dso_uncali_acc",
            "tsl2522",
            "algo-prox",
            "Light Policy Sensor",
            "Proximity Sensor",
        ]
        assert resolve_android_sensor_name(v2334_names, "accelerometer") == "lsm6dso_acc"
        assert resolve_android_sensor_name(v2334_names, "gyroscope") == "lsm6dso_gyro"
        assert resolve_android_sensor_name(v2334_names, "magnetometer") == "qmc6308"
        assert resolve_android_sensor_name(v2334_names, "ambient_light") == "stk36c31_als"
        assert resolve_android_sensor_name(v2334_names, "proximity") == "Proximity Sensor"

    def test_missing_sensor_never_substituted(self):
        # Only accelerometer present -> gyroscope & magnetometer must return None
        partial = ["lsm6dso_acc"]
        assert resolve_android_sensor_name(partial, "accelerometer") == "lsm6dso_acc"
        assert resolve_android_sensor_name(partial, "gyroscope") is None
        assert resolve_android_sensor_name(partial, "magnetometer") is None
        assert resolve_android_sensor_name(partial, "ambient_light") is None
        assert resolve_android_sensor_name(partial, "proximity") is None


class TestCompanionSensorTransport:

    def test_companion_sensor_discover_and_bounded_buffer(self):
        transport = AndroidCompanionTransport()

        with patch.object(
            transport,
            "_discover_raw_sensors",
            return_value=(["lsm6dso_acc", "stk36c31_als"], None),
        ):
            req = BridgeProtocol.create_request("sensors", "SENSOR_DISCOVER")
            resp = transport.send_request(req)
            assert resp.success is True
            sensors = resp.data["sensors"]
            assert sensors["accelerometer"]["available"] is True
            assert sensors["accelerometer"]["name"] == "lsm6dso_acc"
            assert sensors["ambient_light"]["available"] is True
            assert sensors["ambient_light"]["name"] == "stk36c31_als"
            # Missing hardware sensors honestly marked unavailable
            assert sensors["gyroscope"]["available"] is False
            assert sensors["gyroscope"]["name"] is None
            assert sensors["magnetometer"]["available"] is False
            assert sensors["proximity"]["available"] is False

        # Verify CompanionSensorSession bounded deque drops oldest beyond maxlen
        sess = _CompanionSensorSession(
            session_id="nsess-bound",
            sensor_type="accelerometer",
            sensor_name="lsm6dso_acc",
            termux_sensor_exe="/bin/false",
            requested_interval_ms=50,
            actual_interval_ms=50,
            max_duration_sec=10.0,
            max_buffer_samples=5,
        )
        for i in range(12):
            sess._push_reading([float(i), 0.0, 9.8])
        assert len(sess.buffer) == 5
        assert sess.samples_read == 12
        drained = sess.read_samples(max_samples=10, wait_timeout=0.01)
        assert [d["x"] for d in drained] == [7.0, 8.0, 9.0, 10.0, 11.0]


class TestTypedSensorProvidersAndRuntime:

    @pytest.fixture(autouse=True)
    def reset_bridge(self):
        NativeAndroidBridge.reset_instance()
        yield
        NativeAndroidBridge.reset_instance()

    def _build_mock_bridge(self):
        apb = AndroidPermissionBridge()
        apb.set_state("termux.api", BridgePermissionState.READY)
        mock_transport = MagicMock(spec=BaseBridgeTransport)
        mock_transport.is_available.return_value = True
        mock_transport.transport_type = "MockNativeTransport"

        discovery_payload = {
            "sensors": {
                "accelerometer": {
                    "sensor_type": "accelerometer",
                    "available": True,
                    "name": "LSM6DSO Accelerometer",
                    "vendor": "STMicro",
                    "android_type": 1,
                    "version": 1,
                    "resolution": 0.00239,
                    "max_range": 78.45,
                    "min_delay_us": 2500,
                    "power_ma": 0.15,
                    "units": "m/s^2",
                    "source": "native_bridge_apk",
                },
                "gyroscope": {
                    "sensor_type": "gyroscope",
                    "available": False,
                    "error": "Physical sensor missing",
                },
                "magnetometer": {
                    "sensor_type": "magnetometer",
                    "available": True,
                    "name": "QMC6308 Magnetometer",
                    "vendor": "QST",
                    "android_type": 2,
                    "version": 1,
                    "resolution": 0.1,
                    "max_range": 3000.0,
                    "min_delay_us": 10000,
                    "power_ma": 0.5,
                    "units": "uT",
                    "source": "native_bridge_apk",
                },
                "ambient_light": {
                    "sensor_type": "ambient_light",
                    "available": True,
                    "name": "STK36C31 Light",
                    "vendor": "Sensortek",
                    "android_type": 5,
                    "version": 1,
                    "resolution": 1.0,
                    "max_range": 65535.0,
                    "min_delay_us": 0,
                    "power_ma": 0.1,
                    "units": "lux",
                    "source": "native_bridge_apk",
                },
                "proximity": {
                    "sensor_type": "proximity",
                    "available": True,
                    "name": "Proximity Sensor",
                    "vendor": "Sensortek",
                    "android_type": 8,
                    "version": 1,
                    "resolution": 5.0,
                    "max_range": 5.0,
                    "min_delay_us": 0,
                    "power_ma": 0.1,
                    "units": "cm",
                    "source": "native_bridge_apk",
                },
            }
        }

        sessions_state = {}

        def send_side_effect(request, timeout_sec=3.0):
            op = request.operation
            params = request.parameters or {}
            if op == "SENSOR_DISCOVER":
                req_types = params.get("sensor_types") or list(discovery_payload["sensors"].keys())
                subset = {k: discovery_payload["sensors"][k] for k in req_types if k in discovery_payload["sensors"]}
                return BridgeProtocol.create_response(request, success=True, data={"sensors": subset})

            if op == "SENSOR_START":
                st = params["sensor_type"]
                if not discovery_payload["sensors"][st]["available"]:
                    return BridgeProtocol.create_response(
                        request,
                        success=False,
                        status="UNAVAILABLE",
                        error=f"Sensor {st} missing",
                        error_code="SENSOR_UNAVAILABLE",
                    )
                sid = f"nsess-mock-{st}"
                sess_info = {
                    "session_id": sid,
                    "sensor_type": st,
                    "sensor_name": discovery_payload["sensors"][st]["name"],
                    "requested_interval_ms": params["requested_interval_ms"],
                    "actual_interval_ms": params["actual_interval_ms"],
                    "started_at": time.time(),
                    "max_duration_sec": params["max_duration_sec"],
                    "state": "ACTIVE",
                    "max_buffer_size": params["max_buffer_samples"],
                    "rate_clamped": params["rate_clamped"],
                }
                sessions_state[sid] = sess_info
                return BridgeProtocol.create_response(request, success=True, data={"session": sess_info})

            if op == "SENSOR_READ":
                sid = params["session_id"]
                sess = sessions_state.get(sid)
                if not sess or sess["state"] != "ACTIVE":
                    return BridgeProtocol.create_response(
                        request, success=False, error="Session not found", error_code="SESSION_NOT_FOUND"
                    )
                st = sess["sensor_type"]
                reading = {
                    "sensor_type": st,
                    "available": True,
                    "timestamp": time.time(),
                    "accuracy": 3,
                    "units": discovery_payload["sensors"][st]["units"],
                    "state": "ACTIVE",
                    "sensor_name": sess["sensor_name"],
                }
                if st in ("accelerometer", "gyroscope", "magnetometer"):
                    reading.update({"x": 0.1, "y": -0.2, "z": 9.81})
                else:
                    reading.update({"value": 42.5 if st == "ambient_light" else 5.0})
                return BridgeProtocol.create_response(
                    request, success=True, data={"session_id": sid, "sensor_type": st, "readings": [reading], "session": sess}
                )

            if op == "SENSOR_STOP":
                sid = params["session_id"]
                sess = sessions_state.get(sid)
                if not sess:
                    return BridgeProtocol.create_response(request, success=False, error="Not found", error_code="SESSION_NOT_FOUND")
                sess["state"] = "STOPPED"
                return BridgeProtocol.create_response(request, success=True, data={"session": sess})

            if op == "SENSOR_STATUS":
                sid = params.get("session_id")
                if sid:
                    return BridgeProtocol.create_response(request, success=True, data={"session": sessions_state.get(sid)})
                return BridgeProtocol.create_response(request, success=True, data={"sessions": list(sessions_state.values())})

            return BridgeProtocol.create_response(request, success=False, error="Unsupported")

        mock_transport.send_request.side_effect = send_side_effect
        bridge = NativeAndroidBridge(transport=mock_transport, permission_bridge=apb)
        bridge._state = bridge._state.CONNECTED
        return bridge, mock_transport, sessions_state

    def test_discovery_does_not_activate_and_exposes_metadata(self):
        bridge, mock_transport, _ = self._build_mock_bridge()

        acc = AccelerometerProvider(bridge=bridge)
        gyro = GyroscopeProvider(bridge=bridge)

        desc_acc = acc.discover()
        desc_gyro = gyro.discover()

        assert desc_acc.state == CapabilityState.READY
        assert desc_acc.details["vendor"] == "STMicro"
        assert desc_acc.details["max_range"] == 78.45
        assert desc_acc.details["min_delay_us"] == 2500
        assert desc_acc.details["power_ma"] == 0.15
        assert acc.active_session_id is None

        # Missing physical sensor reports UNAVAILABLE honestly
        assert desc_gyro.state == CapabilityState.UNAVAILABLE
        assert gyro.active_session_id is None

        # Verify SENSOR_START was NEVER called during discovery
        called_ops = [call.args[0].operation for call in mock_transport.send_request.call_args_list]
        assert "SENSOR_START" not in called_ops

    def test_typed_providers_return_structured_3axis_and_scalar_readings(self):
        bridge, _, _ = self._build_mock_bridge()

        acc = AccelerometerProvider(bridge=bridge)
        acc.discover()
        start_acc = acc.start_session({"sampling_interval_ms": 100}, timeout_sec=30.0)
        assert start_acc["success"] is True
        assert acc.state == CapabilityState.ACTIVE

        read_acc = acc.read_readings(max_samples=1)
        assert read_acc["success"] is True
        tr = read_acc["typed_readings"][0]
        assert tr.sensor_type == "accelerometer"
        assert tr.x == 0.1 and tr.y == -0.2 and tr.z == 9.81
        assert tr.value is None
        assert tr.units == "m/s^2"
        assert tr.accuracy == 3
        acc.stop_session()
        assert acc.state == CapabilityState.READY

        light = AmbientLightProvider(bridge=bridge)
        light.discover()
        light.start_session({"sampling_interval_ms": 200})
        read_light = light.read_readings(max_samples=1)
        tr_light = read_light["typed_readings"][0]
        assert tr_light.sensor_type == "ambient_light"
        assert tr_light.value == 42.5
        assert tr_light.x is None and tr_light.y is None and tr_light.z is None
        assert tr_light.units == "lux"
        light.stop_session()

        prox = ProximityProvider(bridge=bridge)
        prox.discover()
        prox.start_session({"sampling_interval_ms": 200})
        tr_prox = prox.read_readings(max_samples=1)["typed_readings"][0]
        assert tr_prox.value == 5.0
        assert tr_prox.units == "cm"
        prox.stop_session()

    def test_device_runtime_session_lifecycle_and_watchdog_cleanup(self):
        bridge, _, sessions_state = self._build_mock_bridge()
        NativeAndroidBridge._instance = bridge

        rt = DeviceRuntime(node_id="server-v2334-a531", default_participation=ParticipationState.OFF)

        # Participation OFF must reject sensor session start
        rejected = rt.start_session("sensor.accelerometer", {"sampling_interval_ms": 100})
        assert rejected["success"] is False

        # Enable participation
        rt.participation.set_state(ParticipationState.AVAILABLE)

        # Missing physical sensor (gyroscope) fails cleanly without fake session
        missing = rt.start_session("sensor.gyroscope", {"sampling_interval_ms": 100})
        assert missing["success"] is False

        # Start valid accelerometer session with too-fast rate -> enforced to 20ms floor
        started = rt.start_session(
            "sensor.accelerometer",
            {"sampling_interval_ms": 5},
            timeout_sec=10.0,
        )
        assert started["success"] is True
        sid = started["session"]["session_id"]
        assert started["session"]["extra_data"]["requested_interval_ms"] == 5
        assert started["session"]["extra_data"]["actual_interval_ms"] == SENSOR_MIN_INTERVAL_MS
        assert rt.registry.get_state("sensor.accelerometer") == CapabilityState.ACTIVE

        # Read via DeviceRuntime
        read_res = rt.read_sensor_session(sid, max_samples=5)
        assert read_res["success"] is True
        assert len(read_res["readings"]) == 1

        # Simulate watchdog expiration cleanup
        dsess = rt.sessions.get_session(sid)
        dsess.mark_timed_out()
        assert dsess.state == DeviceSessionState.TIMED_OUT
        assert sessions_state["nsess-mock-accelerometer"]["state"] == "STOPPED"
        assert rt.registry.get_state("sensor.accelerometer") == CapabilityState.READY

        rt.shutdown()

    def test_native_service_disconnect_and_restart_recovery(self):
        bridge, mock_transport, _ = self._build_mock_bridge()

        acc = AccelerometerProvider(bridge=bridge)
        acc.discover()
        assert acc.state == CapabilityState.READY
        acc.start_session({"sampling_interval_ms": 100})
        assert acc.state == CapabilityState.ACTIVE

        # Simulate native bridge process crash / disconnect
        mock_transport.is_available.return_value = False
        read_fail = acc.read_readings()
        assert read_fail["success"] is False
        assert read_fail["error_code"] == "TRANSPORT_UNAVAILABLE"
        assert acc.state == CapabilityState.UNAVAILABLE
        assert acc.active_session_id is None

        # Simulate native service restart recovery
        mock_transport.is_available.return_value = True
        desc_recovered = acc.discover()
        assert desc_recovered.state == CapabilityState.READY

        restart_ok = acc.start_session({"sampling_interval_ms": 150})
        assert restart_ok["success"] is True
        read_recovered = acc.read_readings()
        assert read_recovered["success"] is True
        acc.stop_session()


class TestCompositeTransportAffinityAndFallback:

    def test_session_affinity_and_fallback_when_socket_dies(self):
        sock = MagicMock(spec=BaseBridgeTransport)
        comp = MagicMock(spec=BaseBridgeTransport)
        composite = CompositeAndroidTransport(socket_transport=sock, companion_transport=comp)

        sock.is_available.return_value = True
        comp.is_available.return_value = True

        def sock_send(req, timeout_sec=3.0):
            if req.operation == "SENSOR_START":
                return BridgeProtocol.create_response(req, success=True, data={"session": {"session_id": "nsess-123"}})
            return BridgeProtocol.create_response(req, success=True, data={"ok": True})

        sock.send_request.side_effect = sock_send

        start_req = BridgeProtocol.create_request("sensor.accelerometer", "SENSOR_START", parameters={"sensor_type": "accelerometer"})
        start_resp = composite.send_request(start_req)
        assert start_resp.success is True
        assert composite._session_transports["nsess-123"] is sock

        # Socket dies mid-session -> SENSOR_READ must fail with TRANSPORT_UNAVAILABLE, NOT route to companion
        sock.is_available.return_value = False
        read_req = BridgeProtocol.create_request("sensor.accelerometer", "SENSOR_READ", parameters={"session_id": "nsess-123"})
        read_resp = composite.send_request(read_req)
        assert read_resp.success is False
        assert read_resp.error_code == "TRANSPORT_UNAVAILABLE"
        comp.send_request.assert_not_called()

        # SENSOR_STOP clears stale affinity entry even when socket is down
        stop_req = BridgeProtocol.create_request("sensor.accelerometer", "SENSOR_STOP", parameters={"session_id": "nsess-123"})
        stop_resp = composite.send_request(stop_req)
        assert stop_resp.error_code == "TRANSPORT_UNAVAILABLE"
        assert "nsess-123" not in composite._session_transports

        # New non-session request activates companion fallback cleanly
        comp.send_request.return_value = BridgeProtocol.create_response(
            BridgeProtocol.create_request("battery", "get_status"),
            success=True,
            data={"percentage": 75},
        )
        bat_req = BridgeProtocol.create_request("battery", "get_status")
        bat_resp = composite.send_request(bat_req)
        assert bat_resp.success is True
        assert bat_resp.data["percentage"] == 75
        comp.send_request.assert_called_once()

    def test_empty_response_on_non_session_falls_back_to_companion(self):
        sock = MagicMock(spec=BaseBridgeTransport)
        comp = MagicMock(spec=BaseBridgeTransport)
        composite = CompositeAndroidTransport(socket_transport=sock, companion_transport=comp)

        sock.is_available.return_value = True
        comp.is_available.return_value = True
        sock.send_request.return_value = BridgeProtocol.create_response(
            BridgeProtocol.create_request("sensors", "SENSOR_DISCOVER"),
            success=False,
            status="ERROR",
            error="Empty response from native socket service",
            error_code="EMPTY_RESPONSE",
        )
        comp.send_request.return_value = BridgeProtocol.create_response(
            BridgeProtocol.create_request("sensors", "SENSOR_DISCOVER"),
            success=True,
            data={"source": "termux_sensor", "sensors": {}},
        )

        resp = composite.send_request(BridgeProtocol.create_request("sensors", "SENSOR_DISCOVER"))
        assert resp.success is True
        assert resp.data["source"] == "termux_sensor"
        comp.send_request.assert_called_once()

    def test_socket_probe_burst_does_not_break_subsequent_ipc_requests(self):
        import socket as pysocket
        import threading
        from agent.device_runtime.android.transport import LocalSocketTransport

        srv = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_STREAM)
        srv.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(16)
        stop_ev = threading.Event()

        def serve():
            while not stop_ev.is_set():
                try:
                    srv.settimeout(0.2)
                    conn, _ = srv.accept()
                except (pysocket.timeout, OSError):
                    continue
                try:
                    conn.settimeout(1.0)
                    raw = conn.recv(4096)
                    if not raw or not raw.strip():
                        conn.close()
                        continue
                    req = json.loads(raw.decode("utf-8").strip())
                    resp = {
                        "version": PROTOCOL_VERSION,
                        "operation_id": req.get("operation_id", "op"),
                        "capability": req.get("capability", "ping"),
                        "operation": req.get("operation", "ping"),
                        "success": True,
                        "data": {"status": "pong"},
                        "error_code": None,
                        "error_message": None,
                    }
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            tr = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=1.5)
            for _ in range(25):
                assert tr.is_available() is True
            resp = tr.send_request(BridgeProtocol.create_request("ping", "ping"))
            assert resp.success is True
            assert resp.data["status"] == "pong"
        finally:
            stop_ev.set()
            srv.close()
            t.join(timeout=1.0)



