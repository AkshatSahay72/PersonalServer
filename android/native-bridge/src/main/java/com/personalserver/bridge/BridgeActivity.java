package com.personalserver.bridge;

import android.Manifest;
import android.app.ForegroundServiceStartNotAllowedException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import org.json.JSONObject;

import java.util.Iterator;

/**
 * Control & status Activity for the PersonalServer Native Bridge.
 * Provides foreground launch/stop lifecycle control for Android 14-16 FGS rules
 * and read-only inspection of loopback IPC status and SensorManager availability.
 */
public class BridgeActivity extends AppCompatActivity {

    private static final String TAG = "BridgeActivity";
    private static final int REQ_NOTIFICATIONS = 1001;
    private static final int REQ_RECORD_AUDIO = 1002;

    private TextView statusText;
    private TextView sensorsText;
    private SensorBridgeManager activitySensorManager;
    private volatile boolean activityResumed = false;
    private volatile boolean pendingStartRequest = false;
    private int pendingResumeRetries = 0;
    private volatile String lastLifecycleError = null;
    private final Handler uiHandler = new Handler(Looper.getMainLooper());
    private final Runnable refreshTask = new Runnable() {
        @Override
        public void run() {
            updateStatusView();
            uiHandler.postDelayed(this, 1500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(36, 36, 36, 36);
        root.setBackgroundColor(Color.parseColor("#11161D"));

        TextView title = new TextView(this);
        title.setText("PersonalServer Native Bridge");
        title.setTextColor(Color.WHITE);
        title.setTextSize(20f);
        root.addView(title);

        statusText = new TextView(this);
        statusText.setTextColor(Color.parseColor("#9CDCFE"));
        statusText.setTextSize(14f);
        statusText.setPadding(0, 24, 0, 24);
        root.addView(statusText);

        LinearLayout buttonRow = new LinearLayout(this);
        buttonRow.setOrientation(LinearLayout.HORIZONTAL);

        Button startBtn = new Button(this);
        startBtn.setText("Start Bridge");
        startBtn.setOnClickListener(v -> startNativeBridgeService());
        buttonRow.addView(startBtn);

        Button stopBtn = new Button(this);
        stopBtn.setText("Stop Bridge");
        stopBtn.setOnClickListener(v -> stopNativeBridgeService());
        buttonRow.addView(stopBtn);

        Button refreshBtn = new Button(this);
        refreshBtn.setText("Refresh");
        refreshBtn.setOnClickListener(v -> updateStatusView());
        buttonRow.addView(refreshBtn);

        root.addView(buttonRow);

        Button micPermBtn = new Button(this);
        micPermBtn.setText("Grant Mic Permission");
        micPermBtn.setOnClickListener(v -> requestMicrophonePermissionExplicit());
        root.addView(micPermBtn);

        sensorsText = new TextView(this);
        sensorsText.setTextColor(Color.parseColor("#D4D4D4"));
        sensorsText.setTextSize(13f);
        sensorsText.setPadding(0, 28, 0, 12);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(sensorsText);
        root.addView(scroll);

        setContentView(root);
        activitySensorManager = new SensorBridgeManager(this);
        requestNotificationPermissionIfNeeded();
        handleIntentAction(getIntent(), true);
        updateStatusView();
    }

    @Override
    protected void onDestroy() {
        uiHandler.removeCallbacks(refreshTask);
        if (activitySensorManager != null) {
            activitySensorManager.stopAll();
        }
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntentAction(intent, false);
        updateStatusView();
    }

    @Override
    protected void onResume() {
        super.onResume();
        activityResumed = true;
        if (pendingStartRequest && pendingResumeRetries > 0 && !BridgeService.isRunning()) {
            pendingResumeRetries--;
            attemptStartBridgeService(false);
        }
        uiHandler.post(refreshTask);
    }

    @Override
    protected void onPause() {
        activityResumed = false;
        uiHandler.removeCallbacks(refreshTask);
        super.onPause();
    }

    private void handleIntentAction(Intent intent, boolean defaultStart) {
        String action = intent != null ? intent.getStringExtra("bridge_action") : null;
        if ("stop".equalsIgnoreCase(action)) {
            stopNativeBridgeService();
        } else if ("request_mic_permission".equalsIgnoreCase(action)) {
            startNativeBridgeService();
            requestMicrophonePermissionExplicit();
        } else if ("start".equalsIgnoreCase(action) || defaultStart) {
            startNativeBridgeService();
        }
    }

    protected void requestMicrophonePermissionExplicit() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    REQ_RECORD_AUDIO
            );
        } else {
            refreshRunningServiceCapabilities();
            updateStatusView();
        }
    }

    private void refreshRunningServiceCapabilities() {
        if (BridgeService.isRunning()) {
            Intent svc = new Intent(this, BridgeService.class);
            svc.setAction(BridgeService.ACTION_START);
            try {
                launchBridgeForegroundService(svc);
            } catch (RuntimeException ex) {
                Log.w(TAG, "Unable to refresh running FGS capabilities: " + ex.getMessage());
            }
        }
    }

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

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_RECORD_AUDIO
                && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            refreshRunningServiceCapabilities();
        }
        updateStatusView();
    }

    protected void launchBridgeForegroundService(Intent svc) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }
    }

    protected void dispatchStopBridgeService(Intent svc) {
        stopService(svc);
    }

    protected boolean isPendingStartRequest() {
        return pendingStartRequest;
    }

    protected String getLastLifecycleError() {
        return lastLifecycleError;
    }

    protected void startNativeBridgeService() {
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

        Intent svc = new Intent(this, BridgeService.class);
        svc.setAction(BridgeService.ACTION_START);
        try {
            launchBridgeForegroundService(svc);
            pendingStartRequest = false;
            pendingResumeRetries = 0;
            lastLifecycleError = null;
        } catch (RuntimeException ex) {
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
        if (statusText != null) {
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
        if (statusText != null) {
            uiHandler.postDelayed(this::updateStatusView, 250);
        }
    }

    private void updateStatusIfInitialized() {
        if (statusText != null) {
            updateStatusView();
        }
    }

    private void updateStatusView() {
        if (statusText == null) {
            return;
        }
        boolean running = BridgeService.isRunning();
        boolean starting = BridgeService.isStarting();
        String serviceErr = BridgeService.getLastStartError();
        String effectiveErr = lastLifecycleError != null ? lastLifecycleError : serviceErr;

        String stateLabel;
        if (running) {
            stateLabel = "RUNNING";
        } else if (starting) {
            stateLabel = "STARTING";
        } else if (pendingStartRequest) {
            stateLabel = "PENDING_RESUME";
        } else if (effectiveErr != null) {
            stateLabel = "START_FAILED";
        } else {
            stateLabel = "STOPPED";
        }

        boolean notifGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                || ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED;
        boolean micGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
        boolean micRecording = BridgeService.isRecordingActive();
        String micSessionId = BridgeService.getActiveRecordingSessionId();

        statusText.setText(
                "Service State: " + stateLabel + "\n"
                        + "Loopback IPC: 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT + "\n"
                        + "Protocol Version: v" + LocalBridgeServer.PROTOCOL_VERSION + "\n"
                        + "Notifications Permission: " + (notifGranted ? "GRANTED" : "NOT GRANTED") + "\n"
                        + "Microphone Permission: " + (micGranted ? "GRANTED" : "NOT GRANTED") + "\n"
                        + "Microphone Capture: " + (micRecording ? ("RECORDING (" + micSessionId + ")") : "IDLE") + "\n"
                        + "Android API: " + Build.VERSION.SDK_INT + " (Release " + Build.VERSION.RELEASE + ")"
                        + (effectiveErr != null ? "\nLast Error: " + effectiveErr : "")
        );

        try {
            JSONObject discovered = activitySensorManager.discoverSensors(null);
            JSONObject sensors = discovered.optJSONObject("sensors");
            StringBuilder sb = new StringBuilder("SensorManager Read-Only Discovery:\n\n");
            if (sensors != null) {
                Iterator<String> keys = sensors.keys();
                while (keys.hasNext()) {
                    String k = keys.next();
                    JSONObject s = sensors.optJSONObject(k);
                    if (s == null) continue;
                    boolean avail = s.optBoolean("available", false);
                    sb.append(k).append(": ").append(avail ? "READY" : "UNAVAILABLE");
                    if (avail) {
                        sb.append("\n  name=").append(s.optString("name"))
                                .append(" | vendor=").append(s.optString("vendor"))
                                .append("\n  range=").append(s.opt("max_range"))
                                .append(" | res=").append(s.opt("resolution"))
                                .append(" | power=").append(s.opt("power_ma")).append(" mA");
                    }
                    sb.append("\n\n");
                }
            }
            sensorsText.setText(sb.toString());
        } catch (Exception e) {
            sensorsText.setText("Sensor discovery inspection failed: " + e.getMessage());
        }
    }
}

