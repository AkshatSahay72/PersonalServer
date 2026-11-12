package com.personalserver.bridge;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.util.Log;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Manages Termux integration and approved command execution via com.termux.RUN_COMMAND.
 */
public class TermuxRecoveryManager {

    private static final String TAG = "TermuxRecoveryManager";
    public static final String TERMUX_PACKAGE = "com.termux";
    public static final String RUN_COMMAND_ACTION = "com.termux.RUN_COMMAND";
    public static final String RUN_COMMAND_SERVICE = "com.termux.app.RunCommandService";

    // Strict command allowlist
    public static final String OP_START_SERVICES = "START_SERVICES";
    public static final String OP_STOP_SERVICES = "STOP_SERVICES";
    public static final String OP_RUN_DOCTOR = "RUN_DOCTOR";
    public static final String OP_START_SSHD = "START_SSHD";
    public static final String OP_SERVICE_STATUS = "SERVICE_STATUS";

    public static final Set<String> ALLOWED_OPERATIONS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            OP_START_SERVICES,
            OP_STOP_SERVICES,
            OP_RUN_DOCTOR,
            OP_START_SSHD,
            OP_SERVICE_STATUS
    )));

    private final Context context;
    private final ConcurrentHashMap<String, JSONObject> commandHistory = new ConcurrentHashMap<>();

    public TermuxRecoveryManager(Context context) {
        this.context = context.getApplicationContext();
    }

    public boolean isTermuxInstalled() {
        try {
            PackageManager pm = context.getPackageManager();
            if (pm == null) return false;
            pm.getPackageInfo(TERMUX_PACKAGE, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        } catch (Exception e) {
            Log.w(TAG, "Error checking Termux package: " + e.getMessage());
            return false;
        }
    }

    public boolean isRunCommandPermissionGranted() {
        try {
            return context.checkCallingOrSelfPermission("com.termux.permission.RUN_COMMAND")
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return false;
        }
    }

    public JSONObject executeApprovedCommand(String operation, JSONObject extraParams) throws JSONException {
        if (!ALLOWED_OPERATIONS.contains(operation)) {
            throw new IllegalArgumentException("Operation not permitted in Termux allowlist: " + operation);
        }

        if (!isTermuxInstalled()) {
            throw new IllegalStateException("Termux is not installed on this device");
        }

        String opId = "txop-" + UUID.randomUUID().toString().substring(0, 8);
        long startedAt = System.currentTimeMillis();

        String executable;
        String[] arguments;
        String workDir = "/data/data/com.termux/files/home";

        switch (operation) {
            case OP_START_SERVICES:
                executable = "/data/data/com.termux/files/usr/bin/bash";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/start.sh"};
                break;
            case OP_STOP_SERVICES:
                executable = "/data/data/com.termux/files/usr/bin/bash";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/stop.sh"};
                break;
            case OP_RUN_DOCTOR:
                executable = "/data/data/com.termux/files/usr/bin/python3";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/doctor.py", "--json"};
                workDir = "/data/data/com.termux/files/home/PersonalServer";
                break;
            case OP_START_SSHD:
                executable = "/data/data/com.termux/files/usr/bin/sshd";
                arguments = new String[]{};
                break;
            case OP_SERVICE_STATUS:
                executable = "/data/data/com.termux/files/usr/bin/bash";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/status.sh"};
                break;
            default:
                throw new IllegalArgumentException("Unhandled allowlist operation: " + operation);
        }

        Intent intent = new Intent(RUN_COMMAND_ACTION);
        intent.setClassName(TERMUX_PACKAGE, RUN_COMMAND_SERVICE);
        intent.putExtra("com.termux.RUN_COMMAND_PATH", executable);
        intent.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arguments);
        intent.putExtra("com.termux.RUN_COMMAND_WORKDIR", workDir);
        intent.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        intent.putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", "0"); // Fail/Close

        boolean dispatched = false;
        String dispatchError = null;

        try {
            context.startService(intent);
            dispatched = true;
            Log.i(TAG, "Dispatched Termux RUN_COMMAND: " + operation + " (id=" + opId + ")");
        } catch (Exception e) {
            dispatchError = e.getMessage();
            Log.e(TAG, "Failed to start Termux RunCommandService: " + dispatchError, e);
        }

        JSONObject record = new JSONObject();
        record.put("operation_id", opId);
        record.put("operation", operation);
        record.put("executable", executable);
        record.put("work_dir", workDir);
        record.put("dispatched", dispatched);
        record.put("dispatch_error", dispatchError != null ? dispatchError : JSONObject.NULL);
        record.put("timestamp", startedAt);

        commandHistory.put(opId, record);
        return record;
    }

    public JSONObject getCommandHistory() {
        JSONObject out = new JSONObject();
        for (String key : commandHistory.keySet()) {
            try {
                out.put(key, commandHistory.get(key));
            } catch (Exception ignored) {}
        }
        return out;
    }
}

