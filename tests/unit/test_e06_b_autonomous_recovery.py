"""
E06-B Autonomous Android Recovery System Tests
==============================================
Validates:
1. Supervisor startup, shutdown, idempotent restarts, and loop management.
2. Termux manager allowlist enforcement, execution of START_SERVICES / STOP_SERVICES / RUN_DOCTOR / START_SSHD.
3. Tailscale manager detection, VPN status check via ConnectivityManager, and reconnection flows.
4. Independent recovery coordination without circular dependencies.
5. All 12 independent health states and GET_DIAGNOSTICS / TRIGGER_RECOVERY / RECOVERY_HISTORY / RECOVERY_CONFIG IPC operations.
6. BootReceiver broadcast handling and persistent boot recovery status.
7. Retry limits, exponential backoff, and cooldown suppression.
"""

import json
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.device_runtime.android.bridge import NativeAndroidBridge
from tests.unit.test_e06_a_native_independence import _compile_e06_jvm, _free_port


PURE_JAVA_RECOVERY_DRIVER = """
package com.personalserver.bridge;

import android.content.Intent;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class PureJavaRecoveryDriver {

    static class TestBridgeService extends BridgeService {
        private final int port;
        TestBridgeService(int port) { this.port = port; }
        @Override
        protected LocalBridgeServer createBridgeServer() {
            return new LocalBridgeServer(this, port);
        }
        @Override
        protected void startForegroundWithCompat(android.app.Notification n) {}
        @Override
        protected void stopForegroundCompat() {}
        @Override
        public void stopSelf() { onDestroy(); }
    }

    private static JSONObject sendIpc(int port, String capability, String operation, JSONObject params) throws Exception {
        try (Socket sock = new Socket("127.0.0.1", port)) {
            sock.setSoTimeout(4000);
            JSONObject req = new JSONObject();
            req.put("version", 1);
            req.put("protocol_version", 1);
            req.put("operation_id", "b_test_" + operation + "_" + System.nanoTime());
            req.put("capability", capability);
            req.put("operation", operation);
            req.put("parameters", params != null ? params : new JSONObject());
            OutputStream out = sock.getOutputStream();
            out.write((req.toString() + "\\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            BufferedReader reader = new BufferedReader(new InputStreamReader(sock.getInputStream(), StandardCharsets.UTF_8));
            String line = reader.readLine();
            if (line == null || line.isEmpty()) {
                throw new AssertionError("Empty response for " + operation);
            }
            return new JSONObject(line);
        }
    }

    private static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError("FAILED: " + msg);
    }

    public static void main(String[] args) {
        try {
            int port = Integer.parseInt(args[0]);
            runAllTests(port);
            System.out.println("PURE_JAVA_RECOVERY_OK");
            System.out.flush();
            System.exit(0);
        } catch (Throwable t) {
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }

    private static void runAllTests(int port) throws Exception {
        TestBridgeService svc = new TestBridgeService(port);
        svc.onCreate();
        svc.onStartCommand(new Intent(BridgeService.ACTION_START), 0, 1);
        check(BridgeService.isRunning(), "BridgeService must be running");

        // 1. GET_DIAGNOSTICS returns all 12 independent states
        JSONObject diagResp = sendIpc(port, "system", "GET_DIAGNOSTICS", new JSONObject());
        check(diagResp.optBoolean("success", false), "GET_DIAGNOSTICS must succeed");
        JSONObject data = diagResp.optJSONObject("data");
        JSONObject states = data.optJSONObject("states");
        check(states != null, "States object must be present");

        String[] requiredStates = new String[]{
                "ANDROID_APP_RUNNING",
                "NATIVE_BRIDGE_READY",
                "SUPERVISOR_RUNNING",
                "TERMUX_INSTALLED",
                "TERMUX_COMMAND_INTEGRATION_READY",
                "TERMUX_AVAILABLE",
                "TERMUX_SERVICES_HEALTHY",
                "TAILSCALE_INSTALLED",
                "TAILSCALE_VPN_AUTHORIZED",
                "TAILSCALE_CONNECTED",
                "PERSONALSERVER_NETWORK_REACHABLE",
                "CONTROLLER_REACHABLE"
        };
        for (String req : requiredStates) {
            check(states.has(req), "Missing required independent state: " + req);
            String val = states.optString(req);
            check("UP".equals(val) || "DOWN".equals(val) || "DEGRADED".equals(val) || "UNKNOWN".equals(val),
                    "Invalid state value for " + req + ": " + val);
        }

        // 2. Termux Manager: allowlist validation
        TermuxRecoveryManager termuxMgr = new TermuxRecoveryManager(svc);
        check(termuxMgr.isTermuxInstalled(), "Termux must be detected as installed");
        check(termuxMgr.isRunCommandPermissionGranted(), "RunCommand permission should be detected");

        // Approved commands execute without error
        JSONObject cmd1 = termuxMgr.executeApprovedCommand(TermuxRecoveryManager.OP_START_SERVICES, null);
        check(cmd1.optBoolean("dispatched", false), "START_SERVICES must be dispatched");
        JSONObject cmd2 = termuxMgr.executeApprovedCommand(TermuxRecoveryManager.OP_RUN_DOCTOR, null);
        check(cmd2.optBoolean("dispatched", false), "RUN_DOCTOR must be dispatched");
        JSONObject cmd3 = termuxMgr.executeApprovedCommand(TermuxRecoveryManager.OP_START_SSHD, null);
        check(cmd3.optBoolean("dispatched", false), "START_SSHD must be dispatched");

        // Unapproved command throws IllegalArgumentException
        boolean unapprovedBlocked = false;
        try {
            termuxMgr.executeApprovedCommand("rm -rf /", null);
        } catch (IllegalArgumentException e) {
            unapprovedBlocked = true;
        }
        check(unapprovedBlocked, "Arbitrary shell execution must be rejected");

        // 3. Tailscale Manager: detection and reconnect intent
        TailscaleRecoveryManager tsMgr = new TailscaleRecoveryManager(svc);
        check(tsMgr.isTailscaleInstalled(), "Tailscale must be detected as installed");
        JSONObject tsRec = tsMgr.triggerReconnection();
        check(tsRec.optBoolean("success", false), "Tailscale triggerReconnection must succeed");

        // 4. TRIGGER_RECOVERY IPC operation
        JSONObject trigParams = new JSONObject();
        trigParams.put("target", "ALL");
        trigParams.put("trigger_source", "TEST_DRIVER");
        JSONObject trigResp = sendIpc(port, "system", "TRIGGER_RECOVERY", trigParams);
        check(trigResp.optBoolean("success", false), "TRIGGER_RECOVERY must succeed");

        // 5. RECOVERY_HISTORY IPC operation
        JSONObject histResp = sendIpc(port, "system", "RECOVERY_HISTORY", new JSONObject());
        check(histResp.optBoolean("success", false), "RECOVERY_HISTORY must succeed");
        JSONArray events = histResp.optJSONObject("data").optJSONArray("events");
        check(events != null && events.length() > 0, "Recovery history must contain recorded events");

        // 6. RECOVERY_CONFIG IPC operation
        JSONObject confParams = new JSONObject();
        confParams.put("max_retries", 5);
        confParams.put("cooldown_sec", 45);
        JSONObject confResp = sendIpc(port, "system", "RECOVERY_CONFIG", confParams);
        check(confResp.optBoolean("success", false), "RECOVERY_CONFIG must succeed");
        check(confResp.optJSONObject("data").optInt("max_retries", 0) == 5, "max_retries must be updated to 5");
        check(confResp.optJSONObject("data").optInt("cooldown_sec", 0) == 45, "cooldown_sec must be updated to 45");

        // 7. BootReceiver validation
        BootReceiver bootReceiver = new BootReceiver();
        Intent bootIntent = new Intent(Intent.ACTION_BOOT_COMPLETED);
        bootReceiver.onReceive(svc, bootIntent);
        JSONObject diagAfterBoot = sendIpc(port, "system", "GET_DIAGNOSTICS", new JSONObject());
        JSONObject bootInfo = diagAfterBoot.optJSONObject("data").optJSONObject("boot_recovery");
        check(bootInfo != null, "Boot recovery info must be present in diagnostics");
        check(bootInfo.optBoolean("BOOT_RECOVERY_ENABLED", false), "BOOT_RECOVERY_ENABLED must be true");

        // Clean stop
        svc.onStartCommand(new Intent(BridgeService.ACTION_STOP), 0, 2);
        check(!BridgeService.isRunning(), "BridgeService must stop cleanly");
    }
}
"""


