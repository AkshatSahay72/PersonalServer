package com.personalserver.bridge;

import android.util.Log;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Collections;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * High-performance, low-latency loopback streaming server for PersonalServer live media (E07).
 * Streams real-time Camera video frames, Microphone audio packets, and bidirectional Speaker audio
 * over a dedicated loopback socket channel (127.0.0.1:8766) strictly decoupled from the JSON control IPC.
 */
public class MediaStreamServer {

    private static final String TAG = "MediaStreamServer";
    public static final int DEFAULT_MEDIA_PORT = 8766;

    // Protocol constants
    public static final byte[] MAGIC = new byte[]{(byte) 0x50, (byte) 0x53, (byte) 0x4D, (byte) 0x53}; // "PSMS"
    public static final byte TYPE_VIDEO_FRAME = 0x01;
    public static final byte TYPE_AUDIO_UPSTREAM = 0x02;
    public static final byte TYPE_AUDIO_DOWNSTREAM = 0x03;
    public static final byte TYPE_HEARTBEAT = 0x04;
    public static final byte TYPE_CONTROL = 0x05;

    public static final byte CODEC_JPEG = 0x01;
    public static final byte CODEC_H264 = 0x02;

    public interface DownstreamAudioListener {
        void onDownstreamAudioReceived(int sampleRate, int channels, byte[] pcmData);
    }

    public interface MediaClientEventListener {
        void onClientConnected();
        void onClientDisconnected();
        void onHeartbeatReceived(long timestamp);
    }

    private final int port;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private ServerSocket serverSocket;
    private Thread acceptThread;
    private final ExecutorService clientExecutor = Executors.newCachedThreadPool();
    private final Set<ClientHandler> activeClients = Collections.newSetFromMap(new ConcurrentHashMap<>());
    private volatile DownstreamAudioListener audioListener;
    private volatile MediaClientEventListener clientEventListener;

    private final AtomicLong videoFramesSent = new AtomicLong(0);
    private final AtomicLong audioPacketsSent = new AtomicLong(0);
    private final AtomicLong speakerPacketsReceived = new AtomicLong(0);
    private final AtomicLong bytesTransferred = new AtomicLong(0);

    public MediaStreamServer() {
        this(DEFAULT_MEDIA_PORT);
    }

    public MediaStreamServer(int port) {
        this.port = port;
    }

    public void setDownstreamAudioListener(DownstreamAudioListener listener) {
        this.audioListener = listener;
    }

    public void setMediaClientEventListener(MediaClientEventListener listener) {
        this.clientEventListener = listener;
    }

    public int getPort() {
        return port;
    }

    public boolean isRunning() {
        return running.get() && serverSocket != null && !serverSocket.isClosed();
    }

    public int getActiveClientCount() {
        return activeClients.size();
    }

    public long getVideoFramesSent() {
        return videoFramesSent.get();
    }

    public long getAudioPacketsSent() {
        return audioPacketsSent.get();
    }

    public long getSpeakerPacketsReceived() {
        return speakerPacketsReceived.get();
    }

