package com.personalserver.bridge;

import android.Manifest;
import android.app.ForegroundServiceStartNotAllowedException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Control & status Activity for PersonalServer:
 * 1. Native Bridge foreground launch/stop lifecycle control for Android 14-16 FGS rules.
 * 2. One-Tap Node Lifecycle with explicit state machine (STOPPED -> STARTING -> WAITING_FOR_VPN -> STARTING_SERVICES -> VERIFYING_SERVICES -> ONLINE).
 * 3. Focused, user-friendly UI: single prominent control, dedicated VPN attention panel, and collapsible Advanced Diagnostics.
 */
public class BridgeActivity extends AppCompatActivity {

    private static final String TAG = "BridgeActivity";
    private static final int REQ_NOTIFICATIONS = 1001;
    private static final int REQ_RECORD_AUDIO = 1002;
    private static final int REQ_RUN_COMMAND = 1003;
    private static final int REQ_CAMERA = 1004;
    private static final long START_FAILURE_COOLDOWN_MS = 1000L;
    private static volatile boolean activityAlive = false;

    // Primary UI Elements
    private TextView overallBadgeText;
    private TextView overallDetailText;
    private Button primaryActionButton;

    // Focused Tailscale Attention Panel
    private LinearLayout vpnAttentionCard;
    private TextView vpnAttentionTitle;
    private TextView vpnAttentionMessage;
    private Button openTailscaleBtn;
    private Button retryVpnBtn;
    private TextView vpnAttentionHint;

    // Battery Optimization Attention Panel
    private LinearLayout batteryAttentionCard;
    private Button exemptBatteryBtn;

    // Accessibility Attention Panel
    private LinearLayout accessibilityAttentionCard;
    private Button enableAccessibilityBtn;

    // Subsystem Indicators
    private TextView subsystemSummaryText;

    // Collapsible Advanced Diagnostics Section
    private Button toggleDiagnosticsBtn;
    private LinearLayout diagnosticsContainer;
    private boolean diagnosticsExpanded = false;
    private TextView lifecycleStatusText;
    private TextView runtimeInfoText;
    private TextView sensorsText;

    private SensorBridgeManager activitySensorManager;
    private TermuxRecoveryManager termuxRecoveryManager;
    private TailscaleRecoveryManager tailscaleRecoveryManager;
    private final NodeStartupStateMachine stateMachine = new NodeStartupStateMachine();

    private volatile boolean activityResumed = false;
    private volatile boolean pendingStartRequest = false;
    private int pendingResumeRetries = 0;
    private volatile int consecutiveStartFailures = 0;
    private volatile long lastStartFailureMs = 0L;
    private volatile String lastLifecycleError = null;

    // Node Lifecycle Subsystem States
    private volatile String termuxState = "UNKNOWN";
    private volatile String tailscaleState = "UNKNOWN";
    private volatile String nodeApiState = "UNKNOWN";
    private volatile String workerState = "UNKNOWN";
    private volatile String controllerState = "UNKNOWN";
    private volatile String releaseVersion = "UNKNOWN";
    private volatile String updateAvailability = "UNKNOWN";
    private volatile String lastStartupFailure = null;
    private volatile String lastHttpError = null;
    private volatile boolean startupInProgress = false;

