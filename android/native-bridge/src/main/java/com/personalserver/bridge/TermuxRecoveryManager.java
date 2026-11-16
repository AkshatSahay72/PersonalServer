package com.personalserver.bridge;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Manages Termux integration and approved command execution via com.termux.RUN_COMMAND.
 * Captures asynchronous command exit codes, stdout, and stderr via PendingIntent broadcast callback.
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
    public static final String OP_PULL_PRODUCTION = "PULL_PRODUCTION";

    public static final Set<String> ALLOWED_OPERATIONS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            OP_START_SERVICES,
            OP_STOP_SERVICES,
            OP_RUN_DOCTOR,
            OP_START_SSHD,
            OP_SERVICE_STATUS,
            OP_PULL_PRODUCTION
    )));

    // Distinct recovery operation states
    public static final String STATE_DISPATCHED = "DISPATCHED";
    public static final String STATE_EXECUTING = "EXECUTING";
    public static final String STATE_SUCCEEDED = "SUCCEEDED";
    public static final String STATE_FAILED = "FAILED";
    public static final String STATE_TIMED_OUT = "TIMED_OUT";
    public static final String STATE_VERIFICATION_FAILED = "VERIFICATION_FAILED";
    public static final String STATE_SUPPRESSED = "SUPPRESSED";

    public interface CommandResultListener {
        void onCommandFinished(String opId, int exitCode, String stdout, String stderr);
    }

    private static final ConcurrentHashMap<String, JSONObject> GLOBAL_COMMAND_HISTORY = new ConcurrentHashMap<>();
    private static final List<CommandResultListener> LISTENERS = Collections.synchronizedList(new ArrayList<>());

    private final Context context;

    public TermuxRecoveryManager(Context context) {
        this.context = context.getApplicationContext();
    }

    public static void addListener(CommandResultListener listener) {
        if (listener != null && !LISTENERS.contains(listener)) {
            LISTENERS.add(listener);
        }
    }

    public static void removeListener(CommandResultListener listener) {
        if (listener != null) {
            LISTENERS.remove(listener);
        }
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

    /**
     * Verifies the ability to execute an approved Termux command through a bounded health probe.
     * Independent of Node API or SSH reachability.
     */
    public boolean isCommandIntegrationReady() {
        return isTermuxInstalled() && isRunCommandPermissionGranted();
    }

    public String probeCommandAvailability() {
        try {
            if (!isTermuxInstalled()) {
                return "DOWN";
            }
            if (!isRunCommandPermissionGranted()) {
                return "DOWN";
            }
            return isCommandIntegrationReady() ? "UP" : "DEGRADED";
        } catch (Exception e) {
            return "UNKNOWN";
        }
    }

    public void launchTermuxActivity() {
        try {
            PackageManager pm = context.getPackageManager();
            if (pm != null) {
                Intent launchIntent = pm.getLaunchIntentForPackage(TERMUX_PACKAGE);
                if (launchIntent != null) {
                    launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                    context.startActivity(launchIntent);
                    Log.i(TAG, "Launched Termux activity to awaken process");
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed launching Termux Activity: " + e.getMessage());
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
                executable = "/data/data/com.termux/files/usr/bin/bash";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/doctor.sh", "fix"};
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
            case OP_PULL_PRODUCTION:
                executable = "/data/data/com.termux/files/usr/bin/bash";
                arguments = new String[]{"/data/data/com.termux/files/home/PersonalServer/scripts/update.sh"};
                workDir = "/data/data/com.termux/files/home/PersonalServer";
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

        // Attach PendingIntent to capture command exitCode and output from Termux
        Intent resultIntent = new Intent(context, TermuxCommandResultReceiver.class);
        resultIntent.setAction(TermuxCommandResultReceiver.ACTION_TERMUX_RESULT);
        resultIntent.putExtra("operation_id", opId);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 31) {
            piFlags |= 33554432; // PendingIntent.FLAG_MUTABLE
        }
        try {
            PendingIntent pi = PendingIntent.getBroadcast(context, opId.hashCode(), resultIntent, piFlags);
            intent.putExtra("com.termux.RUN_COMMAND_PENDING_INTENT", pi);
        } catch (Exception e) {
            Log.w(TAG, "Failed creating RUN_COMMAND PendingIntent callback: " + e.getMessage());
        }

        boolean dispatched = false;
        String dispatchError = null;

        try {
            context.startService(intent);
            dispatched = true;
            Log.i(TAG, "Dispatched Termux RUN_COMMAND: " + operation + " (id=" + opId + ")");
        } catch (Exception e) {
            Log.w(TAG, "Initial startService failed (" + e.getMessage() + "), attempting Termux activity launch to awaken...");
            launchTermuxActivity();
            try {
                Thread.sleep(300);
                context.startService(intent);
                dispatched = true;
                Log.i(TAG, "Dispatched Termux RUN_COMMAND after awakening: " + operation + " (id=" + opId + ")");
            } catch (Exception e2) {
                dispatchError = e2.getMessage();
                Log.e(TAG, "Failed to start Termux RunCommandService: " + dispatchError, e2);
            }
        }

        String targetService = (extraParams != null) ? extraParams.optString("target_service", "all") : "all";

        JSONObject record = new JSONObject();
        record.put("operation_id", opId);
        record.put("operation", operation);
        record.put("target_service", targetService);
        record.put("executable", executable);
        record.put("work_dir", workDir);
        record.put("dispatched", dispatched);
        record.put("dispatch_status", dispatched ? STATE_DISPATCHED : STATE_FAILED);
        record.put("dispatch_error", dispatchError != null ? dispatchError : JSONObject.NULL);
        record.put("status", dispatched ? STATE_DISPATCHED : STATE_FAILED);
        record.put("execution_result", JSONObject.NULL);
        record.put("exit_code", JSONObject.NULL);
        record.put("timeout_ms", 15000);
        record.put("timestamp", startedAt);
        record.put("completion_timestamp", JSONObject.NULL);
        record.put("diagnostic_output", JSONObject.NULL);
        record.put("verified", false);
        record.put("success", false); // Never reported as success until verification passes!

        GLOBAL_COMMAND_HISTORY.put(opId, record);
        return record;
    }

    public static void handleResult(String opId, int resultCode, String stdout, String stderr, String errmsg, int errcode) {
        if (opId == null) return;
        JSONObject record = GLOBAL_COMMAND_HISTORY.get(opId);
        if (record != null) {
            try {
                record.put("completion_timestamp", System.currentTimeMillis());
                record.put("exit_code", resultCode);
                boolean succeeded = (resultCode == 0);
                record.put("status", succeeded ? STATE_SUCCEEDED : STATE_FAILED);
                record.put("verified", succeeded);
                record.put("success", succeeded);

                String out = (stdout != null && !stdout.isEmpty()) ? stdout : stderr;
                if (out != null) {
                    String bounded = out.length() > 1024 ? out.substring(0, 1024) : out;
                    record.put("diagnostic_output", bounded);
                    record.put("execution_result", bounded);
                }
                if (errmsg != null && !errmsg.isEmpty()) {
                    record.put("error_message", errmsg);
                }
            } catch (JSONException ignored) {}
        }

        synchronized (LISTENERS) {
            for (CommandResultListener l : LISTENERS) {
                try {
                    l.onCommandFinished(opId, resultCode, stdout, stderr);
                } catch (Exception ignored) {}
            }
        }
    }

    public void updateOperationRecord(String opId, String status, Integer exitCode, String result, String diagOutput) {
        if (opId == null) return;
        JSONObject record = GLOBAL_COMMAND_HISTORY.get(opId);
        if (record == null) return;
        try {
            record.put("status", status);
            record.put("completion_timestamp", System.currentTimeMillis());
            if (exitCode != null) {
                record.put("exit_code", exitCode.intValue());
            }
            if (result != null) {
                record.put("execution_result", result);
            }
            if (diagOutput != null) {
                String bounded = diagOutput.length() > 512 ? diagOutput.substring(0, 512) : diagOutput;
                record.put("diagnostic_output", bounded);
            }
            boolean succeeded = STATE_SUCCEEDED.equals(status);
            record.put("verified", succeeded);
            record.put("success", succeeded);
        } catch (JSONException ignored) {}
    }

    public JSONObject getOperation(String opId) {
        if (opId == null) return null;
        return GLOBAL_COMMAND_HISTORY.get(opId);
    }

    public JSONObject getCommandHistory() {
        JSONObject out = new JSONObject();
        for (String key : GLOBAL_COMMAND_HISTORY.keySet()) {
            try {
                out.put(key, GLOBAL_COMMAND_HISTORY.get(key));
            } catch (Exception ignored) {}
        }
        return out;
    }
}
