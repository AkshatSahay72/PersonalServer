package com.personalserver.bridge;

import android.content.Context;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Local IPC server hosting the PersonalServer Protocol v1.
 * Binds strictly to 127.0.0.1 (loopback) to prevent network exposure.
 */
public class LocalBridgeServer {

    private static final String TAG = "LocalBridgeServer";
    public static final int DEFAULT_PORT = 8765;
    public static final int PROTOCOL_VERSION = 1;
    public static final int MAX_PAYLOAD_BYTES = 65536;

    private final Context context;
    private final int port;
    private final BatteryBridgeManager batteryManager;
    private final ThermalBridgeManager thermalManager;
    private final SensorBridgeManager sensorManager;
    private final ExecutorService executor;
    private ServerSocket serverSocket;
    private volatile boolean isRunning = false;

    public LocalBridgeServer(Context context, int port) {
        this.context = context.getApplicationContext();
        this.port = port;
        this.batteryManager = new BatteryBridgeManager(this.context);
        this.thermalManager = new ThermalBridgeManager(this.context);
        this.sensorManager = new SensorBridgeManager(this.context);
        this.executor = Executors.newCachedThreadPool();
    }

    public boolean isListening() {
        return isRunning && serverSocket != null && !serverSocket.isClosed();
    }

    public synchronized void start() {
        if (isRunning) return;
        isRunning = true;

        executor.execute(() -> {
            try {
                // Strict loopback binding: 127.0.0.1 only, with SO_REUSEADDR for fast service restarts
                serverSocket = new ServerSocket();
                serverSocket.setReuseAddress(true);
                serverSocket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 16);
                Log.i(TAG, "Bridge Server listening on 127.0.0.1:" + port);

                while (isRunning && !serverSocket.isClosed()) {
                    Socket client = null;
                    try {
                        client = serverSocket.accept();
                        final Socket accepted = client;
                        accepted.setSoTimeout(5000);
                        executor.execute(() -> handleClient(accepted));
                    } catch (Exception acceptErr) {
                        if (client != null) {
                            try {
                                client.close();
                            } catch (Exception ignored) {}
                        }
                        if (!isRunning || serverSocket == null || serverSocket.isClosed()) {
                            break;
                        }
                        Log.w(TAG, "Transient client accept/setup error (continuing): " + acceptErr.getMessage());
                    }
                }
            } catch (Exception e) {
                if (isRunning) {
                    Log.e(TAG, "Server socket fatal error: " + e.getMessage());
                }
            } finally {
                isRunning = false;
                try {
                    if (serverSocket != null && !serverSocket.isClosed()) {
                        serverSocket.close();
                    }
                } catch (Exception ignored) {}
            }
        });
    }

    public synchronized void stop() {
        isRunning = false;
        sensorManager.stopAll();
        try {
            if (serverSocket != null && !serverSocket.isClosed()) {
                serverSocket.close();
            }
        } catch (Exception ignored) {}
        executor.shutdownNow();
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
}
