"""
E06-A Native Android Independence & Failure-Injection Tests
===========================================================
Verifies that the native Android bridge (BridgeService, LocalBridgeServer,
SensorBridgeManager, BatteryBridgeManager, ThermalBridgeManager,
MicrophoneBridgeManager) operates independently of Termux, Python, Node Agent,
Node API, Tailscale, and PersonalServer Controller.
"""

import base64
import hashlib
import json
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.device_runtime.android.bridge import NativeAndroidBridge
from agent.device_runtime.android.models import BridgePermissionState
from agent.device_runtime.android.permission_bridge import AndroidPermissionBridge
from agent.device_runtime.android.protocol import BridgeProtocol
from agent.device_runtime.android.transport import (
    BaseBridgeTransport,
    CompositeAndroidTransport,
    LocalSocketTransport,
)
from tests.unit.test_native_android_bridge_lifecycle import ANDROID_STUBS


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_JAVA_DIR = ROOT / "android" / "native-bridge" / "src" / "main" / "java" / "com" / "personalserver" / "bridge"


E06_STUB_ADDITIONS = {
    "android/content/Intent.java": """
package android.content;
import android.os.Bundle;
import java.util.HashMap;
import java.util.Map;
public class Intent {
    public static final String ACTION_BATTERY_CHANGED = "android.intent.action.BATTERY_CHANGED";
    public static final String ACTION_BOOT_COMPLETED = "android.intent.action.BOOT_COMPLETED";
    public static final int FLAG_ACTIVITY_NEW_TASK = 268435456;
    public static final int FLAG_ACTIVITY_SINGLE_TOP = 536870912;
    private String action;
    private final Map<String, Object> extras = new HashMap<>();
    public Intent() {}
    public Intent(String action) { this.action = action; }
    public Intent(Context ctx, Class<?> cls) {}
    public Intent setAction(String action) { this.action = action; return this; }
    public String getAction() { return action; }
    public Intent setClassName(String pkg, String cls) { return this; }
    public Intent addFlags(int flags) { return this; }
    public String getStringExtra(String name) { Object v = extras.get(name); return v instanceof String ? (String) v : null; }
    public Intent putExtra(String name, String val) { extras.put(name, val); return this; }
    public Intent putExtra(String name, String[] val) { extras.put(name, val); return this; }
    public Intent putExtra(String name, int val) { extras.put(name, val); return this; }
    public Intent putExtra(String name, boolean val) { extras.put(name, val); return this; }
    public int getIntExtra(String name, int def) { Object v = extras.get(name); return v instanceof Integer ? (Integer) v : def; }
    public boolean getBooleanExtra(String name, boolean def) { Object v = extras.get(name); return v instanceof Boolean ? (Boolean) v : def; }
}
""",
    "android/content/BroadcastReceiver.java": """
package android.content;
public abstract class BroadcastReceiver {
    public abstract void onReceive(Context context, Intent intent);
}
""",
    "android/content/SharedPreferences.java": """
package android.content;
import java.util.*;
public interface SharedPreferences {
    interface Editor {
        Editor putBoolean(String key, boolean val);
        Editor putString(String key, String val);
        Editor putInt(String key, int val);
        Editor putLong(String key, long val);
        void apply();
        boolean commit();
    }
    boolean getBoolean(String key, boolean def);
    String getString(String key, String def);
    int getInt(String key, int def);
    long getLong(String key, long def);
    Editor edit();
}
""",
    "android/net/ConnectivityManager.java": """
package android.net;
public class ConnectivityManager {
    public Network getActiveNetwork() { return new Network(); }
    public Network[] getAllNetworks() { return new Network[]{new Network()}; }
    public NetworkCapabilities getNetworkCapabilities(Network n) { return new NetworkCapabilities(); }
}
""",
    "android/net/Network.java": """
package android.net;
public class Network {}
""",
    "android/net/NetworkCapabilities.java": """
package android.net;
public class NetworkCapabilities {
    public static final int TRANSPORT_VPN = 4;
    public boolean hasTransport(int t) { return t == TRANSPORT_VPN; }
}
""",
    "android/content/Context.java": """
package android.content;
import android.content.pm.PackageManager;
import java.io.File;
import java.util.HashMap;
import java.util.Map;
public class Context {
    public static final String POWER_SERVICE = "power";
    public static final String SENSOR_SERVICE = "sensor";
    public static final String BATTERY_SERVICE = "batterymanager";
    public static final String CONNECTIVITY_SERVICE = "connectivity";
    public static final int MODE_PRIVATE = 0;
    private final android.os.PowerManager pm = new android.os.PowerManager();
    private final android.hardware.SensorManager sm = new android.hardware.SensorManager();
    private final android.os.BatteryManager bm = new android.os.BatteryManager();
    private final android.net.ConnectivityManager cm = new android.net.ConnectivityManager();
    private final Map<String, SharedPreferences> prefsMap = new HashMap<>();
    private File cacheDir = new File(System.getProperty("java.io.tmpdir"), "e06_bridge_cache");
    public Context getApplicationContext() { return this; }
    public Object getSystemService(String name) {
        if (POWER_SERVICE.equals(name)) return pm;
        if (SENSOR_SERVICE.equals(name)) return sm;
        if (BATTERY_SERVICE.equals(name)) return bm;
        if (CONNECTIVITY_SERVICE.equals(name)) return cm;
        return null;
    }
    public <T> T getSystemService(Class<T> cls) { return null; }
    public PackageManager getPackageManager() {
        return new PackageManager() {
            @Override public boolean hasSystemFeature(String name) { return true; }
        };
    }
    public int checkSelfPermission(String perm) { return PackageManager.PERMISSION_GRANTED; }
    public int checkCallingOrSelfPermission(String perm) { return PackageManager.PERMISSION_GRANTED; }
    public File getCacheDir() { if (!cacheDir.exists()) cacheDir.mkdirs(); return cacheDir; }
    public void startActivity(Intent intent) {}
    public void startService(Intent intent) {}
    public SharedPreferences getSharedPreferences(String name, int mode) {
        return prefsMap.computeIfAbsent(name, k -> new SharedPreferences() {
            private final Map<String, Object> data = new HashMap<>();
            @Override public boolean getBoolean(String key, boolean def) { Object v = data.get(key); return v instanceof Boolean ? (Boolean) v : def; }
            @Override public String getString(String key, String def) { Object v = data.get(key); return v instanceof String ? (String) v : def; }
            @Override public int getInt(String key, int def) { Object v = data.get(key); return v instanceof Integer ? (Integer) v : def; }
            @Override public long getLong(String key, long def) { Object v = data.get(key); return v instanceof Long ? (Long) v : def; }
            @Override public Editor edit() {
                return new Editor() {
                    @Override public Editor putBoolean(String key, boolean val) { data.put(key, val); return this; }
                    @Override public Editor putString(String key, String val) { data.put(key, val); return this; }
                    @Override public Editor putInt(String key, int val) { data.put(key, val); return this; }
                    @Override public Editor putLong(String key, long val) { data.put(key, val); return this; }
                    @Override public void apply() {}
                    @Override public boolean commit() { return true; }
                };
            }
        });
    }
    public Intent registerReceiver(Object r, IntentFilter f) {
        Intent i = new Intent(Intent.ACTION_BATTERY_CHANGED);
        i.putExtra(android.os.BatteryManager.EXTRA_LEVEL, 82);
        i.putExtra(android.os.BatteryManager.EXTRA_SCALE, 100);
        i.putExtra(android.os.BatteryManager.EXTRA_PRESENT, true);
        i.putExtra(android.os.BatteryManager.EXTRA_STATUS, android.os.BatteryManager.BATTERY_STATUS_DISCHARGING);
        i.putExtra(android.os.BatteryManager.EXTRA_PLUGGED, 0);
        i.putExtra(android.os.BatteryManager.EXTRA_HEALTH, android.os.BatteryManager.BATTERY_HEALTH_GOOD);
        i.putExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, 315);
        i.putExtra(android.os.BatteryManager.EXTRA_VOLTAGE, 3950);
        return i;
    }
}
""",
    "android/content/ComponentName.java": """
package android.content;
public class ComponentName {}
""",
    "android/content/pm/PackageInfo.java": """
package android.content.pm;
public class PackageInfo {}
""",
    "android/content/pm/PackageManager.java": """
package android.content.pm;
public abstract class PackageManager {
    public static final int PERMISSION_GRANTED = 0;
    public static final int PERMISSION_DENIED = -1;
    public static final String FEATURE_MICROPHONE = "android.hardware.microphone";
    public static class NameNotFoundException extends Exception {}
    public abstract boolean hasSystemFeature(String name);
    public PackageInfo getPackageInfo(String pkg, int flags) throws NameNotFoundException {
        return new PackageInfo();
    }
}
""",
    "androidx/core/content/ContextCompat.java": """
package androidx.core.content;
import android.content.Context;
import android.content.Intent;
public class ContextCompat {
    public static int checkSelfPermission(Context c, String p) { return 0; }
    public static void startForegroundService(Context c, Intent i) {}
}
""",
    "android/os/Build.java": """
package android.os;
public class Build {
    public static class VERSION {
        public static final int SDK_INT = 34;
        public static final String RELEASE = "14";
    }
    public static class VERSION_CODES {
        public static final int LOLLIPOP = 21;
        public static final int M = 23;
        public static final int N = 24;
        public static final int O = 26;
        public static final int Q = 29;
        public static final int R = 30;
        public static final int S = 31;
        public static final int TIRAMISU = 33;
        public static final int UPSIDE_DOWN_CAKE = 34;
    }
}
""",
    "android/os/PowerManager.java": """
package android.os;
public class PowerManager {
    public static final int PARTIAL_WAKE_LOCK = 1;
    public static final int THERMAL_STATUS_NONE = 0;
    public static final int THERMAL_STATUS_LIGHT = 1;
    public static final int THERMAL_STATUS_MODERATE = 2;
    public static final int THERMAL_STATUS_SEVERE = 3;
    public static final int THERMAL_STATUS_CRITICAL = 4;
    public static final int THERMAL_STATUS_EMERGENCY = 5;
    public static final int THERMAL_STATUS_SHUTDOWN = 6;
    public static class WakeLock {
        private boolean held = false;
        public void setReferenceCounted(boolean value) {}
        public void acquire(long timeout) { held = true; }
        public void release() { held = false; }
        public boolean isHeld() { return held; }
    }
    public WakeLock newWakeLock(int levelAndFlags, String tag) { return new WakeLock(); }
    public int getCurrentThermalStatus() { return THERMAL_STATUS_LIGHT; }
    public float getThermalHeadroom(int forecastSeconds) { return 0.91f; }
}
""",
    "android/hardware/Sensor.java": """
package android.hardware;
public class Sensor {
    public static final int TYPE_ACCELEROMETER = 1;
    public static final int TYPE_MAGNETIC_FIELD = 2;
    public static final int TYPE_GYROSCOPE = 4;
    public static final int TYPE_LIGHT = 5;
    public static final int TYPE_PROXIMITY = 8;
    private final int type;
    private final String name;
    public Sensor(int type, String name) { this.type = type; this.name = name; }
    public int getType() { return type; }
    public String getName() { return name; }
    public String getStringType() { return "android.sensor." + name; }
    public String getVendor() { return "stub-vendor"; }
    public int getVersion() { return 1; }
    public float getResolution() { return 0.01f; }
    public float getMaximumRange() { return 78.4f; }
    public int getMinDelay() { return 10000; }
    public float getPower() { return 0.25f; }
}
""",
    "android/hardware/SensorEvent.java": """
package android.hardware;
public class SensorEvent {
    public Sensor sensor;
    public int accuracy = 3;
    public long timestamp = System.nanoTime();
    public float[] values = new float[]{0.12f, 9.75f, 0.34f};
}
""",
    "android/hardware/SensorManager.java": """
package android.hardware;
import android.os.Handler;
import java.util.*;
public class SensorManager {
    public static final int SENSOR_DELAY_NORMAL = 3;
    private final List<Sensor> sensors = Arrays.asList(
        new Sensor(Sensor.TYPE_ACCELEROMETER, "lsm6dso_acc"),
        new Sensor(Sensor.TYPE_GYROSCOPE, "lsm6dso_gyro"),
        new Sensor(Sensor.TYPE_LIGHT, "stk36c31_als")
    );
    private final List<SensorEventListener> activeListeners = new ArrayList<>();
    public Sensor getDefaultSensor(int type) {
        for (Sensor s : sensors) if (s.getType() == type) return s;
        return null;
    }
    public List<Sensor> getSensorList(int type) { return sensors; }
    public synchronized boolean registerListener(SensorEventListener listener, Sensor sensor, int samplingPeriodUs, Handler handler) {
        activeListeners.add(listener);
        Thread t = new Thread(() -> {
            try { Thread.sleep(15); } catch (InterruptedException ignored) {}
            SensorEvent ev = new SensorEvent();
            ev.sensor = sensor;
            listener.onSensorChanged(ev);
        }, "StubSensorEventThread");
        t.setDaemon(true);
        t.start();
        return true;
    }
    public synchronized void unregisterListener(SensorEventListener listener) {
        activeListeners.remove(listener);
    }
    public synchronized int getActiveListenerCount() { return activeListeners.size(); }
}
""",
    "android/media/AudioFormat.java": """
package android.media;
public class AudioFormat {
    public static final int CHANNEL_IN_MONO = 16;
    public static final int ENCODING_PCM_16BIT = 2;
}
""",
    "android/media/AudioRecord.java": """
package android.media;
public class AudioRecord {
    public static final int STATE_INITIALIZED = 1;
    public static final int RECORDSTATE_RECORDING = 3;
    public static final int ERROR_INVALID_OPERATION = -3;
    private int state = STATE_INITIALIZED;
    private int recState = 1;
    public AudioRecord(int src, int sr, int ch, int enc, int bufSize) {}
    public static int getMinBufferSize(int sr, int ch, int enc) { return 4096; }
    public int getState() { return state; }
    public int getRecordingState() { return recState; }
    public void startRecording() { recState = RECORDSTATE_RECORDING; }
    public void stop() { recState = 1; }
    public void release() { state = 0; recState = 1; }
    public int read(byte[] audioData, int offsetInBytes, int sizeInBytes) {
        if (recState != RECORDSTATE_RECORDING) return -1;
        int n = Math.min(sizeInBytes, 640);
        for (int i = 0; i < n; i++) audioData[offsetInBytes + i] = (byte) (i & 0x7F);
        try { Thread.sleep(15); } catch (InterruptedException ignored) {}
        return n;
    }
}
""",
    "android/media/MediaRecorder.java": """
package android.media;
public class MediaRecorder {
    public static class AudioSource {
        public static final int MIC = 1;
    }
}
""",
}


