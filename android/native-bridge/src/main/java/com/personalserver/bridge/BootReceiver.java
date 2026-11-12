package com.personalserver.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;
import androidx.core.content.ContextCompat;

/**
 * BroadcastReceiver for BOOT_COMPLETED.
 * Starts BridgeService to launch the native recovery supervisor after phone boot.
 */
public class BootReceiver extends BroadcastReceiver {

    private static final String TAG = "BootReceiver";
    public static final String PREFS_NAME = "PersonalServer_BootRecovery";
    public static final String KEY_BOOT_RECOVERY_ENABLED = "boot_recovery_enabled";
    public static final String KEY_BOOT_RECOVERY_LAST_RESULT = "boot_recovery_last_result";
    public static final String KEY_BOOT_RECOVERY_LAST_TIMESTAMP = "boot_recovery_last_timestamp";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        Log.i(TAG, "Received broadcast action: " + action);

        if (Intent.ACTION_BOOT_COMPLETED.equals(action)
                || "android.intent.action.QUICKBOOT_POWERON".equals(action)
                || "com.htc.intent.action.QUICKBOOT_POWERON".equals(action)) {

            SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
            boolean enabled = prefs.getBoolean(KEY_BOOT_RECOVERY_ENABLED, true);
            if (!enabled) {
                Log.i(TAG, "Boot recovery is disabled by owner preference.");
                prefs.edit()
                        .putString(KEY_BOOT_RECOVERY_LAST_RESULT, "DISABLED")
                        .putLong(KEY_BOOT_RECOVERY_LAST_TIMESTAMP, System.currentTimeMillis())
                        .apply();
                return;
            }

            Intent serviceIntent = new Intent(context, BridgeService.class);
            serviceIntent.setAction(BridgeService.ACTION_START);
            serviceIntent.putExtra("source", "BOOT_COMPLETED");

            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    ContextCompat.startForegroundService(context, serviceIntent);
                } else {
                    context.startService(serviceIntent);
                }
                Log.i(TAG, "Boot recovery successfully initiated BridgeService start.");
                prefs.edit()
                        .putString(KEY_BOOT_RECOVERY_LAST_RESULT, "SUCCESS")
                        .putLong(KEY_BOOT_RECOVERY_LAST_TIMESTAMP, System.currentTimeMillis())
                        .apply();
            } catch (Exception e) {
                Log.e(TAG, "Failed to start BridgeService from BootReceiver: " + e.getMessage(), e);
                prefs.edit()
                        .putString(KEY_BOOT_RECOVERY_LAST_RESULT, "BLOCKED: " + e.getMessage())
                        .putLong(KEY_BOOT_RECOVERY_LAST_TIMESTAMP, System.currentTimeMillis())
                        .apply();
            }
        }
    }
}