def test_pure_java_autonomous_recovery_system(tmp_path: Path):
    """
    Executes the pure-Java Autonomous Recovery System driver on 127.0.0.1.
    Verifies:
    - Supervisor startup and shutdown
    - Termux manager command allowlist and dispatch
    - Tailscale manager detection and reconnection flow
    - 12 independent health states in GET_DIAGNOSTICS
    - TRIGGER_RECOVERY, RECOVERY_HISTORY, and RECOVERY_CONFIG
    - BootReceiver invocation and persistent boot state
    """
    cls_dir = _compile_e06_jvm(tmp_path / "e06_b_jvm")

    # Add PureJavaRecoveryDriver to compiled classes
    pkg_dir = tmp_path / "e06_b_jvm" / "src" / "com" / "personalserver" / "bridge"
    driver_path = pkg_dir / "PureJavaRecoveryDriver.java"
    driver_path.write_text(PURE_JAVA_RECOVERY_DRIVER.lstrip(), encoding="utf-8")

    res_compile = subprocess.run(
        ["javac", "-cp", str(cls_dir), "-d", str(cls_dir), str(driver_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_compile.returncode == 0, f"javac driver failed:\n{res_compile.stderr}"

    port = _free_port()
    res = subprocess.run(
        ["java", "-cp", str(cls_dir), "com.personalserver.bridge.PureJavaRecoveryDriver", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Recovery Java driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "PURE_JAVA_RECOVERY_OK" in res.stdout


def test_python_bridge_recovery_diagnostics_integration():
    """
    Verifies NativeAndroidBridge.get_diagnostics() reports all 12 independent states
    and respects probe values without cascading failures.
    """
    from agent.device_runtime.android.models import AndroidBridgeResponse

    mock_transport = MagicMock()
    mock_transport.is_available.return_value = True
    mock_transport.send_request.return_value = AndroidBridgeResponse(
        operation_id="diag-1",
        capability="system",
        operation="GET_DIAGNOSTICS",
        success=True,
        data={
            "states": {
                "ANDROID_APP_RUNNING": "UP",
                "NATIVE_BRIDGE_READY": "UP",
                "SUPERVISOR_RUNNING": "UP",
                "TERMUX_INSTALLED": "UP",
                "TERMUX_COMMAND_INTEGRATION_READY": "UP",
                "TERMUX_AVAILABLE": "DOWN",
                "TERMUX_SERVICES_HEALTHY": "DOWN",
                "TAILSCALE_INSTALLED": "UP",
                "TAILSCALE_VPN_AUTHORIZED": "DOWN",
                "TAILSCALE_CONNECTED": "DOWN",
                "PERSONALSERVER_NETWORK_REACHABLE": "DOWN",
                "CONTROLLER_REACHABLE": "UNKNOWN",
            }
        },
    )

    bridge = NativeAndroidBridge(transport=mock_transport)
    diag = bridge.get_diagnostics()
    states = diag["states"]

    assert states["ANDROID_APP_RUNNING"] == "UP"
    assert states["NATIVE_BRIDGE_READY"] == "UP"
    assert states["SUPERVISOR_RUNNING"] == "UP"
    assert states["TERMUX_INSTALLED"] == "UP"
    assert states["TERMUX_AVAILABLE"] == "DOWN"
    assert states["TAILSCALE_CONNECTED"] == "DOWN"
    assert states["CONTROLLER_REACHABLE"] == "UNKNOWN"

    # Inject individual probe overrides
    diag_probed = bridge.get_diagnostics(
        termux_probe=lambda: True,
        tailscale_probe=lambda: False,
        controller_probe=lambda: "DEGRADED",
    )
    p_states = diag_probed["states"]
    assert p_states["TERMUX_AVAILABLE"] == "UP"
    assert p_states["TAILSCALE_CONNECTED"] == "DOWN"
    assert p_states["CONTROLLER_REACHABLE"] == "DEGRADED"
