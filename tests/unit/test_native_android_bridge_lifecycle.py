"""
Lifecycle Reliability Tests for Native Android Bridge (D4-E01-B-R1)
===================================================================
Compiles and executes the actual Java bridge classes
(LocalBridgeServer, BridgeService, BridgeActivity) on the JVM with minimal
in-test Android framework stubs and verifies real loopback IPC via
Python's LocalSocketTransport.

Covers:
1. Repeated start requests (idempotency)
2. Start after stop
3. Stop during startup
4. Socket bind failure propagation to BridgeService
5. Executor shutdown and recreation
6. Duplicate server / concurrent startup prevention
7. Readiness only after successful bind
8. Startup timeout handling and cleanup
9. Clean shutdown and restart (plus LocalSocketTransport ping/SENSOR_STATUS)
10. BridgeActivity explicit FGS exception handling & bounded single-resume retry
"""

import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent.device_runtime.android.protocol import BridgeProtocol
from agent.device_runtime.android.transport import LocalSocketTransport


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_JAVA_DIR = ROOT / "android" / "native-bridge" / "src" / "main" / "java" / "com" / "personalserver" / "bridge"

ANDROID_STUBS = {
    "org/json/JSONException.java": """
package org.json;
public class JSONException extends Exception {
    public JSONException(String msg) { super(msg); }
}
""",
    "org/json/JSONObject.java": """
package org.json;
import java.util.*;
public class JSONObject {
    public static final Object NULL = new Object() {
        @Override public String toString() { return "null"; }
        @Override public boolean equals(Object o) { return o == this || o == null; }
    };
    private final Map<String, Object> map = new LinkedHashMap<>();

    public JSONObject() {}
    public JSONObject(String json) throws JSONException {
        if (json == null) throw new JSONException("null");
        Parser p = new Parser(json);
        Object v = p.parseValue();
        p.skipWs();
        if (!(v instanceof JSONObject) || p.pos != p.src.length()) {
            throw new JSONException("Invalid JSON object");
        }
        this.map.putAll(((JSONObject) v).map);
    }

    public boolean has(String k) { return map.containsKey(k); }
    public Iterator<String> keys() { return map.keySet().iterator(); }
    public boolean getBoolean(String k) throws JSONException {
        Object v = map.get(k);
        if (v instanceof Boolean) return (Boolean) v;
        throw new JSONException("Key " + k + " is not a boolean");
    }
    public String getString(String k) throws JSONException {
        Object v = map.get(k);
        if (v != null && v != NULL) return String.valueOf(v);
        throw new JSONException("Key " + k + " is missing or null");
    }
    public JSONObject put(String k, Object v) throws JSONException { map.put(k, v == null ? NULL : v); return this; }
    public JSONObject put(String k, int v) throws JSONException { map.put(k, v); return this; }
    public JSONObject put(String k, long v) throws JSONException { map.put(k, v); return this; }
    public JSONObject put(String k, double v) throws JSONException { map.put(k, v); return this; }
    public JSONObject put(String k, boolean v) throws JSONException { map.put(k, v); return this; }
    public Object opt(String k) { Object v = map.get(k); return v == NULL ? null : v; }
    public String optString(String k) { return optString(k, ""); }
    public String optString(String k, String def) {
        Object v = map.get(k);
        return (v == null || v == NULL) ? def : String.valueOf(v);
    }
    public int optInt(String k, int def) {
        Object v = map.get(k);
        return v instanceof Number ? ((Number) v).intValue() : def;
    }
    public long optLong(String k, long def) {
        Object v = map.get(k);
        return v instanceof Number ? ((Number) v).longValue() : def;
    }
    public double optDouble(String k, double def) {
        Object v = map.get(k);
        return v instanceof Number ? ((Number) v).doubleValue() : def;
    }
    public boolean optBoolean(String k, boolean def) {
        Object v = map.get(k);
        return v instanceof Boolean ? (Boolean) v : def;
    }
    public JSONObject optJSONObject(String k) {
        Object v = map.get(k);
        return v instanceof JSONObject ? (JSONObject) v : null;
    }
    public JSONArray optJSONArray(String k) {
        Object v = map.get(k);
        return v instanceof JSONArray ? (JSONArray) v : null;
    }
    public static String quote(String s) {
        StringBuilder sb = new StringBuilder("\\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\\\' || c == '"') sb.append('\\\\').append(c);
            else if (c == '\\n') sb.append("\\\\n");
            else if (c == '\\r') sb.append("\\\\r");
            else if (c == '\\t') sb.append("\\\\t");
            else sb.append(c);
        }
        return sb.append('"').toString();
    }
    static String valToJson(Object v) {
        if (v == null || v == NULL) return "null";
        if (v instanceof String) return quote((String) v);
        if (v instanceof Number || v instanceof Boolean || v instanceof JSONObject || v instanceof JSONArray) return v.toString();
        return quote(String.valueOf(v));
    }
    @Override public String toString() {
        StringBuilder sb = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) sb.append(',');
            first = false;
            sb.append(quote(e.getKey())).append(':').append(valToJson(e.getValue()));
        }
        return sb.append('}').toString();
    }
    static final class Parser {
        final String src;
        int pos = 0;
        Parser(String s) { this.src = s; }
        void skipWs() { while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) pos++; }
        Object parseValue() throws JSONException {
            skipWs();
            if (pos >= src.length()) throw new JSONException("EOF");
            char c = src.charAt(pos);
            if (c == '{') return parseObj();
            if (c == '[') return parseArr();
            if (c == '"') return parseStr();
            if (src.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (src.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            if (src.startsWith("null", pos)) { pos += 4; return NULL; }
            int start = pos;
            while (pos < src.length() && "-0123456789.eE+".indexOf(src.charAt(pos)) >= 0) pos++;
            if (pos == start) throw new JSONException("Unexpected char: " + c);
            String num = src.substring(start, pos);
            try { return num.contains(".") || num.contains("e") || num.contains("E") ? Double.parseDouble(num) : Long.parseLong(num); }
            catch (NumberFormatException nfe) { throw new JSONException("Bad num: " + num); }
        }
        String parseStr() throws JSONException {
            pos++;
            StringBuilder sb = new StringBuilder();
            while (pos < src.length()) {
                char c = src.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\\\') {
                    if (pos >= src.length()) throw new JSONException("Bad escape");
                    char e = src.charAt(pos++);
                    if (e == 'n') sb.append('\\n');
                    else if (e == 'r') sb.append('\\r');
                    else if (e == 't') sb.append('\\t');
                    else sb.append(e);
                } else sb.append(c);
            }
            throw new JSONException("Unterminated string");
        }
        JSONObject parseObj() throws JSONException {
            JSONObject obj = new JSONObject();
            pos++; skipWs();
            if (pos < src.length() && src.charAt(pos) == '}') { pos++; return obj; }
            while (true) {
                skipWs();
                if (pos >= src.length() || src.charAt(pos) != '"') throw new JSONException("Expected key");
                String k = parseStr();
                skipWs();
                if (pos >= src.length() || src.charAt(pos++) != ':') throw new JSONException("Expected :");
                obj.put(k, parseValue());
                skipWs();
                if (pos < src.length() && src.charAt(pos) == ',') { pos++; continue; }
                if (pos < src.length() && src.charAt(pos) == '}') { pos++; return obj; }
                throw new JSONException("Expected , or }");
            }
        }
        JSONArray parseArr() throws JSONException {
            JSONArray arr = new JSONArray();
            pos++; skipWs();
            if (pos < src.length() && src.charAt(pos) == ']') { pos++; return arr; }
            while (true) {
                arr.put(parseValue());
                skipWs();
                if (pos < src.length() && src.charAt(pos) == ',') { pos++; continue; }
                if (pos < src.length() && src.charAt(pos) == ']') { pos++; return arr; }
                throw new JSONException("Expected , or ]");
            }
        }
    }
}
""",
    "org/json/JSONArray.java": """
package org.json;
import java.util.*;
public class JSONArray {
    private final List<Object> list = new ArrayList<>();
    public int length() { return list.size(); }
    public JSONArray put(Object v) { list.add(v); return this; }
    public String getString(int i) { return String.valueOf(list.get(i)); }
    @Override public String toString() {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) sb.append(',');
            sb.append(JSONObject.valToJson(list.get(i)));
        }
        return sb.append(']').toString();
    }
}
""",
    "android/Manifest.java": "package android; public class Manifest { public static class permission { public static final String POST_NOTIFICATIONS = \"android.permission.POST_NOTIFICATIONS\"; public static final String RECORD_AUDIO = \"android.permission.RECORD_AUDIO\"; public static final String CAMERA = \"android.permission.CAMERA\"; } }",
    "android/R.java": "package android; public class R { public static class drawable { public static final int ic_menu_info_details = 1; public static final int ic_dialog_alert = 2; } }",
    "android/annotation/SuppressLint.java": "package android.annotation; public @interface SuppressLint { String[] value(); }",
    "android/util/Base64.java": """
package android.util;
public class Base64 {
    public static final int NO_WRAP = 2;
    public static String encodeToString(byte[] input, int flags) {
        return java.util.Base64.getEncoder().encodeToString(input);
    }
}
""",
    "android/util/Log.java": """
package android.util;
public class Log {
    public static int d(String tag, String msg) { return 0; }
    public static int d(String tag, String msg, Throwable t) { return 0; }
    public static int i(String tag, String msg) { return 0; }
    public static int w(String tag, String msg) { return 0; }
    public static int e(String tag, String msg) { return 0; }
    public static int e(String tag, String msg, Throwable t) { return 0; }
}
""",
    "android/os/Build.java": """
package android.os;
public class Build {
    public static final String MANUFACTURER = "TestManufacturer";
    public static final String MODEL = "TestModel";
    public static class VERSION {
        public static int SDK_INT = 34;
        public static String RELEASE = "14";
    }
    public static class VERSION_CODES {
        public static final int LOLLIPOP = 21;
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
    "android/os/Bundle.java": "package android.os; public class Bundle {}",
    "android/os/IBinder.java": "package android.os; public interface IBinder {}",
    "android/os/Looper.java": """
package android.os;
public class Looper {
    private static final Looper MAIN = new Looper();
    public static Looper getMainLooper() { return MAIN; }
}
""",
    "android/os/Handler.java": """
package android.os;
public class Handler {
    public Handler(Looper l) {}
    public boolean post(Runnable r) { return true; }
    public boolean postDelayed(Runnable r, long ms) { return true; }
    public void removeCallbacks(Runnable r) {}
}
""",
    "android/os/HandlerThread.java": """
package android.os;
public class HandlerThread extends Thread {
    private final Looper looper = new Looper();
    public HandlerThread(String name) { super(name); }
    @Override public synchronized void start() {}
    public Looper getLooper() { return looper; }
    public boolean quitSafely() { return true; }
}
""",
    "android/os/BatteryManager.java": """
package android.os;
public class BatteryManager {
    public static final String EXTRA_LEVEL = "level";
    public static final String EXTRA_SCALE = "scale";
    public static final String EXTRA_PRESENT = "present";
    public static final String EXTRA_STATUS = "status";
    public static final String EXTRA_PLUGGED = "plugged";
    public static final String EXTRA_HEALTH = "health";
    public static final String EXTRA_TEMPERATURE = "temperature";
    public static final String EXTRA_VOLTAGE = "voltage";
    public static final String EXTRA_TECHNOLOGY = "technology";
    public static final int BATTERY_STATUS_CHARGING = 2;
    public static final int BATTERY_STATUS_DISCHARGING = 3;
    public static final int BATTERY_STATUS_NOT_CHARGING = 4;
    public static final int BATTERY_STATUS_FULL = 5;
    public static final int BATTERY_PLUGGED_AC = 1;
    public static final int BATTERY_PLUGGED_USB = 2;
    public static final int BATTERY_PLUGGED_WIRELESS = 4;
    public static final int BATTERY_HEALTH_GOOD = 2;
    public static final int BATTERY_HEALTH_OVERHEAT = 3;
    public static final int BATTERY_HEALTH_DEAD = 4;
    public static final int BATTERY_HEALTH_OVER_VOLTAGE = 5;
    public static final int BATTERY_PROPERTY_CURRENT_NOW = 2;
    public int getIntProperty(int id) { return Integer.MIN_VALUE; }
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
        public boolean isHeld() { return held; }
        public void release() { held = false; }
    }
    public WakeLock newWakeLock(int levelAndFlags, String tag) { return new WakeLock(); }
    public int getCurrentThermalStatus() { return THERMAL_STATUS_NONE; }
    public float getThermalHeadroom(int forecastSeconds) { return 1.0f; }
    public boolean isIgnoringBatteryOptimizations(String packageName) { return false; }
}
""",
    "android/content/Context.java": """
