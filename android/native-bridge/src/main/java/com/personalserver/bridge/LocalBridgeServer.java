package com.personalserver.bridge;

import android.content.Context;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Local IPC server hosting the PersonalServer Protocol v1.
 * Binds strictly to 127.0.0.1 (loopback) to prevent network exposure.
 */
public class LocalBridgeServer {

    private static final String TAG = "LocalBridgeServer";
    public static final int DEFAULT_PORT = 8765;
    public static final int PROTOCOL_VERSION = 1;
    public static final int MAX_PAYLOAD_BYTES = 65536;
    public static final long DEFAULT_START_TIMEOUT_MS = 2500L;

    private enum LifecycleState {
        STOPPED,
        STARTING,
        LISTENING
    }

    private static final class StartupAttempt {
        final CountDownLatch latch = new CountDownLatch(1);
        volatile boolean succeeded = false;
        volatile String error = null;

        void succeed() {
            succeeded = true;
            error = null;
            latch.countDown();
        }

        void fail(String message) {
            succeeded = false;
            error = message;
            latch.countDown();
        }
    }

    private final Context context;
    private final int port;
    private final BatteryBridgeManager batteryManager;
    private final ThermalBridgeManager thermalManager;
    private final Object lifecycleLock = new Object();

    private SensorBridgeManager sensorManager;
    private MicrophoneBridgeManager microphoneManager;
    private LiveMediaBridgeManager liveMediaManager;
    private RecoveryCoordinator recoveryCoordinator;
    private volatile MicrophoneBridgeManager.RecordingNotificationListener recordingNotificationListener = null;
    private volatile LiveMediaBridgeManager.LiveCallNotificationListener liveCallNotificationListener = null;
    private volatile DiagnosticStateProbe diagnosticStateProbe = null;

    public interface DiagnosticStateProbe {
        JSONObject probeExternalStates();
    }

    public void setDiagnosticStateProbe(DiagnosticStateProbe probe) {
        this.diagnosticStateProbe = probe;
    }
    private ExecutorService executor;
    private ServerSocket serverSocket;
    private Thread acceptThread;
    private StartupAttempt inFlightStartup;
    private volatile LifecycleState state = LifecycleState.STOPPED;
    private volatile boolean isRunning = false;
    private volatile boolean stopRequested = false;
    private volatile boolean stoppedOnce = false;
    private volatile String lastError = null;

    public LocalBridgeServer(Context context, int port) {
        this.context = context.getApplicationContext();
        this.port = port;
        this.batteryManager = new BatteryBridgeManager(this.context);
        this.thermalManager = new ThermalBridgeManager(this.context);
        this.sensorManager = new SensorBridgeManager(this.context);
        this.microphoneManager = new MicrophoneBridgeManager(this.context);
        this.liveMediaManager = new LiveMediaBridgeManager(this.context);
        this.recoveryCoordinator = new RecoveryCoordinator(this.context);
        this.executor = Executors.newCachedThreadPool();
    }

    public void setRecordingNotificationListener(MicrophoneBridgeManager.RecordingNotificationListener listener) {
        this.recordingNotificationListener = listener;
        if (this.microphoneManager != null) {
            this.microphoneManager.setRecordingNotificationListener(listener);
        }
    }

    public void setLiveCallNotificationListener(LiveMediaBridgeManager.LiveCallNotificationListener listener) {
        this.liveCallNotificationListener = listener;
        if (this.liveMediaManager != null) {
            this.liveMediaManager.setNotificationListener(listener);
        }
    }

    public LiveMediaBridgeManager getLiveMediaManager() {
        return liveMediaManager;
    }

    public void setLiveMediaManager(LiveMediaBridgeManager manager) {
        this.liveMediaManager = manager;
    }

    public RecoveryCoordinator getRecoveryCoordinator() {
        return recoveryCoordinator;
    }

    public void setRecoveryCoordinator(RecoveryCoordinator coordinator) {
        this.recoveryCoordinator = coordinator;
    }

    public boolean isListening() {
        ServerSocket sock = this.serverSocket;
        return state == LifecycleState.LISTENING
                && isRunning
                && sock != null
                && sock.isBound()
                && !sock.isClosed();
    }

    public String getLastError() {
        return lastError;
    }

