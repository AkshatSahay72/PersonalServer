package com.personalserver.bridge;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import androidx.annotation.NonNull;
import androidx.core.content.ContextCompat;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * On-Demand Live Media Bridge Manager (E07).
 * Coordinates:
 * - Camera capture via Camera2 API (front/rear camera, bounded FPS & resolution)
 * - Microphone audio streaming via AudioRecord
 * - Low-latency speaker playback via AudioTrack (two-way voice communication)
 * - MediaStreamServer loopback media transport on 127.0.0.1:8766
 * - Strict on-demand hardware resource allocation, timeout watchdog, and clean release.
 */
public class LiveMediaBridgeManager {

    private static final String TAG = "LiveMediaBridgeMgr";

    public static final int DEFAULT_WIDTH = 640;
    public static final int DEFAULT_HEIGHT = 480;
    public static final int DEFAULT_FPS = 24;
    public static final int DEFAULT_SAMPLE_RATE = 16000;
    public static final long DEFAULT_MAX_DURATION_MS = 300_000L; // 5 minutes max session
    public static final long INACTIVITY_TIMEOUT_MS = 30_000L;     // 30 seconds without heartbeat

    public interface LiveCallNotificationListener {
        void onLiveCallStateChanged(boolean active, String sessionId, String details);
    }

    private final Context context;
    private final MediaStreamServer mediaStreamServer;
    private final Object stateLock = new Object();

    // Session State
    private volatile String activeSessionId = null;
    private volatile boolean isSessionActive = false;
    private volatile long sessionStartedAt = 0L;
    private volatile long lastActivityTimestamp = 0L;
    private volatile long maxSessionDurationMs = DEFAULT_MAX_DURATION_MS;
    private volatile String activeCameraId = "0";
    private volatile int currentWidth = DEFAULT_WIDTH;
    private volatile int currentHeight = DEFAULT_HEIGHT;
    private volatile int currentFps = DEFAULT_FPS;
    private volatile boolean isMicMuted = false;
    private volatile boolean isSpeakerMuted = false;
    private volatile LiveCallNotificationListener notificationListener;

    // Camera2 components
    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;
    private ImageReader imageReader;
    private HandlerThread cameraThread;
    private Handler cameraHandler;
    private final AtomicBoolean cameraRunning = new AtomicBoolean(false);

    // AudioRecord components
    private AudioRecord audioRecord;
    private Thread audioRecordThread;
    private final AtomicBoolean audioRecording = new AtomicBoolean(false);

    // AudioTrack (Speaker) components
    private AudioTrack audioTrack;
    private final AtomicBoolean speakerActive = new AtomicBoolean(false);

    // Watchdog
    private Thread watchdogThread;
    private final AtomicBoolean watchdogRunning = new AtomicBoolean(false);

    // Metrics
    private final AtomicLong capturedFrames = new AtomicLong(0);
    private final AtomicLong capturedAudioChunks = new AtomicLong(0);
    private final AtomicLong playedSpeakerChunks = new AtomicLong(0);

    public LiveMediaBridgeManager(Context context) {
        this(context, new MediaStreamServer(MediaStreamServer.DEFAULT_MEDIA_PORT));
    }

    public LiveMediaBridgeManager(Context context, MediaStreamServer mediaStreamServer) {
        this.context = context.getApplicationContext();
        this.mediaStreamServer = mediaStreamServer;
        this.mediaStreamServer.setDownstreamAudioListener(this::onDownstreamAudioChunk);
        this.mediaStreamServer.setMediaClientEventListener(new MediaStreamServer.MediaClientEventListener() {
            @Override
            public void onClientConnected() {
                lastActivityTimestamp = System.currentTimeMillis();
            }

            @Override
            public void onClientDisconnected() {
                // If client disconnects, give 10s grace period before watchdog reclaims
            }

            @Override
            public void onHeartbeatReceived(long timestamp) {
                lastActivityTimestamp = System.currentTimeMillis();
            }
        });
    }

    public void setNotificationListener(LiveCallNotificationListener listener) {
        this.notificationListener = listener;
    }

    public boolean isSessionActive() {
        synchronized (stateLock) {
            return isSessionActive;
        }
    }

    public String getActiveSessionId() {
        synchronized (stateLock) {
            return activeSessionId;
        }
    }

    // =========================================================================
    // Capability Discovery
    // =========================================================================