package android.content;
import android.content.pm.PackageManager;
import android.os.PowerManager;
import java.io.File;
public class Context {
    public static final String POWER_SERVICE = "power";
    public static final String SENSOR_SERVICE = "sensor";
    public static final String BATTERY_SERVICE = "batterymanager";
    public static final String CONNECTIVITY_SERVICE = "connectivity";
    public static final String NOTIFICATION_SERVICE = "notification";
    public static final String CAMERA_SERVICE = "camera";
    public static final String AUDIO_SERVICE = "audio";
    public static final int MODE_PRIVATE = 0;
    private final PowerManager pm = new PowerManager();
    private final PackageManager pkgMgr = new PackageManager();
    private final android.app.NotificationManager nm = new android.app.NotificationManager();
    public Context getApplicationContext() { return this; }
    public String getPackageName() { return "com.personalserver.bridge"; }
    public PackageManager getPackageManager() { return pkgMgr; }
    public File getCacheDir() { return new File(System.getProperty("java.io.tmpdir"), "bridge_test_cache"); }
    public Object getSystemService(String name) {
        if (POWER_SERVICE.equals(name)) return pm;
        if (CONNECTIVITY_SERVICE.equals(name)) return new android.net.ConnectivityManager();
        if (NOTIFICATION_SERVICE.equals(name)) return nm;
        if (CAMERA_SERVICE.equals(name)) return new android.hardware.camera2.CameraManager();
        if (AUDIO_SERVICE.equals(name)) return new android.media.AudioManager();
        return null;
    }
    public <T> T getSystemService(Class<T> serviceClass) { return null; }
    public Intent registerReceiver(Object r, IntentFilter f) { return null; }
    public int checkSelfPermission(String perm) { return 0; }
    public int checkCallingOrSelfPermission(String p) { return 0; }
    public void startActivity(Intent i) {}
    public void startService(Intent i) {}
    public SharedPreferences getSharedPreferences(String name, int mode) {
        return new SharedPreferences() {
            @Override public boolean getBoolean(String k, boolean d) { return d; }
            @Override public String getString(String k, String d) { return d; }
            @Override public int getInt(String k, int d) { return d; }
            @Override public long getLong(String k, long d) { return d; }
            @Override public Editor edit() {
                return new Editor() {
                    @Override public Editor putBoolean(String k, boolean v) { return this; }
                    @Override public Editor putString(String k, String v) { return this; }
                    @Override public Editor putInt(String k, int v) { return this; }
                    @Override public Editor putLong(String k, long v) { return this; }
                    @Override public void apply() {}
                    @Override public boolean commit() { return true; }
                };
            }
        };
    }
}
""",
    "android/content/Intent.java": """
