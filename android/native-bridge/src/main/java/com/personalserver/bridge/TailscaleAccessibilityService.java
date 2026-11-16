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
        try {
            AccessibilityServiceInfo info = getServiceInfo();
            if (info == null) {
                info = new AccessibilityServiceInfo();
            }
            info.eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED | AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED;
            info.feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC;
            info.flags = AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            info.notificationTimeout = 100;
            info.packageNames = new String[]{TAILSCALE_PACKAGE};
            setServiceInfo(info);
        } catch (Exception e) {
            Log.w(TAG, "Configuring service info via code failed: " + e.getMessage());
        }
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
            List<AccessibilityNodeInfo> candidates = new java.util.ArrayList<>();
            findCandidatesRecursively(root, candidates);

            for (AccessibilityNodeInfo node : candidates) {
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

    private void findCandidatesRecursively(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> candidates) {
        if (node == null) return;

        boolean matches = false;

        // 1. Checkable switch/toggle that is currently unchecked (Compose or native Switch)
        if (node.isCheckable() && !node.isChecked()) {
            matches = true;
        }

        // 2. View resource ID containing connect or toggle
        CharSequence resId = node.getViewIdResourceName();
        if (!matches && resId != null) {
            String idStr = resId.toString().toLowerCase();
            if (idStr.contains("connect_toggle") || idStr.contains("toggle") || idStr.contains("connect")) {
                matches = true;
            }
        }

        // 3. Content Description matches "connect", "stopped", or "tailscale"
        CharSequence desc = node.getContentDescription();
        if (!matches && desc != null) {
            String descStr = desc.toString().toLowerCase();
            if (descStr.contains("connect") || descStr.contains("toggle") || descStr.contains("stopped") || descStr.contains("tap to connect")) {
                matches = true;
            }
        }

        // 4. Text matches "Connect", "Stopped", or "Tap to connect"
        CharSequence text = node.getText();
        if (!matches && text != null) {
            String textStr = text.toString().toLowerCase();
            if (textStr.equals("connect") || textStr.contains("tap to connect") || textStr.equals("stopped") || textStr.contains("disconnected")) {
                matches = true;
            }
        }

        // 5. Class name contains Switch or ToggleButton
        CharSequence cls = node.getClassName();
        if (!matches && cls != null) {
            String clsStr = cls.toString();
            if ((clsStr.contains("Switch") || clsStr.contains("ToggleButton")) && !node.isChecked()) {
                matches = true;
            }
        }

        if (matches) {
            candidates.add(node);
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) {
                findCandidatesRecursively(child, candidates);
            }
        }
    }

    private boolean attemptClick(AccessibilityNodeInfo node) {
        if (node == null) return false;
        if (node.isClickable() && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            return true;
        }
        AccessibilityNodeInfo curr = node;
        for (int depth = 0; depth < 4; depth++) {
            AccessibilityNodeInfo parent = curr.getParent();
            if (parent != null) {
                if (parent.isClickable() && parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true;
                }
                curr = parent;
            } else {
                break;
            }
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
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
