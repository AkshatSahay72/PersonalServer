package com.personalserver.bridge;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.util.Log;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Manages Tailscale detection, VPN status check via ConnectivityManager, and reconnection flows.
 */
public class TailscaleRecoveryManager {

    private static final String TAG = "TailscaleRecoveryManager";
    public static final String TAILSCALE_PACKAGE = "com.tailscale.ipn";
    public static final String TAILSCALE_MAIN_ACTIVITY = "com.tailscale.ipn.MainActivity";

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

    public boolean isVpnActive() {
        try {
            ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return false;

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                Network activeNetwork = cm.getActiveNetwork();
                if (activeNetwork != null) {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(activeNetwork);
                    if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        return true;
                    }
                }
                // Check all networks if active network is not the VPN
                Network[] allNetworks = cm.getAllNetworks();
                for (Network n : allNetworks) {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(n);
                    if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        return true;
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed checking VPN network capabilities: " + e.getMessage());
        }
        return false;
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

    public JSONObject triggerReconnection() throws JSONException {
        JSONObject result = new JSONObject();
        if (!isTailscaleInstalled()) {
            result.put("success", false);
            result.put("error", "TAILSCALE_NOT_INSTALLED");
            result.put("message", "Tailscale package com.tailscale.ipn is not installed");
            return result;
        }

        try {
            Intent intent = new Intent();
            intent.setClassName(TAILSCALE_PACKAGE, TAILSCALE_MAIN_ACTIVITY);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            context.startActivity(intent);

            result.put("success", true);
            result.put("action", "LAUNCH_ACTIVITY");
            result.put("target", TAILSCALE_PACKAGE);
            result.put("message", "Launched Tailscale MainActivity to trigger reconnection flow");
            result.put("requires_owner_consent", !isVpnActive());
        } catch (Exception e) {
            result.put("success", false);
            result.put("error", "LAUNCH_FAILED");
            result.put("message", e.getMessage());
        }
        return result;
    }
}

