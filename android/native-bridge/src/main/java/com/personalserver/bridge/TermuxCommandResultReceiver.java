package com.personalserver.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * BroadcastReceiver for Termux com.termux.RUN_COMMAND result callbacks.
 * Captures exit code, stdout, stderr, and error messages from Termux execution.
 */
public class TermuxCommandResultReceiver extends BroadcastReceiver {

    private static final String TAG = "TermuxResultReceiver";
    public static final String ACTION_TERMUX_RESULT = "com.personalserver.bridge.ACTION_TERMUX_RESULT";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        if (!ACTION_TERMUX_RESULT.equals(action)) {
            Log.w(TAG, "Ignoring unexpected intent action: " + action);
            return;
        }

        String opId = intent.getStringExtra("operation_id");
        int resultCode = intent.getIntExtra("resultCode", -1);
        int errcode = intent.getIntExtra("errcode", 0);
        String stdout = intent.getStringExtra("stdout");
        String stderr = intent.getStringExtra("stderr");
        String errmsg = intent.getStringExtra("errmsg");

        Log.i(TAG, "Received Termux execution result for opId=" + opId
                + ", resultCode=" + resultCode
                + ", errcode=" + errcode);

        TermuxRecoveryManager.handleResult(opId, resultCode, stdout, stderr, errmsg, errcode);
    }
}
