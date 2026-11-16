package com.personalserver.bridge;

import android.content.Context;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.HandlerThread;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Native Android SensorManager discovery and bounded session acquisition manager (D3).
 * Ensures missing sensors are never fabricated, queues remain strictly bounded,
 * and SensorEventListeners are always unregistered on stop or service destruction.
 */
public class SensorBridgeManager {

    public static final int MIN_INTERVAL_MS = 20;
    public static final int MAX_INTERVAL_MS = 10000;
    public static final int DEFAULT_INTERVAL_MS = 200;
    public static final double MAX_SESSION_DURATION_SEC = 300.0;
    public static final int MAX_BUFFER_SAMPLES = 32;
    public static final int MAX_CONCURRENT_SESSIONS = 4;

    private final SensorManager sensorManager;
    private final Map<String, ActiveSensorSession> activeSessions = new ConcurrentHashMap<>();
    private final HandlerThread sensorThread;
    private final Handler sensorHandler;

    private static final Map<String, Integer> TYPE_MAP = new LinkedHashMap<>();
    private static final Map<String, String> UNITS_MAP = new LinkedHashMap<>();

    static {
        TYPE_MAP.put("accelerometer", Sensor.TYPE_ACCELEROMETER);
        TYPE_MAP.put("gyroscope", Sensor.TYPE_GYROSCOPE);
        TYPE_MAP.put("magnetometer", Sensor.TYPE_MAGNETIC_FIELD);
        TYPE_MAP.put("ambient_light", Sensor.TYPE_LIGHT);
        TYPE_MAP.put("proximity", Sensor.TYPE_PROXIMITY);

        UNITS_MAP.put("accelerometer", "m/s^2");
        UNITS_MAP.put("gyroscope", "rad/s");
        UNITS_MAP.put("magnetometer", "uT");
        UNITS_MAP.put("ambient_light", "lux");
        UNITS_MAP.put("proximity", "cm");
    }

    public SensorBridgeManager(Context context) {
        this.sensorManager = (SensorManager) context.getApplicationContext().getSystemService(Context.SENSOR_SERVICE);
        this.sensorThread = new HandlerThread("PersonalServerSensorThread");
        this.sensorThread.start();
        this.sensorHandler = new Handler(this.sensorThread.getLooper());
    }

    private static Object safeDouble(double value) {
        return Double.isFinite(value) ? value : JSONObject.NULL;
    }

    public JSONObject discoverSensors(JSONArray requestedTypes) throws JSONException {
        JSONObject result = new JSONObject();
        JSONObject sensorsObj = new JSONObject();

        List<String> targets = new ArrayList<>();
        if (requestedTypes != null && requestedTypes.length() > 0) {
            for (int i = 0; i < requestedTypes.length(); i++) {
                targets.add(requestedTypes.getString(i));
            }
        } else {
            targets.addAll(TYPE_MAP.keySet());
        }

        for (String sensorType : targets) {
            Integer androidType = TYPE_MAP.get(sensorType);
            JSONObject info = new JSONObject();
            info.put("sensor_type", sensorType);
            info.put("units", UNITS_MAP.getOrDefault(sensorType, ""));

            if (androidType == null || sensorManager == null) {
                info.put("available", false);
                info.put("error", androidType == null ? "Unsupported sensor type" : "SensorManager unavailable");
                sensorsObj.put(sensorType, info);
                continue;
            }

            Sensor s = sensorManager.getDefaultSensor(androidType);
            if (s == null) {
                info.put("available", false);
                info.put("android_type", androidType);
                info.put("source", JSONObject.NULL);
                info.put("error", "Physical sensor not present on device");
            } else {
                info.put("available", true);
                info.put("name", s.getName());
                info.put("vendor", s.getVendor());
                info.put("android_type", s.getType());
                info.put("string_type", s.getStringType());
                info.put("version", s.getVersion());
                info.put("resolution", safeDouble(s.getResolution()));
                info.put("max_range", safeDouble(s.getMaximumRange()));
                info.put("min_delay_us", s.getMinDelay());
                info.put("power_ma", safeDouble(s.getPower()));
                info.put("source", "native_bridge_apk");
                info.put("error", JSONObject.NULL);
            }
            sensorsObj.put(sensorType, info);
        }

        result.put("sensors", sensorsObj);
        result.put("source", "native_bridge_apk");
        return result;
    }

