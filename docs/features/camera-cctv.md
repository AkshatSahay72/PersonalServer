# Feature: Camera2 Live Video Streaming & CCTV Mode

**Subsystem:** Camera2 Hardware Bridge & Media Streaming  
**Implementation:** `LiveMediaBridgeManager.java`, `MediaStreamServer.java`, `services/node-api/app.py`, `live_viewer.html`  
**Current Baseline:** 720p @ 15 FPS (Verified)  

---

## 1. Design Philosophy

Camera streaming in PersonalServer is an **on-demand live inspection tool**, not a 24/7 NVR recording service.
- **Hardware is Powered Down When Idle:** The camera sensor is only activated when an authenticated client opens the stream.
- **Immediate Cleanup on Disconnect:** Closing the viewer tab or dropping network connection immediately triggers `releaseCamera()`, powering down the image sensor and returning state to `IDLE`.
- **Bandwidth Clamping:** Throttled strictly to 500–800 kbps to avoid saturating mobile Wi-Fi or cellular connections.

---

## 2. Technical Specifications

| Parameter | Default Value | Configurable Options |
| :--- | :--- | :--- |
| **Resolution** | 1280x720 (720p) | 720p (1280x720), 480p (640x480), 360p (480x360) |
| **Target Frame Rate** | 15 FPS | 10–20 FPS |
| **JPEG Quality** | 70 | 50–85 |
| **Transport Protocol** | HTTP Multipart MJPEG | `multipart/x-mixed-replace; boundary=frame` |
| **Bandwidth Consumption** | ~500–800 kbps | Dependent on preset |
| **Glass-to-Glass Latency** | < 1.0s | Over Tailscale WireGuard mesh |

---

## 3. Endpoints & API Contracts

All endpoints are reachable over Tailscale via the Node API (`http://<node-ip>:8080`) or proxied via the Master Controller:

- **`GET /cctv` / `GET /camera`**: Serves the standalone responsive dark-mode CCTV live viewer.
- **`GET /camera/stream` / `GET /cctv/stream`**: Returns the continuous live multipart MJPEG stream. Auto-starts on-demand session if not already active.
- **`GET /camera/frame` / `GET /cctv/frame`**: Returns a single high-resolution JPEG snapshot (`image/jpeg`).
- **`POST /hardware/live-call/start`**: Starts an explicit session:
  ```json
  {"role": "caller", "resolution": "720p", "fps": 15, "camera_id": "0"}
  ```
- **`POST /hardware/live-call/update`**: Dynamically flips camera or adjusts resolution during an active session:
  ```json
  {"camera_id": "1", "quality": "480p"}
  ```
- **`POST /hardware/live-call/stop`**: Terminates the session and immediately releases camera hardware locks.