    public JSONObject getCapabilities() throws JSONException {
        JSONObject out = new JSONObject();
        boolean hasCamera = context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY);
        boolean hasMic = context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_MICROPHONE);
        boolean hasFrontCam = context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_FRONT);

        boolean cameraPerm = ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
        boolean micPerm = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;

        JSONArray cameraList = new JSONArray();
        CameraManager cm = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        if (cm != null) {
            try {
                for (String cid : cm.getCameraIdList()) {
                    CameraCharacteristics cc = cm.getCameraCharacteristics(cid);
                    Integer facing = cc.get(CameraCharacteristics.LENS_FACING);
                    JSONObject cObj = new JSONObject();
                    cObj.put("camera_id", cid);
                    cObj.put("facing", facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT ? "front" : "back");
                    cameraList.put(cObj);
                }
            } catch (Exception ignored) {}
        }

        out.put("camera_supported", hasCamera);
        out.put("microphone_supported", hasMic);
        out.put("front_camera_supported", hasFrontCam);
        out.put("camera_permission_granted", cameraPerm);
        out.put("microphone_permission_granted", micPerm);
        out.put("available_cameras", cameraList);
        out.put("media_port", mediaStreamServer.getPort());
        out.put("session_active", isSessionActive());
        out.put("active_session_id", getActiveSessionId());
        return out;
    }

    // =========================================================================
    // Live Call Lifecycle: Start, Update, Stop
    // =========================================================================

    public JSONObject startLiveCall(JSONObject params) throws JSONException {
        synchronized (stateLock) {
            if (isSessionActive) {
                // Check if prior session timed out
                if (System.currentTimeMillis() - sessionStartedAt > maxSessionDurationMs) {
                    Log.i(TAG, "Reclaiming expired live session: " + activeSessionId);
                    stopLiveCallInternal("Prior session expired");
                } else {
                    JSONObject err = new JSONObject();
                    err.put("success", false);
                    err.put("error", "A live call session is already active (" + activeSessionId + ")");
                    err.put("error_code", "SESSION_ALREADY_ACTIVE");
                    err.put("session_id", activeSessionId);
                    return err;
                }
            }

            // Verify permissions
            boolean cameraPerm = ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
                    == PackageManager.PERMISSION_GRANTED;
            boolean micPerm = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED;

            if (!cameraPerm || !micPerm) {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "ANDROID_PERMISSION_DENIED: Camera and Microphone permissions are required");
                err.put("error_code", "PERMISSION_DENIED");
                err.put("camera_permission", cameraPerm ? "GRANTED" : "DENIED");
                err.put("microphone_permission", micPerm ? "GRANTED" : "DENIED");
                return err;
            }

            String sessionId = params.optString("session_id", "call-" + System.currentTimeMillis());
            activeCameraId = params.optString("camera_id", "0");
            currentWidth = params.optInt("width", DEFAULT_WIDTH);
            currentHeight = params.optInt("height", DEFAULT_HEIGHT);
            currentFps = params.optInt("fps", DEFAULT_FPS);
            maxSessionDurationMs = params.optLong("timeout_sec", 300) * 1000L;
            boolean enableAudio = params.optBoolean("audio_enabled", true);
            boolean enableSpeaker = params.optBoolean("speaker_enabled", true);

            activeSessionId = sessionId;
            isSessionActive = true;
            sessionStartedAt = System.currentTimeMillis();
            lastActivityTimestamp = sessionStartedAt;
            isMicMuted = false;
            isSpeakerMuted = false;
            capturedFrames.set(0);
            capturedAudioChunks.set(0);
            playedSpeakerChunks.set(0);

            // 1. Start loopback media stream server
            boolean serverOk = mediaStreamServer.start();
            if (!serverOk) {
                isSessionActive = false;
                activeSessionId = null;
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "Failed starting loopback MediaStreamServer on port " + mediaStreamServer.getPort());
                return err;
            }

            // 2. Initialize Camera2 capture
            initCamera(activeCameraId, currentWidth, currentHeight, currentFps);

            // 3. Initialize AudioRecord (Microphone)
            if (enableAudio) {
                initAudioRecord(DEFAULT_SAMPLE_RATE);
            }

            // 4. Initialize AudioTrack (Speaker)
            if (enableSpeaker) {
                initAudioTrack(DEFAULT_SAMPLE_RATE);
            }

            // 5. Start Watchdog Thread
            startWatchdog();

            // 6. Notify FGS notification
            if (notificationListener != null) {
                notificationListener.onLiveCallStateChanged(true, sessionId, "Camera & Two-Way Audio active");
            }

            Log.i(TAG, "Live call started: " + sessionId + " on media port " + mediaStreamServer.getPort());

            JSONObject res = new JSONObject();
            res.put("success", true);
            res.put("status", "ACTIVE");
            res.put("session_id", sessionId);
            res.put("media_port", mediaStreamServer.getPort());
            res.put("camera_id", activeCameraId);
            res.put("resolution", currentWidth + "x" + currentHeight);
            res.put("fps", currentFps);
            res.put("audio_enabled", enableAudio);
            res.put("speaker_enabled", enableSpeaker);
            res.put("sample_rate", DEFAULT_SAMPLE_RATE);
            res.put("started_at", sessionStartedAt);
            res.put("timeout_sec", maxSessionDurationMs / 1000L);
            return res;
        }
    }

    public JSONObject stopLiveCall(String reason) throws JSONException {
        synchronized (stateLock) {
            return stopLiveCallInternal(reason);
        }
    }

    private JSONObject stopLiveCallInternal(String reason) throws JSONException {
        if (!isSessionActive) {
            JSONObject res = new JSONObject();
            res.put("success", true);
            res.put("status", "IDLE");
            res.put("message", "No active live call session");
            return res;
        }

        String sid = activeSessionId;
        Log.i(TAG, "Stopping live call: " + sid + " (Reason: " + reason + ")");

        // Release hardware resources immediately
        releaseCamera();
        releaseAudioRecord();
        releaseAudioTrack();
        stopWatchdog();
        mediaStreamServer.stop();

        isSessionActive = false;
        activeSessionId = null;

        if (notificationListener != null) {
            notificationListener.onLiveCallStateChanged(false, sid, "Live call ended");
        }

        JSONObject res = new JSONObject();
        res.put("success", true);
        res.put("status", "STOPPED");
        res.put("session_id", sid);
        res.put("reason", reason);
        res.put("captured_frames", capturedFrames.get());
        res.put("audio_chunks_sent", capturedAudioChunks.get());
        res.put("speaker_chunks_played", playedSpeakerChunks.get());
        return res;
    }

    public JSONObject getLiveCallStatus() throws JSONException {
        synchronized (stateLock) {
            JSONObject res = new JSONObject();
            res.put("session_active", isSessionActive);
            res.put("session_id", activeSessionId != null ? activeSessionId : JSONObject.NULL);
            if (isSessionActive) {
                long durationSec = (System.currentTimeMillis() - sessionStartedAt) / 1000L;
                res.put("duration_sec", durationSec);
                res.put("camera_id", activeCameraId);
                res.put("resolution", currentWidth + "x" + currentHeight);
                res.put("fps", currentFps);
                res.put("mic_muted", isMicMuted);
                res.put("speaker_muted", isSpeakerMuted);
                res.put("captured_frames", capturedFrames.get());
                res.put("audio_chunks_sent", capturedAudioChunks.get());
                res.put("speaker_chunks_played", playedSpeakerChunks.get());
                res.put("media_clients_connected", mediaStreamServer.getActiveClientCount());
            }
            return res;
        }
    }

    public JSONObject updateLiveCall(JSONObject params) throws JSONException {
        synchronized (stateLock) {
            if (!isSessionActive) {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "No active live call session to update");
                return err;
            }

            if (params.has("mute_mic")) {
                isMicMuted = params.getBoolean("mute_mic");
            }
            if (params.has("mute_speaker")) {
                isSpeakerMuted = params.getBoolean("mute_speaker");
            }
            if (params.has("camera_id")) {
                String newCam = params.getString("camera_id");
                if (!newCam.equals(activeCameraId)) {
                    activeCameraId = newCam;
                    releaseCamera();
                    initCamera(activeCameraId, currentWidth, currentHeight, currentFps);
                }
            }

            JSONObject res = new JSONObject();
            res.put("success", true);
            res.put("session_id", activeSessionId);
            res.put("camera_id", activeCameraId);
            res.put("mic_muted", isMicMuted);
            res.put("speaker_muted", isSpeakerMuted);
            return res;
        }
    }

    // =========================================================================
    // Camera2 Capture Implementation
    // =========================================================================

    @SuppressLint("MissingPermission")
    private void initCamera(String cameraId, int width, int height, int fps) {
        CameraManager cm = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        if (cm == null) {
            Log.w(TAG, "CameraManager not available; running in simulated mode");
            return;
        }

        try {
            cameraThread = new HandlerThread("Camera2BackgroundThread");
            cameraThread.start();
            cameraHandler = new Handler(cameraThread.getLooper());

            imageReader = ImageReader.newInstance(width, height, ImageFormat.JPEG, 2);
            imageReader.setOnImageAvailableListener(reader -> {
                Image img = null;
                try {
                    img = reader.acquireLatestImage();
                    if (img != null) {
                        ByteBuffer buffer = img.getPlanes()[0].getBuffer();
                        byte[] bytes = new byte[buffer.remaining()];
                        buffer.get(bytes);

                        capturedFrames.incrementAndGet();
                        long now = System.currentTimeMillis();
                        lastActivityTimestamp = now;
                        mediaStreamServer.broadcastVideoFrame(now, width, height, MediaStreamServer.CODEC_JPEG, bytes);
                    }
                } catch (Exception e) {
                    Log.d(TAG, "Frame processing drop: " + e.getMessage());
                } finally {
                    if (img != null) {
                        img.close();
                    }
                }
            }, cameraHandler);

            cm.openCamera(cameraId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(@NonNull CameraDevice camera) {
                    cameraDevice = camera;
                    cameraRunning.set(true);
                    startCameraPreviewSession();
                }

                @Override
                public void onDisconnected(@NonNull CameraDevice camera) {
                    Log.w(TAG, "Camera disconnected");
                    camera.close();
                    cameraDevice = null;
                    cameraRunning.set(false);
                }

                @Override
                public void onError(@NonNull CameraDevice camera, int error) {
                    Log.e(TAG, "Camera error: " + error);
                    camera.close();
                    cameraDevice = null;
                    cameraRunning.set(false);
                }
            }, cameraHandler);
        } catch (Exception e) {
            Log.e(TAG, "Failed initializing Camera2: " + e.getMessage());
        }
    }

    private void startCameraPreviewSession() {
        if (cameraDevice == null || imageReader == null) {
            return;
        }
        try {
            CaptureRequest.Builder builder = cameraDevice.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
            builder.addTarget(imageReader.getSurface());
            builder.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);

            cameraDevice.createCaptureSession(
                    Collections.singletonList(imageReader.getSurface()),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(@NonNull CameraCaptureSession session) {
                            captureSession = session;
                            try {
                                session.setRepeatingRequest(builder.build(), null, cameraHandler);
                            } catch (CameraAccessException e) {
                                Log.e(TAG, "Failed starting repeating capture request: " + e.getMessage());
                            }
                        }

                        @Override
                        public void onConfigureFailed(@NonNull CameraCaptureSession session) {
                            Log.e(TAG, "Camera capture session configuration failed");
                        }
                    },
                    cameraHandler
            );
        } catch (Exception e) {
            Log.e(TAG, "Error in startCameraPreviewSession: " + e.getMessage());
        }
    }

    private void releaseCamera() {
        cameraRunning.set(false);
        try {
            if (captureSession != null) {
                captureSession.stopRepeating();
                captureSession.close();
                captureSession = null;
            }
        } catch (Exception ignored) {}

        try {
            if (cameraDevice != null) {
                cameraDevice.close();
                cameraDevice = null;
            }
        } catch (Exception ignored) {}

        try {
            if (imageReader != null) {
                imageReader.close();
                imageReader = null;
            }
        } catch (Exception ignored) {}

        if (cameraThread != null) {
            cameraThread.quitSafely();
            try {
                cameraThread.join(1000);
            } catch (Exception ignored) {}
            cameraThread = null;
            cameraHandler = null;
        }
    }

    // =========================================================================
    // AudioRecord (Microphone) Implementation
    // =========================================================================

    @SuppressLint("MissingPermission")
    private void initAudioRecord(int sampleRate) {
        try {
            int channelConfig = AudioFormat.CHANNEL_IN_MONO;
            int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
            int minBuf = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat);
            int bufSize = Math.max(minBuf, sampleRate / 10 * 2); // ~100ms buffer

            audioRecord = new AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    sampleRate,
                    channelConfig,
                    audioFormat,
                    bufSize
            );

            if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "AudioRecord state not initialized, fallback to MIC");
                audioRecord.release();
                audioRecord = new AudioRecord(
                        MediaRecorder.AudioSource.MIC,
                        sampleRate,
                        channelConfig,
                        audioFormat,
                        bufSize
                );
            }

            if (audioRecord.getState() == AudioRecord.STATE_INITIALIZED) {
                audioRecord.startRecording();
                audioRecording.set(true);

                audioRecordThread = new Thread(() -> {
                    byte[] pcmBuf = new byte[bufSize / 2];
                    while (audioRecording.get() && isSessionActive) {
                        int read = audioRecord.read(pcmBuf, 0, pcmBuf.length);
                        if (read > 0) {
                            capturedAudioChunks.incrementAndGet();
                            if (!isMicMuted) {
                                long now = System.currentTimeMillis();
                                mediaStreamServer.broadcastAudioUpstream(now, sampleRate, 1, pcmBuf, 0, read);
                            }
                        }
                    }
                }, "AudioRecordStreamingThread");
                audioRecordThread.setDaemon(true);
                audioRecordThread.start();
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed initializing AudioRecord: " + e.getMessage());
        }
    }

    private void releaseAudioRecord() {
        audioRecording.set(false);
        if (audioRecordThread != null) {
            audioRecordThread.interrupt();
            audioRecordThread = null;
        }
        if (audioRecord != null) {
            try {
                if (audioRecord.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    audioRecord.stop();
                }
                audioRecord.release();
            } catch (Exception ignored) {}
            audioRecord = null;
        }
    }

    // =========================================================================
    // AudioTrack (Speaker) Implementation
    // =========================================================================

    private void initAudioTrack(int sampleRate) {
        try {
            int channelConfig = AudioFormat.CHANNEL_OUT_MONO;
            int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
            int minBuf = AudioTrack.getMinBufferSize(sampleRate, channelConfig, audioFormat);
            int bufSize = Math.max(minBuf * 2, sampleRate / 10 * 2);

            AudioAttributes attrs = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build();

            AudioFormat format = new AudioFormat.Builder()
                    .setSampleRate(sampleRate)
                    .setChannelMask(channelConfig)
                    .setEncoding(audioFormat)
                    .build();

            audioTrack = new AudioTrack(
                    attrs,
                    format,
                    bufSize,
                    AudioTrack.MODE_STREAM,
                    AudioManager.AUDIO_SESSION_ID_GENERATE
            );

            if (audioTrack.getState() == AudioTrack.STATE_INITIALIZED) {
                audioTrack.play();
                speakerActive.set(true);
                Log.i(TAG, "AudioTrack initialized for speaker playback");
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed initializing AudioTrack: " + e.getMessage());
        }
    }

    private void onDownstreamAudioChunk(int sampleRate, int channels, byte[] pcmData) {
        if (!speakerActive.get() || isSpeakerMuted || audioTrack == null || pcmData == null || pcmData.length == 0) {
            return;
        }
        try {
            playedSpeakerChunks.incrementAndGet();
            lastActivityTimestamp = System.currentTimeMillis();
            audioTrack.write(pcmData, 0, pcmData.length);
        } catch (Exception e) {
            Log.d(TAG, "Speaker write error: " + e.getMessage());
        }
    }

    private void releaseAudioTrack() {
        speakerActive.set(false);
        if (audioTrack != null) {
            try {
                audioTrack.stop();
                audioTrack.release();
            } catch (Exception ignored) {}
            audioTrack = null;
        }
    }

    // =========================================================================
    // Watchdog
    // =========================================================================

    private void startWatchdog() {
        watchdogRunning.set(true);
        watchdogThread = new Thread(() -> {
            while (watchdogRunning.get() && isSessionActive) {
                try {
                    Thread.sleep(2000);
                    long now = System.currentTimeMillis();

                    // Max duration check
                    if (now - sessionStartedAt > maxSessionDurationMs) {
                        Log.i(TAG, "Watchdog triggered: max session duration reached (" + (maxSessionDurationMs / 1000) + "s)");
                        synchronized (stateLock) {
                            stopLiveCallInternal("Max session duration reached");
                        }
                        break;
                    }

                    // Inactivity timeout: if clients were connected and then all disconnected for >30s
                    if (mediaStreamServer.getActiveClientCount() == 0 && (now - lastActivityTimestamp > INACTIVITY_TIMEOUT_MS)) {
                        Log.i(TAG, "Watchdog triggered: media client inactivity timeout");
                        synchronized (stateLock) {
                            stopLiveCallInternal("Media client inactivity timeout");
                        }
                        break;
                    }
                } catch (InterruptedException ignored) {
                    break;
                } catch (Exception e) {
                    Log.w(TAG, "Watchdog loop error: " + e.getMessage());
                }
            }
        }, "LiveMediaWatchdog");
        watchdogThread.setDaemon(true);
        watchdogThread.start();
    }

    private void stopWatchdog() {
        watchdogRunning.set(false);
        if (watchdogThread != null) {
            watchdogThread.interrupt();
            watchdogThread = null;
        }
    }
}