package android.content;
import java.util.*;
public class Intent {
    public static final String ACTION_BATTERY_CHANGED = "android.intent.action.BATTERY_CHANGED";
    public static final String ACTION_BOOT_COMPLETED = "android.intent.action.BOOT_COMPLETED";
    public static final int FLAG_ACTIVITY_NEW_TASK = 268435456;
    public static final int FLAG_ACTIVITY_SINGLE_TOP = 536870912;
    private String action;
    private final Map<String, Object> extras = new HashMap<>();
    public Intent() {}
    public Intent(String a) { this.action = a; }
    public Intent(Context ctx, Class<?> cls) {}
    public Intent setAction(String a) { this.action = a; return this; }
    public String getAction() { return action; }
    public Intent setClassName(String p, String c) { return this; }
    public Intent addFlags(int f) { return this; }
    public Intent putExtra(String k, String v) { extras.put(k, v); return this; }
    public Intent putExtra(String k, String[] v) { extras.put(k, v); return this; }
    public Intent putExtra(String k, boolean v) { extras.put(k, v); return this; }
    public Intent putExtra(String k, int v) { extras.put(k, v); return this; }
    public Intent putExtra(String k, Object v) { extras.put(k, v); return this; }
    public String getStringExtra(String k) { Object v = extras.get(k); return v instanceof String ? (String) v : null; }
    public int getIntExtra(String k, int def) { return def; }
    public boolean getBooleanExtra(String k, boolean def) { return def; }
    public Intent setData(android.net.Uri uri) { return this; }
}
""",
    "android/net/Uri.java": """
package android.net;
public class Uri {
    public static Uri parse(String s) { return new Uri(); }
}
""",
    "android/provider/Settings.java": """
package android.provider;
public class Settings {
    public static final String ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS = "android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS";
    public static final String ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS = "android.settings.IGNORE_BATTERY_OPTIMIZATION_SETTINGS";
}
""",
    "android/content/IntentFilter.java": "package android.content; public class IntentFilter { public IntentFilter(String a) {} }",
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
    public static final String MANUFACTURER = "TestManufacturer";
    public static final String MODEL = "TestModel";
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
    "android/content/pm/PackageManager.java": """
package android.content.pm;
import android.content.Intent;
public class PackageManager {
    public static final int PERMISSION_GRANTED = 0;
    public static final String FEATURE_MICROPHONE = "android.hardware.microphone";
    public static final String FEATURE_CAMERA_ANY = "android.hardware.camera.any";
    public static final String FEATURE_CAMERA_FRONT = "android.hardware.camera.front";
    public static class NameNotFoundException extends Exception {}
    public boolean hasSystemFeature(String name) { return true; }
    public PackageInfo getPackageInfo(String pkg, int flags) throws NameNotFoundException { return new PackageInfo(); }
    public Intent getLaunchIntentForPackage(String pkg) { return new Intent("android.intent.action.MAIN"); }
}
""",
    "android/content/pm/PackageInfo.java": "package android.content.pm; public class PackageInfo {}",
    "android/content/BroadcastReceiver.java": "package android.content; public abstract class BroadcastReceiver { public abstract void onReceive(Context context, Intent intent); }",
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
    "android/net/Network.java": "package android.net; public class Network {}",
    "android/net/NetworkCapabilities.java": """
package android.net;
public class NetworkCapabilities {
    public static final int TRANSPORT_VPN = 4;
    public boolean hasTransport(int t) { return t == TRANSPORT_VPN; }
}
""",
    "android/content/pm/ServiceInfo.java": "package android.content.pm; public class ServiceInfo { public static final int FOREGROUND_SERVICE_TYPE_SPECIAL_USE = 1073741824; public static final int FOREGROUND_SERVICE_TYPE_MICROPHONE = 128; public static final int FOREGROUND_SERVICE_TYPE_CAMERA = 64; }",
    "android/media/MediaRecorder.java": """
package android.media;
public class MediaRecorder {
    public static class AudioSource {
        public static final int MIC = 1;
        public static final int VOICE_COMMUNICATION = 7;
    }
}
""",
    "android/media/AudioFormat.java": """
