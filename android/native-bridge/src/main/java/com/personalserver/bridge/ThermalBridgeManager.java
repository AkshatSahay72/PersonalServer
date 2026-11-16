package com.personalserver.bridge;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.Build;
import android.os.PowerManager;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Manages native Android thermal and throttling telemetry via PowerManager.
 */
public class ThermalBridgeManager {

    private final Context context;
    private final PowerManager powerManager;

    public ThermalBridgeManager(Context context) {
        this.context = context.getApplicationContext();
        this.powerManager = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
    }

    public JSONObject getThermalStatus() throws JSONException {
        JSONObject result = new JSONObject();

        int severityCode = 0;
        String statusStr = "NONE";

        // Query Android 10+ (API 29+) PowerManager thermal status
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && powerManager != null) {
            severityCode = powerManager.getCurrentThermalStatus();
            switch (severityCode) {
                case PowerManager.THERMAL_STATUS_NONE:
                    statusStr = "NONE";
                    break;
                case PowerManager.THERMAL_STATUS_LIGHT:
                    statusStr = "LIGHT";
                    break;
                case PowerManager.THERMAL_STATUS_MODERATE:
                    statusStr = "MODERATE";
                    break;
                case PowerManager.THERMAL_STATUS_SEVERE:
                    statusStr = "SEVERE";
                    break;
                case PowerManager.THERMAL_STATUS_CRITICAL:
                    statusStr = "CRITICAL";
                    break;
                case PowerManager.THERMAL_STATUS_EMERGENCY:
                    statusStr = "EMERGENCY";
                    break;
                case PowerManager.THERMAL_STATUS_SHUTDOWN:
                    statusStr = "SHUTDOWN";
                    break;
                default:
                    statusStr = "NONE";
                    break;
            }
        }

        result.put("severity_code", severityCode);
        result.put("thermal_status", statusStr);
        result.put("throttling_active", severityCode >= 2); // MODERATE (2) or higher triggers throttling

        // Query thermal headroom on Android 11+ (API 30+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && powerManager != null) {
            try {
                float headroom = powerManager.getThermalHeadroom(30);
                if (!Float.isNaN(headroom)) {
                    result.put("headroom_forecast", headroom);
                } else {
                    result.put("headroom_forecast", JSONObject.NULL);
                }
            } catch (Exception ignored) {
                result.put("headroom_forecast", JSONObject.NULL);
            }
        } else {
            result.put("headroom_forecast", JSONObject.NULL);
        }

        // Query battery temperature as hardware sensor reference
        IntentFilter ifilter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
        Intent batteryStatus = context.registerReceiver(null, ifilter);
        if (batteryStatus != null) {
            int temp = batteryStatus.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
            if (temp > 0) {
                result.put("battery_temperature_c", temp / 10.0);
            } else {
                result.put("battery_temperature_c", JSONObject.NULL);
            }
        } else {
            result.put("battery_temperature_c", JSONObject.NULL);
        }

        result.put("cpu_temperature_c", JSONObject.NULL);

        return result;
    }
}

