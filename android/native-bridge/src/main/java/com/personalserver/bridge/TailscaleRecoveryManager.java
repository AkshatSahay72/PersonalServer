package com.personalserver.bridge;

import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.util.Log;
import androidx.core.app.NotificationCompat;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.util.Enumeration;

/**
 * Manages Tailscale detection, VPN status check via ConnectivityManager and NetworkInterfaces,
 * dynamic Intent resolution for com.tailscale.ipn via PackageManager, and notification-backed reconnection flows.
 */
public class TailscaleRecoveryManager {

    private static final String TAG = "TailscaleRecoveryManager";
    public static final String TAILSCALE_PACKAGE = "com.tailscale.ipn";
    public static final String TAILSCALE_FALLBACK_ACTIVITY = "com.tailscale.ipn.MainActivity";

    public static final String ACTION_OWNER_REQUIRED = "OWNER_ACTION_REQUIRED";
    public static final String ACTION_LAUNCH_ATTEMPTED = "LAUNCH_ATTEMPTED";
    public static final String ACTION_LAUNCH_FAILED = "LAUNCH_FAILED";
    public static final int TAILSCALE_NOTIFICATION_ID = 2002;
    private static final int REQ_TAILSCALE_LAUNCH = 2003;

    private final Context context;

    public TailscaleRecoveryManager(Context context) {
        this.context = context.getApplicationContext();
    }

    public boolean isTailscaleInstalled() {
        try {
            PackageManager pm = context.getPackageManager();
            if (pm == null) return false;
            pm.getPackageInfo(TAILSCALE_PACKAGE, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        } catch (Exception e) {
            Log.w(TAG, "Error checking Tailscale package: " + e.getMessage());
            return false;
        }
    }

    /**
     * Checks if a VPN is currently active on the device.
     * Evaluates ConnectivityManager network capabilities and checks for active tun/tap interfaces.
     */
    public boolean isVpnActive() {
        boolean vpnTransport = false;
        try {
            ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                Network activeNetwork = cm.getActiveNetwork();
                if (activeNetwork != null) {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(activeNetwork);
                    if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        vpnTransport = true;
                    }
                }
                if (!vpnTransport) {
                    Network[] allNetworks = cm.getAllNetworks();
                    if (allNetworks != null) {
                        for (Network n : allNetworks) {
                            NetworkCapabilities caps = cm.getNetworkCapabilities(n);
                            if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                                vpnTransport = true;
                                break;
                            }
                        }
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed checking VPN network capabilities: " + e.getMessage());
        }

        boolean tunUp = false;
        boolean ifacesEnumerated = false;
        try {
            Enumeration<NetworkInterface> ifaces = NetworkInterface.getNetworkInterfaces();
            if (ifaces != null) {
                ifacesEnumerated = true;
                while (ifaces.hasMoreElements()) {
                    NetworkInterface nif = ifaces.nextElement();
                    String name = nif.getName().toLowerCase();
                    if ((name.startsWith("tun") || name.startsWith("tap") || name.contains("tailscale")) && nif.isUp()) {
                        tunUp = true;
                        break;
                    }
                }
            }
        } catch (Exception ignored) {}

        if (ifacesEnumerated) {
            return vpnTransport && tunUp;
        }
        return vpnTransport;
    }

    public boolean checkPeerConnectivity(String peerIp, int port, int timeoutMs) {
        if (peerIp == null || peerIp.isEmpty()) return false;
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress(peerIp, port), timeoutMs);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * Resolves the launch Intent for the installed Tailscale Android app dynamically
     * via PackageManager rather than assuming a hard-coded Activity name.
     */
    public Intent resolveLaunchIntent() {
        try {
            PackageManager pm = context.getPackageManager();
            if (pm != null) {
                Intent launchIntent = pm.getLaunchIntentForPackage(TAILSCALE_PACKAGE);
                if (launchIntent != null) {
                    launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                    return launchIntent;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed resolving launch intent for " + TAILSCALE_PACKAGE + ": " + e.getMessage());
        }
        Intent fallback = new Intent();
        fallback.setClassName(TAILSCALE_PACKAGE, TAILSCALE_FALLBACK_ACTIVITY);
        fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return fallback;
    }

    /**
     * Initiates the strongest supported recovery action:
     * 1. Resolves installed Tailscale launch intent dynamically via PackageManager.
     * 2. Attempts direct startActivity.
     * 3. Posts high-priority notification with a 1-tap PendingIntent to Tailscale.
     * App launch is NEVER reported as VPN recovery success.
     */
    public JSONObject triggerReconnection() throws JSONException {
        JSONObject result = new JSONObject();
        if (!isTailscaleInstalled()) {
            result.put("success", false);
            result.put("status", "TAILSCALE_NOT_INSTALLED");
            result.put("error", "TAILSCALE_NOT_INSTALLED");
            result.put("message", "Tailscale package " + TAILSCALE_PACKAGE + " is not installed");
            return result;
        }

        Intent launchIntent = resolveLaunchIntent();
        boolean launchDispatched = false;
        String launchError = null;

        try {
            context.startActivity(launchIntent);
            launchDispatched = true;
            Log.i(TAG, "Dispatched launch intent for " + TAILSCALE_PACKAGE);
        } catch (Exception e) {
            launchError = e.getMessage();
            Log.w(TAG, "Direct background launch of Tailscale activity was restricted: " + launchError);
        }

        // Post high-priority notification for owner action
        postOwnerActionNotification(launchIntent);

        result.put("action", launchDispatched ? ACTION_LAUNCH_ATTEMPTED : ACTION_LAUNCH_FAILED);
        result.put("target", TAILSCALE_PACKAGE);
        result.put("launch_dispatched", launchDispatched);
        if (launchError != null) {
            result.put("launch_error", launchError);
        }
        result.put("status", ACTION_OWNER_REQUIRED);
        result.put("requires_owner_action", true);
        result.put("message", "Tailscale app launch attempted and alert notification posted. Owner action required to re-enable VPN connection on Android.");
        result.put("success", false); // App launch is never recovery success!
        return result;
    }

    public void postOwnerActionNotification(Intent launchIntent) {
        try {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;

            PendingIntent pendingIntent = PendingIntent.getActivity(
                    context,
                    REQ_TAILSCALE_LAUNCH,
                    launchIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
            );

            NotificationCompat.Builder builder = new NotificationCompat.Builder(context, BridgeService.CHANNEL_ID)
                    .setSmallIcon(android.R.drawable.ic_dialog_alert)
                    .setContentTitle("Tailscale Disconnected")
                    .setContentText("Tap to open Tailscale and reconnect VPN for PersonalServer")
                    .setPriority(NotificationCompat.PRIORITY_HIGH)
                    .setContentIntent(pendingIntent)
                    .setAutoCancel(true);

            nm.notify(TAILSCALE_NOTIFICATION_ID, builder.build());
        } catch (Exception e) {
            Log.w(TAG, "Failed posting owner action notification: " + e.getMessage());
        }
    }

    public void clearOwnerActionNotification() {
        try {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.cancel(TAILSCALE_NOTIFICATION_ID);
            }
        } catch (Exception ignored) {}
    }
}
