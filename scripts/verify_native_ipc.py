#!/usr/bin/env python3
"""
D3.1 Physical Validation — Native Android Loopback IPC (LocalBridgeServer & LocalSocketTransport)
=================================================================================================
Validates live loopback IPC against the com.personalserver.bridge Android companion app on 127.0.0.1:8765.
Supports checking full IPC functionality, fallback transition when service stops, and restart recovery.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import (
    BridgePermissionState,
    MAX_PAYLOAD_BYTES,
    SENSOR_MAX_CONCURRENT_SESSIONS,
    SensorType,
)
from agent.device_runtime.android.permission_bridge import AndroidPermissionBridge
from agent.device_runtime.android.protocol import BridgeProtocol
from agent.device_runtime.android.providers.battery import AndroidBatteryProvider
from agent.device_runtime.android.providers.thermal import AndroidThermalProvider
from agent.device_runtime.android.transport import (
    AndroidCompanionTransport,
    CompositeAndroidTransport,
    LocalSocketTransport,
)
from agent.device_runtime.capability_registry import CapabilityState
from agent.device_runtime.participation_manager import ParticipationState
from agent.device_runtime.runtime import DeviceRuntime


def raw_socket_exchange(host: str, port: int, payload_bytes: bytes, timeout: float = 3.0) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(payload_bytes)
        chunks = []
        total = 0
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PAYLOAD_BYTES + 1024 or b"\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8", errors="replace").strip()


def run_live_ipc_suite(port: int) -> str:
    """Runs core checks 5-10, 12, 14 against live LocalBridgeServer on 127.0.0.1:port."""
    t0 = time.time()
    def ts() -> str:
        return f"+{time.time() - t0:.2f}s"

    sock_transport = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=3.0)
    if not sock_transport.is_available():
        raise RuntimeError(
            f"LocalBridgeServer is NOT listening on 127.0.0.1:{port}. "
            "Install & open PersonalServer Native Bridge on Vivo V2334 and ensure BridgeService is started."
        )

    print(f"[5] ({ts()}) Local IPC Connectivity, Loopback-Only Binding & Readiness")
    ready, err = sock_transport.check_readiness()
    assert ready, f"Readiness failed: {err}"
    ping_req = BridgeProtocol.create_request("ping", "ping")
    ping_resp = sock_transport.send_request(ping_req)
    assert ping_resp.success is True, f"Ping failed: {ping_resp}"

    # Verify service is NOT listening on non-loopback IPv4 interfaces
    ext_ips = set()
    for target in ("100.85.108.5", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 80))
                addr = probe.getsockname()[0]
                if addr and not addr.startswith("127."):
                    ext_ips.add(addr)
        except Exception:
            pass
    for ext_ip in sorted(ext_ips):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ext_sock:
            ext_sock.settimeout(0.5)
            conn_res = ext_sock.connect_ex((ext_ip, port))
            assert conn_res != 0, f"SECURITY VIOLATION: port {port} reachable on external IP {ext_ip}"
    print(f"  Connected to 127.0.0.1:{port} (verified closed on {len(ext_ips)} non-loopback IP(s): {sorted(ext_ips)}) | ping={ping_resp.data}")

    print("\n[6 & 12] Protocol v1 Errors, Malformed Input, Size Bounds & Timeouts")
    bad_json_raw = raw_socket_exchange("127.0.0.1", port, b"{not valid json\n")
    bad_json = json.loads(bad_json_raw)
    assert bad_json["success"] is False and bad_json["error_code"] == "INVALID_JSON", bad_json
    print(f"  Malformed JSON -> error_code={bad_json['error_code']}")

    ver_mismatch_raw = raw_socket_exchange(
        "127.0.0.1",
        port,
        (json.dumps({"version": 99, "operation_id": "op_ver", "capability": "battery", "operation": "get_status"}) + "\n").encode(),
    )
    ver_mismatch = json.loads(ver_mismatch_raw)
    assert ver_mismatch["success"] is False and ver_mismatch["error_code"] == "VERSION_MISMATCH", ver_mismatch
    print(f"  Version mismatch -> error_code={ver_mismatch['error_code']}")

    oversized_raw = raw_socket_exchange("127.0.0.1", port, b"{" + b"a" * (MAX_PAYLOAD_BYTES + 128) + b"\n")
    oversized = json.loads(oversized_raw)
    assert oversized["success"] is False and oversized["error_code"] == "PAYLOAD_TOO_LARGE", oversized
    print(f"  Oversized payload -> error_code={oversized['error_code']}")

    # Timeout test against stalled loopback socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    stall_port = srv.getsockname()[1]
    srv.listen(1)
    stop_stall = threading.Event()
    conns = []

    def stall_accept():
        while not stop_stall.is_set():
            try:
                srv.settimeout(0.2)
                c, _ = srv.accept()
                conns.append(c)
            except Exception:
                pass

    th = threading.Thread(target=stall_accept, daemon=True)
    th.start()
    try:
        stall_tr = LocalSocketTransport(host="127.0.0.1", port=stall_port, timeout_sec=0.3)
        timeout_resp = stall_tr.send_request(BridgeProtocol.create_request("ping", "ping"), timeout_sec=0.3)
        assert timeout_resp.success is False and timeout_resp.error_code == "TIMEOUT", timeout_resp
        print(f"  Socket timeout handling -> error_code={timeout_resp.error_code}")
    finally:
        stop_stall.set()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        srv.close()
        th.join(timeout=1.0)

    # Clear any orphaned active sessions from interrupted prior runs before read-only discovery check
    pre_status = sock_transport.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STATUS"))
    if pre_status.success:
        for s_item in pre_status.data.get("sessions", []):
            sock_transport.send_request(
                BridgeProtocol.create_request(
                    "sensors",
                    "SENSOR_STOP",
                    parameters={"session_id": s_item.get("session_id"), "reason": "Pre-suite cleanup"},
                )
            )

    print(f"\n[7 & 8] ({ts()}) Native SensorManager Discovery & Hardware Metadata")
    disc_req = BridgeProtocol.create_request("sensors", "SENSOR_DISCOVER", parameters={})
    disc_resp = sock_transport.send_request(disc_req)
    assert disc_resp.success is True, disc_resp
    sensors = disc_resp.data.get("sensors", {})
    assert disc_resp.data.get("source") == "native_bridge_apk", disc_resp.data

    available_types = []
    for st in [t.value for t in SensorType]:
        info = sensors.get(st, {})
        avail = bool(info.get("available"))
        if avail:
            available_types.append(st)
            for field in ("name", "vendor", "android_type", "version", "resolution", "max_range", "min_delay_us", "power_ma"):
                assert field in info and info[field] is not None, f"Missing metadata {field} for {st}: {info}"
            print(
                f"  {st:<14} | name={info['name']} | vendor={info['vendor']} | type={info['android_type']} "
                f"| ver={info['version']} | res={info['resolution']} | max={info['max_range']} "
                f"| min_delay_us={info['min_delay_us']} | power_ma={info['power_ma']}"
            )
        else:
            print(f"  {st:<14} | UNAVAILABLE ({info.get('error')})")

    assert len(available_types) >= 1, "No physical sensors discovered via SensorManager"

    # Confirm discovery activated zero sessions
    status_before = sock_transport.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STATUS"))
    assert status_before.success and len(status_before.data.get("sessions", [])) == 0, status_before.data
    print("  Zero active sessions confirmed after SENSOR_DISCOVER.")

    print(f"\n[9] ({ts()}) Start -> Read -> Status -> Stop Physical Sensor Sessions via LocalSocketTransport")
    apb = AndroidPermissionBridge()
    apb.verify_all_probes()
    print(f"  ({ts()}) probes done; initializing DeviceRuntime...")
    composite = CompositeAndroidTransport(socket_transport=sock_transport, socket_port=port)
    bridge = NativeAndroidBridge(transport=composite, permission_bridge=apb)
    bridge.initialize()
    rt = DeviceRuntime(default_participation=ParticipationState.AVAILABLE)
    # Replace bridge instance references cleanly without touching singleton globally
    for p in rt._providers.values():
        if hasattr(p, "_bridge"):
            p._bridge = bridge
    rt.discover_all()
    print(f"  ({ts()}) DeviceRuntime ready")

    last_sample_summary = ""
    for st in available_types:
        cap_id = f"sensor.{st}"
        desc = rt.registry.get(cap_id)
        assert desc is not None and desc.state == CapabilityState.READY, (cap_id, desc)

        start_res = rt.start_session(cap_id, {"sampling_interval_ms": 50, "max_duration_sec": 15.0, "max_buffer_samples": 16})
        assert start_res.get("success") is True, start_res
        rt_sid = start_res["session"]["session_id"]
        native_sid = start_res["provider_result"]["native_session_id"]
        assert native_sid.startswith("nsess-"), f"Expected native socket session id nsess-*, got {native_sid}"

        try:
            # Status check over socket
            st_resp = bridge.get_sensor_status(session_id=native_sid)
            assert st_resp.success is True and st_resp.data["session"]["state"] == "ACTIVE", st_resp

            readings = []
            max_polls = 3 if st == SensorType.PROXIMITY.value else 8
            for _ in range(max_polls):
                r_res = rt.read_sensor_session(rt_sid, max_samples=10)
                assert r_res.get("success") is True, r_res
                readings.extend(r_res.get("readings", []))
                if readings:
                    break
                time.sleep(0.15)

            if st == SensorType.PROXIMITY.value and len(readings) == 0:
                print(
                    f"  {st:<14} | sid={native_sid} | samples=0 | "
                    "ON_CHANGE_NO_EVENT (Sensor.TYPE_PROXIMITY started/stopped cleanly; no physical change event while stationary)"
                )
            else:
                assert len(readings) >= 1, f"No readings obtained over LocalSocketTransport for {st}"
                sample = readings[0]
                if st in ("accelerometer", "gyroscope", "magnetometer"):
                    assert sample["x"] is not None and sample["y"] is not None and sample["z"] is not None, sample
                    vals = f"x={sample['x']:.4f}, y={sample['y']:.4f}, z={sample['z']:.4f} {sample['units']}"
                else:
                    assert sample["value"] is not None, sample
                    vals = f"value={sample['value']} {sample['units']}"
                last_sample_summary = f"{st}: {vals}"
                print(f"  {st:<14} | sid={native_sid} | samples={len(readings)} | {vals}")
        finally:
            stop_res = rt.stop_session(rt_sid, reason="D3.1 physical check")
            assert stop_res.get("success") is True, stop_res
            assert rt.registry.get_state(cap_id) == CapabilityState.READY

    # Bounded sampling rate & session cleanup verification via DeviceRuntime
    print(f"\n[9b & 12] ({ts()}) Rate Clamping & Bounded Session Cleanup on Socket")
    clamp_res = bridge.start_sensor_session("accelerometer", sampling_interval_ms=5, max_duration_sec=10.0)
    assert clamp_res.success is True, clamp_res
    sess_obj = clamp_res.data["session"]
    assert sess_obj["requested_interval_ms"] == 5
    assert sess_obj["actual_interval_ms"] >= 20
    assert sess_obj["rate_clamped"] is True
    bridge.stop_sensor_session(sess_obj["session_id"], reason="Rate clamp check cleanup")
    print(f"  Rate clamp verified: requested=5ms -> actual={sess_obj['actual_interval_ms']}ms")

    neg_res = bridge.start_sensor_session("accelerometer", sampling_interval_ms=-5)
    assert neg_res.success is False and neg_res.error_code == "INVALID_SAMPLING_RATE", neg_res
    print(f"  Negative rate rejected: {neg_res.error_code}")

    # Direct socket invalid/bounds checks
    # Max concurrent sessions limit (SENSOR_MAX_CONCURRENT_SESSIONS)
    held_sids = []
    try:
        for _ in range(SENSOR_MAX_CONCURRENT_SESSIONS):
            s = sock_transport.send_request(
                BridgeProtocol.create_request("sensor.accelerometer", "SENSOR_START", parameters={"sensor_type": "accelerometer", "sampling_interval_ms": 200})
            )
            assert s.success, s
            held_sids.append(s.data["session"]["session_id"])
        over = sock_transport.send_request(
            BridgeProtocol.create_request("sensor.accelerometer", "SENSOR_START", parameters={"sensor_type": "accelerometer", "sampling_interval_ms": 200})
        )
        assert over.success is False, f"Expected max concurrent failure, got {over}"
        print(f"  Concurrent session cap ({SENSOR_MAX_CONCURRENT_SESSIONS}) enforced: {over.error_message}")
    finally:
        for sid in held_sids:
            sock_transport.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STOP", parameters={"session_id": sid}))

    # Confirm 0 active native sessions left after cleanup
    status_after = sock_transport.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STATUS"))
    assert status_after.success and len(status_after.data.get("sessions", [])) == 0, status_after.data
    print("  All native sensor sessions cleaned up (0 active remaining).")

    print("\n[10] Battery & Thermal Providers over LocalSocketTransport")
    sock_only_bridge = NativeAndroidBridge(transport=sock_transport, permission_bridge=apb)
    sock_only_bridge.initialize()
    tapi_state = apb.get_state("termux.api")
    if tapi_state != BridgePermissionState.READY:
        print(f"  DIAG termux.api state={tapi_state.value}: {apb.to_dict()}")
    bat = AndroidBatteryProvider(bridge=sock_only_bridge).get_battery_status()
    therm = AndroidThermalProvider(bridge=sock_only_bridge).get_thermal_status()
    assert bat.percentage is not None and 0 <= bat.percentage <= 100, bat
    assert therm.thermal_status is not None and therm.battery_temperature_c is not None, therm
    print(f"  Battery (native): {bat.percentage}% ({bat.status}, {bat.temperature_c}C, {bat.voltage_v}V, {bat.current_ma}mA)")
    print(f"  Thermal (native): {therm.thermal_status} (sev={therm.severity_code}, bat_temp={therm.battery_temperature_c}C, headroom={therm.headroom_forecast})")

    print("\n[14] Permission Boundary Verification while Native Service is Live")
    # Privileged capabilities without granted Android runtime permissions must fail fast
    for priv_cap in ("camera", "microphone", "body_sensors"):
        denied = bridge.execute(priv_cap, "get_status")
        assert denied.success is False and denied.error_code == "PERMISSION_DENIED", denied
    # Raw socket also cannot invoke unsupported/privileged capabilities
    raw_priv_resp = sock_transport.send_request(BridgeProtocol.create_request("camera", "get_status"))
    assert raw_priv_resp.success is False and raw_priv_resp.error_code in ("UNKNOWN_CAPABILITY", "PERMISSION_DENIED"), raw_priv_resp

    # Revoking termux.api permission blocks execution even with socket listening
    apb.set_state("termux.api", BridgePermissionState.PERMISSION_DENIED)
    blocked_disc = bridge.discover_sensors(["accelerometer"])
    assert blocked_disc.success is False and blocked_disc.error_code == "PERMISSION_DENIED", blocked_disc
    blocked_bat = bridge.execute("battery", "get_status")
    assert blocked_bat.success is False and blocked_bat.error_code == "PERMISSION_DENIED", blocked_bat
    apb.set_state("termux.api", BridgePermissionState.READY)
    print("  Confirmed: native service availability NEVER bypasses AndroidPermissionBridge checks.")

    rt.shutdown()
    bridge.shutdown()
    return last_sample_summary


def run_fallback_affinity_check(port: int) -> None:
    """
    Phase B helper for Req 11 & 13:
    1) Requires native service UP initially -> starts session nsess-*.
    2) Waits for native service stop (up to 45s OR immediate if stopped externally).
    3) Confirms active nsess-* does NOT cross-route (TRANSPORT_UNAVAILABLE).
    4) Confirms new discovery/battery/sensor requests fall back cleanly to AndroidCompanionTransport (termux_sensor).
    5) Optionally waits for restart recovery.
    """
    sock_tr = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=2.0)
    if not sock_tr.is_available():
        raise RuntimeError("Native service must be running before calling --verify-fallback-transition")

    # Clean up any leftover sessions from interrupted prior runs before testing affinity
    pre_status = sock_tr.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STATUS"))
    if pre_status.success:
        for s_item in pre_status.data.get("sessions", []):
            sock_tr.send_request(
                BridgeProtocol.create_request(
                    "sensors",
                    "SENSOR_STOP",
                    parameters={"session_id": s_item.get("session_id"), "reason": "Pre-fallback cleanup"},
                )
            )

    apb = AndroidPermissionBridge()
    apb.verify_all_probes()
    comp = CompositeAndroidTransport(socket_transport=sock_tr, socket_port=port)
    bridge = NativeAndroidBridge(transport=comp, permission_bridge=apb)
    bridge.initialize()

    start_resp = bridge.start_sensor_session("accelerometer", sampling_interval_ms=200, max_duration_sec=300.0)
    assert start_resp.success is True, start_resp
    native_sid = start_resp.data["session"]["session_id"]
    print(f"Started active native session {native_sid}; stop the native service now (waiting up to 240s)...", flush=True)

    deadline = time.time() + 240.0
    while sock_tr.is_available() and time.time() < deadline:
        time.sleep(0.5)
    assert not sock_tr.is_available(), "Timed out waiting for LocalBridgeServer on 127.0.0.1:8765 to stop"

    # Active session affinity must refuse cross-routing to Termux companion
    dead_read = bridge.read_sensor_session(native_sid, sensor_type="accelerometer")
    assert dead_read.success is False and dead_read.error_code == "TRANSPORT_UNAVAILABLE", dead_read
    print(f"  Transport affinity preserved: read on stopped {native_sid} returned {dead_read.error_code}", flush=True)

    # Clean up affinity map cleanly with stop
    bridge.stop_sensor_session(native_sid, sensor_type="accelerometer")

    # New requests fall back transparently to AndroidCompanionTransport (termux_sensor)
    fb_disc = bridge.discover_sensors(["accelerometer"])
    assert fb_disc.success is True
    assert fb_disc.data["sensors"]["accelerometer"]["source"] == "termux_sensor", fb_disc.data
    fb_start = bridge.start_sensor_session("accelerometer", sampling_interval_ms=200, max_duration_sec=15.0)
    assert fb_start.success is True, fb_start
    fb_sid = fb_start.data["session"]["session_id"]
    assert fb_sid.startswith("csess-"), f"Expected companion session csess-*, got {fb_sid}"
    fb_read = bridge.read_sensor_session(fb_sid, sensor_type="accelerometer")
    assert fb_read.success is True and len(fb_read.data.get("readings", [])) >= 1, fb_read
    bridge.stop_sensor_session(fb_sid, sensor_type="accelerometer")
    print(f"  Fallback activated cleanly via AndroidCompanionTransport ({fb_sid}, {len(fb_read.data['readings'])} sample(s))", flush=True)

    print("  Restart the native service now to test reconnection (waiting up to 240s)...", flush=True)
    deadline = time.time() + 240.0
    while not sock_tr.is_available() and time.time() < deadline:
        time.sleep(0.5)
    assert sock_tr.is_available(), "Timed out waiting for LocalBridgeServer restart on 127.0.0.1:8765"
    time.sleep(0.5)

    re_disc = bridge.discover_sensors(["accelerometer"])
    assert re_disc.success and re_disc.data.get("source") == "native_bridge_apk", re_disc.data
    re_start = bridge.start_sensor_session("accelerometer", sampling_interval_ms=100, max_duration_sec=10.0)
    assert re_start.success and re_start.data["session"]["session_id"].startswith("nsess-"), re_start
    re_sid = re_start.data["session"]["session_id"]
    re_read = bridge.read_sensor_session(re_sid, sensor_type="accelerometer")
    assert re_read.success and len(re_read.data.get("readings", [])) >= 1, re_read
    bridge.stop_sensor_session(re_sid, sensor_type="accelerometer")
    assert len(comp._session_transports) == 0, f"Stale session affinity entries remain: {comp._session_transports}"
    print(f"  Reconnection after service restart verified via {re_sid} (source=native_bridge_apk, affinity entries=0)!")
    bridge.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate PersonalServer D3.1 Native Android Loopback IPC")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--verify-fallback-transition",
        action="store_true",
        help="Interactive/timed test verifying mid-session affinity, Termux companion fallback on stop, and socket restart recovery.",
    )
    args = parser.parse_args()

    print("=" * 72)
    print(" D3.1 Physical Validation — Native Android Loopback IPC (LocalBridgeServer)")
    print("=" * 72)

    if args.verify_fallback_transition:
        run_fallback_affinity_check(args.port)
    else:
        run_live_ipc_suite(args.port)

    print("\n" + "=" * 72)
    print(" D3.1 VALIDATION PASSED")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