package android.media;
public class AudioFormat {
    public static final int CHANNEL_IN_MONO = 16;
    public static final int CHANNEL_OUT_MONO = 4;
    public static final int ENCODING_PCM_16BIT = 2;
    public static class Builder {
        public Builder setEncoding(int encoding) { return this; }
        public Builder setSampleRate(int rate) { return this; }
        public Builder setChannelMask(int mask) { return this; }
        public AudioFormat build() { return new AudioFormat(); }
    }
}
""",
    "android/graphics/ImageFormat.java": """
package android.graphics;
public class ImageFormat {
    public static final int JPEG = 256;
    public static final int YUV_420_888 = 35;
}
""",
    "android/util/Size.java": """
package android.util;
public class Size {
    private final int width;
    private final int height;
    public Size(int w, int h) { this.width = w; this.height = h; }
    public int getWidth() { return width; }
    public int getHeight() { return height; }
}
""",
    "android/util/Range.java": """
package android.util;
public class Range<T extends Comparable<? super T>> {
    private final T lower;
    private final T upper;
    public Range(T lower, T upper) { this.lower = lower; this.upper = upper; }
    public T getLower() { return lower; }
    public T getUpper() { return upper; }
}
""",
    "android/view/Surface.java": "package android.view; public class Surface {}",
    "android/media/Image.java": """
package android.media;
import java.nio.ByteBuffer;
public class Image implements AutoCloseable {
    public static class Plane {
        public ByteBuffer getBuffer() { return ByteBuffer.allocate(0); }
        public int getPixelStride() { return 1; }
        public int getRowStride() { return 1; }
    }
    public Plane[] getPlanes() { return new Plane[0]; }
    public int getWidth() { return 0; }
    public int getHeight() { return 0; }
    public int getFormat() { return 256; }
    @Override public void close() {}
}
""",
    "android/media/ImageReader.java": """
package android.media;
import android.os.Handler;
import android.view.Surface;
public class ImageReader implements AutoCloseable {
    public interface OnImageAvailableListener {
        void onImageAvailable(ImageReader reader);
    }
    public static ImageReader newInstance(int width, int height, int format, int maxImages) {
        return new ImageReader();
    }
    public Surface getSurface() { return new Surface(); }
    public Image acquireLatestImage() { return null; }
    public void setOnImageAvailableListener(OnImageAvailableListener listener, Handler handler) {}
    @Override public void close() {}
}
""",
    "android/media/AudioAttributes.java": """
package android.media;
public class AudioAttributes {
    public static final int USAGE_VOICE_COMMUNICATION = 2;
    public static final int CONTENT_TYPE_SPEECH = 1;
    public static class Builder {
        public Builder setUsage(int usage) { return this; }
        public Builder setContentType(int type) { return this; }
        public AudioAttributes build() { return new AudioAttributes(); }
    }
}
""",
    "android/media/AudioManager.java": """
package android.media;
public class AudioManager {
    public static final int STREAM_VOICE_CALL = 0;
    public static final int STREAM_MUSIC = 3;
    public static final int AUDIO_SESSION_ID_GENERATE = 0;
    public static final int MODE_IN_COMMUNICATION = 3;
    public static final int MODE_NORMAL = 0;
    public void setMode(int mode) {}
    public void setSpeakerphoneOn(boolean on) {}
    public boolean isSpeakerphoneOn() { return true; }
}
""",
    "android/media/AudioTrack.java": """
package android.media;
public class AudioTrack {
    public static final int MODE_STREAM = 1;
    public static final int STATE_INITIALIZED = 1;
    public static final int PLAYSTATE_PLAYING = 3;
    public static final int PLAYSTATE_STOPPED = 1;
    public static class Builder {
        public Builder setAudioAttributes(AudioAttributes attributes) { return this; }
        public Builder setAudioFormat(AudioFormat format) { return this; }
        public Builder setBufferSizeInBytes(int bufferSizeInBytes) { return this; }
        public Builder setTransferMode(int mode) { return this; }
        public Builder setSessionId(int sessionId) { return this; }
        public AudioTrack build() { return new AudioTrack(0, 0, 0, 0, 0, 0); }
    }
    public AudioTrack(int streamType, int sampleRateInHz, int channelConfig, int audioFormat, int bufferSizeInBytes, int mode) {}
    public AudioTrack(AudioAttributes attributes, AudioFormat format, int bufferSizeInBytes, int mode, int sessionId) {}
    public static int getMinBufferSize(int sampleRateInHz, int channelConfig, int audioFormat) { return 4096; }
    public int getState() { return STATE_INITIALIZED; }
    public void play() {}
    public void stop() {}
    public void release() {}
    public int write(byte[] audioData, int offsetInBytes, int sizeInBytes) { return sizeInBytes; }
}
""",
    "android/hardware/camera2/CameraAccessException.java": """
package android.hardware.camera2;
public class CameraAccessException extends Exception {
    public CameraAccessException(String msg) { super(msg); }
}
""",
    "android/hardware/camera2/CameraCharacteristics.java": """
package android.hardware.camera2;
import android.hardware.camera2.params.StreamConfigurationMap;
public class CameraCharacteristics {
    public static class Key<T> {}
    public static final Key<Integer> LENS_FACING = new Key<>();
    public static final Key<StreamConfigurationMap> SCALER_STREAM_CONFIGURATION_MAP = new Key<>();
    public static final Key<android.util.Range<Integer>[]> CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES = new Key<>();
    public static final int LENS_FACING_FRONT = 0;
    public static final int LENS_FACING_BACK = 1;
    public <T> T get(Key<T> key) { return null; }
}
""",
    "android/hardware/camera2/params/StreamConfigurationMap.java": """
package android.hardware.camera2.params;
import android.util.Size;
public class StreamConfigurationMap {
    public Size[] getOutputSizes(int format) {
        return new Size[] { new Size(640, 480), new Size(1280, 720) };
    }
    public Size[] getOutputSizes(Class<?> klass) {
        return new Size[] { new Size(640, 480), new Size(1280, 720) };
    }
}
""",
    "android/hardware/camera2/CameraDevice.java": """