PURE_JAVA_DRIVER = """
package com.personalserver.bridge;

import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorManager;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class PureJavaIndependenceDriver {

    static class TestBridgeService extends BridgeService {
        private final int port;
        TestBridgeService(int port) { this.port = port; }
        @Override
        protected LocalBridgeServer createBridgeServer() {
            return new LocalBridgeServer(this, port);
        }
        @Override
        protected void startForegroundWithCompat(android.app.Notification n) {}
        @Override
        protected void stopForegroundCompat() {}
        @Override
        public void stopSelf() { onDestroy(); }
    }

    static class TestBridgeActivity extends BridgeActivity {
        int launchCount = 0;
        boolean failNextLaunch = false;
        @Override
        protected void launchBridgeForegroundService(Intent svc) {
            launchCount++;
            if (failNextLaunch) {
                throw new IllegalStateException("Background FGS start restricted");
            }
        }
        @Override
        protected void dispatchStopBridgeService(Intent svc) {}
    }

    private static JSONObject sendPureJavaIpc(int port, String capability, String operation, JSONObject params) throws Exception {
        try (Socket sock = new Socket("127.0.0.1", port)) {
            sock.setSoTimeout(4000);
            JSONObject req = new JSONObject();
            req.put("version", 1);
            req.put("protocol_version", 1);
            req.put("operation_id", "java_" + operation + "_" + System.nanoTime());
            req.put("capability", capability);
            req.put("operation", operation);
            req.put("parameters", params != null ? params : new JSONObject());
            OutputStream out = sock.getOutputStream();
            out.write((req.toString() + "\\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            BufferedReader reader = new BufferedReader(new InputStreamReader(sock.getInputStream(), StandardCharsets.UTF_8));
            String line = reader.readLine();
            if (line == null || line.isEmpty()) {
                throw new AssertionError("Empty response for " + operation);
            }
            return new JSONObject(line);
        }
    }

    private static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError("FAILED: " + msg);
    }

    public static void main(String[] args) throws Exception {
        try {
            runDriver(args);
            System.out.println("PURE_JAVA_INDEPENDENCE_OK");
            System.out.flush();
            System.exit(0);
        } catch (Throwable t) {
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }

    private static void runDriver(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);

        // 1 & 2: Start BridgeService without Termux, Python, Tailscale, or Controller
        TestBridgeService svc = new TestBridgeService(port);
        svc.onCreate();
        svc.onStartCommand(new Intent(BridgeService.ACTION_START), 0, 1);
        check(BridgeService.isRunning(), "BridgeService should be running without Termux/Tailscale");
        check(BridgeService.isProcessActive(), "BridgeService process should be active");

        // 3: Pure Java IPC PING without Python
        JSONObject pingResp = sendPureJavaIpc(port, "system", "PING", new JSONObject());
        check(pingResp.optBoolean("success", false), "Pure Java PING must succeed");
        check("pong".equals(pingResp.optJSONObject("data").optString("status")), "PING must return pong");

        // 15: Pure Java GET_DIAGNOSTICS without Python or Controller
        JSONObject diagResp = sendPureJavaIpc(port, "system", "GET_DIAGNOSTICS", new JSONObject());
        check(diagResp.optBoolean("success", false), "GET_DIAGNOSTICS must succeed");
        JSONObject diagData = diagResp.optJSONObject("data");
        JSONObject states = diagData.optJSONObject("states");
        check("UP".equals(states.optString("ANDROID_APP_RUNNING")), "ANDROID_APP_RUNNING should be UP");
        check("UP".equals(states.optString("NATIVE_BRIDGE_READY")), "NATIVE_BRIDGE_READY should be UP");
        String termuxState = states.optString("TERMUX_AVAILABLE");
        check("UNKNOWN".equals(termuxState) || "DOWN".equals(termuxState) || "UP".equals(termuxState), "TERMUX_AVAILABLE valid state");
        String controllerState = states.optString("CONTROLLER_REACHABLE");
        check("UNKNOWN".equals(controllerState) || "DOWN".equals(controllerState) || "UP".equals(controllerState), "CONTROLLER_REACHABLE valid state");
        check(diagData.optBoolean("loopback_only", false), "Must report loopback_only=true");
        check(!diagData.optBoolean("requires_controller", true), "Must not require controller");

        // 4: Pure Java SENSOR_DISCOVER without Python
        JSONObject discResp = sendPureJavaIpc(port, "sensors", "SENSOR_DISCOVER", new JSONObject());
        check(discResp.optBoolean("success", false), "SENSOR_DISCOVER must succeed");
        JSONObject sensors = discResp.optJSONObject("data").optJSONObject("sensors");
        check(sensors != null && sensors.optJSONObject("accelerometer").optBoolean("available", false),
                "Accelerometer must be discovered");

        // 5: Pure Java SENSOR_START -> SENSOR_READ -> SENSOR_STOP without Python
        JSONObject startSensParams = new JSONObject();
        startSensParams.put("sensor_type", "accelerometer");
        startSensParams.put("sampling_interval_ms", 50);
        JSONObject sStart = sendPureJavaIpc(port, "sensor.accelerometer", "SENSOR_START", startSensParams);
        check(sStart.optBoolean("success", false), "SENSOR_START must succeed");
        String sensSid = sStart.optJSONObject("data").optJSONObject("session").optString("session_id");
        check(sensSid != null && !sensSid.isEmpty(), "Sensor session_id must be returned");

        Thread.sleep(50);
        JSONObject readSensParams = new JSONObject();
        readSensParams.put("session_id", sensSid);
        readSensParams.put("max_samples", 5);
        JSONObject sRead = sendPureJavaIpc(port, "sensor.accelerometer", "SENSOR_READ", readSensParams);
        check(sRead.optBoolean("success", false), "SENSOR_READ must succeed");
        JSONArray samples = sRead.optJSONObject("data").optJSONArray("readings");
        check(samples != null && samples.length() >= 1, "SENSOR_READ must return real readings");

        JSONObject stopSensParams = new JSONObject();
        stopSensParams.put("session_id", sensSid);
        JSONObject sStop = sendPureJavaIpc(port, "sensor.accelerometer", "SENSOR_STOP", stopSensParams);
        check(sStop.optBoolean("success", false), "SENSOR_STOP must succeed");

        // 6: Pure Java battery.get_status without Python
        JSONObject batResp = sendPureJavaIpc(port, "battery", "get_status", new JSONObject());
        check(batResp.optBoolean("success", false), "battery.get_status must succeed");
        check(batResp.optJSONObject("data").optInt("percentage", -1) == 82, "Battery percentage must be 82");

        // 7: Pure Java thermals.get_status without Python
        JSONObject thermResp = sendPureJavaIpc(port, "thermals", "get_status", new JSONObject());
        check(thermResp.optBoolean("success", false), "thermals.get_status must succeed");
        check("LIGHT".equals(thermResp.optJSONObject("data").optString("thermal_status")), "Thermal status must be LIGHT");

        // 8: Pure Java unauthorized rejection + authorized microphone recording + chunked read + cleanup
        JSONObject unauthParams = new JSONObject();
        unauthParams.put("duration_sec", 2);
        JSONObject unauthResp = sendPureJavaIpc(port, "microphone", "START_RECORDING", unauthParams);
        check(!unauthResp.optBoolean("success", true), "Unauthorized START_RECORDING must be rejected");

        JSONObject authParams = new JSONObject();
        authParams.put("authorized", true);
        authParams.put("duration_sec", 2);
        authParams.put("sample_rate_hz", 16000);
        JSONObject mStart = sendPureJavaIpc(port, "microphone", "START_RECORDING", authParams);
        check(mStart.optBoolean("success", false), "Authorized START_RECORDING must succeed");
        String micSid = mStart.optJSONObject("data").optJSONObject("session").optString("session_id");
        check(BridgeService.isRecordingActive(), "BridgeService must reflect active recording");

        Thread.sleep(150);
        JSONObject mStop = sendPureJavaIpc(port, "microphone", "STOP_RECORDING", new JSONObject());
        check(mStop.optBoolean("success", false), "STOP_RECORDING must succeed");
        JSONObject recMeta = mStop.optJSONObject("data").optJSONObject("recording");
        check(recMeta != null && recMeta.optInt("file_size_bytes", 0) > 44, "Recorded WAV must contain audio frames");

        JSONObject readMicParams = new JSONObject();
        readMicParams.put("session_id", micSid);
        readMicParams.put("offset", 0);
        readMicParams.put("length", 24576);
        JSONObject mRead = sendPureJavaIpc(port, "microphone", "RECORDING_READ", readMicParams);
        check(mRead.optBoolean("success", false), "RECORDING_READ must succeed");
        String b64 = mRead.optJSONObject("data").optString("chunk_base64");
        check(b64 != null && b64.length() > 16, "RECORDING_READ must return base64 WAV chunk");

        JSONObject cleanParams = new JSONObject();
        cleanParams.put("session_id", micSid);
        JSONObject mClean = sendPureJavaIpc(port, "microphone", "RECORDING_CLEANUP", cleanParams);
        check(mClean.optBoolean("success", false), "RECORDING_CLEANUP must succeed");

        // 13 & 14: Repeated start/stop and active session cleanup
        JSONObject s2 = sendPureJavaIpc(port, "sensor.accelerometer", "SENSOR_START", startSensParams);
        check(s2.optBoolean("success", false), "Second SENSOR_START must succeed");
        JSONObject m2 = sendPureJavaIpc(port, "microphone", "START_RECORDING", authParams);
        check(m2.optBoolean("success", false), "Second START_RECORDING must succeed");

        svc.onStartCommand(new Intent(BridgeService.ACTION_STOP), 0, 2);
        check(!BridgeService.isRunning(), "BridgeService must stop cleanly");
        check(!BridgeService.isRecordingActive(), "Active recording must be cleaned up on service stop");
        SensorManager sm = (SensorManager) svc.getSystemService(android.content.Context.SENSOR_SERVICE);
        check(sm.getActiveListenerCount() == 0, "All active sensor listeners must be unregistered on stop");

        // Restart after stop works cleanly
        svc.onStartCommand(new Intent(BridgeService.ACTION_START), 0, 3);
        check(BridgeService.isRunning(), "BridgeService must restart cleanly after stop");
        JSONObject pingAfterRestart = sendPureJavaIpc(port, "system", "PING", new JSONObject());
        check(pingAfterRestart.optBoolean("success", false), "PING after restart must succeed");
        svc.onStartCommand(new Intent(BridgeService.ACTION_STOP), 0, 4);

        // Cooldown guard in BridgeActivity prevents tight FGS retry loops
        TestBridgeActivity act = new TestBridgeActivity();
        act.failNextLaunch = true;
        act.startNativeBridgeService();
        int afterFirstFail = act.launchCount;
        // Immediate non-deferred retry should be blocked by cooldown
        act.onResume();
        check(act.launchCount <= afterFirstFail + 1, "Cooldown must bound repeated FGS launches");

        System.out.println("PURE_JAVA_INDEPENDENCE_OK");
        System.out.flush();
        System.exit(0);
    }
}
"""


