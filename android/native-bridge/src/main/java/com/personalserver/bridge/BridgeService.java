package com.personalserver.bridge;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

/**
 * Foreground Android Service hosting the loopback-only LocalBridgeServer.
 * Uses foregroundServiceType="specialUse" for Android 14-16 compatibility.
 */
public class BridgeService extends Service {

    private static final String TAG = "BridgeService";
    public static final String CHANNEL_ID = "PersonalServer_Native_Bridge_Channel";
    private static final int NOTIFICATION_ID = 8765;

    public static final String ACTION_START = "com.personalserver.bridge.ACTION_START";
    public static final String ACTION_STOP = "com.personalserver.bridge.ACTION_STOP";

    private static volatile BridgeService activeInstance = null;
    private static volatile boolean serviceCreated = false;
    private static volatile boolean running = false;
    private static volatile boolean starting = false;
    private static volatile boolean recordingActive = false;
    private static volatile String activeRecordingSessionId = null;
    private static volatile String lastStartError = null;
    private LocalBridgeServer bridgeServer;
    private PowerManager.WakeLock wakeLock;

    public static boolean isProcessActive() {
        return serviceCreated || running || activeInstance != null;
    }

    public static boolean isRunning() {
        BridgeService inst = activeInstance;
        return running && inst != null && inst.bridgeServer != null && inst.bridgeServer.isListening();
    }

    public static boolean isRecordingActive() {
        return recordingActive && isRunning();
    }

    public static String getActiveRecordingSessionId() {
        return recordingActive ? activeRecordingSessionId : null;
    }

    public static boolean isStarting() {
        return starting;
    }

    public static String getLastStartError() {
        return lastStartError;
    }

    protected LocalBridgeServer createBridgeServer() {
        return new LocalBridgeServer(this, LocalBridgeServer.DEFAULT_PORT);
    }

    protected void startForegroundWithCompat(Notification notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            int fgsTypes = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE;
            boolean micGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED;
            if (micGranted) {
                fgsTypes |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            }
            try {
                startForeground(NOTIFICATION_ID, notification, fgsTypes);
            } catch (SecurityException micFgsEx) {
                Log.w(TAG, "Microphone FGS type unavailable cleanly falling back to SPECIAL_USE: " + micFgsEx.getMessage());
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            }
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void onRecordingNotificationUpdate(boolean active, String sessionId, int maxDurationSec) {
        recordingActive = active;
        activeRecordingSessionId = active ? sessionId : null;
        if (!running || bridgeServer == null || !bridgeServer.isListening()) {
            return;
        }
        String text = active
                ? ("Recording microphone audio (" + sessionId + ", max " + maxDurationSec + "s)")
                : ("Loopback IPC active on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
        try {
            Notification n = buildNotification(text);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.notify(NOTIFICATION_ID, n);
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed updating recording notification: " + e.getMessage());
        }
    }

    protected void stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            stopForeground(true);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        activeInstance = this;
        serviceCreated = true;
        Log.i(TAG, "Creating PersonalServer Bridge Service...");
        createNotificationChannel();
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PersonalServer:NativeBridgeWakeLock");
            wakeLock.setReferenceCounted(false);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        activeInstance = this;
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            Log.i(TAG, "Received ACTION_STOP; stopping PersonalServer Bridge Service.");
            starting = false;
            running = false;
            lastStartError = null;
            stopServiceResources();
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }

        // Satisfy Android FGS contract immediately before awaiting socket bind
        Notification startingNotification = buildNotification("Starting loopback IPC on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
        startForegroundWithCompat(startingNotification);

        // Idempotent reuse when loopback socket is already bound and healthy
        if (bridgeServer != null && bridgeServer.isListening()) {
            starting = false;
            running = true;
            lastStartError = null;
            acquireWakeLock();
            if (bridgeServer.getRecoveryCoordinator() != null && !bridgeServer.getRecoveryCoordinator().isRunning()) {
                bridgeServer.getRecoveryCoordinator().start();
            }
            Notification activeNotification = buildNotification("Loopback IPC active on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
            startForegroundWithCompat(activeNotification);
            return START_STICKY;
        }

        starting = true;
        running = false;
        lastStartError = null;

        if (bridgeServer == null) {
            bridgeServer = createBridgeServer();
            bridgeServer.setRecordingNotificationListener(this::onRecordingNotificationUpdate);
        }
        boolean bound = bridgeServer.startAndAwaitBind(LocalBridgeServer.DEFAULT_START_TIMEOUT_MS);
        if (bound && bridgeServer != null && bridgeServer.isListening()) {
            starting = false;
            running = true;
            lastStartError = null;
            acquireWakeLock();
            if (bridgeServer.getRecoveryCoordinator() != null) {
                bridgeServer.getRecoveryCoordinator().start();
            }
            Notification activeNotification = buildNotification("Loopback IPC active on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
            startForegroundWithCompat(activeNotification);
            Log.i(TAG, "PersonalServer Bridge Service started on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
            return START_STICKY;
        }

        String err = (bridgeServer != null && bridgeServer.getLastError() != null)
                ? bridgeServer.getLastError()
                : "Loopback socket bind failed";
        Log.e(TAG, "BridgeService startup aborted because loopback IPC bind failed: " + err);
        lastStartError = err;
        starting = false;
        running = false;
        stopServiceResources();
        stopForegroundCompat();
        stopSelf();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        Log.i(TAG, "Stopping PersonalServer Bridge Service...");
        starting = false;
        running = false;
        serviceCreated = false;
        if (activeInstance == this) {
            activeInstance = null;
        }
        stopServiceResources();
        super.onDestroy();
    }

    private void stopServiceResources() {
        recordingActive = false;
        activeRecordingSessionId = null;
        releaseWakeLock();
        LocalBridgeServer server = bridgeServer;
        bridgeServer = null;
        if (server != null) {
            if (server.getRecoveryCoordinator() != null) {
                server.getRecoveryCoordinator().stop();
            }
            server.stop();
        }
    }

    private void acquireWakeLock() {
        try {
            if (wakeLock != null && !wakeLock.isHeld()) {
                wakeLock.acquire(30 * 60 * 1000L);
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed to acquire WakeLock: " + e.getMessage());
        }
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) {}
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "PersonalServer Native Bridge",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Local loopback IPC service for Android sensors, battery, and thermals");
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(channel);
            }
        }
    }

    private Notification buildNotification(String text) {
        Intent activityIntent = new Intent(this, BridgeActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                activityIntent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("PersonalServer Native Bridge")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }
}
