package com.personalserver.bridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Autonomous Recovery Supervisor coordinating health checks, retry budgets, cooldowns,
 * and recovery workflows for Termux and Tailscale.
 */
public class RecoveryCoordinator {

    private static final String TAG = "RecoveryCoordinator";
    public static final String PREFS_NAME = "PersonalServer_RecoveryState";

    public static final String KEY_RECOVERY_ENABLED = "recovery_enabled";
    public static final String KEY_MAX_RETRY_COUNT = "max_retry_count";
    public static final String KEY_COOLDOWN_SEC = "cooldown_sec";

    public static final int DEFAULT_MAX_RETRY = 3;
    public static final int DEFAULT_COOLDOWN_SEC = 60;
    public static final int SUPERVISOR_LOOP_SEC = 30;
    public static final int MAX_UNKNOWN_ATTEMPTS = 2;

    public interface ServiceHealthProbe {
        Boolean probeNodeApi();
        Boolean probeSsh();
        Boolean probeCommandAvailable();
        Boolean probeTailscaleVpn();
    }

    private final Context context;
    private final TermuxRecoveryManager termuxManager;
    private final TailscaleRecoveryManager tailscaleManager;
    private final SharedPreferences prefs;

    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "RecoveryCoordinatorThread");
        t.setDaemon(true);
        return t;
    });
    private ScheduledFuture<?> scheduledTask = null;

    private volatile boolean running = false;
    private volatile int termuxRetryCount = 0;
    private volatile int tailscaleRetryCount = 0;
    private volatile int unknownTermuxCount = 0;
    private volatile long lastTermuxRecoveryMs = 0L;
    private volatile long lastTailscaleRecoveryMs = 0L;

    private volatile String activeTermuxOpId = null;
    private volatile long activeTermuxOpStartedAt = 0L;
    private volatile String activeTermuxTarget = null;
    private volatile String lastTsConnectedState = "UP";

    private volatile ServiceHealthProbe serviceHealthProbe = null;

    private final List<JSONObject> recoveryEvents = Collections.synchronizedList(new ArrayList<>());

    public RecoveryCoordinator(Context context) {
        this.context = context.getApplicationContext();
        this.termuxManager = new TermuxRecoveryManager(this.context);
        this.tailscaleManager = new TailscaleRecoveryManager(this.context);
        this.prefs = this.context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    public void setServiceHealthProbe(ServiceHealthProbe probe) {
        this.serviceHealthProbe = probe;
    }

    public synchronized void start() {
        if (running) return;
        running = true;
        Log.i(TAG, "Starting RecoveryCoordinator loop (interval=" + SUPERVISOR_LOOP_SEC + "s)...");
        scheduledTask = scheduler.scheduleWithFixedDelay(
                this::runSupervisorCycle,
                5,
                SUPERVISOR_LOOP_SEC,
                TimeUnit.SECONDS
        );
        recordEvent("SUPERVISOR_START", "Autonomous recovery supervisor started", null);
    }

    public synchronized void stop() {
        running = false;
        if (scheduledTask != null) {
            scheduledTask.cancel(true);
            scheduledTask = null;
        }
        recordEvent("SUPERVISOR_STOP", "Autonomous recovery supervisor stopped", null);
        Log.i(TAG, "RecoveryCoordinator stopped.");
    }

    public boolean isRunning() {
        return running;
    }

    public boolean isRecoveryEnabled() {
        return prefs.getBoolean(KEY_RECOVERY_ENABLED, true);
    }

    public void setRecoveryEnabled(boolean enabled) {
        prefs.edit().putBoolean(KEY_RECOVERY_ENABLED, enabled).apply();
        recordEvent("CONFIG_CHANGED", "recovery_enabled set to " + enabled, null);
    }

    public int getMaxRetryCount() {
        return prefs.getInt(KEY_MAX_RETRY_COUNT, DEFAULT_MAX_RETRY);
    }

    public void setMaxRetryCount(int count) {
        prefs.edit().putInt(KEY_MAX_RETRY_COUNT, count).apply();
    }

    public int getCooldownSec() {
        return prefs.getInt(KEY_COOLDOWN_SEC, DEFAULT_COOLDOWN_SEC);
    }

    public void setCooldownSec(int sec) {
        prefs.edit().putInt(KEY_COOLDOWN_SEC, sec).apply();
    }

    public int getTermuxRetryCount() {
        return termuxRetryCount;
    }

    public int getTailscaleRetryCount() {
        return tailscaleRetryCount;
    }

    public int getUnknownTermuxCount() {
        return unknownTermuxCount;
    }

    public long getLastTermuxRecoveryMs() {
        return lastTermuxRecoveryMs;
    }

    public TermuxRecoveryManager getTermuxManager() {
        return termuxManager;
    }

    public TailscaleRecoveryManager getTailscaleManager() {
        return tailscaleManager;
    }

    public void runSupervisorCycle() {
        try {
            if (!isRecoveryEnabled()) {
                return;
            }
            JSONObject diag = evaluateAllStates();
            JSONObject states = diag.optJSONObject("states");
            if (states == null) return;

            long now = System.currentTimeMillis();
            long baseCooldownMs = getCooldownSec() * 1000L;
            int maxRetries = getMaxRetryCount();

            // Duplicate-operation suppression & timeout check
            if (activeTermuxOpId != null) {
                if ((now - activeTermuxOpStartedAt) > 20000L) {
                    termuxManager.updateOperationRecord(activeTermuxOpId, TermuxRecoveryManager.STATE_TIMED_OUT,
                            null, "TIMED_OUT", "Operation timed out during verification");
                    recordEvent("RECOVERY_TIMED_OUT", "Operation " + activeTermuxOpId + " timed out", null);
                    activeTermuxOpId = null;
                } else {
                    return; // Suppress duplicate: active recovery operation still in flight
                }
            }

            // 1. Evaluate Termux
            String termuxServices = states.optString("TERMUX_SERVICES_HEALTHY", "UNKNOWN");
            String termuxAvail = states.optString("TERMUX_AVAILABLE", "UNKNOWN");

            // Exponential backoff
            long effectiveCooldownMs = baseCooldownMs * (1L << Math.min(termuxRetryCount, 4));

            if ("UP".equals(termuxServices) && "UP".equals(termuxAvail)) {
                // UP: No recovery
                termuxRetryCount = 0;
                unknownTermuxCount = 0;
            } else if ("DEGRADED".equals(termuxServices)) {
                unknownTermuxCount = 0;
                if (termuxRetryCount < maxRetries && (now - lastTermuxRecoveryMs) >= effectiveCooldownMs) {
                    JSONObject services = diag.optJSONObject("services");
                    boolean nodeApiUp = services != null && "UP".equals(services.optString("node_api"));
                    boolean sshUp = services != null && "UP".equals(services.optString("sshd"));

                    if (!nodeApiUp && sshUp) {
                        Log.i(TAG, "Initiating targeted Termux recovery for Node API (attempt " + (termuxRetryCount + 1) + ")...");
                        triggerTargetedRecovery("node-api", "AUTONOMOUS");
                    } else if (nodeApiUp && !sshUp) {
                        Log.i(TAG, "Initiating targeted Termux recovery for SSH (attempt " + (termuxRetryCount + 1) + ")...");
                        triggerTargetedRecovery("sshd", "AUTONOMOUS");
                    } else {
                        Log.i(TAG, "Initiating targeted Termux recovery for all degraded services (attempt " + (termuxRetryCount + 1) + ")...");
                        triggerTargetedRecovery("all", "AUTONOMOUS");
                    }
                }
            } else if ("DOWN".equals(termuxServices) || "DOWN".equals(termuxAvail)) {
                unknownTermuxCount = 0;
                if (termuxRetryCount < maxRetries && (now - lastTermuxRecoveryMs) >= effectiveCooldownMs) {
                    Log.i(TAG, "Initiating autonomous Termux recovery (attempt " + (termuxRetryCount + 1) + ")...");
                    triggerTargetedRecovery("all", "AUTONOMOUS");
                }
            } else if ("UNKNOWN".equals(termuxServices) || "UNKNOWN".equals(termuxAvail)) {
                unknownTermuxCount++;
                recordEvent("TERMUX_UNKNOWN_STATE", "Termux state returned UNKNOWN (consecutive=" + unknownTermuxCount + ")", null);
                if (unknownTermuxCount >= MAX_UNKNOWN_ATTEMPTS && termuxRetryCount < maxRetries) {
                    if ((now - lastTermuxRecoveryMs) >= (effectiveCooldownMs * 2)) {
                        unknownTermuxCount = 0;
                        Log.i(TAG, "Initiating bounded recovery for persistent UNKNOWN Termux state...");
                        triggerTargetedRecovery("all", "AUTONOMOUS_UNKNOWN_BOUNDED");
                    }
                }
            }

            // 2. Evaluate Tailscale independently
            String tsConnected = states.optString("TAILSCALE_CONNECTED", "UNKNOWN");
            long tsEffectiveCooldown = baseCooldownMs * (1L << Math.min(tailscaleRetryCount, 4));

            if ("DOWN".equals(tsConnected)) {
                if (!"DOWN".equals(lastTsConnectedState)) {
                    recordEvent("TAILSCALE_DISCONNECTION_DETECTED", "Tailscale VPN disconnected", null);
                }
                lastTsConnectedState = "DOWN";

                if (tailscaleRetryCount < maxRetries && (now - lastTailscaleRecoveryMs) >= tsEffectiveCooldown) {
                    Log.i(TAG, "Initiating autonomous Tailscale recovery (attempt " + (tailscaleRetryCount + 1) + ")...");
                    tailscaleRetryCount++;
                    lastTailscaleRecoveryMs = now;
                    recordEvent("TAILSCALE_RECOVERY_ATTEMPT", "Attempt " + tailscaleRetryCount + " of " + maxRetries, null);
                    triggerTailscaleRecovery("AUTONOMOUS", 0);
                }
            } else if ("UP".equals(tsConnected)) {
                if ("DOWN".equals(lastTsConnectedState) || tailscaleRetryCount > 0) {
                    recordEvent("TAILSCALE_RECOVERY_VERIFIED", "Tailscale VPN connection verified UP", null);
                    tailscaleManager.clearOwnerActionNotification();
                }
                lastTsConnectedState = "UP";
                tailscaleRetryCount = 0;
            } else {
                lastTsConnectedState = tsConnected;
            }

        } catch (Exception e) {
            Log.e(TAG, "Error in supervisor cycle: " + e.getMessage(), e);
        }
    }

    private volatile int defaultVerifyTimeoutMs = 2000;

    public void setDefaultVerifyTimeoutMs(int ms) {
        this.defaultVerifyTimeoutMs = ms;
    }

    public int getDefaultVerifyTimeoutMs() {
        return defaultVerifyTimeoutMs;
    }

    public synchronized JSONObject triggerTargetedRecovery(String target, String triggerSource) {
        return triggerTargetedRecovery(target, triggerSource, defaultVerifyTimeoutMs);
    }

    public static final long DEFAULT_EXECUTION_GUARD_MS = 15000L;
    private volatile long commandExecutionGuardMs = DEFAULT_EXECUTION_GUARD_MS;

    public void setCommandExecutionGuardMs(long ms) {
        this.commandExecutionGuardMs = ms;
    }

    public long getCommandExecutionGuardMs() {
        return commandExecutionGuardMs;
    }

    public synchronized JSONObject triggerTargetedRecovery(String target, String triggerSource, int verifyTimeoutMs) {
        JSONObject res = new JSONObject();
        long now = System.currentTimeMillis();

        // Duplicate-operation suppression & execution guard window
        long elapsedSinceDispatch = now - lastTermuxRecoveryMs;
        if (activeTermuxOpId != null || (lastTermuxRecoveryMs > 0 && elapsedSinceDispatch < commandExecutionGuardMs)) {
            long remainingMs = Math.max(0, commandExecutionGuardMs - elapsedSinceDispatch);
            try {
                res.put("success", false);
                res.put("status", TermuxRecoveryManager.STATE_SUPPRESSED);
                res.put("message", "A recovery operation is already active or in execution guard (" + remainingMs + "ms remaining)");
                res.put("active_operation_id", (activeTermuxOpId != null) ? activeTermuxOpId : "guard_active");
                recordEvent("RECOVERY_SUPPRESSED", "Recovery suppressed; active operation guard (" + remainingMs + "ms remaining)", null);
            } catch (JSONException ignored) {}
            return res;
        }

        String opToRun;
        if ("sshd".equalsIgnoreCase(target)) {
            opToRun = TermuxRecoveryManager.OP_START_SSHD;
        } else {
            opToRun = TermuxRecoveryManager.OP_START_SERVICES;
        }

        try {
            JSONObject extra = new JSONObject();
            extra.put("target_service", target);
            JSONObject execRes = termuxManager.executeApprovedCommand(opToRun, extra);
            String opId = execRes.optString("operation_id", "unknown");

            boolean dispatched = execRes.optBoolean("dispatched", false);
            if (!dispatched) {
                termuxManager.updateOperationRecord(opId, TermuxRecoveryManager.STATE_FAILED,
                        null, "DISPATCH_FAILED", execRes.optString("dispatch_error", "Dispatch failed"));
                res.put("success", false);
                res.put("status", TermuxRecoveryManager.STATE_FAILED);
                res.put("operation_id", opId);
                res.put("termux_command", execRes);
                recordEvent("TERMUX_RECOVERY_TRIGGER", "Failed dispatch for " + target + " (source: " + triggerSource + ")", null);
                return res;
            }

            termuxRetryCount++;
            lastTermuxRecoveryMs = now;
            activeTermuxOpId = opId;
            activeTermuxOpStartedAt = now;
            activeTermuxTarget = target;

            recordEvent("TERMUX_RECOVERY_TRIGGER", "Dispatched " + opToRun + " for target " + target + " (source: " + triggerSource + ", opId: " + opId + ")", null);

            res.put("operation_id", opId);
            res.put("dispatched", true);
            res.put("status", TermuxRecoveryManager.STATE_DISPATCHED);
            res.put("target", target);
            res.put("termux_command", execRes);
            res.put("success", false); // Not reported as success until verification passes!

            // Perform independent verification
            JSONObject verifyResult = performVerification(opId, target, verifyTimeoutMs);
            boolean verified = verifyResult.optBoolean("verified", false);
            res.put("verified", verified);
            res.put("status", verifyResult.optString("status", TermuxRecoveryManager.STATE_DISPATCHED));
            res.put("success", verified);
            res.put("verification", verifyResult);

        } catch (Exception e) {
            try {
                res.put("success", false);
                res.put("status", TermuxRecoveryManager.STATE_FAILED);
                res.put("error", e.getMessage());
            } catch (JSONException ignored) {}
        }
        return res;
    }

    public JSONObject triggerTermuxRecovery(String triggerSource) {
        return triggerTargetedRecovery("all", triggerSource, defaultVerifyTimeoutMs);
    }

    public JSONObject performVerification(String opId, String target, int timeoutMs) {
        JSONObject vRes = new JSONObject();
        long deadline = System.currentTimeMillis() + Math.max(0, timeoutMs);
        boolean verified = false;
        String diagOutput = "";

        while (true) {
            boolean ok = false;
            if ("sshd".equalsIgnoreCase(target)) {
                ok = (serviceHealthProbe != null && serviceHealthProbe.probeSsh() != null)
                        ? serviceHealthProbe.probeSsh()
                        : probeTcp("127.0.0.1", 8022, 500);
                diagOutput = "probeTcp(:8022)=" + ok;
            } else if ("node-api".equalsIgnoreCase(target)) {
                ok = (serviceHealthProbe != null && serviceHealthProbe.probeNodeApi() != null)
                        ? serviceHealthProbe.probeNodeApi()
                        : probeNodeApiHealth(500);
                diagOutput = "probeNodeApiHealth(:8080)=" + ok;
            } else {
                boolean nOk = (serviceHealthProbe != null && serviceHealthProbe.probeNodeApi() != null)
                        ? serviceHealthProbe.probeNodeApi()
                        : probeNodeApiHealth(500);
                boolean sOk = (serviceHealthProbe != null && serviceHealthProbe.probeSsh() != null)
                        ? serviceHealthProbe.probeSsh()
                        : probeTcp("127.0.0.1", 8022, 500);
                ok = nOk && sOk;
                diagOutput = "probeNodeApiHealth(:8080)=" + nOk + ", probeTcp(:8022)=" + sOk;
            }

            if (ok) {
                verified = true;
                break;
            }

            if (System.currentTimeMillis() >= deadline) {
                break;
            }

            try {
                long sleepMs = Math.min(100L, Math.max(10L, deadline - System.currentTimeMillis()));
                Thread.sleep(sleepMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }

        try {
            if (verified) {
                termuxManager.updateOperationRecord(opId, TermuxRecoveryManager.STATE_SUCCEEDED, 0, "RESTORED", diagOutput);
                activeTermuxOpId = null;
                termuxRetryCount = 0; // Reset on verified success!
                recordEvent("RECOVERY_VERIFIED", "Service " + target + " verified healthy (opId=" + opId + ")", null);
                vRes.put("verified", true);
                vRes.put("status", TermuxRecoveryManager.STATE_SUCCEEDED);
                vRes.put("diagnostic_output", diagOutput);
            } else {
                termuxManager.updateOperationRecord(opId, TermuxRecoveryManager.STATE_VERIFICATION_FAILED, null, "UNHEALTHY", diagOutput);
                activeTermuxOpId = null;
                recordEvent("RECOVERY_VERIFICATION_FAILED", "Service " + target + " verification failed (opId=" + opId + ")", null);
                vRes.put("verified", false);
                vRes.put("status", TermuxRecoveryManager.STATE_VERIFICATION_FAILED);
                vRes.put("diagnostic_output", diagOutput);
            }
        } catch (JSONException ignored) {}

        return vRes;
    }

    public JSONObject triggerTailscaleRecovery(String triggerSource) {
        return triggerTailscaleRecovery(triggerSource, defaultVerifyTimeoutMs);
    }

    public JSONObject triggerTailscaleRecovery(String triggerSource, int verifyTimeoutMs) {
        JSONObject res = new JSONObject();
        try {
            recordEvent("TAILSCALE_RECOVERY_TRIGGER", "Trigger source: " + triggerSource, null);
            JSONObject reconn = tailscaleManager.triggerReconnection();
            String action = reconn.optString("action", TailscaleRecoveryManager.ACTION_LAUNCH_FAILED);
            boolean launchDispatched = reconn.optBoolean("launch_dispatched", false);

            recordEvent("TAILSCALE_APP_LAUNCH_RESULT", "action=" + action + ", dispatched=" + launchDispatched, null);
            if (reconn.optBoolean("requires_owner_action", false)) {
                recordEvent("OWNER_ACTION_REQUIRED", "Owner interaction required to authorize/enable VPN in Tailscale app", null);
            }

            res = reconn;

            // Bounded verification if timeout > 0
            if (verifyTimeoutMs > 0) {
                long deadline = System.currentTimeMillis() + verifyTimeoutMs;
                boolean vpnOk = false;
                while (System.currentTimeMillis() < deadline) {
                    vpnOk = (serviceHealthProbe != null && serviceHealthProbe.probeTailscaleVpn() != null)
                            ? serviceHealthProbe.probeTailscaleVpn()
                            : tailscaleManager.isVpnActive();
                    if (vpnOk) break;
                    try {
                        Thread.sleep(Math.min(200L, Math.max(20L, deadline - System.currentTimeMillis())));
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
                if (vpnOk) {
                    res.put("verified", true);
                    res.put("status", "RECONNECTED");
                    res.put("success", true);
                    recordEvent("TAILSCALE_RECOVERY_VERIFIED", "Tailscale VPN reconnected and verified UP", null);
                    tailscaleManager.clearOwnerActionNotification();
                    tailscaleRetryCount = 0;
                } else {
                    res.put("verified", false);
                    recordEvent("TAILSCALE_RECOVERY_TIMED_OUT", "Tailscale VPN verification timed out (" + verifyTimeoutMs + "ms)", null);
                }
            }
        } catch (Exception e) {
            try {
                res.put("success", false);
                res.put("error", e.getMessage());
            } catch (Exception ignored) {}
        }
        return res;
    }

    public JSONObject triggerCombinedRecovery(String triggerSource) {
        JSONObject res = new JSONObject();
        try {
            JSONObject tx = triggerTermuxRecovery(triggerSource);
            JSONObject ts = triggerTailscaleRecovery(triggerSource);
            res.put("termux", tx);
            res.put("tailscale", ts);
            boolean txDisp = tx.optBoolean("dispatched", false);
            boolean tsDisp = ts.optBoolean("launch_dispatched", false) || ts.optBoolean("dispatched", false);
            res.put("dispatched", txDisp || tsDisp);
            boolean txOk = tx.optBoolean("success", false);
            boolean tsOk = ts.optBoolean("success", false);
            res.put("success", txOk || tsOk);
            res.put("status", (txOk || tsOk) ? TermuxRecoveryManager.STATE_SUCCEEDED : (txDisp || tsDisp ? TermuxRecoveryManager.STATE_DISPATCHED : TermuxRecoveryManager.STATE_FAILED));
        } catch (Exception ignored) {}
        return res;
    }

    public JSONObject evaluateAllStates() throws JSONException {
        JSONObject states = new JSONObject();

        boolean appRunning = BridgeService.isProcessActive() || BridgeActivity.isActivityAlive();
        states.put("ANDROID_APP_RUNNING", appRunning ? "UP" : "UNKNOWN");
        states.put("NATIVE_BRIDGE_READY", BridgeService.isRunning() ? "UP" : "DOWN");
        states.put("SUPERVISOR_RUNNING", running ? "UP" : "DOWN");

        // Termux states
        boolean termuxInstalled = termuxManager.isTermuxInstalled();
        states.put("TERMUX_INSTALLED", termuxInstalled ? "UP" : "DOWN");
        states.put("TERMUX_COMMAND_INTEGRATION_READY", (termuxInstalled && termuxManager.isRunCommandPermissionGranted()) ? "UP" : "DOWN");

        String termuxAvail;
        if (serviceHealthProbe != null && serviceHealthProbe.probeCommandAvailable() != null) {
            termuxAvail = serviceHealthProbe.probeCommandAvailable() ? "UP" : "DOWN";
        } else {
            termuxAvail = termuxManager.probeCommandAvailability();
        }
        states.put("TERMUX_AVAILABLE", termuxAvail);

        // Probe local Node API (:8080) and SSH (:8022) without Termux dependency
        boolean nodeApiUp = (serviceHealthProbe != null && serviceHealthProbe.probeNodeApi() != null)
                ? serviceHealthProbe.probeNodeApi()
                : probeNodeApiHealth(1500);

        boolean sshUp = (serviceHealthProbe != null && serviceHealthProbe.probeSsh() != null)
                ? serviceHealthProbe.probeSsh()
                : probeTcp("127.0.0.1", 8022, 1000);

        String servicesState;
        if (nodeApiUp && sshUp) {
            servicesState = "UP";
        } else if (nodeApiUp || sshUp) {
            servicesState = "DEGRADED";
        } else {
            servicesState = "DOWN";
        }
        states.put("TERMUX_SERVICES_HEALTHY", servicesState);

        JSONObject serviceDetails = new JSONObject();
        serviceDetails.put("node_api", nodeApiUp ? "UP" : "DOWN");
        serviceDetails.put("sshd", sshUp ? "UP" : "DOWN");

        // Tailscale states
        boolean tsInstalled = tailscaleManager.isTailscaleInstalled();
        states.put("TAILSCALE_INSTALLED", tsInstalled ? "UP" : "DOWN");
        boolean vpnActive = (serviceHealthProbe != null && serviceHealthProbe.probeTailscaleVpn() != null)
                ? serviceHealthProbe.probeTailscaleVpn()
                : tailscaleManager.isVpnActive();
        states.put("TAILSCALE_VPN_AUTHORIZED", vpnActive ? "UP" : (tsInstalled ? "DOWN" : "UNKNOWN"));
        states.put("TAILSCALE_CONNECTED", vpnActive ? "UP" : "DOWN");

        // Network / Controller reachability
        boolean controllerUp = probeLoopbackHttp(8000, "/api/v1/health", 1000, 1000) || probeTcp("100.120.251.42", 8000, 1500);
        states.put("PERSONALSERVER_NETWORK_REACHABLE", vpnActive ? "UP" : "DOWN");
        states.put("CONTROLLER_REACHABLE", controllerUp ? "UP" : "UNKNOWN");

        // Boot recovery info
        SharedPreferences bootPrefs = context.getSharedPreferences(BootReceiver.PREFS_NAME, Context.MODE_PRIVATE);
        boolean bootEnabled = bootPrefs.getBoolean(BootReceiver.KEY_BOOT_RECOVERY_ENABLED, true);
        String lastBootResult = bootPrefs.getString(BootReceiver.KEY_BOOT_RECOVERY_LAST_RESULT, "NONE");

        JSONObject bootInfo = new JSONObject();
        bootInfo.put("BOOT_RECOVERY_ENABLED", bootEnabled);
        bootInfo.put("BOOT_RECOVERY_READY", true);
        bootInfo.put("BOOT_RECOVERY_LAST_RESULT", lastBootResult);

        JSONObject out = new JSONObject();
        out.put("states", states);
        out.put("services", serviceDetails);
        out.put("boot_recovery", bootInfo);
        out.put("retry_counters", new JSONObject()
                .put("termux", termuxRetryCount)
                .put("tailscale", tailscaleRetryCount));
        return out;
    }

    public static final String LOOPBACK_HOST = "127.0.0.1";
    public static final int NODE_API_PORT = 8080;
    public static final String NODE_API_HEALTH_PATH = "/health";

    public boolean probeNodeApiHealth(int timeoutMs) {
        return probeNodeApiHealth(NODE_API_PORT, timeoutMs);
    }

    public boolean probeNodeApiHealth(int port, int timeoutMs) {
        return probeLoopbackHttp(port, NODE_API_HEALTH_PATH, timeoutMs, timeoutMs);
    }

    public boolean probeLoopbackHttp(int port, String path, int connectTimeoutMs, int readTimeoutMs) {
        return probeLoopbackEndpoint(LOOPBACK_HOST, port, path, connectTimeoutMs, readTimeoutMs);
    }

    /**
     * Bounded TCP socket-based loopback HTTP probe.
     * Enforces strict loopback constraints (127.0.0.1 only), short timeouts,
     * parses status line (200 OK only), does not follow redirects,
     * and guarantees socket closure on all exit paths.
     */
    public boolean probeLoopbackEndpoint(String host, int port, String path, int connectTimeoutMs, int readTimeoutMs) {
        if (!LOOPBACK_HOST.equals(host)) {
            return false;
        }
        if (port <= 0 || port > 65535) {
            return false;
        }
        if (path == null || !path.startsWith("/")) {
            return false;
        }

        Socket socket = null;
        try {
            socket = new Socket();
            socket.setSoTimeout(Math.max(1, readTimeoutMs));
            socket.connect(new InetSocketAddress(host, port), Math.max(1, connectTimeoutMs));

            OutputStream out = socket.getOutputStream();
            String req = "GET " + path + " HTTP/1.1\r\n" +
                         "Host: " + host + "\r\n" +
                         "Connection: close\r\n\r\n";
            out.write(req.getBytes(StandardCharsets.US_ASCII));
            out.flush();

            InputStream in = socket.getInputStream();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.US_ASCII));
            String statusLine = reader.readLine();
            if (statusLine == null) {
                return false;
            }

            String[] tokens = statusLine.trim().split("\\s+");
            if (tokens.length >= 2 && tokens[0].toUpperCase().startsWith("HTTP/")) {
                int code = Integer.parseInt(tokens[1]);
                return code == 200;
            }
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            if (socket != null) {
                try {
                    socket.close();
                } catch (Exception ignored) {}
            }
        }
    }

    protected boolean probeTcp(String host, int port, int timeoutMs) {
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress(host, port), timeoutMs);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    @Deprecated
    protected boolean probeHttp(String urlStr, int timeoutMs) {
        try {
            URL url = new URL(urlStr);
            if (!LOOPBACK_HOST.equals(url.getHost()) && !"localhost".equals(url.getHost())) {
                return false;
            }
            int port = url.getPort() != -1 ? url.getPort() : 80;
            String path = url.getPath();
            if (path == null || path.isEmpty()) {
                path = "/";
            }
            return probeLoopbackEndpoint(LOOPBACK_HOST, port, path, timeoutMs, timeoutMs);
        } catch (Exception e) {
            return false;
        }
    }

    public synchronized void recordEvent(String type, String message, JSONObject details) {
        try {
            JSONObject event = new JSONObject();
            event.put("event_id", "rev-" + UUID.randomUUID().toString().substring(0, 8));
            event.put("type", type);
            event.put("message", message);
            event.put("timestamp", System.currentTimeMillis());
            if (details != null) {
                event.put("details", details);
            }
            recoveryEvents.add(event);
            if (recoveryEvents.size() > 50) {
                recoveryEvents.remove(0);
            }
        } catch (Exception ignored) {}
    }

    public JSONArray getRecoveryHistory() {
        JSONArray arr = new JSONArray();
        synchronized (recoveryEvents) {
            for (JSONObject ev : recoveryEvents) {
                arr.put(ev);
            }
        }
        return arr;
    }
}
