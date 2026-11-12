package com.personalserver.bridge;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.util.Base64;
import android.util.Log;
import androidx.core.content.ContextCompat;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

/**
 * Native Android microphone capture manager (D4-E02).
 * Uses Android AudioRecord to capture bounded, user-authorized mono PCM WAV audio
 * into an app-private cache directory with strict RECORD_AUDIO runtime permission gating,
 * SHA-256 verification, chunked loopback IPC retrieval, and deterministic cleanup.
 */
public class MicrophoneBridgeManager {

    private static final String TAG = "MicrophoneBridgeMgr";

    public static final int DEFAULT_SAMPLE_RATE_HZ = 16000;
    public static final int CHANNELS = 1;
    public static final int BITS_PER_SAMPLE = 16;
    public static final int MIN_DURATION_SEC = 1;
    public static final int MAX_DURATION_SEC = 60;
    public static final int DEFAULT_DURATION_SEC = 15;
    public static final long MAX_FILE_BYTES = 8L * 1024L * 1024L; // 8 MB hard limit
    public static final int MAX_TRANSFER_CHUNK_BYTES = 24576;     // 24 KB -> <= 32 KB Base64 (< 64 KB IPC frame)
    public static final long TEMP_TTL_MS = 10L * 60L * 1000L;     // 10 min temporary retention max
    public static final int MAX_RETAINED_RECORDINGS = 3;

    private static final int WAV_HEADER_BYTES = 44;
    private static final Set<Integer> SUPPORTED_SAMPLE_RATES = new HashSet<>(Arrays.asList(16000, 22050, 44100));
    private static final Pattern SAFE_SESSION_ID = Pattern.compile("^[A-Za-z0-9_-]{4,64}$");

    public interface RecordingNotificationListener {
        void onRecordingStateChanged(boolean active, String sessionId, int maxDurationSec);
    }

    public interface AudioRecorderHandle {
        boolean isInitialized();
        boolean startRecording();
        int read(byte[] buffer, int offset, int size);
        void stopAndRelease();
    }

    private static final class AndroidAudioRecorderHandle implements AudioRecorderHandle {
        private final AudioRecord audioRecord;
        private boolean released = false;

        AndroidAudioRecorderHandle(AudioRecord audioRecord) {
            this.audioRecord = audioRecord;
        }

        @Override
        public boolean isInitialized() {
            return audioRecord != null && audioRecord.getState() == AudioRecord.STATE_INITIALIZED;
        }

        @Override
        public boolean startRecording() {
            if (!isInitialized()) {
                return false;
            }
            audioRecord.startRecording();
            return audioRecord.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING;
        }

        @Override
        public int read(byte[] buffer, int offset, int size) {
            if (released || audioRecord == null) {
                return -1;
            }
            return audioRecord.read(buffer, offset, size);
        }

        @Override
        public synchronized void stopAndRelease() {
            if (released || audioRecord == null) {
                return;
            }
            released = true;
            try {
                if (audioRecord.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    audioRecord.stop();
                }
            } catch (Exception ignored) {}
            try {
                audioRecord.release();
            } catch (Exception ignored) {}
        }
    }

    private static final class RecordingRecord {
        final String sessionId;
        final File file;
        final int sampleRateHz;
        final int maxDurationSec;
        final long maxDataBytes;
        final long startedAtMs;
        final long deadlineMs;

        volatile String state; // "RECORDING", "COMPLETED", "ERROR"
        volatile long stoppedAtMs = 0L;
        volatile long expiresAtMs = 0L;
        volatile long dataBytesWritten = 0L;
        volatile long fileSizeBytes = 0L;
        volatile double durationSec = 0.0;
        volatile String sha256 = null;
        volatile String stopReason = null;
        volatile String errorMessage = null;
        volatile boolean transferred = false;

        volatile boolean stopRequested = false;
        AudioRecorderHandle recorderHandle;
        Thread workerThread;

        RecordingRecord(
                String sessionId,
                File file,
                int sampleRateHz,
                int maxDurationSec,
                long maxDataBytes,
                long startedAtMs
        ) {
            this.sessionId = sessionId;
            this.file = file;
            this.sampleRateHz = sampleRateHz;
            this.maxDurationSec = maxDurationSec;
            this.maxDataBytes = maxDataBytes;
            this.startedAtMs = startedAtMs;
            this.deadlineMs = startedAtMs + (maxDurationSec * 1000L);
            this.state = "RECORDING";
        }