package android.hardware.camera2;
import android.os.Handler;
import android.view.Surface;
import java.util.List;
public class CameraDevice implements AutoCloseable {
    public static final int TEMPLATE_PREVIEW = 1;
    public static final int TEMPLATE_RECORD = 3;
    public static abstract class StateCallback {
        public static final int ERROR_CAMERA_IN_USE = 1;
        public static final int ERROR_MAX_CAMERAS_IN_USE = 2;
        public static final int ERROR_CAMERA_DISABLED = 3;
        public static final int ERROR_CAMERA_DEVICE = 4;
        public static final int ERROR_CAMERA_SERVICE = 5;
        public abstract void onOpened(CameraDevice camera);
        public abstract void onDisconnected(CameraDevice camera);
        public abstract void onError(CameraDevice camera, int error);
    }
    public CaptureRequest.Builder createCaptureRequest(int templateType) throws CameraAccessException {
        return new CaptureRequest.Builder();
    }
    public void createCaptureSession(List<Surface> outputs, CameraCaptureSession.StateCallback callback, Handler handler) throws CameraAccessException {
        if (callback != null) callback.onConfigured(new CameraCaptureSession());
    }
    @Override public void close() {}
}
""",
    "android/hardware/camera2/CaptureRequest.java": """
package android.hardware.camera2;
import android.view.Surface;
public class CaptureRequest {
    public static class Key<T> {}
    public static final Key<Integer> CONTROL_AF_MODE = new Key<>();
    public static final int CONTROL_AF_MODE_CONTINUOUS_PICTURE = 4;
    public static final Key<Byte> JPEG_QUALITY = new Key<>();
    public static final Key<android.util.Range<Integer>> CONTROL_AE_TARGET_FPS_RANGE = new Key<>();
    public static class Builder {
        public void addTarget(Surface outputTarget) {}
        public <T> void set(Key<T> key, T value) {}
        public CaptureRequest build() { return new CaptureRequest(); }
    }
}
""",
    "android/hardware/camera2/CameraCaptureSession.java": """
package android.hardware.camera2;
import android.os.Handler;
public class CameraCaptureSession implements AutoCloseable {
    public static abstract class StateCallback {
        public abstract void onConfigured(CameraCaptureSession session);
        public abstract void onConfigureFailed(CameraCaptureSession session);
    }
    public static abstract class CaptureCallback {}
    public void setRepeatingRequest(CaptureRequest request, CaptureCallback listener, Handler handler) throws CameraAccessException {}
    public void stopRepeating() throws CameraAccessException {}
    public void abortCaptures() throws CameraAccessException {}
    @Override public void close() {}
}
""",
    "android/hardware/camera2/CameraManager.java": """
package android.hardware.camera2;
import android.os.Handler;
public class CameraManager {
    public static abstract class AvailabilityCallback {}
    public String[] getCameraIdList() throws CameraAccessException {
        return new String[] { "0", "1" };
    }
    public CameraCharacteristics getCameraCharacteristics(String cameraId) throws CameraAccessException {
        return new CameraCharacteristics();
    }
    public void openCamera(String cameraId, CameraDevice.StateCallback callback, Handler handler) throws CameraAccessException {
        if (callback != null) callback.onOpened(new CameraDevice());
    }
    public void registerAvailabilityCallback(AvailabilityCallback callback, Handler handler) {}
}
""",
    "android/media/AudioRecord.java": """
package android.media;
public class AudioRecord {
    public static final int STATE_INITIALIZED = 1;
    public static final int RECORDSTATE_STOPPED = 1;
    public static final int RECORDSTATE_RECORDING = 3;
    public static final int ERROR_INVALID_OPERATION = -3;
    public AudioRecord(int src, int rate, int ch, int fmt, int buf) {}
    public static int getMinBufferSize(int rate, int ch, int fmt) { return 4096; }
    public int getState() { return STATE_INITIALIZED; }
    public int getRecordingState() { return RECORDSTATE_STOPPED; }
    public void startRecording() {}
    public void stop() {}
    public void release() {}
    public int read(byte[] buf, int off, int len) { return 0; }
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
    public String getName() { return "StubSensor"; }
    public String getVendor() { return "Stub"; }
    public int getType() { return 1; }
    public String getStringType() { return "android.sensor.accelerometer"; }
    public int getVersion() { return 1; }
    public float getResolution() { return 0.01f; }
    public float getMaximumRange() { return 10.0f; }
    public int getMinDelay() { return 5000; }
    public float getPower() { return 0.1f; }
}
""",
    "android/hardware/SensorEvent.java": "package android.hardware; public class SensorEvent { public float[] values; public long timestamp; public int accuracy; }",
    "android/hardware/SensorEventListener.java": "package android.hardware; public interface SensorEventListener { void onSensorChanged(SensorEvent e); void onAccuracyChanged(Sensor s, int a); }",
    "android/hardware/SensorManager.java": """
package android.hardware;
import android.os.Handler;
public class SensorManager {
    public Sensor getDefaultSensor(int type) { return null; }
    public boolean registerListener(SensorEventListener l, Sensor s, int us, Handler h) { return false; }
    public void unregisterListener(SensorEventListener l) {}
}
""",
    "android/app/Notification.java": "package android.app; public class Notification {}",
    "android/app/NotificationChannel.java": "package android.app; public class NotificationChannel { public NotificationChannel(String id, CharSequence name, int imp) {} public void setDescription(String d) {} }",
    "android/app/NotificationManager.java": "package android.app; public class NotificationManager { public static final int IMPORTANCE_LOW = 2; public void createNotificationChannel(NotificationChannel c) {} public void notify(int id, Notification n) {} public void cancel(int id) {} }",
    "android/app/PendingIntent.java": "package android.app; import android.content.*; public class PendingIntent { public static final int FLAG_IMMUTABLE = 1; public static final int FLAG_UPDATE_CURRENT = 2; public static final int FLAG_MUTABLE = 33554432; public static PendingIntent getActivity(Context c, int r, Intent i, int f) { return new PendingIntent(); } public static PendingIntent getBroadcast(Context c, int r, Intent i, int f) { return new PendingIntent(); } }",
    "android/app/ForegroundServiceStartNotAllowedException.java": "package android.app; public class ForegroundServiceStartNotAllowedException extends IllegalStateException { public ForegroundServiceStartNotAllowedException(String msg) { super(msg); } }",
    "android/app/Service.java": """
