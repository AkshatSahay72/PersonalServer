package com.personalserver.bridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
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
    private volatile long lastTermuxRecoveryMs = 0L;
    private volatile long lastTailscaleRecoveryMs = 0L;

    private final List<JSONObject> recoveryEvents = Collections.synchronizedList(new ArrayList<>());

    public RecoveryCoordinator(Context context) {
        this.context = context.getApplicationContext();
        this.termuxManager = new TermuxRecoveryManager(this.context);
        this.tailscaleManager = new TailscaleRecoveryManager(this.context);
        this.prefs = this.context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
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

    public TermuxRecoveryManager getTermuxManager() {
        return termuxManager;
    }

    public TailscaleRecoveryManager getTailscaleManager() {
        return tailscaleManager;
    }

    private void runSupervisorCycle() {
        try {
            if (!isRecoveryEnabled()) {
                return;
            }
            JSONObject diag = evaluateAllStates();
            JSONObject states = diag.optJSONObject("states");
            if (states == null) return;

            long now = System.currentTimeMillis();
            long cooldownMs = getCooldownSec() * 1000L;
            int maxRetries = getMaxRetryCount();

            // 1. Evaluate Termux
            String termuxServices = states.optString("TERMUX_SERVICES_HEALTHY", "UNKNOWN");
            String termuxAvail = states.optString("TERMUX_AVAILABLE", "UNKNOWN");
            if ("DOWN".equals(termuxServices) || "DOWN".equals(termuxAvail)) {
                if (termuxRetryCount < maxRetries && (now - lastTermuxRecoveryMs) >= cooldownMs) {
                    Log.i(TAG, "Initiating autonomous Termux recovery (attempt " + (termuxRetryCount + 1) + ")...");
                    termuxRetryCount++;
                    lastTermuxRecoveryMs = now;
                    triggerTermuxRecovery("AUTONOMOUS");
                }
            } else if ("UP".equals(termuxServices)) {
                termuxRetryCount = 0; // Reset on success
            }

            // 2. Evaluate Tailscale
            String tsConnected = states.optString("TAILSCALE_CONNECTED", "UNKNOWN");
            if ("DOWN".equals(tsConnected)) {
                if (tailscaleRetryCount < maxRetries && (now - lastTailscaleRecoveryMs) >= cooldownMs) {
                    Log.i(TAG, "Initiating autonomous Tailscale recovery (attempt " + (tailscaleRetryCount + 1) + ")...");
                    tailscaleRetryCount++;
                    lastTailscaleRecoveryMs = now;
                    triggerTailscaleRecovery("AUTONOMOUS");
                }
            } else if ("UP".equals(tsConnected)) {
                tailscaleRetryCount = 0; // Reset on success
            }

        } catch (Exception e) {
            Log.e(TAG, "Error in supervisor cycle: " + e.getMessage(), e);
        }
    }

    public JSONObject triggerTermuxRecovery(String triggerSource) {
        JSONObject res = new JSONObject();
        try {
            recordEvent("TERMUX_RECOVERY_TRIGGER", "Trigger source: " + triggerSource, null);
            // Execute start.sh
            JSONObject execRes = termuxManager.executeApprovedCommand(TermuxRecoveryManager.OP_START_SERVICES, null);
            res.put("success", execRes.optBoolean("dispatched", false));
            res.put("termux_command", execRes);
        } catch (Exception e) {
            try {
                res.put("success", false);
                res.put("error", e.getMessage());
            } catch (Exception ignored) {}
        }
        return res;
    }

    public JSONObject triggerTailscaleRecovery(String triggerSource) {
        JSONObject res = new JSONObject();
        try {
            recordEvent("TAILSCALE_RECOVERY_TRIGGER", "Trigger source: " + triggerSource, null);
            res = tailscaleManager.triggerReconnection();
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
            res.put("success", tx.optBoolean("success", false) || ts.optBoolean("success", false));
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

        // Probe local Node API (:8080) and SSH (:8022) without Termux dependency
        boolean nodeApiUp = probeHttp("http://127.0.0.1:8080/health", 1500);
        boolean sshUp = probeTcp("127.0.0.1", 8022, 1000);

        states.put("TERMUX_AVAILABLE", (nodeApiUp || sshUp) ? "UP" : (termuxInstalled ? "DOWN" : "UNKNOWN"));
        states.put("TERMUX_SERVICES_HEALTHY", (nodeApiUp && sshUp) ? "UP" : ((nodeApiUp || sshUp) ? "DEGRADED" : "DOWN"));

        // Tailscale states
        boolean tsInstalled = tailscaleManager.isTailscaleInstalled();
        states.put("TAILSCALE_INSTALLED", tsInstalled ? "UP" : "DOWN");
        boolean vpnActive = tailscaleManager.isVpnActive();
        states.put("TAILSCALE_VPN_AUTHORIZED", vpnActive ? "UP" : (tsInstalled ? "DOWN" : "UNKNOWN"));
        states.put("TAILSCALE_CONNECTED", vpnActive ? "UP" : "DOWN");

        // Network / Controller reachability
        boolean controllerUp = probeHttp("http://127.0.0.1:8000/api/v1/health", 1000) || probeTcp("100.120.251.42", 8000, 1500);
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
        out.put("boot_recovery", bootInfo);
        out.put("retry_counters", new JSONObject()
                .put("termux", termuxRetryCount)
                .put("tailscale", tailscaleRetryCount));
        return out;
    }

    private boolean probeTcp(String host, int port, int timeoutMs) {
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress(host, port), timeoutMs);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private boolean probeHttp(String urlStr, int timeoutMs) {
        try {
            URL url = new URL(urlStr);
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setRequestMethod("GET");
            int code = conn.getResponseCode();
            return code >= 200 && code < 400;
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