        JSONObject toJson() throws JSONException {
            JSONObject obj = new JSONObject();
            long now = System.currentTimeMillis();
            obj.put("session_id", sessionId);
            obj.put("state", state);
            obj.put("mime_type", "audio/wav");
            obj.put("file_name", file.getName());
            obj.put("sample_rate_hz", sampleRateHz);
            obj.put("channels", CHANNELS);
            obj.put("bits_per_sample", BITS_PER_SAMPLE);
            obj.put("max_duration_sec", maxDurationSec);
            obj.put("max_file_bytes", MAX_FILE_BYTES);
            obj.put("started_at", startedAtMs / 1000.0);
            obj.put("stopped_at", stoppedAtMs > 0 ? stoppedAtMs / 1000.0 : JSONObject.NULL);
            if ("RECORDING".equals(state)) {
                double elapsed = Math.max(0.0, (now - startedAtMs) / 1000.0);
                double remaining = Math.max(0.0, (deadlineMs - now) / 1000.0);
                double approxDur = dataBytesWritten / (double) (sampleRateHz * CHANNELS * (BITS_PER_SAMPLE / 8));
                obj.put("elapsed_sec", Math.round(elapsed * 100.0) / 100.0);
                obj.put("remaining_sec", Math.round(remaining * 100.0) / 100.0);
                obj.put("bytes_written", WAV_HEADER_BYTES + dataBytesWritten);
                obj.put("duration_sec", Math.round(approxDur * 1000.0) / 1000.0);
                obj.put("available_for_transfer", false);
            } else {
                obj.put("elapsed_sec", durationSec);
                obj.put("remaining_sec", 0.0);
                obj.put("bytes_written", fileSizeBytes);
                obj.put("file_size_bytes", fileSizeBytes);
                obj.put("duration_sec", durationSec);
                obj.put("sha256", sha256 != null ? sha256 : JSONObject.NULL);
                obj.put("stop_reason", stopReason != null ? stopReason : JSONObject.NULL);
                obj.put("error", errorMessage != null ? errorMessage : JSONObject.NULL);
                obj.put("available_for_transfer", "COMPLETED".equals(state) && file.exists() && fileSizeBytes > WAV_HEADER_BYTES);
                obj.put("transferred", transferred);
                obj.put("expires_at", expiresAtMs > 0 ? expiresAtMs / 1000.0 : JSONObject.NULL);
            }
            return obj;
        }
    }

    private final Context context;
    private final File recordingsDir;
    private final Object stateLock = new Object();
    private final Map<String, RecordingRecord> completedRecordings = new ConcurrentHashMap<>();

    private volatile RecordingRecord activeRecording = null;
    private volatile String latestCompletedId = null;
    private volatile RecordingNotificationListener notificationListener = null;

    public MicrophoneBridgeManager(Context context) {
        this.context = context.getApplicationContext();
        this.recordingsDir = new File(this.context.getCacheDir(), "recordings");
        if (!this.recordingsDir.exists()) {
            this.recordingsDir.mkdirs();
        }
        sweepOrphanedFiles();
    }

    public void setRecordingNotificationListener(RecordingNotificationListener listener) {
        this.notificationListener = listener;
    }

    protected boolean hasMicrophoneFeature() {
        PackageManager pm = context.getPackageManager();
        return pm != null && pm.hasSystemFeature(PackageManager.FEATURE_MICROPHONE);
    }

    protected boolean hasRecordAudioPermission() {
        return ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
    }

    @SuppressLint("MissingPermission")
    protected AudioRecorderHandle createAudioRecorder(int sampleRateHz, int bufferBytes) {
        AudioRecord record = new AudioRecord(
                MediaRecorder.AudioSource.MIC,
                sampleRateHz,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes
        );
        return new AndroidAudioRecorderHandle(record);
    }