package android.app;
import android.content.*;
import android.os.IBinder;
public abstract class Service extends Context {
    public static final int START_STICKY = 1;
    public static final int START_NOT_STICKY = 2;
    public static final int STOP_FOREGROUND_REMOVE = 1;
    public int stopSelfCalls = 0;
    public int startForegroundCalls = 0;
    public int stopForegroundCalls = 0;
    public void onCreate() {}
    public int onStartCommand(Intent i, int flags, int startId) { return START_NOT_STICKY; }
    public void onDestroy() {}
    public abstract IBinder onBind(Intent i);
    public void startForeground(int id, Notification n) { startForegroundCalls++; }
    public void startForeground(int id, Notification n, int type) { startForegroundCalls++; }
    public void stopForeground(boolean remove) { stopForegroundCalls++; }
    public void stopForeground(int flags) { stopForegroundCalls++; }
    public void stopSelf() { stopSelfCalls++; }
}
""",
    "android/graphics/Color.java": "package android.graphics; public class Color { public static final int WHITE = -1; public static int parseColor(String s) { return 0; } }",
    "android/view/View.java": "package android.view; public class View { public static final int VISIBLE = 0; public static final int INVISIBLE = 4; public static final int GONE = 8; public void setVisibility(int v) {} public void setBackgroundColor(int c) {} public interface OnClickListener { void onClick(View v); } }",
    "android/widget/LinearLayout.java": "package android.widget; import android.content.Context; import android.view.View; public class LinearLayout extends View { public static final int VERTICAL = 1; public static final int HORIZONTAL = 0; public LinearLayout(Context c) {} public void setOrientation(int o) {} public void setPadding(int l, int t, int r, int b) {} public void setBackgroundColor(int c) {} public void setVisibility(int v) {} public void addView(Object v) {} }",
    "android/widget/TextView.java": "package android.widget; import android.content.Context; import android.view.View; public class TextView extends View { public TextView(Context c) {} public void setText(CharSequence t) {} public void setTextColor(int c) {} public void setTextSize(float s) {} public void setPadding(int l, int t, int r, int b) {} public void setBackgroundColor(int c) {} public void setVisibility(int v) {} }",
    "android/widget/Button.java": "package android.widget; import android.content.Context; import android.view.View; public class Button extends TextView { public Button(Context c) { super(c); } public void setOnClickListener(View.OnClickListener l) {} public void setBackgroundColor(int c) {} public void setVisibility(int v) {} }",
    "android/widget/ScrollView.java": "package android.widget; import android.content.Context; import android.view.View; public class ScrollView extends View { public ScrollView(Context c) {} public void addView(Object v) {} public void setBackgroundColor(int c) {} public void setVisibility(int v) {} }",
    "androidx/annotation/NonNull.java": "package androidx.annotation; public @interface NonNull {}",
    "androidx/core/app/ActivityCompat.java": "package androidx.core.app; public class ActivityCompat { public static void requestPermissions(Object a, String[] p, int c) {} }",
    "androidx/core/app/NotificationCompat.java": """
package androidx.core.app;
import android.app.*;
import android.content.Context;
public class NotificationCompat {
    public static final int PRIORITY_LOW = -1;
    public static final int PRIORITY_HIGH = 1;
    public static class Builder {
        public Builder(Context c, String ch) {}
        public Builder setContentTitle(CharSequence t) { return this; }
        public Builder setContentText(CharSequence t) { return this; }
        public Builder setSmallIcon(int i) { return this; }
        public Builder setContentIntent(PendingIntent pi) { return this; }
        public Builder setOngoing(boolean b) { return this; }
        public Builder setPriority(int p) { return this; }
        public Builder setAutoCancel(boolean b) { return this; }
        public Notification build() { return new Notification(); }
    }
}
""",
    "androidx/core/content/ContextCompat.java": "package androidx.core.content; import android.content.*; public class ContextCompat { public static int checkSelfPermission(Context c, String p) { return 0; } public static void startForegroundService(Context c, Intent i) {} }",
    "androidx/appcompat/app/AppCompatActivity.java": """
package androidx.appcompat.app;
import android.content.*;
import android.os.Bundle;
import androidx.annotation.NonNull;
public class AppCompatActivity extends Context {
    protected void onCreate(Bundle b) {}
    protected void onDestroy() {}
    protected void onNewIntent(Intent i) {}
    protected void onResume() {}
    protected void onPause() {}
    public void onRequestPermissionsResult(int rc, @NonNull String[] p, @NonNull int[] g) {}
    public void setContentView(Object v) {}
    public Intent getIntent() { return new Intent(); }
    public void setIntent(Intent i) {}
    public Object startForegroundService(Intent i) { return null; }
    public void startService(Intent i) {}
    public boolean stopService(Intent i) { return true; }
}
""",
}

HARNESS_JAVA = """
package com.personalserver.bridge;

import android.app.ForegroundServiceStartNotAllowedException;
import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

public class BridgeLifecycleTestDriver {