def _compile_e06_jvm(build_dir: Path) -> Path:
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("JDK (javac/java) not available in PATH")

    src_dir = build_dir / "src"
    cls_dir = build_dir / "classes"
    src_dir.mkdir(parents=True, exist_ok=True)
    cls_dir.mkdir(parents=True, exist_ok=True)

    stubs = dict(ANDROID_STUBS)
    stubs.update(E06_STUB_ADDITIONS)
    for rel, content in stubs.items():
        target = src_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.lstrip(), encoding="utf-8")

    pkg_dir = src_dir / "com" / "personalserver" / "bridge"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for jf in BRIDGE_JAVA_DIR.glob("*.java"):
        shutil.copy2(jf, pkg_dir / jf.name)

    driver_path = pkg_dir / "PureJavaIndependenceDriver.java"
    driver_path.write_text(PURE_JAVA_DRIVER.lstrip(), encoding="utf-8")

    java_files = [str(p) for p in src_dir.rglob("*.java")]
    res = subprocess.run(
        ["javac", "-d", str(cls_dir), *java_files],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"javac failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    return cls_dir


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pure_java_native_independence_without_python_termux_tailscale_or_controller(tmp_path: Path):
    """
    Covers Phase A4 tests 1-8, 13, 14, and native 15:
    Executes BridgeService, LocalBridgeServer, SensorBridgeManager, BatteryBridgeManager,
    ThermalBridgeManager, MicrophoneBridgeManager, and BridgeActivity purely on the JVM
    using a pure-Java TCP client on 127.0.0.1 with zero Python/Termux/Tailscale/Controller.
    """
    cls_dir = _compile_e06_jvm(tmp_path / "e06_jvm")
    port = _free_port()
    res = subprocess.run(
        ["java", "-cp", str(cls_dir), "com.personalserver.bridge.PureJavaIndependenceDriver", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Java driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "PURE_JAVA_INDEPENDENCE_OK" in res.stdout


def test_failure_isolation_termux_tailscale_controller_and_node_api_unavailable():
    """
    Covers Phase A4 tests 9-12 & 15:
    Simulates Termux crash/unavailability, Node API failure, Tailscale down,
    and Controller unreachable while LocalSocketTransport and NativeAndroidBridge
    remain operational and report independent health states.
    """
    # Local loopback mock server simulating live Android LocalBridgeServer
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(8)
    stop_ev = threading.Event()

    def serve():
        while not stop_ev.is_set():
            try:
                srv.settimeout(0.2)
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                raw = conn.recv(4096)
                if not raw:
                    continue
                req = json.loads(raw.decode("utf-8").strip())
                op = req.get("operation", "")
                if op == "GET_DIAGNOSTICS":
                    data = {
                        "states": {
                            "ANDROID_APP_RUNNING": "UP",
                            "NATIVE_BRIDGE_READY": "UP",
                            "TERMUX_AVAILABLE": "UNKNOWN",
                            "NODE_AGENT_AVAILABLE": "UNKNOWN",
                            "NODE_API_AVAILABLE": "UNKNOWN",
                            "TAILSCALE_CONNECTED": "UNKNOWN",
                            "CONTROLLER_REACHABLE": "UNKNOWN",
                        },
                        "loopback_only": True,
                        "requires_controller": False,
                    }
                elif op == "PING":
                    data = {"status": "pong"}
                else:
                    data = {"percentage": 88, "status": "DISCHARGING"}
                resp = {
                    "version": 1,
                    "operation_id": req.get("operation_id", "op"),
                    "capability": req.get("capability", "system"),
                    "operation": op,
                    "success": True,
                    "data": data,
                    "error_code": None,
                    "error_message": None,
                }
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            finally:
                conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        sock_tr = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=1.5)
        broken_termux = MagicMock(spec=BaseBridgeTransport)
        broken_termux.is_available.return_value = False
        broken_termux.send_request.side_effect = RuntimeError("Termux crashed")

        composite = CompositeAndroidTransport(socket_transport=sock_tr, companion_transport=broken_termux)
        apb = AndroidPermissionBridge()
        # Even without termux.api permission granted, PING and GET_DIAGNOSTICS succeed
        bridge = NativeAndroidBridge(transport=composite, permission_bridge=apb)

        ping_resp = bridge.execute("system", "PING")
        assert ping_resp.success is True
        assert ping_resp.data["status"] == "pong"
        assert ping_resp.transport_used == "local_socket"
        assert ping_resp.fallback_used is False

        # Partial failures: Termux DOWN, Node API DOWN, Tailscale DOWN, Controller DOWN, Node Agent UNKNOWN
        diag = bridge.get_diagnostics(
            termux_probe=lambda: False,
            node_agent_probe=None,
            node_api_probe=lambda: (_ for _ in ()).throw(ConnectionRefusedError("Node API down")),
            tailscale_probe=lambda: False,
            controller_probe=lambda: False,
        )
        states = diag["states"]
        assert states["ANDROID_APP_RUNNING"] == "UP"
        assert states["NATIVE_BRIDGE_READY"] == "UP"
        assert states["TERMUX_AVAILABLE"] == "DOWN"
        assert states["NODE_AGENT_AVAILABLE"] == "UNKNOWN"
        assert states["NODE_API_AVAILABLE"] == "DOWN"
        assert states["TAILSCALE_CONNECTED"] == "DOWN"
        assert states["CONTROLLER_REACHABLE"] == "DOWN"
        assert diag["requires_controller"] is False
    finally:
        stop_ev.set()
        srv.close()
        t.join(timeout=1.0)


def test_no_silent_termux_fallback_for_native_only_ops_and_explicit_fallback_reporting():
    """
    Verifies:
    1. Native-only operations (START_RECORDING, PING, GET_DIAGNOSTICS) never silently fall back to Termux.
    2. When Termux companion fallback IS used for a fallback-eligible op (e.g. battery), it is explicitly reported.
    """
    sock = MagicMock(spec=BaseBridgeTransport)
    sock.is_available.return_value = False

    comp = MagicMock(spec=BaseBridgeTransport)
    comp.is_available.return_value = True
    comp.send_request.side_effect = lambda req, timeout_sec=4.0: BridgeProtocol.create_response(
        req, success=True, data={"percentage": 71}
    )

    composite = CompositeAndroidTransport(socket_transport=sock, companion_transport=comp)

    # Native-only ops must fail with TRANSPORT_UNAVAILABLE rather than touching Termux
    for cap, op in [("microphone", "START_RECORDING"), ("system", "PING"), ("system", "GET_DIAGNOSTICS")]:
        req = BridgeProtocol.create_request(cap, op, {"authorized": True} if op == "START_RECORDING" else {})
        resp = composite.send_request(req)
        assert resp.success is False
        assert resp.error_code == "TRANSPORT_UNAVAILABLE"
    comp.send_request.assert_not_called()

    # Fallback-eligible op explicitly marks fallback_used=True & transport_used='termux_companion'
    bat_req = BridgeProtocol.create_request("battery", "get_status")
    bat_resp = composite.send_request(bat_req)
    assert bat_resp.success is True
    assert bat_resp.transport_used == "termux_companion"
    assert bat_resp.fallback_used is True
    assert bat_resp.fallback_reason == "native_socket_unavailable"
    assert composite.last_fallback_used is True
    assert composite.fallback_count == 1
