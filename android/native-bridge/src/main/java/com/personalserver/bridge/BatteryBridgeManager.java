package com.personalserver.bridge;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.Build;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Manages native Android battery subsystem telemetry via BatteryManager.
 */
public class BatteryBridgeManager {

    private final Context context;

    public BatteryBridgeManager(Context context) {
        this.context = context.getApplicationContext();
    }

    public JSONObject getBatteryStatus() throws JSONException {
        JSONObject result = new JSONObject();

        IntentFilter ifilter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
        Intent batteryStatus = context.registerReceiver(null, ifilter);

        if (batteryStatus == null) {
            result.put("error", "Unable to register battery receiver");
            return result;
        }

        // Level & Scale
        int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        boolean present = batteryStatus.getBooleanExtra(BatteryManager.EXTRA_PRESENT, true);
        int percentage = -1;
        if (level >= 0 && scale > 0) {
            percentage = (int) ((level / (float) scale) * 100);
        }
        result.put("present", present);
        result.put("level", level);
        result.put("scale", scale);
        result.put("percentage", percentage);

        // Status
        int status = batteryStatus.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        String statusStr;
        switch (status) {
            case BatteryManager.BATTERY_STATUS_CHARGING:
                statusStr = "CHARGING";
                break;
            case BatteryManager.BATTERY_STATUS_DISCHARGING:
                statusStr = "DISCHARGING";
                break;
            case BatteryManager.BATTERY_STATUS_FULL:
                statusStr = "FULL";
                break;
            case BatteryManager.BATTERY_STATUS_NOT_CHARGING:
                statusStr = "NOT_CHARGING";
                break;
            default:
                statusStr = "UNKNOWN";
                break;
        }
        result.put("status", statusStr);

        // Plugged
        int chargePlug = batteryStatus.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1);
        String pluggedStr;
        switch (chargePlug) {
            case BatteryManager.BATTERY_PLUGGED_AC:
                pluggedStr = "PLUGGED_AC";
                break;
            case BatteryManager.BATTERY_PLUGGED_USB:
                pluggedStr = "PLUGGED_USB";
                break;
            case BatteryManager.BATTERY_PLUGGED_WIRELESS:
                pluggedStr = "PLUGGED_WIRELESS";
                break;
            default:
                pluggedStr = "UNPLUGGED";
                break;
        }
        result.put("plugged", pluggedStr);

        // Health
        int health = batteryStatus.getIntExtra(BatteryManager.EXTRA_HEALTH, -1);
        String healthStr;
        switch (health) {
            case BatteryManager.BATTERY_HEALTH_GOOD:
                healthStr = "GOOD";
                break;
            case BatteryManager.BATTERY_HEALTH_OVERHEAT:
                healthStr = "OVERHEAT";
                break;
            case BatteryManager.BATTERY_HEALTH_DEAD:
                healthStr = "DEAD";
                break;
            case BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE:
                healthStr = "OVER_VOLTAGE";
                break;
            default:
                healthStr = "UNKNOWN";
                break;
        }
        result.put("health", healthStr);

        // Temperature (tenths of a degree Celsius)
        int temp = batteryStatus.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
        if (temp > 0) {
            result.put("temperature", temp / 10.0);
        } else {
            result.put("temperature", JSONObject.NULL);
        }

        // Voltage (millivolts)
        int voltage = batteryStatus.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1);
        if (voltage > 0) {
            result.put("voltage", voltage / 1000.0);
        } else {
            result.put("voltage", JSONObject.NULL);
        }

        // Technology
        String tech = batteryStatus.getStringExtra(BatteryManager.EXTRA_TECHNOLOGY);
        result.put("technology", tech != null ? tech : "Li-ion");

        // Current via BatteryManager API if available (API 21+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            BatteryManager bm = (BatteryManager) context.getSystemService(Context.BATTERY_SERVICE);
            if (bm != null) {
                int currentNow = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CURRENT_NOW);
                if (currentNow != Integer.MIN_VALUE) {
                    result.put("current", currentNow / 1000.0); // microamps to milliamps
                }
            }
        }

        return result;
    }
}