    public synchronized JSONObject startSession(JSONObject params) throws JSONException {
        reapExpiredSessions();

        String sensorType = params.optString("sensor_type", "");
        Integer androidType = TYPE_MAP.get(sensorType);
        if (androidType == null) {
            throw new IllegalArgumentException("Unsupported sensor_type: " + sensorType);
        }
        if (sensorManager == null) {
            throw new IllegalStateException("Android SensorManager unavailable");
        }

        Sensor sensor = sensorManager.getDefaultSensor(androidType);
        if (sensor == null) {
            throw new IllegalStateException("Physical sensor unavailable: " + sensorType);
        }

        if (activeSessions.size() >= MAX_CONCURRENT_SESSIONS) {
            throw new IllegalStateException("Max concurrent sensor sessions reached (" + MAX_CONCURRENT_SESSIONS + ")");
        }

        int samplingIntervalMs = params.optInt("sampling_interval_ms", DEFAULT_INTERVAL_MS);
        int rawRequestedIntervalMs = params.optInt("requested_interval_ms", samplingIntervalMs);
        if (rawRequestedIntervalMs <= 0 || rawRequestedIntervalMs > 60000 || samplingIntervalMs <= 0 || samplingIntervalMs > 60000) {
            throw new IllegalArgumentException("Invalid sampling_interval_ms: " + rawRequestedIntervalMs);
        }

        int hardwareMinMs = Math.max(MIN_INTERVAL_MS, (int) Math.ceil(sensor.getMinDelay() / 1000.0));
        int actualIntervalMs = Math.max(hardwareMinMs, Math.min(samplingIntervalMs, MAX_INTERVAL_MS));

        double maxDurationSec = Math.max(1.0, Math.min(params.optDouble("max_duration_sec", 60.0), MAX_SESSION_DURATION_SEC));
        int maxBuffer = Math.max(1, Math.min(params.optInt("max_buffer_samples", MAX_BUFFER_SAMPLES), MAX_BUFFER_SAMPLES));

        String sessionId = "nsess-" + UUID.randomUUID().toString().replace("-", "").substring(0, 10);
        ActiveSensorSession session = new ActiveSensorSession(
            sessionId,
            sensorType,
            sensor,
            rawRequestedIntervalMs,
            actualIntervalMs,
            maxDurationSec,
            maxBuffer
        );

        boolean registered = sensorManager.registerListener(
            session,
            sensor,
            actualIntervalMs * 1000,
            sensorHandler
        );
        if (!registered) {
            throw new IllegalStateException("SensorManager failed to register listener for " + sensor.getName());
        }

        activeSessions.put(sessionId, session);
        JSONObject out = new JSONObject();
        out.put("session", session.toStatusJson());
        return out;
    }

    public JSONObject readSession(String sessionId, int maxSamples) throws JSONException {
        reapExpiredSessions();
        ActiveSensorSession session = activeSessions.get(sessionId);
        if (session == null) {
            throw new IllegalStateException("Session not found: " + sessionId);
        }

        session.waitForSample(600L);
        int boundedMax = Math.max(1, Math.min(maxSamples, MAX_BUFFER_SAMPLES));
        JSONArray readings = session.drainReadings(boundedMax);

        JSONObject out = new JSONObject();
        out.put("session_id", sessionId);
        out.put("sensor_type", session.sensorType);
        out.put("readings", readings);
        out.put("session", session.toStatusJson());
        return out;
    }

    public synchronized JSONObject stopSession(String sessionId, String reason) throws JSONException {
        ActiveSensorSession session = activeSessions.remove(sessionId);
        if (session == null) {
            throw new IllegalStateException("Session not found: " + sessionId);
        }
        unregisterSession(session, reason);
        JSONObject out = new JSONObject();
        out.put("session", session.toStatusJson());
        return out;
    }

    public JSONObject getStatus(String sessionId) throws JSONException {
        reapExpiredSessions();
        JSONObject out = new JSONObject();
        if (sessionId != null && !sessionId.isEmpty()) {
            ActiveSensorSession session = activeSessions.get(sessionId);
            if (session == null) {
                throw new IllegalStateException("Session not found: " + sessionId);
            }
            out.put("session", session.toStatusJson());
            return out;
        }
        JSONArray arr = new JSONArray();
        for (ActiveSensorSession s : activeSessions.values()) {
            arr.put(s.toStatusJson());
        }
        out.put("sessions", arr);
        return out;
    }

    public synchronized void stopAll() {
        for (ActiveSensorSession s : activeSessions.values()) {
            unregisterSession(s, "Bridge service shutdown");
        }
        activeSessions.clear();
        try {
            sensorThread.quitSafely();
        } catch (Exception ignored) {}
    }

    private void unregisterSession(ActiveSensorSession session, String reason) {
        if (sensorManager != null) {
            try {
                sensorManager.unregisterListener(session);
            } catch (Exception ignored) {}
        }
        session.markStopped(reason);
    }

    private void reapExpiredSessions() {
        long now = System.currentTimeMillis();
        for (Map.Entry<String, ActiveSensorSession> entry : activeSessions.entrySet()) {
            ActiveSensorSession s = entry.getValue();
            if (s.isExpired(now)) {
                activeSessions.remove(entry.getKey());
                unregisterSession(s, "Maximum session duration expired");
            }
        }
    }