    private final ExecutorService backgroundExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "BridgeActivityBackground");
        t.setDaemon(true);
        return t;
    });

    public static boolean isActivityAlive() {
        return activityAlive;
    }

    private final Handler uiHandler = new Handler(Looper.getMainLooper());
    private final Runnable refreshTask = new Runnable() {
        @Override
        public void run() {
            updateStatusView();
            uiHandler.postDelayed(this, 2500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        activityAlive = true;

        termuxRecoveryManager = new TermuxRecoveryManager(this);
        tailscaleRecoveryManager = new TailscaleRecoveryManager(this);
        activitySensorManager = new SensorBridgeManager(this);

        stateMachine.setListener((state, message) -> uiHandler.post(this::updateStatusView));

        ScrollView rootScroll = new ScrollView(this);
        rootScroll.setBackgroundColor(Color.parseColor("#0F141C"));

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(32, 28, 32, 32);

        // Header
        TextView title = new TextView(this);
        title.setText("PersonalServer");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24f);
        title.setPadding(0, 0, 0, 4);
        root.addView(title);

        TextView subtitle = new TextView(this);
        String deviceModel = (Build.MANUFACTURER != null ? Build.MANUFACTURER.toUpperCase() : "Android") + " " + Build.MODEL;
        subtitle.setText(deviceModel + "  (Android " + Build.VERSION.RELEASE + ", API " + Build.VERSION.SDK_INT + ")");
        subtitle.setTextColor(Color.parseColor("#94A3B8"));
        subtitle.setTextSize(13f);
        subtitle.setPadding(0, 0, 0, 16);
        root.addView(subtitle);

        // Overall Status Card
        LinearLayout statusCard = new LinearLayout(this);
        statusCard.setOrientation(LinearLayout.VERTICAL);
        statusCard.setBackgroundColor(Color.parseColor("#1E293B"));
        statusCard.setPadding(24, 18, 24, 18);

        overallBadgeText = new TextView(this);
        overallBadgeText.setText("● STOPPED");
        overallBadgeText.setTextSize(20f);
        overallBadgeText.setTextColor(Color.parseColor("#94A3B8"));
        overallBadgeText.setPadding(0, 0, 0, 4);
        statusCard.addView(overallBadgeText);

        overallDetailText = new TextView(this);
        overallDetailText.setText("Node is ready to start");
        overallDetailText.setTextSize(13.5f);
        overallDetailText.setTextColor(Color.parseColor("#CBD5E1"));
        statusCard.addView(overallDetailText);

        root.addView(statusCard);

        // Prominent Single Action Button
        LinearLayout actionContainer = new LinearLayout(this);
        actionContainer.setOrientation(LinearLayout.VERTICAL);
        actionContainer.setPadding(0, 16, 0, 16);

        primaryActionButton = new Button(this);
        primaryActionButton.setText("▶ Start PersonalServer");
        primaryActionButton.setTextColor(Color.WHITE);
        primaryActionButton.setTextSize(16f);
        primaryActionButton.setBackgroundColor(Color.parseColor("#059669")); // Emerald green
        primaryActionButton.setPadding(24, 16, 24, 16);
        primaryActionButton.setOnClickListener(v -> {
            NodeStartupStateMachine.State curr = stateMachine.getCurrentState();
            if (curr == NodeStartupStateMachine.State.ONLINE
                    || curr == NodeStartupStateMachine.State.STARTING
                    || curr == NodeStartupStateMachine.State.STARTING_SERVICES
                    || curr == NodeStartupStateMachine.State.VERIFYING_SERVICES) {
                stopPersonalServerOneTap();
            } else {
                startPersonalServerOneTap();
            }
        });
        actionContainer.addView(primaryActionButton);
        root.addView(actionContainer);

        // Focused Tailscale Attention Panel (visible only when waiting for VPN)
        vpnAttentionCard = new LinearLayout(this);
        vpnAttentionCard.setOrientation(LinearLayout.VERTICAL);
        vpnAttentionCard.setBackgroundColor(Color.parseColor("#2A1E0B"));
        vpnAttentionCard.setPadding(24, 18, 24, 18);
        vpnAttentionCard.setVisibility(View.GONE);

        vpnAttentionTitle = new TextView(this);
        vpnAttentionTitle.setText("Tailscale needs your attention");
        vpnAttentionTitle.setTextColor(Color.parseColor("#FBBF24")); // Amber gold
        vpnAttentionTitle.setTextSize(16f);
        vpnAttentionTitle.setPadding(0, 0, 0, 6);
        vpnAttentionCard.addView(vpnAttentionTitle);

        vpnAttentionMessage = new TextView(this);
        vpnAttentionMessage.setText("Open Tailscale and enable the VPN connection. PersonalServer will continue automatically when connected.");
        vpnAttentionMessage.setTextColor(Color.parseColor("#FEF3C7"));
        vpnAttentionMessage.setTextSize(13f);
        vpnAttentionMessage.setPadding(0, 0, 0, 12);
        vpnAttentionCard.addView(vpnAttentionMessage);

        LinearLayout vpnBtnRow = new LinearLayout(this);
        vpnBtnRow.setOrientation(LinearLayout.HORIZONTAL);

        openTailscaleBtn = new Button(this);
        openTailscaleBtn.setText("Open Tailscale");
        openTailscaleBtn.setTextColor(Color.WHITE);
        openTailscaleBtn.setBackgroundColor(Color.parseColor("#D97706"));
        openTailscaleBtn.setPadding(16, 10, 16, 10);
        openTailscaleBtn.setOnClickListener(v -> launchTailscaleApp());
        vpnBtnRow.addView(openTailscaleBtn);

        retryVpnBtn = new Button(this);
        retryVpnBtn.setText("Retry Connection");
        retryVpnBtn.setTextColor(Color.WHITE);
        retryVpnBtn.setBackgroundColor(Color.parseColor("#4B5563"));
        retryVpnBtn.setPadding(16, 10, 16, 10);
        retryVpnBtn.setOnClickListener(v -> retryTailscaleCheck());
        vpnBtnRow.addView(retryVpnBtn);

        vpnAttentionCard.addView(vpnBtnRow);

        vpnAttentionHint = new TextView(this);
        vpnAttentionHint.setText("Tip: Return to PersonalServer after enabling the VPN.");
        vpnAttentionHint.setTextColor(Color.parseColor("#FCD34D"));
        vpnAttentionHint.setTextSize(11.5f);
        vpnAttentionHint.setPadding(0, 8, 0, 0);
        vpnAttentionCard.addView(vpnAttentionHint);

        root.addView(vpnAttentionCard);

        // Battery Optimization Attention Panel
        batteryAttentionCard = new LinearLayout(this);
        batteryAttentionCard.setOrientation(LinearLayout.VERTICAL);
        batteryAttentionCard.setBackgroundColor(Color.parseColor("#2A1E0B"));
        batteryAttentionCard.setPadding(24, 18, 24, 18);
        batteryAttentionCard.setVisibility(View.GONE);

        TextView batteryTitle = new TextView(this);
        batteryTitle.setText("⚠️ Battery Optimization Active");
        batteryTitle.setTextColor(Color.parseColor("#FBBF24"));
        batteryTitle.setTextSize(16f);
        batteryTitle.setPadding(0, 0, 0, 6);
        batteryAttentionCard.addView(batteryTitle);

        TextView batteryMessage = new TextView(this);
        batteryMessage.setText("To keep PersonalServer running reliably when the screen is locked, exempt the Native Bridge from Android battery optimizations.");
        batteryMessage.setTextColor(Color.parseColor("#FEF3C7"));
        batteryMessage.setTextSize(13f);
        batteryMessage.setPadding(0, 0, 0, 12);
        batteryAttentionCard.addView(batteryMessage);

        exemptBatteryBtn = new Button(this);
        exemptBatteryBtn.setText("Disable Battery Optimization");
        exemptBatteryBtn.setTextColor(Color.WHITE);
        exemptBatteryBtn.setBackgroundColor(Color.parseColor("#D97706"));
        exemptBatteryBtn.setPadding(16, 10, 16, 10);
        exemptBatteryBtn.setOnClickListener(v -> requestIgnoreBatteryOptimizations());
        batteryAttentionCard.addView(exemptBatteryBtn);

        root.addView(batteryAttentionCard);

        // Accessibility Attention Panel
        accessibilityAttentionCard = new LinearLayout(this);
        accessibilityAttentionCard.setOrientation(LinearLayout.VERTICAL);
        accessibilityAttentionCard.setBackgroundColor(Color.parseColor("#1B2A1E"));
        accessibilityAttentionCard.setPadding(24, 18, 24, 18);
        accessibilityAttentionCard.setVisibility(View.GONE);

        TextView a11yTitle = new TextView(this);
        a11yTitle.setText("🛡️ Tailscale Zero-Touch Auto-Recovery");
        a11yTitle.setTextColor(Color.parseColor("#34D399"));
        a11yTitle.setTextSize(16f);
        a11yTitle.setPadding(0, 0, 0, 6);
        accessibilityAttentionCard.addView(a11yTitle);

        TextView a11yMessage = new TextView(this);
        a11yMessage.setText("To enable automatic zero-touch VPN reconnection across network drops and reboots without physical touches, enable PersonalServer in Accessibility Settings.");
        a11yMessage.setTextColor(Color.parseColor("#D1FAE5"));
        a11yMessage.setTextSize(13f);
        a11yMessage.setPadding(0, 0, 0, 12);
        accessibilityAttentionCard.addView(a11yMessage);

        enableAccessibilityBtn = new Button(this);
        enableAccessibilityBtn.setText("Enable Accessibility Service");
        enableAccessibilityBtn.setTextColor(Color.WHITE);
        enableAccessibilityBtn.setBackgroundColor(Color.parseColor("#059669"));
        enableAccessibilityBtn.setPadding(16, 10, 16, 10);
        enableAccessibilityBtn.setOnClickListener(v -> openAccessibilitySettings());
        accessibilityAttentionCard.addView(enableAccessibilityBtn);

        root.addView(accessibilityAttentionCard);

        // Subsystems Status Overview Card
        LinearLayout subsystemCard = new LinearLayout(this);
        subsystemCard.setOrientation(LinearLayout.VERTICAL);
        subsystemCard.setBackgroundColor(Color.parseColor("#151D2A"));
        subsystemCard.setPadding(20, 16, 20, 16);

        TextView subsystemHeader = new TextView(this);
        subsystemHeader.setText("Subsystems Overview");
        subsystemHeader.setTextColor(Color.parseColor("#94A3B8"));
        subsystemHeader.setTextSize(12.5f);
        subsystemHeader.setPadding(0, 0, 0, 8);
        subsystemCard.addView(subsystemHeader);

        subsystemSummaryText = new TextView(this);
        subsystemSummaryText.setTextSize(13f);
        subsystemSummaryText.setTextColor(Color.parseColor("#E2E8F0"));
        subsystemCard.addView(subsystemSummaryText);

        root.addView(subsystemCard);

        // Collapsible Advanced Diagnostics Toggle
        LinearLayout diagToggleRow = new LinearLayout(this);
        diagToggleRow.setOrientation(LinearLayout.VERTICAL);
        diagToggleRow.setPadding(0, 16, 0, 8);

        toggleDiagnosticsBtn = new Button(this);
        toggleDiagnosticsBtn.setText("▼ Advanced Diagnostics");
        toggleDiagnosticsBtn.setTextColor(Color.parseColor("#94A3B8"));
        toggleDiagnosticsBtn.setBackgroundColor(Color.parseColor("#1E293B"));
        toggleDiagnosticsBtn.setOnClickListener(v -> {
            diagnosticsExpanded = !diagnosticsExpanded;
            diagnosticsContainer.setVisibility(diagnosticsExpanded ? View.VISIBLE : View.GONE);
            toggleDiagnosticsBtn.setText(diagnosticsExpanded ? "▲ Hide Advanced Diagnostics" : "▼ Advanced Diagnostics");
        });
        diagToggleRow.addView(toggleDiagnosticsBtn);
        root.addView(diagToggleRow);

        // Diagnostics Container (Hidden by default)
        diagnosticsContainer = new LinearLayout(this);
        diagnosticsContainer.setOrientation(LinearLayout.VERTICAL);
        diagnosticsContainer.setBackgroundColor(Color.parseColor("#111827"));
        diagnosticsContainer.setPadding(20, 16, 20, 16);
        diagnosticsContainer.setVisibility(View.GONE);

        // Action Buttons Row 1: Termux & Diagnostics
        LinearLayout diagBtnRow1 = new LinearLayout(this);
        diagBtnRow1.setOrientation(LinearLayout.HORIZONTAL);
        diagBtnRow1.setPadding(0, 0, 0, 8);

        Button openTermuxBtn = new Button(this);
        openTermuxBtn.setText("Open Termux");
        openTermuxBtn.setOnClickListener(v -> openTermuxApp());
        diagBtnRow1.addView(openTermuxBtn);

        Button runDoctorBtn = new Button(this);
        runDoctorBtn.setText("Run Doctor");
        runDoctorBtn.setOnClickListener(v -> runDoctorAction());
        diagBtnRow1.addView(runDoctorBtn);

        Button refreshBtn = new Button(this);
        refreshBtn.setText("Refresh");
        refreshBtn.setOnClickListener(v -> {
            checkLocalServicesStateAsync();
            updateStatusView();
        });
        diagBtnRow1.addView(refreshBtn);

        diagnosticsContainer.addView(diagBtnRow1);

        // Action Buttons Row 2: Hardware & Permissions
        LinearLayout diagBtnRow2 = new LinearLayout(this);
        diagBtnRow2.setOrientation(LinearLayout.HORIZONTAL);
        diagBtnRow2.setPadding(0, 0, 0, 12);

        Button permBtn = new Button(this);
        permBtn.setText("Camera & Mic Perms");
        permBtn.setOnClickListener(v -> {
            requestMicrophonePermissionExplicit();
            requestCameraPermissionExplicit();
        });
        diagBtnRow2.addView(permBtn);

        Button restartBridgeBtn = new Button(this);
        restartBridgeBtn.setText("Restart Bridge");
        restartBridgeBtn.setOnClickListener(v -> {
            stopNativeBridgeService();
            uiHandler.postDelayed(this::startNativeBridgeService, 500);
        });
        diagBtnRow2.addView(restartBridgeBtn);

        diagnosticsContainer.addView(diagBtnRow2);

        // Lifecycle State Machine Timeline
        TextView timelineHeader = new TextView(this);
        timelineHeader.setText("Lifecycle State Machine Timeline");
        timelineHeader.setTextColor(Color.parseColor("#38BDF8"));
        timelineHeader.setTextSize(13f);
        timelineHeader.setPadding(0, 8, 0, 4);
        diagnosticsContainer.addView(timelineHeader);

        lifecycleStatusText = new TextView(this);
        lifecycleStatusText.setTextColor(Color.parseColor("#94A3B8"));
        lifecycleStatusText.setTextSize(11.5f);
        lifecycleStatusText.setPadding(0, 0, 0, 12);
        diagnosticsContainer.addView(lifecycleStatusText);

        // Runtime & Hardware info
        TextView runtimeHeader = new TextView(this);
        runtimeHeader.setText("Runtime & Ports");
        runtimeHeader.setTextColor(Color.parseColor("#38BDF8"));
        runtimeHeader.setTextSize(13f);
        runtimeHeader.setPadding(0, 4, 0, 4);
        diagnosticsContainer.addView(runtimeHeader);

        runtimeInfoText = new TextView(this);
        runtimeInfoText.setTextColor(Color.parseColor("#94A3B8"));
        runtimeInfoText.setTextSize(11.5f);
        runtimeInfoText.setPadding(0, 0, 0, 12);
        diagnosticsContainer.addView(runtimeInfoText);

        // Sensors
        TextView sensorHeader = new TextView(this);
        sensorHeader.setText("Discovered Sensors");
        sensorHeader.setTextColor(Color.parseColor("#38BDF8"));
        sensorHeader.setTextSize(13f);
        sensorHeader.setPadding(0, 4, 0, 4);
        diagnosticsContainer.addView(sensorHeader);

        sensorsText = new TextView(this);
        sensorsText.setTextColor(Color.parseColor("#94A3B8"));
        sensorsText.setTextSize(11f);
        sensorsText.setPadding(0, 0, 0, 8);
        diagnosticsContainer.addView(sensorsText);

        root.addView(diagnosticsContainer);

        rootScroll.addView(root);
        setContentView(rootScroll);

        requestNotificationPermissionIfNeeded();
        handleIntentAction(getIntent(), true);
        checkLocalServicesStateAsync();
        updateStatusView();
    }

    @Override
    protected void onDestroy() {
        activityAlive = false;
        uiHandler.removeCallbacks(refreshTask);
        if (activitySensorManager != null) {
            activitySensorManager.stopAll();
        }
        super.onDestroy();
    }

    public boolean isPendingStartRequest() {
        return pendingStartRequest;
    }

    public String getLastLifecycleError() {
        return lastLifecycleError;
    }

    @Override
    protected void onResume() {
        super.onResume();
        activityResumed = true;
        uiHandler.post(refreshTask);

        NodeStartupStateMachine.State curr = stateMachine.getCurrentState();
        if (curr == NodeStartupStateMachine.State.WAITING_FOR_VPN
                || curr == NodeStartupStateMachine.State.NEEDS_USER_ACTION) {
            boolean vpnNow = tailscaleRecoveryManager != null && tailscaleRecoveryManager.isVpnActive();
            if (vpnNow) {
                tailscaleState = "CONNECTED";
                if ("ONLINE".equals(nodeApiState)) {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.ONLINE, "VPN connected; all services online");
                    lastStartupFailure = null;
                } else if (!startupInProgress) {
                    Log.i(TAG, "VPN connected while waiting for VPN; automatically resuming startup sequence");
                    startPersonalServerOneTap();
                }
            } else {
                tailscaleState = "DISCONNECTED";
                stateMachine.transitionTo(NodeStartupStateMachine.State.NEEDS_USER_ACTION, "Tailscale VPN is still disconnected. Tap Open Tailscale to connect.");
            }
        }

        checkLocalServicesStateAsync();

        if (pendingStartRequest && pendingResumeRetries > 0 && !BridgeService.isRunning()) {
            pendingResumeRetries--;
            Log.i(TAG, "Consuming pending start request in onResume (remaining retries: " + pendingResumeRetries + ")");
            attemptStartBridgeService(false);
        } else if (!pendingStartRequest) {
            pendingResumeRetries = 0;
        }

        updateStatusIfInitialized();
    }

    @Override
    protected void onPause() {
        super.onPause();
        activityResumed = false;
        uiHandler.removeCallbacks(refreshTask);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntentAction(intent, false);
    }

    private void handleIntentAction(Intent intent, boolean defaultStart) {
        String action = intent != null ? intent.getStringExtra("bridge_action") : null;
        if ("stop".equalsIgnoreCase(action)) {
            stopNativeBridgeService();
        } else if ("start_all".equalsIgnoreCase(action)) {
            startPersonalServerOneTap();
        } else if ("stop_all".equalsIgnoreCase(action)) {
            stopPersonalServerOneTap();
        } else if ("request_mic_permission".equalsIgnoreCase(action)) {
            startNativeBridgeService();
            requestMicrophonePermissionExplicit();
        } else if ("request_camera_permission".equalsIgnoreCase(action)) {
            startNativeBridgeService();
            requestCameraPermissionExplicit();
        } else if ("request_all_permissions".equalsIgnoreCase(action)) {
            startNativeBridgeService();
            requestMicrophonePermissionExplicit();
            requestCameraPermissionExplicit();
        } else if ("start".equalsIgnoreCase(action) || defaultStart) {
            startNativeBridgeService();
        }
    }

    // =========================================================================
    // One-Tap Node Startup State Machine Sequence
    // =========================================================================

    public void startPersonalServerOneTap() {
        if (startupInProgress) {
            Log.w(TAG, "Startup already in progress; ignoring duplicate start request");
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastStartFailureMs < START_FAILURE_COOLDOWN_MS) {
            Log.w(TAG, "Start requested within failure cooldown period; ignoring");
            return;
        }

        // Idempotency: If already ONLINE and Node API healthy and VPN connected
        if (stateMachine.getCurrentState() == NodeStartupStateMachine.State.ONLINE
                && "ONLINE".equals(nodeApiState)
                && tailscaleRecoveryManager != null && tailscaleRecoveryManager.isVpnActive()) {
            Log.i(TAG, "PersonalServer is already ONLINE and healthy; start is a no-op");
            updateStatusView();
            return;
        }

        startupInProgress = true;
        lastStartupFailure = null;
        stateMachine.transitionTo(NodeStartupStateMachine.State.STARTING, "Initializing startup sequence");
        updateStatusView();

        backgroundExecutor.execute(() -> {
            try {
                // 1. Native bridge foreground service
                stateMachine.logStep("Native Bridge", "STARTING", "Launching foreground service", 0);
                uiHandler.post(this::startNativeBridgeService);
                long waitBridgeMs = 0;
                while (!BridgeService.isRunning() && waitBridgeMs < 2000) {
                    Thread.sleep(200);
                    waitBridgeMs += 200;
                }
                stateMachine.logStep("Native Bridge", BridgeService.isRunning() ? "SUCCESS" : "DEGRADED",
                        BridgeService.isRunning() ? "Active on port " + LocalBridgeServer.DEFAULT_PORT : "Pending", waitBridgeMs);

                // 2. Termux prerequisites
                if (!termuxRecoveryManager.isTermuxInstalled()) {
                    termuxState = "NOT INSTALLED";
                    lastStartupFailure = "Termux application is not installed on this device.";
                    stateMachine.transitionTo(NodeStartupStateMachine.State.FAILED, lastStartupFailure);
                    finishStartup(false);
                    return;
                }

                termuxRecoveryManager.launchTermuxActivity();

                if (!termuxRecoveryManager.isRunCommandPermissionGranted()) {
                    termuxState = "PERMISSION NEEDED";
                    lastStartupFailure = "Termux RUN_COMMAND permission is not granted.";
                    stateMachine.transitionTo(NodeStartupStateMachine.State.NEEDS_USER_ACTION, lastStartupFailure);
                    uiHandler.post(this::requestRunCommandPermissionExplicit);
                    finishStartup(false);
                    return;
                }
                termuxState = "READY";
                stateMachine.logStep("Termux Integration", "SUCCESS", "RUN_COMMAND permission verified", 0);

                // 3. Tailscale VPN check
                boolean vpn = tailscaleRecoveryManager != null && tailscaleRecoveryManager.isVpnActive();
                if (!vpn) {
                    tailscaleState = "DISCONNECTED";
                    lastStartupFailure = "Tailscale VPN is disconnected. Tap Open Tailscale to connect.";
                    stateMachine.transitionTo(NodeStartupStateMachine.State.WAITING_FOR_VPN, "Tailscale needs your attention. Enable the VPN connection.");
                    uiHandler.post(this::launchTailscaleApp);
                    finishStartup(false);
                    return;
                }
                tailscaleState = "CONNECTED";
                stateMachine.logStep("Tailscale VPN", "SUCCESS", "VPN transport active", 0);

                // 3.5 Pull latest production branch updates for cross-node consistency
                if (termuxRecoveryManager.isCommandIntegrationReady()) {
                    long syncStart = System.currentTimeMillis();
                    try {
                        stateMachine.logStep("Production Sync", "STARTING", "Checking production (main) branch updates", 0);
                        termuxRecoveryManager.executeApprovedCommand(TermuxRecoveryManager.OP_PULL_PRODUCTION, null);
                        stateMachine.logStep("Production Sync", "SUCCESS", "Production update preflight dispatched", System.currentTimeMillis() - syncStart);
                    } catch (Exception e) {
                        stateMachine.logStep("Production Sync", "DEGRADED", "Sync preflight skipped: " + e.getMessage(), System.currentTimeMillis() - syncStart);
                    }
                }

                // 4. Starting services (check if already running to ensure idempotency)
                stateMachine.transitionTo(NodeStartupStateMachine.State.STARTING_SERVICES, "Starting local node services via Termux");

                JSONObject existingHealth = fetchNodeApiHealth(1500, 2500);
                boolean alreadyRunning = (existingHealth != null && "online".equalsIgnoreCase(existingHealth.optString("status")));

                if (alreadyRunning) {
                    stateMachine.logStep("Service Dispatch", "SKIPPED", "Node API already running (avoiding duplicate dispatch)", 0);
                } else {
                    long dispatchStart = System.currentTimeMillis();
                    termuxRecoveryManager.executeApprovedCommand(TermuxRecoveryManager.OP_START_SERVICES, null);
                    stateMachine.logStep("Service Dispatch", "SUCCESS", "scripts/start.sh dispatched", System.currentTimeMillis() - dispatchStart);
                }

                // 5. Verifying services
                stateMachine.transitionTo(NodeStartupStateMachine.State.VERIFYING_SERVICES, "Verifying Node API & Worker health");
                boolean apiOnline = false;
                String nodeRole = "worker";
                JSONObject health = null;
                long verifyStart = System.currentTimeMillis();

                // Poll for up to 20 seconds (12 iterations * 1500ms sleep)
                for (int i = 0; i < 12; i++) {
                    Thread.sleep(1500);
                    health = fetchNodeApiHealth(2000, 3500);
                    if (health != null && "online".equalsIgnoreCase(health.optString("status"))) {
                        apiOnline = true;
                        break;
                    }
                }
                long verifyDuration = System.currentTimeMillis() - verifyStart;

                if (!apiOnline) {
                    nodeApiState = "OFFLINE";
                    String errDetail = (lastHttpError != null && !lastHttpError.isEmpty()) ? (" (" + lastHttpError + ")") : "";
                    lastStartupFailure = "Node API (:8080) did not report online within 20s" + errDetail + ".";
                    stateMachine.logStep("Health Verification", "FAILED", lastStartupFailure, verifyDuration);
                    stateMachine.transitionTo(NodeStartupStateMachine.State.FAILED, lastStartupFailure);
                    finishStartup(false);
                    return;
                }

                nodeApiState = "ONLINE";
                workerState = "RUNNING";
                JSONObject nodeObj = health.optJSONObject("node");
                nodeRole = nodeObj != null ? nodeObj.optString("role", "worker") : health.optString("role", "worker");
                JSONObject release = health.optJSONObject("release");
                if (release != null) {
                    String c = release.optString("installed_commit", "");
                    releaseVersion = c.length() > 8 ? c.substring(0, 8) : c;
                    boolean upAvail = release.optBoolean("update_available", false);
                    updateAvailability = upAvail ? "UPDATE AVAILABLE" : "UP TO DATE";
                }
                stateMachine.logStep("Health Verification", "SUCCESS", "Node API online (:8080)", verifyDuration);

                // 6. Check controller status
                if ("controller".equalsIgnoreCase(nodeRole)) {
                    JSONObject ctrlHealth = fetchHttpJson("http://127.0.0.1:8000/api/health", 2000);
                    if (ctrlHealth != null && "online".equalsIgnoreCase(ctrlHealth.optString("status"))) {
                        controllerState = "ONLINE (LOCAL)";
                    } else {
                        controllerState = "OFFLINE (LOCAL)";
                    }
                } else {
                    try {
                        JSONObject ctrlHealth = fetchHttpJson("http://100.85.108.5:8000/api/health", 2000);
                        controllerState = (ctrlHealth != null && "online".equalsIgnoreCase(ctrlHealth.optString("status"))) ? "REACHABLE" : "UNREACHABLE";
                    } catch (Exception e) {
                        controllerState = "UNREACHABLE";
                    }
                }

                // 7. Final State Transition: ONLINE or DEGRADED
                if ("UNREACHABLE".equals(controllerState)) {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.DEGRADED, "Online locally; remote controller unreachable");
                } else {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.ONLINE, "PersonalServer node is operational");
                }
                finishStartup(true);

            } catch (Exception e) {
                lastStartupFailure = "Startup exception: " + e.getMessage();
                stateMachine.transitionTo(NodeStartupStateMachine.State.FAILED, lastStartupFailure);
                finishStartup(false);
            }
        });
    }

    private void finishStartup(boolean success) {
        if (!success) {
            lastStartFailureMs = System.currentTimeMillis();
            consecutiveStartFailures++;
        } else {
            lastStartFailureMs = 0L;
            consecutiveStartFailures = 0;
        }
        startupInProgress = false;
        uiHandler.post(this::updateStatusView);
    }

    public void stopPersonalServerOneTap() {
        stateMachine.transitionTo(NodeStartupStateMachine.State.STOPPING, "Stopping local services");
        updateStatusView();

        backgroundExecutor.execute(() -> {
            try {
                if (termuxRecoveryManager != null && termuxRecoveryManager.isTermuxInstalled()) {
                    termuxRecoveryManager.executeApprovedCommand(TermuxRecoveryManager.OP_STOP_SERVICES, null);
                }
            } catch (Exception ignored) {}

            try {
                Thread.sleep(1000);
            } catch (InterruptedException ignored) {}

            nodeApiState = "STOPPED";
            workerState = "STOPPED";
            startupInProgress = false;
            stateMachine.transitionTo(NodeStartupStateMachine.State.STOPPED, "Services stopped by user");
            uiHandler.post(this::updateStatusView);
        });
    }

    private void checkLocalServicesStateAsync() {
        backgroundExecutor.execute(() -> {
            boolean vpn = tailscaleRecoveryManager != null && tailscaleRecoveryManager.isVpnActive();
            tailscaleState = vpn ? "CONNECTED" : (tailscaleRecoveryManager != null && tailscaleRecoveryManager.isTailscaleInstalled() ? "DISCONNECTED" : "NOT INSTALLED");
            termuxState = (termuxRecoveryManager != null && termuxRecoveryManager.isCommandIntegrationReady()) ? "READY" : (termuxRecoveryManager != null && termuxRecoveryManager.isTermuxInstalled() ? "PERMISSION NEEDED" : "NOT INSTALLED");

            JSONObject health = fetchNodeApiHealth(2000, 3500);
            if (health != null && "online".equalsIgnoreCase(health.optString("status"))) {
                nodeApiState = "ONLINE";
                workerState = "RUNNING";
                JSONObject rel = health.optJSONObject("release");
                if (rel != null) {
                    String c = rel.optString("installed_commit", "");
                    releaseVersion = c.length() > 8 ? c.substring(0, 8) : c;
                    boolean upAvail = rel.optBoolean("update_available", false);
                    updateAvailability = upAvail ? "UPDATE AVAILABLE" : "UP TO DATE";
                }
                NodeStartupStateMachine.State curr = stateMachine.getCurrentState();
                if (curr != NodeStartupStateMachine.State.STARTING
                        && curr != NodeStartupStateMachine.State.STARTING_SERVICES
                        && curr != NodeStartupStateMachine.State.VERIFYING_SERVICES) {
                    if (vpn) {
                        stateMachine.transitionTo(NodeStartupStateMachine.State.ONLINE, "Node services online and mesh connected");
                    } else {
                        stateMachine.transitionTo(NodeStartupStateMachine.State.NEEDS_USER_ACTION, "Node services online; VPN disconnected");
                    }
                }
            } else {
                nodeApiState = "OFFLINE";
                NodeStartupStateMachine.State curr = stateMachine.getCurrentState();
                if (curr != NodeStartupStateMachine.State.STARTING
                        && curr != NodeStartupStateMachine.State.STARTING_SERVICES
                        && curr != NodeStartupStateMachine.State.VERIFYING_SERVICES
                        && curr != NodeStartupStateMachine.State.WAITING_FOR_VPN
                        && curr != NodeStartupStateMachine.State.NEEDS_USER_ACTION
                        && curr != NodeStartupStateMachine.State.FAILED) {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.STOPPED, "Services offline");
                }
            }
            uiHandler.post(this::updateStatusView);
        });
    }

    public void openTermuxApp() {
        if (termuxRecoveryManager != null) {
            termuxRecoveryManager.launchTermuxActivity();
        }
    }

    public void launchTailscaleApp() {
        if (tailscaleRecoveryManager != null) {
            TailscaleAccessibilityService.requestAutoToggle();
            Intent intent = tailscaleRecoveryManager.resolveLaunchIntent();
            try {
                startActivity(intent);
            } catch (Exception e) {
                lastStartupFailure = "Failed to launch Tailscale app: " + e.getMessage();
                updateStatusView();
            }
        }
    }

    public void retryTailscaleCheck() {
        backgroundExecutor.execute(() -> {
            boolean vpn = tailscaleRecoveryManager != null && tailscaleRecoveryManager.isVpnActive();
            tailscaleState = vpn ? "CONNECTED" : "DISCONNECTED";
            if (vpn) {
                lastStartupFailure = null;
                if ("ONLINE".equals(nodeApiState)) {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.ONLINE, "VPN connected on retry; services online");
                } else {
                    stateMachine.transitionTo(NodeStartupStateMachine.State.STARTING, "VPN connected; resuming service startup");
                    startPersonalServerOneTap();
                }
            } else {
                lastStartupFailure = "Tailscale VPN remains disconnected. Tap Open Tailscale to connect.";
                stateMachine.transitionTo(NodeStartupStateMachine.State.NEEDS_USER_ACTION, lastStartupFailure);
            }
            uiHandler.post(this::updateStatusView);
        });
    }

    public void retryServicesStart() {
        startPersonalServerOneTap();
    }

    public void runDoctorAction() {
        backgroundExecutor.execute(() -> {
            try {
                if (termuxRecoveryManager != null) {
                    termuxRecoveryManager.executeApprovedCommand(TermuxRecoveryManager.OP_RUN_DOCTOR, null);
                }
            } catch (Exception e) {
                lastStartupFailure = "Run Doctor failed: " + e.getMessage();
                uiHandler.post(this::updateStatusView);
            }
        });
    }

    public void syncProductionAction() {
        backgroundExecutor.execute(() -> {
            try {
                if (termuxRecoveryManager != null && termuxRecoveryManager.isCommandIntegrationReady()) {
                    long start = System.currentTimeMillis();
                    termuxRecoveryManager.executeApprovedCommand(TermuxRecoveryManager.OP_PULL_PRODUCTION, null);
                    try {
                        getSharedPreferences(RecoveryCoordinator.PREFS_NAME, MODE_PRIVATE)
                                .edit()
                                .putLong(RecoveryCoordinator.KEY_LAST_DAILY_SYNC_MS, System.currentTimeMillis())
                                .apply();
                    } catch (Exception ignored) {}
                    Log.i(TAG, "Manual production sync (main branch) dispatched via Termux");
                    uiHandler.post(() -> {
                        stateMachine.logStep("Production Sync", "SUCCESS", "Manual production update dispatched", System.currentTimeMillis() - start);
                        updateStatusView();
                    });
                }
            } catch (Exception e) {
                lastStartupFailure = "Sync Production failed: " + e.getMessage();
                uiHandler.post(this::updateStatusView);
            }
        });
    }

    private JSONObject fetchNodeApiHealth(int connectTimeoutMs, int readTimeoutMs) {
        JSONObject res = fetchHttpJson("http://127.0.0.1:8080/health", connectTimeoutMs, readTimeoutMs);
        if (res == null) {
            // Fallback to local mesh IP in case VPN routing isolates loopback
            res = fetchHttpJson("http://100.113.234.61:8080/health", connectTimeoutMs, readTimeoutMs);
        }
        return res;
    }

    private JSONObject fetchHttpJson(String urlStr, int timeoutMs) {
        return fetchHttpJson(urlStr, Math.min(2000, timeoutMs), timeoutMs);
    }

    private JSONObject fetchHttpJson(String urlStr, int connectTimeoutMs, int readTimeoutMs) {
        HttpURLConnection conn = null;
        try {
            URL u = new URL(urlStr);
            conn = (HttpURLConnection) u.openConnection();
            conn.setConnectTimeout(connectTimeoutMs);
            conn.setReadTimeout(readTimeoutMs);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("User-Agent", "PersonalServerBridge/1.0");
            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                BufferedReader br = new BufferedReader(new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = br.readLine()) != null) {
                    sb.append(line);
                }
                br.close();
                lastHttpError = null;
                return new JSONObject(sb.toString());
            } else {
                lastHttpError = "HTTP " + code + " from " + urlStr;
                Log.w(TAG, "fetchHttpJson: " + lastHttpError);
            }
        } catch (Exception ex) {
            lastHttpError = ex.getClass().getSimpleName() + ": " + ex.getMessage();
            Log.w(TAG, "fetchHttpJson failed for " + urlStr + ": " + lastHttpError);
        } finally {
            if (conn != null) {
                try {
                    conn.disconnect();
                } catch (Exception ignored) {}
            }
        }
        return null;
    }

    // =========================================================================
    // Foreground Service & Permissions
    // =========================================================================

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                        this,
                        new String[]{Manifest.permission.POST_NOTIFICATIONS},
                        REQ_NOTIFICATIONS
                );
            }
        }
    }

    public void requestMicrophonePermissionExplicit() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    REQ_RECORD_AUDIO
            );
        }
    }

    public void requestCameraPermissionExplicit() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{Manifest.permission.CAMERA},
                    REQ_CAMERA
            );
        }
    }

    public void requestRunCommandPermissionExplicit() {
        if (ContextCompat.checkSelfPermission(this, "com.termux.permission.RUN_COMMAND")
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{"com.termux.permission.RUN_COMMAND"},
                    REQ_RUN_COMMAND
            );
        }
    }

    public void requestIgnoreBatteryOptimizations() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                Intent intent = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                intent.setData(android.net.Uri.parse("package:" + getPackageName()));
                startActivity(intent);
            } catch (Exception e) {
                try {
                    Intent intent = new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                    startActivity(intent);
                } catch (Exception ignored) {}
            }
        }
    }

    public void openAccessibilitySettings() {
        try {
            Intent intent = new Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception e) {
            Log.w(TAG, "Failed to open accessibility settings: " + e.getMessage());
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_RECORD_AUDIO && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            Log.i(TAG, "RECORD_AUDIO permission explicitly granted by user.");
        } else if (requestCode == REQ_CAMERA && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            Log.i(TAG, "CAMERA permission explicitly granted by user.");
        } else if (requestCode == REQ_RUN_COMMAND && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            termuxState = "READY";
        }
        updateStatusIfInitialized();
    }

    protected void launchBridgeForegroundService(Intent svc) {
        ContextCompat.startForegroundService(this, svc);
    }

    protected void dispatchStopBridgeService(Intent svc) {
        stopService(svc);
    }

    public void startNativeBridgeService() {
        pendingStartRequest = true;
        pendingResumeRetries = activityResumed ? 0 : 1;
        attemptStartBridgeService(true);
    }

    private void attemptStartBridgeService(boolean allowDeferOnFailure) {
        if (BridgeService.isRunning()) {
            pendingStartRequest = false;
            pendingResumeRetries = 0;
            lastLifecycleError = null;
            updateStatusIfInitialized();
            return;
        }

        if (BridgeService.isStarting()) {
            updateStatusIfInitialized();
            return;
        }

        long nowMs = System.currentTimeMillis();
        if (consecutiveStartFailures >= 2 && lastStartFailureMs > 0L
                && (nowMs - lastStartFailureMs) < START_FAILURE_COOLDOWN_MS) {
            lastLifecycleError = "Start cooldown active after repeated failures";
            updateStatusIfInitialized();
            return;
        }

        Intent svc = new Intent(this, BridgeService.class);
        svc.setAction(BridgeService.ACTION_START);
        try {
            launchBridgeForegroundService(svc);
            pendingStartRequest = false;
            pendingResumeRetries = 0;
            consecutiveStartFailures = 0;
            lastStartFailureMs = 0L;
            lastLifecycleError = null;
        } catch (RuntimeException ex) {
            consecutiveStartFailures++;
            lastStartFailureMs = System.currentTimeMillis();
            boolean fgsRestricted = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                    && ex instanceof ForegroundServiceStartNotAllowedException)
                    || ex instanceof IllegalStateException;
            lastLifecycleError = ex.getClass().getSimpleName() + ": " + ex.getMessage();
            Log.w(TAG, "Bridge foreground service start rejected: " + lastLifecycleError);
            if (!(allowDeferOnFailure && fgsRestricted && pendingResumeRetries > 0)) {
                pendingStartRequest = false;
                pendingResumeRetries = 0;
            }
        }
        if (overallBadgeText != null) {
            uiHandler.postDelayed(this::updateStatusView, 250);
        }
    }

    protected void stopNativeBridgeService() {
        pendingStartRequest = false;
        pendingResumeRetries = 0;
        lastLifecycleError = null;
        Intent svc = new Intent(this, BridgeService.class);
        try {
            dispatchStopBridgeService(svc);
        } catch (RuntimeException ex) {
            lastLifecycleError = ex.getClass().getSimpleName() + ": " + ex.getMessage();
            Log.w(TAG, "Bridge service stop failed: " + lastLifecycleError);
        }
        if (overallBadgeText != null) {
            uiHandler.postDelayed(this::updateStatusView, 250);
        }
    }

    private void updateStatusIfInitialized() {
        if (overallBadgeText != null) {
            updateStatusView();
        }
    }

    private void updateStatusView() {
        if (overallBadgeText == null || overallDetailText == null || primaryActionButton == null) {
            return;
        }

        NodeStartupStateMachine.State currentState = stateMachine.getCurrentState();
        String currentMsg = stateMachine.getCurrentMessage();

        // 1. Color and badge text for Overall State
        int stateColor;
        String badgeLabel;
        switch (currentState) {
            case ONLINE:
                stateColor = Color.parseColor("#10B981"); // Emerald Green
                badgeLabel = "● ONLINE";
                break;
            case STARTING:
            case STARTING_SERVICES:
            case VERIFYING_SERVICES:
                stateColor = Color.parseColor("#3B82F6"); // Cobalt Blue
                badgeLabel = "● " + currentState.name().replace('_', ' ');
                break;
            case WAITING_FOR_VPN:
            case NEEDS_USER_ACTION:
                stateColor = Color.parseColor("#F59E0B"); // Amber Gold
                badgeLabel = "● " + currentState.name().replace('_', ' ');
                break;
            case DEGRADED:
                stateColor = Color.parseColor("#EAB308"); // Warm Yellow
                badgeLabel = "● DEGRADED";
                break;
            case STOPPING:
                stateColor = Color.parseColor("#94A3B8"); // Slate
                badgeLabel = "● STOPPING";
                break;
            case FAILED:
                stateColor = Color.parseColor("#EF4444"); // Crimson Red
                badgeLabel = "● FAILED";
                break;
            default:
                stateColor = Color.parseColor("#94A3B8"); // Slate
                badgeLabel = "● STOPPED";
                break;
        }

        overallBadgeText.setText(badgeLabel);
        overallBadgeText.setTextColor(stateColor);
        overallDetailText.setText(currentMsg);

        // 2. Prominent Action Button Toggle (Start vs Stop)
        if (currentState == NodeStartupStateMachine.State.ONLINE
                || currentState == NodeStartupStateMachine.State.STARTING
                || currentState == NodeStartupStateMachine.State.STARTING_SERVICES
                || currentState == NodeStartupStateMachine.State.VERIFYING_SERVICES) {
            primaryActionButton.setText("■ Stop PersonalServer");
            primaryActionButton.setBackgroundColor(Color.parseColor("#DC2626")); // Red
        } else {
            primaryActionButton.setText("▶ Start PersonalServer");
            primaryActionButton.setBackgroundColor(Color.parseColor("#059669")); // Green
        }

        // 3. Focused VPN Attention Panel visibility
        boolean showVpnPanel = (currentState == NodeStartupStateMachine.State.WAITING_FOR_VPN
                || (currentState == NodeStartupStateMachine.State.NEEDS_USER_ACTION && "DISCONNECTED".equals(tailscaleState)));
        if (vpnAttentionCard != null) {
            vpnAttentionCard.setVisibility(showVpnPanel ? View.VISIBLE : View.GONE);
        }

        // 3b. Battery Optimization Attention Panel visibility
        if (batteryAttentionCard != null) {
            boolean isOptimized = false;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                android.os.PowerManager pm = (android.os.PowerManager) getSystemService(Context.POWER_SERVICE);
                isOptimized = pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName());
            }
            batteryAttentionCard.setVisibility(isOptimized ? View.VISIBLE : View.GONE);
        }

        // 3c. Accessibility Attention Panel visibility
        if (accessibilityAttentionCard != null) {
            boolean a11yGranted = TailscaleAccessibilityService.isAccessibilityPermissionGranted(this);
            accessibilityAttentionCard.setVisibility(a11yGranted ? View.GONE : View.VISIBLE);
        }

        // 4. Subsystems Summary block
        if (subsystemSummaryText != null) {
            String tsIp = (tailscaleRecoveryManager != null) ? tailscaleRecoveryManager.getTailscaleIp() : null;
            String tsLabel = tailscaleState + (tsIp != null ? " (" + tsIp + ")" : "");
            subsystemSummaryText.setText(
                    "• Tailscale VPN   : " + tsLabel + "\n"
                            + "• Termux Envir.   : " + termuxState + "\n"
                            + "• Node API (:8080): " + nodeApiState + "\n"
                            + "• Node Worker     : " + workerState + "\n"
                            + "• Last Result     : " + (lastStartupFailure != null ? lastStartupFailure : currentMsg)
            );
        }

        // 5. Diagnostics Details
        if (lifecycleStatusText != null) {
            lifecycleStatusText.setText(stateMachine.getFormattedSummary());
        }

        if (runtimeInfoText != null) {
            boolean running = BridgeService.isRunning();
            boolean starting = BridgeService.isStarting();
            String bridgeStatus = running ? "RUNNING (port " + LocalBridgeServer.DEFAULT_PORT + ")" : (starting ? "STARTING" : "STOPPED");
            runtimeInfoText.setText(
                    "• Native Bridge Service : " + bridgeStatus + "\n"
                            + "• Media Streaming Port  : 127.0.0.1:" + MediaStreamServer.DEFAULT_MEDIA_PORT + "\n"
                            + "• Remote Controller     : " + controllerState + " (100.85.108.5:8000)\n"
                            + "• Last HTTP Probe       : " + (lastHttpError != null ? lastHttpError : "HTTP 200 OK") + "\n"
                            + "• Release Channel       : release/stable\n"
                            + "• Installed Commit      : " + releaseVersion + " (" + updateAvailability + ")"
            );
        }

        if (sensorsText != null) {
            try {
                JSONObject discovered = activitySensorManager.discoverSensors(null);
                JSONObject sensors = discovered != null ? discovered.optJSONObject("sensors") : null;
                StringBuilder sb = new StringBuilder();
                if (sensors != null) {
                    Iterator<String> keys = sensors.keys();
                    while (keys.hasNext()) {
                        String k = keys.next();
                        JSONObject s = sensors.optJSONObject(k);
                        if (s == null) continue;
                        boolean avail = s.optBoolean("available", false);
                        sb.append("• ").append(k).append(": ").append(avail ? "READY" : "UNAVAILABLE");
                        if (avail) {
                            sb.append(" (").append(s.optString("name")).append(")");
                        }
                        sb.append("\n");
                    }
                }
                sensorsText.setText(sb.length() > 0 ? sb.toString() : "No sensors detected");
            } catch (Exception e) {
                sensorsText.setText("Sensor inspection: " + e.getMessage());
            }
        }
    }
}