    static int freePort() throws IOException {
        try (ServerSocket s = new ServerSocket()) {
            s.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0));
            return s.getLocalPort();
        }
    }

    static void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    static class CountingServer extends LocalBridgeServer {
        final AtomicInteger bindAttempts = new AtomicInteger(0);
        CountingServer(Context ctx, int port) { super(ctx, port); }
        @Override protected void bindServerSocket(ServerSocket sock) throws IOException {
            bindAttempts.incrementAndGet();
            super.bindServerSocket(sock);
        }
    }

    static class DelayedBindServer extends LocalBridgeServer {
        final long delayMs;
        final CountDownLatch bindEntered = new CountDownLatch(1);
        DelayedBindServer(Context ctx, int port, long delayMs) {
            super(ctx, port);
            this.delayMs = delayMs;
        }
        @Override protected void bindServerSocket(ServerSocket sock) throws IOException {
            bindEntered.countDown();
            try {
                Thread.sleep(delayMs);
            } catch (InterruptedException ie) {
                throw new IOException("Interrupted during delayed bind", ie);
            }
            super.bindServerSocket(sock);
        }
    }

    static class TestableBridgeService extends BridgeService {
        LocalBridgeServer injectedServer;
        final AtomicInteger factoryCalls = new AtomicInteger(0);
        @Override protected LocalBridgeServer createBridgeServer() {
            factoryCalls.incrementAndGet();
            return injectedServer;
        }
        @Override protected void startForegroundWithCompat(Notification n) { startForegroundCalls++; }
        @Override protected void stopForegroundCompat() { stopForegroundCalls++; }
    }

    static class TestableBridgeActivity extends BridgeActivity {
        int launchAttempts = 0;
        int stopAttempts = 0;
        RuntimeException nextLaunchException = null;
        BridgeService backingService = null;

        void simulateResume() { super.onResume(); }
        void simulatePause() { super.onPause(); }
        void simulateStartIntent() {
            Intent i = new Intent();
            i.putExtra("bridge_action", "start");
            super.onNewIntent(i);
        }
        void simulateStopIntent() {
            Intent i = new Intent();
            i.putExtra("bridge_action", "stop");
            super.onNewIntent(i);
        }

        @Override protected void launchBridgeForegroundService(Intent svc) {
            launchAttempts++;
            if (nextLaunchException != null) {
                RuntimeException toThrow = nextLaunchException;
                nextLaunchException = null;
                throw toThrow;
            }
            if (backingService != null) {
                backingService.onCreate();
                backingService.onStartCommand(svc, 0, launchAttempts);
            }
        }

        @Override protected void dispatchStopBridgeService(Intent svc) {
            stopAttempts++;
            if (backingService != null) {
                backingService.onDestroy();
            }
        }
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "suite";
        if ("serve".equals(mode)) {
            runServeMode(Integer.parseInt(args[1]));
            return;
        }
        runUnitSuite();
        System.out.println("JAVA_LIFECYCLE_SUITE_OK");
    }

    static void runServeMode(int port) throws Exception {
        java.io.BufferedReader reader = new java.io.BufferedReader(new java.io.InputStreamReader(System.in));
        TestableBridgeService svc = new TestableBridgeService();
        CountingServer srv = new CountingServer(svc, port);
        svc.injectedServer = srv;
        svc.onCreate();
        int rc = svc.onStartCommand(new Intent().setAction(BridgeService.ACTION_START), 0, 1);
        assertTrue(rc == Service.START_STICKY, "Expected START_STICKY");
        assertTrue(BridgeService.isRunning(), "Service must report running once bound");
        System.out.println("READY:" + port);
        System.out.flush();

        String cmd = reader.readLine();
        if ("STOP".equals(cmd)) {
            svc.onDestroy();
            assertTrue(!BridgeService.isRunning(), "Stopped cleanly");
            System.out.println("STOPPED");
            System.out.flush();

            String nextCmd = reader.readLine();
            if ("RESTART".equals(nextCmd)) {
                CountingServer srv2 = new CountingServer(svc, port);
                svc.injectedServer = srv2;
                svc.onCreate();
                svc.onStartCommand(new Intent().setAction(BridgeService.ACTION_START), 0, 2);
                assertTrue(BridgeService.isRunning(), "Restarted cleanly");
                System.out.println("RESTARTED:" + port);
                System.out.flush();
                reader.readLine();
            }
        }
        svc.onDestroy();
        System.out.println("EXITED");
    }

    static void runUnitSuite() throws Exception {
        Context ctx = new Context();

        // 1. Readiness only after successful bind + Startup timeout
        int timeoutPort = freePort();
        DelayedBindServer slow = new DelayedBindServer(ctx, timeoutPort, 1500);
        assertTrue(!slow.isListening(), "Not listening before start");
        boolean timedOutOk = slow.startAndAwaitBind(120);
        assertTrue(!timedOutOk, "Timed out bind must return false");
        assertTrue(!slow.isListening(), "Must not report listening on startup timeout");
        assertTrue(slow.getLastError() != null && slow.getLastError().contains("Timed out"), "Error must record timeout");

        // 2. Stop during startup aborts bind cleanly & does not leak a listener
        int abortPort = freePort();
        DelayedBindServer abortable = new DelayedBindServer(ctx, abortPort, 2000);
        CompletableFuture<Boolean> startFuture = CompletableFuture.supplyAsync(() -> abortable.startAndAwaitBind(3000));
        assertTrue(abortable.bindEntered.await(1500, TimeUnit.MILLISECONDS), "Bind must begin");
        assertTrue(!abortable.isListening(), "Must not report ready while bind is still blocked");
        abortable.stop();
        assertTrue(!startFuture.get(1500, TimeUnit.MILLISECONDS), "Aborted start must return false");
        assertTrue(!abortable.isListening(), "Must remain stopped after stop-during-startup");

        // 3. Duplicate server / concurrent startup + repeated start requests
        int port = freePort();
        CountingServer srv = new CountingServer(ctx, port);
        CompletableFuture<Boolean> f1 = CompletableFuture.supplyAsync(() -> srv.startAndAwaitBind(2000));
        CompletableFuture<Boolean> f2 = CompletableFuture.supplyAsync(() -> srv.startAndAwaitBind(2000));
        assertTrue(f1.get(3000, TimeUnit.MILLISECONDS) && f2.get(3000, TimeUnit.MILLISECONDS), "Concurrent starts succeed");
        assertTrue(srv.startAndAwaitBind(1000), "Repeated start succeeds");
        assertTrue(srv.bindAttempts.get() == 1, "Expected 1 bind attempt, got " + srv.bindAttempts.get());

        // 4. Start after stop + Executor shutdown and recreation (repeated across cycles)
        for (int i = 0; i < 4; i++) {
            srv.stop();
            assertTrue(!srv.isListening(), "Stop must close server on cycle " + i);
            boolean restarted = srv.startAndAwaitBind(2000);
            assertTrue(restarted && srv.isListening(), "Restart after stop/executor shutdown failed on cycle " + i);
        }
        srv.stop();

        // 5. Socket bind failure propagates to BridgeService
        try (ServerSocket blocker = new ServerSocket()) {
            blocker.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0));
            int busyPort = blocker.getLocalPort();
            TestableBridgeService failSvc = new TestableBridgeService();
            failSvc.injectedServer = new CountingServer(failSvc, busyPort);
            failSvc.onCreate();
            int rc = failSvc.onStartCommand(new Intent().setAction(BridgeService.ACTION_START), 0, 1);
            assertTrue(rc == Service.START_NOT_STICKY, "Failed bind must return START_NOT_STICKY");
            assertTrue(!BridgeService.isRunning(), "Service must NOT report running when bind failed");
            assertTrue(failSvc.stopSelfCalls >= 1, "Service must stopSelf on bind failure");
            assertTrue(BridgeService.getLastStartError() != null, "Service must expose bind error");
            failSvc.onDestroy();
        }

        // 6. BridgeService repeated start idempotency + clean shutdown/restart
        int svcPort = freePort();
        TestableBridgeService okSvc = new TestableBridgeService();
        CountingServer okSrv1 = new CountingServer(okSvc, svcPort);
        okSvc.injectedServer = okSrv1;
        okSvc.onCreate();
        assertTrue(okSvc.onStartCommand(new Intent().setAction(BridgeService.ACTION_START), 0, 1) == Service.START_STICKY, "Start 1");
        assertTrue(BridgeService.isRunning(), "Service running after start 1");
        assertTrue(okSvc.onStartCommand(new Intent().setAction(BridgeService.ACTION_START), 0, 2) == Service.START_STICKY, "Repeated start");
        assertTrue(okSrv1.bindAttempts.get() == 1, "BridgeService repeated start must not rebind");
        assertTrue(okSvc.factoryCalls.get() == 1, "BridgeService repeated start must reuse healthy server");
        okSvc.onDestroy();
        assertTrue(!BridgeService.isRunning(), "Service not running after destroy");

        // 7. BridgeActivity FGS exception handling & bounded single-retry on resume
        TestableBridgeActivity act = new TestableBridgeActivity();
        TestableBridgeService backing = new TestableBridgeService();
        backing.injectedServer = new CountingServer(backing, svcPort);
        act.backingService = backing;

        act.simulatePause();
        act.nextLaunchException = new ForegroundServiceStartNotAllowedException("Background restriction simulated");
        act.simulateStartIntent();
        assertTrue(act.launchAttempts == 1, "Initial attempt made");
        assertTrue(!BridgeService.isRunning(), "Service not running yet after caught FGS exception");
        assertTrue(act.isPendingStartRequest(), "Pending start must be recorded cleanly");
        assertTrue(act.getLastLifecycleError() != null && act.getLastLifecycleError().contains("ForegroundServiceStartNotAllowedException"), "Error recorded");

        // Resume triggers one bounded retry and clears pending flag on success
        act.simulateResume();
        assertTrue(act.launchAttempts == 2, "Resume triggers single deferred retry");
        assertTrue(BridgeService.isRunning(), "BridgeService running after resume retry");
        assertTrue(!act.isPendingStartRequest(), "Pending flag cleared after success");

        // Repeated start while running must be idempotent (no duplicate startForegroundService)
        act.simulateStartIntent();
        assertTrue(act.launchAttempts == 2, "Idempotent start when already running must not relaunch service");

        // Stop clears state cleanly
        act.simulateStopIntent();
        assertTrue(!BridgeService.isRunning(), "BridgeService stopped");

        // Persistent FGS failure must NOT loop uncontrollably
        act.simulatePause();
        act.nextLaunchException = new ForegroundServiceStartNotAllowedException("Denied #1");
        act.simulateStartIntent();
        act.nextLaunchException = new ForegroundServiceStartNotAllowedException("Denied #2");
        act.simulateResume();
        act.simulatePause();
        act.simulateResume();
        assertTrue(act.launchAttempts == 4, "Exactly 1 initial + 1 resume retry, never unbounded loop: got " + act.launchAttempts);
        assertTrue(!act.isPendingStartRequest(), "Pending flag must clear after bounded retry exhausts");
    }
}
"""


def _compile_bridge_test_classes(build_dir: Path) -> Path:
    src_dir = build_dir / "src"
    cls_dir = build_dir / "classes"
    src_dir.mkdir(parents=True, exist_ok=True)
    cls_dir.mkdir(parents=True, exist_ok=True)

    for rel, content in ANDROID_STUBS.items():
        target = src_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.lstrip(), encoding="utf-8")

    pkg_dir = src_dir / "com" / "personalserver" / "bridge"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for jf in BRIDGE_JAVA_DIR.glob("*.java"):
        shutil.copy2(jf, pkg_dir / jf.name)

    driver_path = pkg_dir / "BridgeLifecycleTestDriver.java"
    driver_path.write_text(HARNESS_JAVA.lstrip(), encoding="utf-8")

    java_files = [str(p) for p in src_dir.rglob("*.java")]
    res = subprocess.run(
        ["javac", "-d", str(cls_dir), *java_files],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"javac failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    return cls_dir


@pytest.fixture(scope="module")
def compiled_bridge_classes(tmp_path_factory):
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("JDK (javac/java) not available on PATH")
    build_dir = tmp_path_factory.mktemp("bridge_lifecycle_jvm")
    return _compile_bridge_test_classes(build_dir)


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_java_bridge_lifecycle_state_machine_and_error_paths(compiled_bridge_classes: Path):
    """
    Runs JVM driver covering:
    - Readiness only after bind & startup timeout
    - Stop during startup
    - Duplicate/concurrent startup & repeated start idempotency
    - Start after stop & executor shutdown/recreation
    - Socket bind failure propagation to BridgeService
    - BridgeActivity explicit FGS exception handling & bounded single-retry
    """
    res = subprocess.run(
        ["java", "-cp", str(compiled_bridge_classes), "com.personalserver.bridge.BridgeLifecycleTestDriver", "suite"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Java lifecycle driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "JAVA_LIFECYCLE_SUITE_OK" in res.stdout


def test_local_socket_transport_end_to_end_clean_shutdown_and_restart(compiled_bridge_classes: Path):
    """
    Spawns the compiled Java BridgeService + LocalBridgeServer on a loopback port
    and verifies live Python LocalSocketTransport IPC across clean shutdown and restart.
    """
    port = _get_free_port()
    proc = subprocess.Popen(
        [
            "java",
            "-cp",
            str(compiled_bridge_classes),
            "com.personalserver.bridge.BridgeLifecycleTestDriver",
            "serve",
            str(port),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = proc.stdout.readline().strip()
        assert ready_line == f"READY:{port}", f"Unexpected startup line: {ready_line}"

        transport = LocalSocketTransport(host="127.0.0.1", port=port, timeout_sec=3.0)
        assert transport.is_available() is True

        pong1 = transport.send_request(BridgeProtocol.create_request("ping", "ping"))
        assert pong1.success is True
        assert pong1.data.get("status") == "pong"

        sens1 = transport.send_request(BridgeProtocol.create_request("sensors", "SENSOR_STATUS"))
        assert sens1.success is True
        assert sens1.data.get("sessions") == []

        # Trigger clean destroy, verify stopped state, then restart
        proc.stdin.write("STOP\n")
        proc.stdin.flush()

        stopped_line = proc.stdout.readline().strip()
        assert stopped_line == "STOPPED", f"Unexpected stop line: {stopped_line}"
        assert transport.is_available() is False
        dead_ping = transport.send_request(BridgeProtocol.create_request("ping", "ping"))
        assert dead_ping.success is False
        assert dead_ping.error_code == "SOCKET_ERROR"

        proc.stdin.write("RESTART\n")
        proc.stdin.flush()

        restarted_line = proc.stdout.readline().strip()
        assert restarted_line == f"RESTARTED:{port}", f"Unexpected restart line: {restarted_line}"
        assert transport.is_available() is True

        pong2 = transport.send_request(BridgeProtocol.create_request("ping", "ping"))
        assert pong2.success is True
        assert pong2.data.get("status") == "pong"

        proc.stdin.write("QUIT\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