    public JSONObject startRecording(JSONObject params) throws JSONException {
        cleanupExpired();

        boolean authorized = params != null && params.optBoolean("authorized", false);
        if (!authorized) {
            throw new SecurityException("Explicit user authorization ('authorized': true) is required before microphone capture");
        }

        if (!hasMicrophoneFeature()) {
            throw new IllegalStateException("Device reports no microphone hardware");
        }

        if (!hasRecordAudioPermission()) {
            throw new SecurityException("RECORD_AUDIO permission not granted on device");
        }

        int reqDuration = params.optInt("duration_sec", DEFAULT_DURATION_SEC);
        if (reqDuration < MIN_DURATION_SEC || reqDuration > MAX_DURATION_SEC) {
            throw new IllegalArgumentException(
                    "duration_sec (" + reqDuration + ") must be between " + MIN_DURATION_SEC + " and " + MAX_DURATION_SEC + " seconds"
            );
        }

        int sampleRateHz = params.optInt("sample_rate_hz", DEFAULT_SAMPLE_RATE_HZ);
        if (!SUPPORTED_SAMPLE_RATES.contains(sampleRateHz)) {
            throw new IllegalArgumentException(
                    "Unsupported sample_rate_hz: " + sampleRateHz + " (supported: 16000, 22050, 44100)"
            );
        }

        String requestedSid = params.optString("session_id", "").trim();
        String sessionId;
        if (!requestedSid.isEmpty()) {
            if (!SAFE_SESSION_ID.matcher(requestedSid).matches()) {
                throw new IllegalArgumentException("Invalid session_id format");
            }
            sessionId = requestedSid;
        } else {
            sessionId = "mic-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        }

        int minBuf = AudioRecord.getMinBufferSize(
                sampleRateHz,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
        );
        if (minBuf <= 0) {
            minBuf = 4096;
        }
        final int readBufferBytes = Math.max(minBuf, 4096);
        final long maxDataBytes = Math.max(0L, MAX_FILE_BYTES - WAV_HEADER_BYTES);
        final File outFile = new File(recordingsDir, sessionId + ".wav");

        final RecordingRecord session;
        final AudioRecorderHandle recorder;

        synchronized (stateLock) {
            if (activeRecording != null && "RECORDING".equals(activeRecording.state)) {
                if (System.currentTimeMillis() >= activeRecording.deadlineMs) {
                    stopRecordingLocked("Session duration limit reached");
                } else {
                    throw new IllegalStateException(
                            "Microphone recording session already active: " + activeRecording.sessionId
                    );
                }
            }

            if (outFile.exists()) {
                outFile.delete();
            }

            try {
                writeWavHeader(outFile, sampleRateHz, CHANNELS, BITS_PER_SAMPLE, 0L);
            } catch (IOException ioe) {
                throw new IllegalStateException("Failed to initialize temporary recording file: " + ioe.getMessage());
            }

            recorder = createAudioRecorder(sampleRateHz, readBufferBytes * 2);
            if (recorder == null || !recorder.isInitialized()) {
                if (recorder != null) {
                    recorder.stopAndRelease();
                }
                outFile.delete();
                throw new IllegalStateException("AudioRecord failed to initialize for " + sampleRateHz + " Hz mono PCM");
            }

            boolean started = false;
            try {
                started = recorder.startRecording();
            } catch (SecurityException se) {
                recorder.stopAndRelease();
                outFile.delete();
                throw se;
            } catch (Exception e) {
                recorder.stopAndRelease();
                outFile.delete();
                throw new IllegalStateException("Failed to start AudioRecord: " + e.getMessage());
            }

            if (!started) {
                recorder.stopAndRelease();
                outFile.delete();
                throw new IllegalStateException("AudioRecord refused to enter RECORDING state (microphone may be in use or blocked)");
            }

            long nowMs = System.currentTimeMillis();
            session = new RecordingRecord(sessionId, outFile, sampleRateHz, reqDuration, maxDataBytes, nowMs);
            session.recorderHandle = recorder;
            Thread captureThread = new Thread(
                    () -> captureLoop(session, readBufferBytes),
                    "PersonalServerMic-" + sessionId
            );
            session.workerThread = captureThread;
            activeRecording = session;
            notifyListener(true, sessionId, reqDuration);
            captureThread.start();
        }

        JSONObject out = new JSONObject();
        out.put("status", "RECORDING");
        out.put("visible_indication", true);
        out.put("session", session.toJson());
        out.put("limits", buildLimitsJson());
        return out;
    }

    public JSONObject stopRecording(String reason) throws JSONException {
        cleanupExpired();
        String effectiveReason = (reason == null || reason.trim().isEmpty()) ? "Requested stop" : reason.trim();

        RecordingRecord stopped = null;
        Thread threadToJoin = null;

        synchronized (stateLock) {
            RecordingRecord cur = activeRecording;
            if (cur == null) {
                JSONObject idle = new JSONObject();
                idle.put("state", "IDLE");
                idle.put("visible_indication", false);
                idle.put("message", "No active microphone recording session");
                if (latestCompletedId != null && completedRecordings.containsKey(latestCompletedId)) {
                    idle.put("recording", completedRecordings.get(latestCompletedId).toJson());
                } else {
                    idle.put("recording", JSONObject.NULL);
                }
                return idle;
            }
            cur.stopRequested = true;
            cur.stopReason = effectiveReason;
            if (cur.recorderHandle != null) {
                cur.recorderHandle.stopAndRelease();
            }
            threadToJoin = cur.workerThread;
            stopped = cur;
        }

        if (threadToJoin != null && threadToJoin != Thread.currentThread()) {
            try {
                threadToJoin.join(2500L);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
        }

        synchronized (stateLock) {
            finalizeSessionIfNeeded(stopped, effectiveReason, null);
            if (activeRecording == stopped) {
                activeRecording = null;
            }
            notifyListener(false, stopped.sessionId, 0);
        }

        JSONObject out = new JSONObject();
        out.put("status", stopped.state);
        out.put("visible_indication", false);
        out.put("stop_reason", effectiveReason);
        out.put("session", stopped.toJson());
        out.put("recording", stopped.toJson());
        return out;
    }

    public JSONObject getRecordingStatus(String requestedSessionId) throws JSONException {
        cleanupExpired();

        RecordingRecord curActive;
        synchronized (stateLock) {
            curActive = activeRecording;
            if (curActive != null && "RECORDING".equals(curActive.state)
                    && System.currentTimeMillis() >= curActive.deadlineMs) {
                stopRecordingLocked("Recording duration limit elapsed");
                curActive = null;
            }
        }

        boolean micAvail = hasMicrophoneFeature();
        boolean permGranted = hasRecordAudioPermission();

        RecordingRecord completed = null;
        if (requestedSessionId != null && !requestedSessionId.trim().isEmpty()) {
            completed = completedRecordings.get(requestedSessionId.trim());
        } else if (latestCompletedId != null) {
            completed = completedRecordings.get(latestCompletedId);
        }

        String overallState;
        if (curActive != null && "RECORDING".equals(curActive.state)) {
            overallState = "RECORDING";
        } else if (completed != null) {
            overallState = completed.state;
        } else {
            overallState = "IDLE";
        }

        JSONObject out = new JSONObject();
        out.put("state", overallState);
        out.put("mic_available", micAvail);
        out.put("permission", permGranted ? "GRANTED" : "PERMISSION_REQUIRED");
        out.put("visible_indication", curActive != null && "RECORDING".equals(curActive.state));
        out.put("active_session", curActive != null ? curActive.toJson() : JSONObject.NULL);
        out.put("recording", completed != null ? completed.toJson() : JSONObject.NULL);
        out.put("limits", buildLimitsJson());
        return out;
    }

    public JSONObject readRecordingChunk(String sessionId, long offset, int length) throws JSONException {
        cleanupExpired();
        if (sessionId == null || sessionId.trim().isEmpty() || !SAFE_SESSION_ID.matcher(sessionId.trim()).matches()) {
            throw new IllegalArgumentException("Valid 'session_id' is required");
        }
        String sid = sessionId.trim();
        RecordingRecord rec = completedRecordings.get(sid);
        if (rec == null) {
            if (activeRecording != null && sid.equals(activeRecording.sessionId)) {
                throw new IllegalStateException("Recording session is still active; stop recording before transfer");
            }
            throw new IllegalStateException("Completed recording not found or expired: " + sid);
        }
        if (!"COMPLETED".equals(rec.state) || !rec.file.exists()) {
            throw new IllegalStateException("Recording file unavailable for transfer: " + sid);
        }

        long totalBytes = rec.file.length();
        if (offset < 0 || offset > totalBytes) {
            throw new IllegalArgumentException("Invalid offset " + offset + " for total size " + totalBytes);
        }

        int boundedLen = Math.max(1, Math.min(length > 0 ? length : MAX_TRANSFER_CHUNK_BYTES, MAX_TRANSFER_CHUNK_BYTES));
        int toRead = (int) Math.min(boundedLen, totalBytes - offset);
        byte[] buf = new byte[toRead];
        int bytesRead = 0;

        if (toRead > 0) {
            try (RandomAccessFile raf = new RandomAccessFile(rec.file, "r")) {
                raf.seek(offset);
                while (bytesRead < toRead) {
                    int n = raf.read(buf, bytesRead, toRead - bytesRead);
                    if (n < 0) {
                        break;
                    }
                    bytesRead += n;
                }
            } catch (IOException ioe) {
                throw new IllegalStateException("Failed reading recording chunk: " + ioe.getMessage());
            }
        }

        byte[] exactSlice = (bytesRead == buf.length) ? buf : Arrays.copyOf(buf, bytesRead);
        String b64 = Base64.encodeToString(exactSlice, Base64.NO_WRAP);
        boolean eof = (offset + bytesRead) >= totalBytes;
        if (eof) {
            rec.transferred = true;
        }

        JSONObject out = new JSONObject();
        out.put("session_id", sid);
        out.put("mime_type", "audio/wav");
        out.put("offset", offset);
        out.put("bytes_read", bytesRead);
        out.put("total_bytes", totalBytes);
        out.put("eof", eof);
        out.put("sha256", rec.sha256 != null ? rec.sha256 : JSONObject.NULL);
        out.put("chunk_base64", b64);
        return out;
    }

    public JSONObject deleteRecording(String sessionId) throws JSONException {
        if (sessionId == null || sessionId.trim().isEmpty()) {
            throw new IllegalArgumentException("'session_id' is required for cleanup");
        }
        String sid = sessionId.trim();
        RecordingRecord rec = completedRecordings.remove(sid);
        boolean deleted = false;
        if (rec != null) {
            deleted = rec.file.delete();
            if (sid.equals(latestCompletedId)) {
                latestCompletedId = null;
            }
        } else if (SAFE_SESSION_ID.matcher(sid).matches()) {
            File candidate = new File(recordingsDir, sid + ".wav");
            if (candidate.exists()) {
                deleted = candidate.delete();
            }
        }
        JSONObject out = new JSONObject();
        out.put("session_id", sid);
        out.put("deleted", deleted);
        return out;
    }

    public void stopAll() {
        RecordingRecord active;
        Thread worker = null;
        synchronized (stateLock) {
            active = activeRecording;
            if (active != null) {
                active.stopRequested = true;
                active.stopReason = "Service shutdown";
                if (active.recorderHandle != null) {
                    active.recorderHandle.stopAndRelease();
                }
                worker = active.workerThread;
            }
        }
        if (worker != null && worker != Thread.currentThread()) {
            try {
                worker.join(2000L);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
        }
        synchronized (stateLock) {
            if (active != null) {
                finalizeSessionIfNeeded(active, "Service shutdown", null);
                if (activeRecording == active) {
                    activeRecording = null;
                }
                notifyListener(false, active.sessionId, 0);
            }
        }
        purgeAllTempFiles();
    }

    private void stopRecordingLocked(String reason) {
        RecordingRecord cur = activeRecording;
        if (cur == null) {
            return;
        }
        cur.stopRequested = true;
        cur.stopReason = reason;
        if (cur.recorderHandle != null) {
            cur.recorderHandle.stopAndRelease();
        }
        if (cur.workerThread != null && cur.workerThread != Thread.currentThread()) {
            try {
                cur.workerThread.join(1500L);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
        }
        finalizeSessionIfNeeded(cur, reason, null);
        if (activeRecording == cur) {
            activeRecording = null;
        }
        notifyListener(false, cur.sessionId, 0);
    }

    private void captureLoop(RecordingRecord session, int bufferBytes) {
        byte[] buffer = new byte[bufferBytes];
        String err = null;
        try (RandomAccessFile raf = new RandomAccessFile(session.file, "rw")) {
            raf.seek(WAV_HEADER_BYTES);
            while (!session.stopRequested) {
                long now = System.currentTimeMillis();
                if (now >= session.deadlineMs) {
                    session.stopReason = "Duration limit reached";
                    break;
                }
                long remainingCap = session.maxDataBytes - session.dataBytesWritten;
                if (remainingCap <= 0) {
                    session.stopReason = "Maximum file size reached";
                    break;
                }
                int toRead = (int) Math.min(buffer.length, remainingCap);
                int readBytes = session.recorderHandle.read(buffer, 0, toRead);
                if (readBytes > 0) {
                    raf.write(buffer, 0, readBytes);
                    session.dataBytesWritten += readBytes;
                } else if (readBytes == 0) {
                    try {
                        Thread.sleep(10L);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                } else {
                    if (!session.stopRequested) {
                        err = "AudioRecord read failed with code " + readBytes;
                    }
                    break;
                }
            }
        } catch (Exception e) {
            err = "Recording write failure: " + e.getMessage();
            Log.e(TAG, err, e);
        } finally {
            if (session.recorderHandle != null) {
                session.recorderHandle.stopAndRelease();
            }
            synchronized (stateLock) {
                finalizeSessionIfNeeded(
                        session,
                        session.stopReason != null ? session.stopReason : "Capture finished",
                        err
                );
                if (activeRecording == session) {
                    activeRecording = null;
                }
                notifyListener(false, session.sessionId, 0);
            }
        }
    }

    private void finalizeSessionIfNeeded(RecordingRecord session, String reason, String error) {
        if (!"RECORDING".equals(session.state)) {
            return;
        }
        session.stoppedAtMs = System.currentTimeMillis();
        session.stopReason = reason;
        session.expiresAtMs = session.stoppedAtMs + TEMP_TTL_MS;

        if (error != null) {
            session.state = "ERROR";
            session.errorMessage = error;
            session.fileSizeBytes = session.file.exists() ? session.file.length() : 0L;
            if (session.file.exists()) {
                session.file.delete();
            }
            completedRecordings.put(session.sessionId, session);
            latestCompletedId = session.sessionId;
            enforceRetentionLimit();
            return;
        }

        try {
            long actualDataBytes = Math.max(0L, session.dataBytesWritten);
            updateWavHeaderSizes(session.file, actualDataBytes);
            session.fileSizeBytes = session.file.length();
            double bytesPerSec = session.sampleRateHz * CHANNELS * (BITS_PER_SAMPLE / 8.0);
            session.durationSec = Math.round((actualDataBytes / bytesPerSec) * 1000.0) / 1000.0;
            session.sha256 = computeSha256(session.file);
            session.state = "COMPLETED";
        } catch (Exception e) {
            session.state = "ERROR";
            session.errorMessage = "Failed to finalize WAV recording: " + e.getMessage();
            if (session.file.exists()) {
                session.file.delete();
            }
        }

        completedRecordings.put(session.sessionId, session);
        latestCompletedId = session.sessionId;
        enforceRetentionLimit();
    }

    private void notifyListener(boolean active, String sessionId, int maxDurationSec) {
        RecordingNotificationListener listener = this.notificationListener;
        if (listener != null) {
            try {
                listener.onRecordingStateChanged(active, sessionId, maxDurationSec);
            } catch (Exception e) {
                Log.w(TAG, "Notification listener update failed: " + e.getMessage());
            }
        }
    }

    private void cleanupExpired() {
        long now = System.currentTimeMillis();
        for (Map.Entry<String, RecordingRecord> entry : completedRecordings.entrySet()) {
            RecordingRecord rec = entry.getValue();
            if (rec.expiresAtMs > 0 && now >= rec.expiresAtMs) {
                completedRecordings.remove(entry.getKey());
                if (rec.file.exists()) {
                    rec.file.delete();
                }
                if (entry.getKey().equals(latestCompletedId)) {
                    latestCompletedId = null;
                }
            }
        }
    }

    private void enforceRetentionLimit() {
        if (completedRecordings.size() <= MAX_RETAINED_RECORDINGS) {
            return;
        }
        List<RecordingRecord> all = new ArrayList<>(completedRecordings.values());
        Collections.sort(all, Comparator.comparingLong(r -> r.stoppedAtMs));
        while (all.size() > MAX_RETAINED_RECORDINGS) {
            RecordingRecord oldest = all.remove(0);
            completedRecordings.remove(oldest.sessionId);
            if (oldest.file.exists()) {
                oldest.file.delete();
            }
        }
    }

    private void sweepOrphanedFiles() {
        File[] files = recordingsDir.listFiles();
        if (files == null) {
            return;
        }
        for (File f : files) {
            if (f.isFile() && f.getName().endsWith(".wav")) {
                f.delete();
            }
        }
    }

    private void purgeAllTempFiles() {
        completedRecordings.clear();
        latestCompletedId = null;
        sweepOrphanedFiles();
    }

    private JSONObject buildLimitsJson() throws JSONException {
        JSONObject limits = new JSONObject();
        limits.put("min_duration_sec", MIN_DURATION_SEC);
        limits.put("max_duration_sec", MAX_DURATION_SEC);
        limits.put("max_file_bytes", MAX_FILE_BYTES);
        limits.put("max_chunk_bytes", MAX_TRANSFER_CHUNK_BYTES);
        limits.put("temp_ttl_sec", TEMP_TTL_MS / 1000L);
        JSONArray rates = new JSONArray();
        rates.put(16000);
        rates.put(22050);
        rates.put(44100);
        limits.put("supported_sample_rates_hz", rates);
        return limits;
    }

    private static void writeWavHeader(File file, int sampleRate, int channels, int bitsPerSample, long dataLength)
            throws IOException {
        byte[] header = buildWavHeaderBytes(sampleRate, channels, bitsPerSample, dataLength);
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            raf.seek(0);
            raf.write(header);
        }
    }

    private static void updateWavHeaderSizes(File file, long dataLength) throws IOException {
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            long totalAudioLen = dataLength;
            long totalDataLen = totalAudioLen + 36L;
            raf.seek(4);
            raf.write(intToLittleEndian((int) totalDataLen));
            raf.seek(40);
            raf.write(intToLittleEndian((int) totalAudioLen));
        }
    }

    private static byte[] buildWavHeaderBytes(int sampleRate, int channels, int bitsPerSample, long totalAudioLen) {
        long totalDataLen = totalAudioLen + 36L;
        long byteRate = (long) sampleRate * channels * bitsPerSample / 8L;
        int blockAlign = channels * bitsPerSample / 8;

        byte[] header = new byte[WAV_HEADER_BYTES];
        header[0] = 'R'; header[1] = 'I'; header[2] = 'F'; header[3] = 'F';
        System.arraycopy(intToLittleEndian((int) totalDataLen), 0, header, 4, 4);
        header[8] = 'W'; header[9] = 'A'; header[10] = 'V'; header[11] = 'E';
        header[12] = 'f'; header[13] = 'm'; header[14] = 't'; header[15] = ' ';
        header[16] = 16; header[17] = 0; header[18] = 0; header[19] = 0; // PCM Subchunk1Size = 16
        header[20] = 1; header[21] = 0;                                   // AudioFormat = 1 (PCM)
        header[22] = (byte) (channels & 0xff); header[23] = 0;
        System.arraycopy(intToLittleEndian(sampleRate), 0, header, 24, 4);
        System.arraycopy(intToLittleEndian((int) byteRate), 0, header, 28, 4);
        header[32] = (byte) (blockAlign & 0xff); header[33] = 0;
        header[34] = (byte) (bitsPerSample & 0xff); header[35] = 0;
        header[36] = 'd'; header[37] = 'a'; header[38] = 't'; header[39] = 'a';
        System.arraycopy(intToLittleEndian((int) totalAudioLen), 0, header, 40, 4);
        return header;
    }

    private static byte[] intToLittleEndian(int value) {
        return new byte[]{
                (byte) (value & 0xff),
                (byte) ((value >> 8) & 0xff),
                (byte) ((value >> 16) & 0xff),
                (byte) ((value >> 24) & 0xff)
        };
    }

    private static String computeSha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buf = new byte[8192];
        try (FileInputStream fis = new FileInputStream(file)) {
            int n;
            while ((n = fis.read(buf)) > 0) {
                digest.update(buf, 0, n);
            }
        }
        byte[] hash = digest.digest();
        StringBuilder hex = new StringBuilder(hash.length * 2);
        for (byte b : hash) {
            hex.append(String.format("%02x", b & 0xff));
        }
        return hex.toString();
    }
}

