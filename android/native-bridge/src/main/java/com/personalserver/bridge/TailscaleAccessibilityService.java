package com.personalserver.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.Context;
import android.os.Build;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.List;

/**
 * Unprivileged Android Accessibility Service dedicated to zero-touch Tailscale VPN recovery.
 * Scoped strictly to com.tailscale.ipn via tailscale_accessibility_service_config.xml.
 *
 * When TailscaleRecoveryManager detects a dropped 100.x.y.z interface and launches Tailscale,
 * this service catches the window change event and dynamically clicks the connect toggle widget
 * without relying on brittle pixel coordinates, making it 100% immune to screen resolution,
 * DPI, and orientation differences across cluster devices (Vivo V2334, Vivo Y31, Realme 5 Pro).
 */
public class TailscaleAccessibilityService extends AccessibilityService {

    private static final String TAG = "TailscaleA11yService";
    public static final String TAILSCALE_PACKAGE = "com.tailscale.ipn";
    public static final String TOGGLE_RES_ID = "com.tailscale.ipn:id/connect_toggle";

    private static volatile TailscaleAccessibilityService activeInstance = null;
    private static volatile boolean reconnectActive = false;
    private static volatile long reconnectRequestedAt = 0L;
    private static final long RECONNECT_WINDOW_MS = 25000L; // 25 seconds window

    public static boolean isServiceRunning() {
        return activeInstance != null;
    }

    public static void requestAutoToggle() {
        reconnectActive = true;
        reconnectRequestedAt = System.currentTimeMillis();
        Log.i(TAG, "Tailscale auto-toggle arming: listening for com.tailscale.ipn window events");
    }

    public static void cancelAutoToggle() {
        reconnectActive = false;
    }

    public static boolean isAutoToggleArmed() {
        if (reconnectActive && (System.currentTimeMillis() - reconnectRequestedAt > RECONNECT_WINDOW_MS)) {
            reconnectActive = false; // Expiry guard
        }
        return reconnectActive;
    }

    /**
     * Checks whether the user has enabled this Accessibility Service in Android Settings.
     */
    public static boolean isAccessibilityPermissionGranted(Context context) {
        if (context == null) return false;
        try {
            int accessibilityEnabled = Settings.Secure.getInt(
                    context.getContentResolver(),
                    Settings.Secure.ACCESSIBILITY_ENABLED,
                    0
            );
            if (accessibilityEnabled != 1) {
                return false;
            }
            String serviceName = context.getPackageName() + "/" + TailscaleAccessibilityService.class.getName();
            String enabledServices = Settings.Secure.getString(
                    context.getContentResolver(),
                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            );
            if (enabledServices != null) {
                TextUtils.SimpleStringSplitter colonSplitter = new TextUtils.SimpleStringSplitter(':');
                colonSplitter.setString(enabledServices);
                while (colonSplitter.hasNext()) {
                    String component = colonSplitter.next();
                    if (component.equalsIgnoreCase(serviceName)) {
                        return true;
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Error checking accessibility permission: " + e.getMessage());
        }
        return false;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        activeInstance = this;
        Log.i(TAG, "TailscaleAccessibilityService connected and ready");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) return;
        CharSequence pkg = event.getPackageName();
        if (pkg == null || !TAILSCALE_PACKAGE.contentEquals(pkg)) {
            return;
        }

        int eventType = event.getEventType();
        if (eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            eventType != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED) {
            return;
        }

        if (!isAutoToggleArmed()) {
            return; // Not armed: do not intercept normal user interactions
        }

        tryPerformToggleClick();
    }

    private synchronized void tryPerformToggleClick() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return;

        try {
            // 1. Primary Strategy: Look for known connect toggle view ID
            List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByViewId(TOGGLE_RES_ID);

            // 2. Fallback Strategy A: Look for text "Connect" or "Tap to connect"
            if (nodes.isEmpty()) {
                nodes = root.findAccessibilityNodeInfosByText("Connect");
            }
            if (nodes.isEmpty()) {
                nodes = root.findAccessibilityNodeInfosByText("Tap to connect");
            }

            // 3. Fallback Strategy B: Search for Switch widget if disconnected
            if (nodes.isEmpty()) {
                searchForSwitchNode(root, nodes);
            }

            for (AccessibilityNodeInfo node : nodes) {
                if (node == null) continue;
                if (attemptClick(node)) {
                    Log.i(TAG, "Zero-touch accessibility action: successfully activated Tailscale connection toggle");
                    reconnectActive = false; // Disarm once clicked
                    break;
                }
            }
        } finally {
            try {
                root.recycle();
            } catch (Exception ignored) {}
        }
    }

    private boolean attemptClick(AccessibilityNodeInfo node) {
        if (node == null) return false;
        if (node.isClickable()) {
            return node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
        }
        AccessibilityNodeInfo parent = node.getParent();
        if (parent != null && parent.isClickable()) {
            boolean clicked = parent.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            try { parent.recycle(); } catch (Exception ignored) {}
            return clicked;
        }
        return false;
    }

    private void searchForSwitchNode(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> result) {
        if (node == null || result.size() > 0) return;
        CharSequence className = node.getClassName();
        if (className != null && (className.toString().contains("Switch") || className.toString().contains("ToggleButton"))) {
            if (!node.isChecked()) {
                result.add(node);
                return;
            }
        }
        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) {
                searchForSwitchNode(child, result);
                if (result.size() > 0) return;
                try { child.recycle(); } catch (Exception ignored) {}
            }
        }
    }

    @Override
    public void onInterrupt() {
        Log.w(TAG, "TailscaleAccessibilityService interrupted");
        reconnectActive = false;
    }

    @Override
    public void onDestroy() {
        Log.i(TAG, "TailscaleAccessibilityService destroyed");
        activeInstance = null;
        reconnectActive = false;
        super.onDestroy();
    }
}