    protected ServerSocket createServerSocket() throws IOException {
        return new ServerSocket();
    }

    protected void bindServerSocket(ServerSocket sock) throws IOException {
        // Strict loopback binding: 127.0.0.1 only, with SO_REUSEADDR for fast service restarts
        sock.setReuseAddress(true);
        sock.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 16);
    }

    public void start() {
        startAndAwaitBind(DEFAULT_START_TIMEOUT_MS);
    }

    public boolean startAndAwaitBind(long timeoutMs) {
        final StartupAttempt attempt;
        final boolean initiateNewStart;
        final ExecutorService targetExecutor;

        synchronized (lifecycleLock) {
            if (isListening()) {
                return true;
            }
            if (state == LifecycleState.STARTING && inFlightStartup != null) {
                attempt = inFlightStartup;
                initiateNewStart = false;
                targetExecutor = this.executor;
            } else {
                if (executor == null || executor.isShutdown() || executor.isTerminated()) {
                    executor = Executors.newCachedThreadPool();
                }
                if (stoppedOnce) {
                    sensorManager = new SensorBridgeManager(this.context);
                    microphoneManager = new MicrophoneBridgeManager(this.context);
                    microphoneManager.setRecordingNotificationListener(recordingNotificationListener);
                    stoppedOnce = false;
                }
                stopRequested = false;
                isRunning = false;
                lastError = null;
                state = LifecycleState.STARTING;
                attempt = new StartupAttempt();
                inFlightStartup = attempt;
                initiateNewStart = true;
                targetExecutor = this.executor;
            }
        }

        if (initiateNewStart) {
            try {
                targetExecutor.execute(() -> runAcceptLoop(attempt, targetExecutor));
            } catch (RuntimeException submitErr) {
                synchronized (lifecycleLock) {
                    if (inFlightStartup == attempt) {
                        inFlightStartup = null;
                        state = LifecycleState.STOPPED;
                    }
                    isRunning = false;
                    lastError = "Failed to launch server accept loop: " + submitErr.getMessage();
                }
                attempt.fail(lastError);
                return false;
            }
        }

        long waitMs = Math.max(1L, timeoutMs);
        boolean completed;
        try {
            completed = attempt.latch.await(waitMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            lastError = "Interrupted waiting for bridge socket bind";
            stop();
            return false;
        }

        if (!completed) {
            lastError = "Timed out waiting for loopback bind on 127.0.0.1:" + port + " after " + waitMs + "ms";
            Log.e(TAG, lastError);
            stop();
            return false;
        }

        if (!attempt.succeeded && attempt.error != null) {
            lastError = attempt.error;
        }
        return attempt.succeeded && isListening();
    }

    private void runAcceptLoop(StartupAttempt attempt, ExecutorService ownExecutor) {
        ServerSocket sock = null;
        synchronized (lifecycleLock) {
            acceptThread = Thread.currentThread();
        }
        try {
            synchronized (lifecycleLock) {
                if (stopRequested || inFlightStartup != attempt) {
                    attempt.fail("Bridge startup cancelled before socket creation");
                    return;
                }
                sock = createServerSocket();
                serverSocket = sock;
            }

            bindServerSocket(sock);

            synchronized (lifecycleLock) {
                if (stopRequested || inFlightStartup != attempt || sock.isClosed() || !sock.isBound()) {
                    safeClose(sock);
                    if (serverSocket == sock) {
                        serverSocket = null;
                    }
                    state = LifecycleState.STOPPED;
                    isRunning = false;
                    attempt.fail("Bridge stopped during socket bind");
                    return;
                }
                state = LifecycleState.LISTENING;
                isRunning = true;
                lastError = null;
                inFlightStartup = null;
                attempt.succeed();
            }

            Log.i(TAG, "Bridge Server listening on 127.0.0.1:" + sock.getLocalPort());

            while (isRunning && !stopRequested && !sock.isClosed()) {
                Socket client = null;
                try {
                    client = sock.accept();
                    final Socket accepted = client;
                    accepted.setSoTimeout(5000);
                    ownExecutor.execute(() -> handleClient(accepted));
                } catch (Exception acceptErr) {
                    if (client != null) {
                        try {
                            client.close();
                        } catch (Exception ignored) {}
                    }
                    if (!isRunning || stopRequested || sock.isClosed()) {
                        break;
                    }
                    Log.w(TAG, "Transient client accept/setup error (continuing): " + acceptErr.getMessage());
                }
            }
        } catch (Exception e) {
            String msg = "Server socket bind/loop failed on 127.0.0.1:" + port + ": " + e.getMessage();
            synchronized (lifecycleLock) {
                if (!stopRequested) {
                    lastError = msg;
                    Log.e(TAG, msg);
                }
                if (inFlightStartup == attempt) {
                    inFlightStartup = null;
                    attempt.fail(msg);
                }
            }
        } finally {
            synchronized (lifecycleLock) {
                if (serverSocket == sock) {
                    serverSocket = null;
                    isRunning = false;
                    state = LifecycleState.STOPPED;
                }
                if (acceptThread == Thread.currentThread()) {
                    acceptThread = null;
                }
                if (inFlightStartup == attempt) {
                    inFlightStartup = null;
                    attempt.fail(lastError != null ? lastError : "Bridge server exited before bind");
                }
            }
            safeClose(sock);
        }
    }

    private static void safeClose(ServerSocket sock) {
        if (sock != null) {
            try {
                if (!sock.isClosed()) {
                    sock.close();
                }
            } catch (Exception ignored) {}
        }
    }

    public void stop() {
        final StartupAttempt pending;
        final ServerSocket sockToClose;
        final Thread threadToInterrupt;
        final ExecutorService execToShutdown;
        final SensorBridgeManager sensorsToStop;
        final MicrophoneBridgeManager micToStop;

        synchronized (lifecycleLock) {
            stopRequested = true;
            isRunning = false;
            stoppedOnce = true;
            state = LifecycleState.STOPPED;

            pending = inFlightStartup;
            inFlightStartup = null;

            sockToClose = serverSocket;
            serverSocket = null;

            threadToInterrupt = acceptThread;
            acceptThread = null;

            execToShutdown = executor;
            sensorsToStop = sensorManager;
            micToStop = microphoneManager;
        }

        if (pending != null) {
            pending.fail("Bridge stopped during startup");
        }
        sensorsToStop.stopAll();
        if (micToStop != null) {
            micToStop.stopAll();
        }
        safeClose(sockToClose);
        if (threadToInterrupt != null) {
            threadToInterrupt.interrupt();
        }
        if (execToShutdown != null) {
            execToShutdown.shutdownNow();
        }
    }

    private void handleClient(Socket socket) {
        try (
            InputStream input = new BufferedInputStream(socket.getInputStream(), 8192);
            OutputStream writer = socket.getOutputStream()
        ) {
            ByteArrayOutputStream lineBuf = new ByteArrayOutputStream();
            boolean exceededLimit = false;
            int b;
            while ((b = input.read()) != -1) {
                if (b == '\n') {
                    break;
                }
                if (b == '\r') {
                    continue;
                }
                if (lineBuf.size() >= MAX_PAYLOAD_BYTES) {
                    exceededLimit = true;
                    break;
                }
                lineBuf.write(b);
            }

            if (exceededLimit) {
                writeResponse(writer, buildErrorResponse("unknown", "", "", "ERROR", "PAYLOAD_TOO_LARGE",
                        "Request payload exceeded MAX_PAYLOAD_BYTES (" + MAX_PAYLOAD_BYTES + ")"));
                return;
            }

            String line = lineBuf.toString(StandardCharsets.UTF_8.name()).trim();
            if (line.isEmpty()) {
                return;
            }

            JSONObject request;
            try {
                request = new JSONObject(line);
            } catch (JSONException je) {
                writeResponse(writer, buildErrorResponse("unknown", "", "", "ERROR", "INVALID_JSON",
                        "Malformed JSON request: " + je.getMessage()));
                return;
            }

            JSONObject response = processRequest(request);
            writeResponse(writer, response);
        } catch (Exception e) {
            Log.e(TAG, "Error handling client connection: " + e.getMessage());
        } finally {
            try {
                socket.close();
            } catch (Exception ignored) {}
        }
    }

    private void writeResponse(OutputStream writer, JSONObject response) throws Exception {
        byte[] outBytes = (response.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        if (outBytes.length > MAX_PAYLOAD_BYTES) {
            JSONObject fallbackErr = buildErrorResponse(
                    response.optString("operation_id", "unknown"),
                    response.optString("capability", ""),
                    response.optString("operation", ""),
                    "ERROR",
                    "PAYLOAD_TOO_LARGE",
                    "Response exceeded MAX_PAYLOAD_BYTES (" + MAX_PAYLOAD_BYTES + ")"
            );
            outBytes = (fallbackErr.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        }
        writer.write(outBytes);
        writer.flush();
    }

    private JSONObject buildErrorResponse(
            String opId,
            String capability,
            String operation,
            String status,
            String errorCode,
            String errorMessage
    ) {
        JSONObject err = new JSONObject();
        try {
            err.put("version", PROTOCOL_VERSION);
            err.put("protocol_version", PROTOCOL_VERSION);
            err.put("operation_id", opId != null && !opId.isEmpty() ? opId : "unknown");
            err.put("request_id", opId != null && !opId.isEmpty() ? opId : "unknown");
            err.put("capability", capability != null ? capability : "");
            err.put("operation", operation != null ? operation : "");
            err.put("success", false);
            err.put("status", status);
            err.put("data", new JSONObject());
            err.put("error_code", errorCode);
            err.put("error_message", errorMessage);
        } catch (JSONException ignored) {}
        return err;
    }

    public JSONObject processRequest(JSONObject request) {
        JSONObject response = new JSONObject();
        String opId = request.optString("operation_id", request.optString("request_id", "unknown"));
        String capability = request.optString("capability", "");
        String operation = request.optString("operation", "");
        int version = request.has("version") ? request.optInt("version", 0) : request.optInt("protocol_version", 0);
        JSONObject params = request.optJSONObject("parameters");
        if (params == null) {
            params = request.optJSONObject("arguments");
        }
        if (params == null) {
            params = new JSONObject();
        }

        try {
            response.put("version", PROTOCOL_VERSION);
            response.put("protocol_version", PROTOCOL_VERSION);
            response.put("operation_id", opId);
            response.put("request_id", opId);
            response.put("capability", capability);
            response.put("operation", operation);

            // Protocol Version Check
            if (version != PROTOCOL_VERSION) {
                response.put("success", false);
                response.put("status", "ERROR");
                response.put("data", new JSONObject());
                response.put("error_code", "VERSION_MISMATCH");
                response.put("error_message", "Expected protocol v" + PROTOCOL_VERSION + ", received v" + version);
                return response;
            }

            if (capability.isEmpty() || operation.isEmpty()) {
                return buildErrorResponse(opId, capability, operation, "ERROR", "INVALID_REQUEST",
                        "Request requires non-empty 'capability' and 'operation'");
            }

            // Dispatch Sensor Operations (D3)
            if (operation.startsWith("SENSOR_") || "sensors".equalsIgnoreCase(capability) || capability.toLowerCase().startsWith("sensor.")) {
                JSONObject sensorOut;
                if ("SENSOR_DISCOVER".equals(operation)) {
                    JSONArray targetTypes = params.optJSONArray("sensor_types");
                    sensorOut = sensorManager.discoverSensors(targetTypes);
                } else if ("SENSOR_START".equals(operation)) {
                    sensorOut = sensorManager.startSession(params);
                } else if ("SENSOR_READ".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    int maxSamples = params.optInt("max_samples", 10);
                    sensorOut = sensorManager.readSession(sid, maxSamples);
                } else if ("SENSOR_STOP".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    String reason = params.optString("reason", "Requested stop");
                    sensorOut = sensorManager.stopSession(sid, reason);
                } else if ("SENSOR_STATUS".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    sensorOut = sensorManager.getStatus(sid);
                } else {
                    return buildErrorResponse(opId, capability, operation, "ERROR", "UNKNOWN_OPERATION",
                            "Unknown sensor operation: " + operation);
                }
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", sensorOut);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
                return response;
            }

            // Dispatch Native Microphone Capture Operations (D4-E02)
            if ("microphone".equalsIgnoreCase(capability)
                    || "START_RECORDING".equals(operation)
                    || "RECORDING_STATUS".equals(operation)
                    || "STOP_RECORDING".equals(operation)
                    || "RECORDING_DISCOVER".equals(operation)
                    || "RECORDING_READ".equals(operation)
                    || "RECORDING_CLEANUP".equals(operation)) {
                JSONObject micOut;
                if ("RECORDING_DISCOVER".equals(operation) || "RECORDING_STATUS".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    micOut = microphoneManager.getRecordingStatus(sid);
                } else if ("START_RECORDING".equals(operation)) {
                    micOut = microphoneManager.startRecording(params);
                } else if ("STOP_RECORDING".equals(operation)) {
                    String reason = params.optString("reason", "Requested stop");
                    micOut = microphoneManager.stopRecording(reason);
                } else if ("RECORDING_READ".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    long offset = params.optLong("offset", 0L);
                    int length = params.optInt("length", MicrophoneBridgeManager.MAX_TRANSFER_CHUNK_BYTES);
                    micOut = microphoneManager.readRecordingChunk(sid, offset, length);
                } else if ("RECORDING_CLEANUP".equals(operation)) {
                    String sid = params.optString("session_id", "");
                    micOut = microphoneManager.deleteRecording(sid);
                } else {
                    return buildErrorResponse(opId, capability, operation, "ERROR", "UNKNOWN_OPERATION",
                            "Unknown microphone operation: " + operation);
                }
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", micOut);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
                return response;
            }

            // Dispatch Live Media / Call Operations (E07)
            if ("live_call".equalsIgnoreCase(capability)
                    || "live_media".equalsIgnoreCase(capability)
                    || "media".equalsIgnoreCase(capability)
                    || "START_LIVE_CALL".equals(operation)
                    || "STOP_LIVE_CALL".equals(operation)
                    || "GET_LIVE_CALL_STATUS".equals(operation)
                    || "GET_LIVE_CALL_CAPABILITIES".equals(operation)
                    || "UPDATE_LIVE_CALL".equals(operation)) {
                JSONObject callOut;
                if ("GET_LIVE_CALL_CAPABILITIES".equals(operation)) {
                    callOut = liveMediaManager.getCapabilities();
                } else if ("START_LIVE_CALL".equals(operation)) {
                    callOut = liveMediaManager.startLiveCall(params);
                } else if ("STOP_LIVE_CALL".equals(operation)) {
                    String reason = params.optString("reason", "Requested stop");
                    callOut = liveMediaManager.stopLiveCall(reason);
                } else if ("GET_LIVE_CALL_STATUS".equals(operation)) {
                    callOut = liveMediaManager.getLiveCallStatus();
                } else if ("UPDATE_LIVE_CALL".equals(operation)) {
                    callOut = liveMediaManager.updateLiveCall(params);
                } else {
                    return buildErrorResponse(opId, capability, operation, "ERROR", "UNKNOWN_OPERATION",
                            "Unknown live call operation: " + operation);
                }
                boolean ok = callOut.optBoolean("success", true);
                response.put("success", ok);
                response.put("status", ok ? "READY" : "ERROR");
                response.put("data", callOut);
                response.put("error_code", ok ? JSONObject.NULL : callOut.optString("error_code", "CALL_ERROR"));
                response.put("error_message", ok ? JSONObject.NULL : callOut.optString("error", "Operation failed"));
                return response;
            }

            // Dispatch Battery / Thermal / Ping Operations (D2)
            if ("battery".equalsIgnoreCase(capability)) {
                if ("get_status".equalsIgnoreCase(operation)) {
                    JSONObject batData = batteryManager.getBatteryStatus();
                    response.put("success", true);
                    response.put("status", "READY");
                    response.put("data", batData);
                    response.put("error_code", JSONObject.NULL);
                    response.put("error_message", JSONObject.NULL);
                } else {
                    return buildErrorResponse(opId, capability, operation, "ERROR", "UNKNOWN_OPERATION",
                            "Unknown operation: " + operation);
                }
            } else if ("thermals".equalsIgnoreCase(capability)) {
                if ("get_status".equalsIgnoreCase(operation)) {
                    JSONObject thermalData = thermalManager.getThermalStatus();
                    response.put("success", true);
                    response.put("status", "READY");
                    response.put("data", thermalData);
                    response.put("error_code", JSONObject.NULL);
                    response.put("error_message", JSONObject.NULL);
                } else {
                    return buildErrorResponse(opId, capability, operation, "ERROR", "UNKNOWN_OPERATION",
                            "Unknown operation: " + operation);
                }
            } else if ("GET_DIAGNOSTICS".equalsIgnoreCase(operation) || "BRIDGE_DIAGNOSTICS".equalsIgnoreCase(operation)) {
                JSONObject diag = buildDiagnosticsPayload();
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", diag);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
            } else if ("TRIGGER_RECOVERY".equalsIgnoreCase(operation)) {
                JSONObject recResult;
                String target = params.optString("target", "ALL").toUpperCase();
                String triggerSource = params.optString("trigger_source", "IPC_REQUEST");
                if (recoveryCoordinator != null) {
                    if ("TERMUX".equals(target)) {
                        recResult = recoveryCoordinator.triggerTermuxRecovery(triggerSource);
                    } else if ("NODE_API".equals(target) || "NODE-API".equals(target)) {
                        recResult = recoveryCoordinator.triggerTargetedRecovery("node-api", triggerSource);
                    } else if ("SSHD".equals(target) || "SSH".equals(target)) {
                        recResult = recoveryCoordinator.triggerTargetedRecovery("sshd", triggerSource);
                    } else if ("TAILSCALE".equals(target)) {
                        recResult = recoveryCoordinator.triggerTailscaleRecovery(triggerSource);
                    } else {
                        recResult = recoveryCoordinator.triggerCombinedRecovery(triggerSource);
                    }
                } else {
                    recResult = new JSONObject();
                    recResult.put("success", false);
                    recResult.put("error", "RECOVERY_COORDINATOR_UNAVAILABLE");
                }
                boolean opSuccess = recResult.optBoolean("success", false);
                response.put("success", opSuccess);
                response.put("status", opSuccess ? "READY" : "ERROR");
                response.put("data", recResult);
                response.put("error_code", opSuccess ? JSONObject.NULL : "RECOVERY_UNVERIFIED");
                response.put("error_message", opSuccess ? JSONObject.NULL : recResult.optString("status", "Recovery unverified or failed"));
            } else if ("RECOVERY_HISTORY".equalsIgnoreCase(operation)) {
                JSONArray history = (recoveryCoordinator != null) ? recoveryCoordinator.getRecoveryHistory() : new JSONArray();
                JSONObject histData = new JSONObject();
                histData.put("events", history);
                histData.put("total_events", history.length());
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", histData);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
            } else if ("RECOVERY_CONFIG".equalsIgnoreCase(operation)) {
                JSONObject confData = new JSONObject();
                if (recoveryCoordinator != null) {
                    if (params.has("enabled")) {
                        recoveryCoordinator.setRecoveryEnabled(params.optBoolean("enabled", true));
                    }
                    if (params.has("max_retries")) {
                        recoveryCoordinator.setMaxRetryCount(params.optInt("max_retries", RecoveryCoordinator.DEFAULT_MAX_RETRY));
                    }
                    if (params.has("cooldown_sec")) {
                        recoveryCoordinator.setCooldownSec(params.optInt("cooldown_sec", RecoveryCoordinator.DEFAULT_COOLDOWN_SEC));
                    }
                    confData.put("enabled", recoveryCoordinator.isRecoveryEnabled());
                    confData.put("max_retries", recoveryCoordinator.getMaxRetryCount());
                    confData.put("cooldown_sec", recoveryCoordinator.getCooldownSec());
                }
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", confData);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
            } else if ("ping".equalsIgnoreCase(operation) || "ping".equalsIgnoreCase(capability)) {
                JSONObject pong = new JSONObject();
                pong.put("status", "pong");
                pong.put("timestamp", System.currentTimeMillis());
                response.put("success", true);
                response.put("status", "READY");
                response.put("data", pong);
                response.put("error_code", JSONObject.NULL);
                response.put("error_message", JSONObject.NULL);
            } else {
                return buildErrorResponse(opId, capability, operation, "UNAVAILABLE", "UNKNOWN_CAPABILITY",
                        "Unsupported capability: " + capability);
            }

        } catch (SecurityException se) {
            return buildErrorResponse(opId, capability, operation, "PERMISSION_DENIED", "PERMISSION_DENIED",
                    "Android security permission denied: " + se.getMessage());
        } catch (IllegalArgumentException iae) {
            return buildErrorResponse(opId, capability, operation, "ERROR", "INVALID_REQUEST", iae.getMessage());
        } catch (IllegalStateException ise) {
            return buildErrorResponse(opId, capability, operation, "UNAVAILABLE", "UNAVAILABLE_OR_NOT_FOUND", ise.getMessage());
        } catch (JSONException je) {
            return buildErrorResponse(opId, capability, operation, "ERROR", "JSON_ERROR", je.getMessage());
        }

        return response;
    }

    private JSONObject buildDiagnosticsPayload() throws JSONException {
        JSONObject payload = new JSONObject();
        if (recoveryCoordinator != null) {
            try {
                JSONObject superStates = recoveryCoordinator.evaluateAllStates();
                payload.put("states", superStates.optJSONObject("states"));
                payload.put("services", superStates.optJSONObject("services"));
                payload.put("boot_recovery", superStates.optJSONObject("boot_recovery"));
                payload.put("retry_counters", superStates.optJSONObject("retry_counters"));
            } catch (Exception e) {
                Log.w(TAG, "Error evaluating supervisor states: " + e.getMessage());
            }
        }

        if (!payload.has("states")) {
            JSONObject states = new JSONObject();
            boolean appActive = BridgeService.isProcessActive() || BridgeActivity.isActivityAlive();
            states.put("ANDROID_APP_RUNNING", appActive ? "UP" : "UNKNOWN");
            states.put("NATIVE_BRIDGE_READY", isListening() ? "UP" : "DOWN");
            states.put("SUPERVISOR_RUNNING", (recoveryCoordinator != null && recoveryCoordinator.isRunning()) ? "UP" : "DOWN");
            states.put("TERMUX_INSTALLED", "UNKNOWN");
            states.put("TERMUX_COMMAND_INTEGRATION_READY", "UNKNOWN");
            states.put("TERMUX_AVAILABLE", "UNKNOWN");
            states.put("TERMUX_SERVICES_HEALTHY", "UNKNOWN");
            states.put("TAILSCALE_INSTALLED", "UNKNOWN");
            states.put("TAILSCALE_VPN_AUTHORIZED", "UNKNOWN");
            states.put("TAILSCALE_CONNECTED", "UNKNOWN");
            states.put("PERSONALSERVER_NETWORK_REACHABLE", "UNKNOWN");
            states.put("CONTROLLER_REACHABLE", "UNKNOWN");
            payload.put("states", states);
        }

        DiagnosticStateProbe probe = this.diagnosticStateProbe;
        if (probe != null) {
            try {
                JSONObject ext = probe.probeExternalStates();
                if (ext != null) {
                    JSONObject states = payload.optJSONObject("states");
                    if (states != null) {
                        String[] keys = new String[]{
                                "ANDROID_APP_RUNNING",
                                "NATIVE_BRIDGE_READY",
                                "SUPERVISOR_RUNNING",
                                "TERMUX_INSTALLED",
                                "TERMUX_COMMAND_INTEGRATION_READY",
                                "TERMUX_AVAILABLE",
                                "TERMUX_SERVICES_HEALTHY",
                                "TAILSCALE_INSTALLED",
                                "TAILSCALE_VPN_AUTHORIZED",
                                "TAILSCALE_CONNECTED",
                                "PERSONALSERVER_NETWORK_REACHABLE",
                                "CONTROLLER_REACHABLE"
                        };
                        for (String k : keys) {
                            if (ext.has(k)) {
                                String v = ext.optString(k, "");
                                if (v != null && !v.isEmpty()) {
                                    states.put(k, v);
                                }
                            }
                        }
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "External diagnostic probe failed cleanly: " + e.getMessage());
            }
        }

        payload.put("loopback_only", true);
        payload.put("host", "127.0.0.1");
        payload.put("port", this.port);
        payload.put("requires_termux", false);
        payload.put("requires_python", false);
        payload.put("requires_tailscale", false);
        payload.put("requires_controller", false);

        JSONObject activeSessions = new JSONObject();
        activeSessions.put("microphone_recording", BridgeService.isRecordingActive());
        String micSid = BridgeService.getActiveRecordingSessionId();
        activeSessions.put("microphone_session_id", micSid != null ? micSid : JSONObject.NULL);
        payload.put("active_sessions", activeSessions);
        payload.put("timestamp", System.currentTimeMillis());
        return payload;
    }
}