    public synchronized boolean start() {
        if (running.get()) {
            return true;
        }

        try {
            serverSocket = new ServerSocket();
            serverSocket.setReuseAddress(true);
            serverSocket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 8);
            running.set(true);
            Log.i(TAG, "MediaStreamServer bound strictly to loopback 127.0.0.1:" + port);

            acceptThread = new Thread(this::acceptLoop, "MediaStreamServer-Accept");
            acceptThread.setDaemon(true);
            acceptThread.start();
            return true;
        } catch (IOException e) {
            Log.e(TAG, "Failed binding MediaStreamServer on port " + port + ": " + e.getMessage());
            stop();
            return false;
        }
    }

    public synchronized void stop() {
        running.set(false);
        if (serverSocket != null) {
            try {
                serverSocket.close();
            } catch (Exception ignored) {}
            serverSocket = null;
        }

        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }

        for (ClientHandler client : activeClients) {
            client.close();
        }
        activeClients.clear();
        Log.i(TAG, "MediaStreamServer stopped.");
    }

    private void acceptLoop() {
        while (running.get()) {
            try {
                Socket clientSocket = serverSocket.accept();
                clientSocket.setTcpNoDelay(true);
                clientSocket.setSendBufferSize(256 * 1024);
                clientSocket.setReceiveBufferSize(64 * 1024);
                clientSocket.setSoTimeout(10000); // 10s inactivity timeout

                ClientHandler handler = new ClientHandler(clientSocket);
                activeClients.add(handler);
                clientExecutor.execute(handler);

                if (clientEventListener != null) {
                    clientEventListener.onClientConnected();
                }
                Log.i(TAG, "Media client connected: " + clientSocket.getRemoteSocketAddress());
            } catch (IOException e) {
                if (!running.get()) {
                    break;
                }
                Log.w(TAG, "Error in acceptLoop: " + e.getMessage());
            }
        }
    }

    /**
     * Broadcasts a compressed video frame (JPEG or H.264) to connected media clients.
     */
    public void broadcastVideoFrame(long timestampMs, int width, int height, byte codec, byte[] frameData) {
        if (!running.get() || activeClients.isEmpty() || frameData == null || frameData.length == 0) {
            return;
        }

        // Frame header: 2 bytes width, 2 bytes height, 1 byte codec
        byte[] meta = new byte[5];
        meta[0] = (byte) ((width >> 8) & 0xFF);
        meta[1] = (byte) (width & 0xFF);
        meta[2] = (byte) ((height >> 8) & 0xFF);
        meta[3] = (byte) (height & 0xFF);
        meta[4] = codec;

        byte[] payload = new byte[meta.length + frameData.length];
        System.arraycopy(meta, 0, payload, 0, meta.length);
        System.arraycopy(frameData, 0, payload, meta.length, frameData.length);

        for (ClientHandler client : activeClients) {
            client.sendPacket(TYPE_VIDEO_FRAME, timestampMs, payload);
        }
        videoFramesSent.incrementAndGet();
        bytesTransferred.addAndGet(payload.length);
    }

    /**
     * Broadcasts raw PCM audio captured from the device microphone.
     */
    public void broadcastAudioUpstream(long timestampMs, int sampleRate, int channels, byte[] pcmData, int offset, int length) {
        if (!running.get() || activeClients.isEmpty() || pcmData == null || length <= 0) {
            return;
        }

        byte[] meta = new byte[3];
        meta[0] = (byte) ((sampleRate >> 8) & 0xFF);
        meta[1] = (byte) (sampleRate & 0xFF);
        meta[2] = (byte) (channels & 0xFF);

        byte[] payload = new byte[meta.length + length];
        System.arraycopy(meta, 0, payload, 0, meta.length);
        System.arraycopy(pcmData, offset, payload, meta.length, length);

        for (ClientHandler client : activeClients) {
            client.sendPacket(TYPE_AUDIO_UPSTREAM, timestampMs, payload);
        }
        audioPacketsSent.incrementAndGet();
        bytesTransferred.addAndGet(payload.length);
    }

    private final class ClientHandler implements Runnable {
        private final Socket socket;
        private final AtomicBoolean clientAlive = new AtomicBoolean(true);
        private DataOutputStream out;
        private DataInputStream in;

        ClientHandler(Socket socket) {
            this.socket = socket;
        }

        void close() {
            clientAlive.set(false);
            try {
                socket.close();
            } catch (Exception ignored) {}
            activeClients.remove(this);
        }

        synchronized void sendPacket(byte type, long timestampMs, byte[] payload) {
            if (!clientAlive.get() || out == null) {
                return;
            }
            try {
                // Packet structure:
                // 4 bytes MAGIC
                // 1 byte Type
                // 4 bytes Payload Length
                // 8 bytes Timestamp
                // Payload
                out.write(MAGIC);
                out.writeByte(type);
                out.writeInt(payload.length);
                out.writeLong(timestampMs);
                out.write(payload);
                out.flush();
            } catch (IOException e) {
                Log.w(TAG, "Client write failed, closing connection: " + e.getMessage());
                close();
                if (clientEventListener != null) {
                    clientEventListener.onClientDisconnected();
                }
            }
        }

        @Override
        public void run() {
            try {
                out = new DataOutputStream(new BufferedOutputStream(socket.getOutputStream(), 64 * 1024));
                in = new DataInputStream(new BufferedInputStream(socket.getInputStream(), 16 * 1024));

                byte[] magicBuffer = new byte[4];
                while (clientAlive.get() && running.get()) {
                    in.readFully(magicBuffer);
                    if (magicBuffer[0] != MAGIC[0] || magicBuffer[1] != MAGIC[1] ||
                        magicBuffer[2] != MAGIC[2] || magicBuffer[3] != MAGIC[3]) {
                        Log.w(TAG, "Invalid magic bytes from media client, closing connection.");
                        break;
                    }

                    byte type = in.readByte();
                    int length = in.readInt();
                    long timestamp = in.readLong();

                    if (length < 0 || length > 1024 * 1024) { // 1 MB sanity limit
                        Log.w(TAG, "Payload length out of bounds: " + length);
                        break;
                    }

                    byte[] payload = new byte[length];
                    in.readFully(payload);

                    if (type == TYPE_AUDIO_DOWNSTREAM) {
                        // Speaker playback packet from browser/server
                        if (payload.length >= 3) {
                            int sampleRate = ((payload[0] & 0xFF) << 8) | (payload[1] & 0xFF);
                            int channels = payload[2] & 0xFF;
                            int pcmLen = payload.length - 3;
                            byte[] pcmData = new byte[pcmLen];
                            System.arraycopy(payload, 3, pcmData, 0, pcmLen);

                            speakerPacketsReceived.incrementAndGet();
                            DownstreamAudioListener listener = audioListener;
                            if (listener != null) {
                                listener.onDownstreamAudioReceived(sampleRate, channels, pcmData);
                            }
                        }
                    } else if (type == TYPE_HEARTBEAT) {
                        if (clientEventListener != null) {
                            clientEventListener.onHeartbeatReceived(timestamp);
                        }
                    }
                }
            } catch (Exception e) {
                if (clientAlive.get()) {
                    Log.d(TAG, "Media client stream closed: " + e.getMessage());
                }
            } finally {
                close();
                if (clientEventListener != null) {
                    clientEventListener.onClientDisconnected();
                }
            }
        }
    }
}
