package com.personalserver.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import androidx.core.app.NotificationCompat;

/**
 * Foreground Android Service hosting the loopback-only LocalBridgeServer.
 * Uses foregroundServiceType="specialUse" for Android 14-16 compatibility.
 */
public class BridgeService extends Service {

    private static final String TAG = "BridgeService";
    private static final String CHANNEL_ID = "PersonalServer_Native_Bridge_Channel";
    private static final int NOTIFICATION_ID = 8765;

    public static final String ACTION_START = "com.personalserver.bridge.ACTION_START";
    public static final String ACTION_STOP = "com.personalserver.bridge.ACTION_STOP";

    private static volatile boolean running = false;
    private LocalBridgeServer bridgeServer;
    private PowerManager.WakeLock wakeLock;

    public static boolean isRunning() {
        return running;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "Creating PersonalServer Bridge Service...");
        createNotificationChannel();
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PersonalServer:NativeBridgeWakeLock");
            wakeLock.setReferenceCounted(false);
        }
        bridgeServer = new LocalBridgeServer(this, LocalBridgeServer.DEFAULT_PORT);
        bridgeServer.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            Log.i(TAG, "Received ACTION_STOP; stopping PersonalServer Bridge Service.");
            running = false;
            releaseWakeLock();
            if (bridgeServer != null) {
                bridgeServer.stop();
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE);
            } else {
                stopForeground(true);
            }
            stopSelf();
            return START_NOT_STICKY;
        }

        Notification notification = buildNotification("Loopback IPC active on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }

        acquireWakeLock();
        if (bridgeServer != null && !bridgeServer.isListening()) {
            bridgeServer.start();
        }
        running = true;
        Log.i(TAG, "PersonalServer Bridge Service started on 127.0.0.1:" + LocalBridgeServer.DEFAULT_PORT);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        Log.i(TAG, "Stopping PersonalServer Bridge Service...");
        running = false;
        releaseWakeLock();
        if (bridgeServer != null) {
            bridgeServer.stop();
        }
        super.onDestroy();
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
