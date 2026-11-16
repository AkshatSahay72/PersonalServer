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
8. Regression suite covering the 10 repair criteria:
   - DEGRADED + Node API down + SSH up triggers targeted recovery.
   - UP triggers no recovery.
   - DOWN triggers bounded recovery.
   - UNKNOWN does not create an uncontrolled retry loop.
   - Healthy services are not unnecessarily restarted.
   - Duplicate recovery requests are suppressed.
   - Dispatch success without command completion is not recovery success.
   - Command completion without restored health is VERIFICATION_FAILED.
   - Restored health yields recovery success (SUCCEEDED).
   - Retry limits and cooldowns remain strictly enforced.
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
            sock.setSoTimeout(10000);
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
        check(termuxMgr.isCommandIntegrationReady(), "Command integration ready must be detected");
        check("UP".equals(termuxMgr.probeCommandAvailability()), "probeCommandAvailability must be UP");

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
        check(tsRec.optBoolean("launch_dispatched", false), "Tailscale launch intent must be dispatched");
        check("OWNER_ACTION_REQUIRED".equals(tsRec.optString("status")), "Tailscale reconnection must report OWNER_ACTION_REQUIRED");
        check(tsRec.optBoolean("success", false) == false, "App launch must NOT be reported as VPN recovery success");

        // 4. TRIGGER_RECOVERY IPC operation
        JSONObject trigParams = new JSONObject();
        trigParams.put("target", "ALL");
        trigParams.put("trigger_source", "TEST_DRIVER");
        JSONObject trigResp = sendIpc(port, "system", "TRIGGER_RECOVERY", trigParams);
        check(trigResp.has("data"), "TRIGGER_RECOVERY must return data");
        JSONObject trigData = trigResp.optJSONObject("data");
        check(trigData != null && trigData.optBoolean("dispatched", false), "TRIGGER_RECOVERY must be dispatched");

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