    private static class ActiveSensorSession implements SensorEventListener {
        final String sessionId;
        final String sensorType;
        final Sensor sensor;
        final int requestedIntervalMs;
        final int actualIntervalMs;
        final double maxDurationSec;
        final int maxBufferSize;
        final long startedAtMs;
        volatile String state = "ACTIVE";
        volatile long stoppedAtMs = 0L;
        volatile int samplesRead = 0;

        private final Deque<JSONObject> buffer = new ArrayDeque<>();
        private long lastSampleAcceptedMs = 0L;

        ActiveSensorSession(
            String sessionId,
            String sensorType,
            Sensor sensor,
            int requestedIntervalMs,
            int actualIntervalMs,
            double maxDurationSec,
            int maxBufferSize
        ) {
            this.sessionId = sessionId;
            this.sensorType = sensorType;
            this.sensor = sensor;
            this.requestedIntervalMs = requestedIntervalMs;
            this.actualIntervalMs = actualIntervalMs;
            this.maxDurationSec = maxDurationSec;
            this.maxBufferSize = maxBufferSize;
            this.startedAtMs = System.currentTimeMillis();
        }

        boolean isExpired(long nowMs) {
            return "ACTIVE".equals(state) && ((nowMs - startedAtMs) / 1000.0) >= maxDurationSec;
        }

        synchronized void markStopped(String reason) {
            if ("ACTIVE".equals(state)) {
                state = reason != null && reason.toLowerCase().contains("expired") ? "TIMED_OUT" : "STOPPED";
                stoppedAtMs = System.currentTimeMillis();
            }
            notifyAll();
        }

        synchronized void waitForSample(long timeoutMs) {
            if (!"ACTIVE".equals(state) || !buffer.isEmpty()) {
                return;
            }
            long deadline = System.currentTimeMillis() + timeoutMs;
            while ("ACTIVE".equals(state) && buffer.isEmpty()) {
                long remaining = deadline - System.currentTimeMillis();
                if (remaining <= 0) {
                    break;
                }
                try {
                    wait(remaining);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }

        @Override
        public synchronized void onSensorChanged(SensorEvent event) {
            if (!"ACTIVE".equals(state) || event == null || event.values == null) {
                return;
            }
            long nowMs = System.currentTimeMillis();
            if (isExpired(nowMs)) {
                return;
            }
            // Enforce software rate ceiling if HAL delivers faster than requested
            if (lastSampleAcceptedMs > 0 && (nowMs - lastSampleAcceptedMs) < Math.max(10, actualIntervalMs - 5)) {
                return;
            }
            lastSampleAcceptedMs = nowMs;

            try {
                JSONObject r = new JSONObject();
                r.put("sensor_type", sensorType);
                r.put("sensor_name", sensor.getName());
                r.put("timestamp", nowMs / 1000.0);
                r.put("sensor_timestamp_ns", event.timestamp);
                r.put("accuracy", event.accuracy);
                r.put("units", UNITS_MAP.getOrDefault(sensorType, ""));
                r.put("session_id", sessionId);
                r.put("available", true);
                r.put("state", state);

                if ("accelerometer".equals(sensorType) || "gyroscope".equals(sensorType) || "magnetometer".equals(sensorType)) {
                    r.put("x", event.values.length > 0 ? safeDouble(event.values[0]) : JSONObject.NULL);
                    r.put("y", event.values.length > 1 ? safeDouble(event.values[1]) : JSONObject.NULL);
                    r.put("z", event.values.length > 2 ? safeDouble(event.values[2]) : JSONObject.NULL);
                } else {
                    r.put("value", event.values.length > 0 ? safeDouble(event.values[0]) : JSONObject.NULL);
                }

                if (buffer.size() >= maxBufferSize) {
                    buffer.pollFirst(); // Strictly bounded ring behavior
                }
                buffer.addLast(r);
                samplesRead++;
                notifyAll();
            } catch (JSONException ignored) {}
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {}

        synchronized JSONArray drainReadings(int maxSamples) {
            JSONArray arr = new JSONArray();
            while (!buffer.isEmpty() && arr.length() < maxSamples) {
                arr.put(buffer.pollFirst());
            }
            return arr;
        }

        synchronized JSONObject toStatusJson() throws JSONException {
            JSONObject obj = new JSONObject();
            obj.put("session_id", sessionId);
            obj.put("sensor_type", sensorType);
            obj.put("sensor_name", sensor.getName());
            obj.put("requested_interval_ms", requestedIntervalMs);
            obj.put("actual_interval_ms", actualIntervalMs);
            obj.put("rate_clamped", actualIntervalMs != requestedIntervalMs);
            obj.put("started_at", startedAtMs / 1000.0);
            obj.put("stopped_at", stoppedAtMs > 0 ? (stoppedAtMs / 1000.0) : JSONObject.NULL);
            obj.put("max_duration_sec", maxDurationSec);
            obj.put("state", state);
            obj.put("samples_read", samplesRead);
            obj.put("buffered_samples", buffer.size());
            obj.put("max_buffer_size", maxBufferSize);
            return obj;
        }
    }
}
