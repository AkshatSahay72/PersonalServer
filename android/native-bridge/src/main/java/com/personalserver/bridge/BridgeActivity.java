package com.personalserver.bridge;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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

    private static final int REQ_NOTIFICATIONS = 1001;

    private TextView statusText;
    private TextView sensorsText;
    private SensorBridgeManager activitySensorManager;
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
        uiHandler.post(refreshTask);
    }

    @Override
    protected void onPause() {
        uiHandler.removeCallbacks(refreshTask);
        super.onPause();
    }

    private void handleIntentAction(Intent intent, boolean defaultStart) {
        String action = intent != null ? intent.getStringExtra("bridge_action") : null;
        if ("stop".equalsIgnoreCase(action)) {
            stopNativeBridgeService();
        } else if ("start".equalsIgnoreCase(action) || defaultStart) {
            startNativeBridgeService();
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
        updateStatusView();
    }

    private void startNativeBridgeService() {
        Intent svc = new Intent(this, BridgeService.class);
        svc.setAction(BridgeService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }
        uiHandler.postDelayed(this::updateStatusView, 250);
    }

    private void stopNativeBridgeService() {
        Intent svc = new Intent(this, BridgeService.class);
        stopService(svc);
        uiHandler.postDelayed(this::updateStatusView, 250);
    }

    private void updateStatusView() {
        boolean running = BridgeService.isRunning();
        boolean notifGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                || ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED;

        statusText.setText(
                "Service State: " + (running ? "RUNNING" : "STOPPED") + "\n"
                        + "Loopback IPC: 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT + "\n"
                        + "Protocol Version: v" + LocalBridgeServer.PROTOCOL_VERSION + "\n"
                        + "Notifications Permission: " + (notifGranted ? "GRANTED" : "NOT GRANTED") + "\n"
                        + "Android API: " + Build.VERSION.SDK_INT + " (Release " + Build.VERSION.RELEASE + ")"
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