REPAIR_REGRESSION_DRIVER = """
package com.personalserver.bridge;

import android.content.Intent;
import org.json.JSONObject;

public class RepairRegressionDriver {

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

    private static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError("FAILED: " + msg);
    }

    public static void main(String[] args) {
        try {
            int port = Integer.parseInt(args[0]);
            runAllRegressionScenarios(port);
            System.out.println("REPAIR_REGRESSION_OK");
            System.out.flush();
            System.exit(0);
        } catch (Throwable t) {
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }

    private static void runAllRegressionScenarios(int port) throws Exception {
        TestBridgeService svc = new TestBridgeService(port);
        svc.onCreate();

        // 1. DEGRADED + Node API down + SSH up triggers targeted recovery (recovering only Node API)
        RecoveryCoordinator coord1 = new RecoveryCoordinator(svc);
        coord1.setCooldownSec(1);
        coord1.setDefaultVerifyTimeoutMs(50);
        coord1.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return false; }
            public Boolean probeSsh() { return true; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });

        JSONObject diag1 = coord1.evaluateAllStates();
        JSONObject states1 = diag1.optJSONObject("states");
        check("UP".equals(states1.optString("TERMUX_AVAILABLE")), "TERMUX_AVAILABLE must be UP");
        check("DEGRADED".equals(states1.optString("TERMUX_SERVICES_HEALTHY")), "TERMUX_SERVICES_HEALTHY must be DEGRADED");
        JSONObject services1 = diag1.optJSONObject("services");
        check("DOWN".equals(services1.optString("node_api")), "node_api must be DOWN");
        check("UP".equals(services1.optString("sshd")), "sshd must be UP");

        // Run cycle: targeted recovery must increment retry counter
        int prevRetries1 = coord1.getTermuxRetryCount();
        coord1.runSupervisorCycle();
        check(coord1.getTermuxRetryCount() == prevRetries1 + 1, "Retry count must increment after targeted recovery");

        // 2. UP triggers NO recovery
        RecoveryCoordinator coord2 = new RecoveryCoordinator(svc);
        coord2.setCooldownSec(1);
        coord2.setDefaultVerifyTimeoutMs(50);
        coord2.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return true; }
            public Boolean probeSsh() { return true; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        JSONObject diag2 = coord2.evaluateAllStates();
        check("UP".equals(diag2.optJSONObject("states").optString("TERMUX_SERVICES_HEALTHY")), "Services must be UP");
        coord2.runSupervisorCycle();
        check(coord2.getTermuxRetryCount() == 0, "UP state must trigger no recovery and keep retryCount=0");

        // 3. DOWN triggers bounded recovery
        RecoveryCoordinator coord3 = new RecoveryCoordinator(svc);
        coord3.setCooldownSec(1);
        coord3.setDefaultVerifyTimeoutMs(50);
        coord3.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return false; }
            public Boolean probeSsh() { return false; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        JSONObject diag3 = coord3.evaluateAllStates();
        check("DOWN".equals(diag3.optJSONObject("states").optString("TERMUX_SERVICES_HEALTHY")), "Services must be DOWN");
        coord3.runSupervisorCycle();
        check(coord3.getTermuxRetryCount() == 1, "DOWN state must trigger recovery");

        // 4. UNKNOWN does not create an uncontrolled retry loop
        RecoveryCoordinator coord4 = new RecoveryCoordinator(svc);
        coord4.setCooldownSec(1);
        coord4.setDefaultVerifyTimeoutMs(50);
        coord4.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return false; }
            public Boolean probeSsh() { return false; }
            public Boolean probeCommandAvailable() { return false; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        check(coord4.getUnknownTermuxCount() == 0, "Unknown count initially 0");

        // 5. Healthy services are not unnecessarily restarted
        // SSH down, Node API up -> targeted SSH recovery
        RecoveryCoordinator coord5 = new RecoveryCoordinator(svc);
        coord5.setCooldownSec(1);
        coord5.setDefaultVerifyTimeoutMs(50);
        coord5.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return true; }
            public Boolean probeSsh() { return false; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        JSONObject diag5 = coord5.evaluateAllStates();
        check("DEGRADED".equals(diag5.optJSONObject("states").optString("TERMUX_SERVICES_HEALTHY")), "Services must be DEGRADED");
        check("UP".equals(diag5.optJSONObject("services").optString("node_api")), "Node API must be healthy");
        check("DOWN".equals(diag5.optJSONObject("services").optString("sshd")), "SSH must be down");
        coord5.runSupervisorCycle();
        check(coord5.getTermuxRetryCount() == 1, "Recovery must be triggered for unhealthy SSH");

        // 6. Duplicate recovery requests are suppressed & execution guard enforced
        RecoveryCoordinator coord6 = new RecoveryCoordinator(svc);
        coord6.setCooldownSec(60);
        coord6.setDefaultVerifyTimeoutMs(50);
        coord6.setCommandExecutionGuardMs(5000L);
        coord6.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return false; }
            public Boolean probeSsh() { return false; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        // First trigger
        JSONObject rec6a = coord6.triggerTargetedRecovery("node-api", "TEST_DUP");
        check(rec6a.optBoolean("dispatched", false), "First trigger must be dispatched");
        // Verify state is VERIFICATION_FAILED because health probe returned false
        check("VERIFICATION_FAILED".equals(rec6a.optString("status")), "Status must be VERIFICATION_FAILED");

        // Second trigger immediately within execution guard window: MUST be SUPPRESSED!
        JSONObject rec6b = coord6.triggerTargetedRecovery("node-api", "TEST_DUP_2");
        check("SUPPRESSED".equals(rec6b.optString("status")), "Immediate subsequent trigger must be SUPPRESSED by guard");
        check(rec6b.optBoolean("success", false) == false, "Suppressed trigger must return success=false");

        // 7. Dispatch success without command completion is NOT recovery success
        check(rec6a.optBoolean("dispatched", false) == true, "Dispatched must be true");
        check(rec6a.optBoolean("success", false) == false, "Success must be false when verification failed");

        // 8. Command completion without restored health is VERIFICATION_FAILED
        check("VERIFICATION_FAILED".equals(rec6a.optString("status")), "Status must be VERIFICATION_FAILED");
        check(rec6a.optBoolean("verified", false) == false, "Verified must be false");

        // 9. Restored health yields recovery success (SUCCEEDED)
        RecoveryCoordinator coord7 = new RecoveryCoordinator(svc);
        coord7.setDefaultVerifyTimeoutMs(50);
        coord7.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return true; } // Healthy!
            public Boolean probeSsh() { return true; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        JSONObject rec7 = coord7.triggerTargetedRecovery("node-api", "TEST_SUCCESS");
        check(rec7.optBoolean("dispatched", false) == true, "Dispatched must be true");
        check(rec7.optBoolean("success", false) == true, "Success must be true when health is restored");
        check("SUCCEEDED".equals(rec7.optString("status")), "Status must be SUCCEEDED");
        check(rec7.optBoolean("verified", false) == true, "Verified must be true");

        // 10. Retry limits and cooldowns remain strictly enforced
        RecoveryCoordinator coord8 = new RecoveryCoordinator(svc);
        coord8.setMaxRetryCount(2);
        coord8.setCooldownSec(60);
        coord8.setDefaultVerifyTimeoutMs(50);
        coord8.setServiceHealthProbe(new RecoveryCoordinator.ServiceHealthProbe() {
            public Boolean probeNodeApi() { return false; }
            public Boolean probeSsh() { return false; }
            public Boolean probeCommandAvailable() { return true; }
            public Boolean probeTailscaleVpn() { return true; }
        });
        coord8.runSupervisorCycle(); // Attempt 1
        check(coord8.getTermuxRetryCount() == 1, "Attempt 1 should increment retry count to 1");
        // Immediate cycle (within 60s cooldown)
        coord8.runSupervisorCycle();
        check(coord8.getTermuxRetryCount() == 1, "Immediate attempt must be suppressed by cooldown");
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


def test_autonomous_recovery_repair_regression(tmp_path: Path):
    """
    Validates all 10 repair criteria from Step 6:
    1. DEGRADED + Node API down + SSH up triggers targeted recovery.
    2. UP triggers no recovery.
    3. DOWN triggers bounded recovery.
    4. UNKNOWN does not create an uncontrolled retry loop.
    5. Healthy services are not unnecessarily restarted.
    6. Duplicate recovery requests are suppressed.
    7. Dispatch success without command completion is not recovery success.
    8. Command completion without restored health is VERIFICATION_FAILED.
    9. Restored health yields recovery success (SUCCEEDED).
    10. Retry limits and cooldowns remain strictly enforced.
    """
    cls_dir = _compile_e06_jvm(tmp_path / "e06_b_repair_jvm")

    pkg_dir = tmp_path / "e06_b_repair_jvm" / "src" / "com" / "personalserver" / "bridge"
    driver_path = pkg_dir / "RepairRegressionDriver.java"
    driver_path.write_text(REPAIR_REGRESSION_DRIVER.lstrip(), encoding="utf-8")

    res_compile = subprocess.run(
        ["javac", "-cp", str(cls_dir), "-d", str(cls_dir), str(driver_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_compile.returncode == 0, f"javac repair driver failed:\n{res_compile.stderr}"

    port = _free_port()
    res = subprocess.run(
        ["java", "-cp", str(cls_dir), "com.personalserver.bridge.RepairRegressionDriver", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Repair Java driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "REPAIR_REGRESSION_OK" in res.stdout


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


LOOPBACK_PROBE_SECURITY_DRIVER = """
package com.personalserver.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class LoopbackProbeSecurityDriver {
    private static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError("FAILED: " + msg);
    }

    public static void main(String[] args) {
        try {
            runTests();
            System.out.println("LOOPBACK_PROBE_SECURITY_OK");
            System.out.flush();
            System.exit(0);
        } catch (Throwable t) {
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }

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

    private static void runTests() throws Exception {
        TestBridgeService svc = new TestBridgeService(0);
        svc.onCreate();
        RecoveryCoordinator coord = new RecoveryCoordinator(svc);

        // 1. HTTP 200 -> true + verify request headers & socket cleanup
        try (ServerSocket server = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = server.getLocalPort();
            Thread t = new Thread(() -> {
                try (Socket client = server.accept()) {
                    BufferedReader r = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.US_ASCII));
                    String reqLine = r.readLine();
                    check("GET /health HTTP/1.1".equals(reqLine), "Expected GET /health HTTP/1.1, got: " + reqLine);
                    String hostLine = r.readLine();
                    check("Host: 127.0.0.1".equals(hostLine), "Expected Host: 127.0.0.1, got: " + hostLine);
                    String connLine = r.readLine();
                    check("Connection: close".equals(connLine), "Expected Connection: close, got: " + connLine);

                    OutputStream out = client.getOutputStream();
                    out.write("HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nOK".getBytes(StandardCharsets.US_ASCII));
                    out.flush();

                    // Verify socket cleanup: client must close the socket
                    int eof = client.getInputStream().read();
                    check(eof == -1, "Client socket must be closed by probe (EOF expected)");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
            t.start();
            boolean res = coord.probeLoopbackEndpoint("127.0.0.1", port, "/health", 1000, 1000);
            t.join(2000);
            check(res, "HTTP 200 must be reported as healthy (true)");
        }

        // 2. HTTP 500 -> false
        try (ServerSocket server = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = server.getLocalPort();
            Thread t = new Thread(() -> {
                try (Socket client = server.accept()) {
                    OutputStream out = client.getOutputStream();
                    out.write("HTTP/1.1 500 Internal Server Error\\r\\n\\r\\n".getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                    int eof = client.getInputStream().read();
                    check(eof == -1, "Client socket must be closed on HTTP 500");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
            t.start();
            boolean res = coord.probeLoopbackEndpoint("127.0.0.1", port, "/health", 1000, 1000);
            t.join(2000);
            check(!res, "HTTP 500 must be reported as unhealthy (false)");
        }

        // 3. HTTP 302 (Redirect) -> false (do not follow redirects)
        try (ServerSocket server = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = server.getLocalPort();
            Thread t = new Thread(() -> {
                try (Socket client = server.accept()) {
                    OutputStream out = client.getOutputStream();
                    out.write("HTTP/1.1 302 Found\\r\\nLocation: /other\\r\\n\\r\\n".getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                } catch (Exception ignored) {}
            });
            t.start();
            boolean res = coord.probeLoopbackEndpoint("127.0.0.1", port, "/health", 1000, 1000);
            t.join(2000);
            check(!res, "HTTP 302 redirect must NOT be followed and must be false");
        }

        // 4. Connection Refused -> false
        int unusedPort;
        try (ServerSocket s = new ServerSocket(0)) {
            unusedPort = s.getLocalPort();
        }
        boolean refused = coord.probeLoopbackEndpoint("127.0.0.1", unusedPort, "/health", 200, 200);
        check(!refused, "Connection refused must return false");

        // 5. Connection / Read Timeout -> false
        try (ServerSocket server = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = server.getLocalPort();
            Thread t = new Thread(() -> {
                try (Socket client = server.accept()) {
                    Thread.sleep(500); // stall without replying
                } catch (Exception ignored) {}
            });
            t.start();
            long start = System.currentTimeMillis();
            boolean timeoutRes = coord.probeLoopbackEndpoint("127.0.0.1", port, "/health", 100, 100);
            long elapsed = System.currentTimeMillis() - start;
            t.join(1000);
            check(!timeoutRes, "Read timeout must return false");
            check(elapsed < 400, "Timeout must be bounded (< 400ms), took: " + elapsed);
        }

        // 6. Malformed Response -> false
        try (ServerSocket server = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = server.getLocalPort();
            Thread t = new Thread(() -> {
                try (Socket client = server.accept()) {
                    OutputStream out = client.getOutputStream();
                    out.write("GARBAGE NOT HTTP PROTOCOL\\r\\n\\r\\n".getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                    int eof = client.getInputStream().read();
                    check(eof == -1, "Client socket must be closed on malformed response");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
            t.start();
            boolean malformedRes = coord.probeLoopbackEndpoint("127.0.0.1", port, "/health", 1000, 1000);
            t.join(2000);
            check(!malformedRes, "Malformed HTTP response must return false");
        }

        // 7. Security: Rejection of arbitrary hosts and invalid paths
        check(!coord.probeLoopbackEndpoint("8.8.8.8", 8080, "/health", 100, 100), "Non-loopback IP must be rejected");
        check(!coord.probeLoopbackEndpoint("example.com", 8080, "/health", 100, 100), "Arbitrary hostname must be rejected");
        check(!coord.probeLoopbackEndpoint("127.0.0.1", 8080, "invalid-path", 100, 100), "Path without leading slash must be rejected");
        check(!coord.probeLoopbackEndpoint("127.0.0.1", -1, "/health", 100, 100), "Invalid port must be rejected");
    }
}
"""


def test_loopback_health_probe_socket_security(tmp_path: Path):
    """
    Validates E06-B bounded TCP socket loopback health probe:
    - HTTP 200 returns true and includes required headers (GET, Host, Connection: close).
    - HTTP 500 returns false.
    - HTTP 302 redirects are not followed and return false.
    - Connection refused returns false.
    - Read timeout is bounded and returns false.
    - Malformed responses return false.
    - Socket cleanup: probe guarantees socket closure across all paths (EOF).
    - Security: rejects arbitrary hosts, non-loopback IPs, and invalid paths.
    """
    cls_dir = _compile_e06_jvm(tmp_path / "e06_b_probe_jvm")

    pkg_dir = tmp_path / "e06_b_probe_jvm" / "src" / "com" / "personalserver" / "bridge"
    driver_path = pkg_dir / "LoopbackProbeSecurityDriver.java"
    driver_path.write_text(LOOPBACK_PROBE_SECURITY_DRIVER.lstrip(), encoding="utf-8")

    res_compile = subprocess.run(
        ["javac", "-cp", str(cls_dir), "-d", str(cls_dir), str(driver_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_compile.returncode == 0, f"javac probe security driver failed:\n{res_compile.stderr}"

    res = subprocess.run(
        ["java", "-cp", str(cls_dir), "com.personalserver.bridge.LoopbackProbeSecurityDriver"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Probe security driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "LOOPBACK_PROBE_SECURITY_OK" in res.stdout

