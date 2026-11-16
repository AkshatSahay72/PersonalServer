# PersonalServer — System Architecture & Technical Specifications

**Version:** 1.0.0  
**Status:** Authoritative Architectural Reference  
**Last Updated:** November 15, 2026  

---

## 1. System Overview

PersonalServer unites heterogeneous personal devices into a resilient, unprivileged edge computing cluster. The system uses a modular, layered architecture with zero heavy runtime dependencies:

```mermaid
flowchart TD
    subgraph Public Ingress Plane
        CF["Cloudflare Zero Trust Access<br/>(Identity Verification)"] --> CFT["Cloudflare Tunnel<br/>(server.akshatsahay.space)"]
        CFT --> ROUTER["Reverse Proxy Router<br/>(:8088 / :8080)"]
    end

    subgraph Private Mesh Plane (Tailscale WireGuard 100.x.y.z)
        ROUTER --> CTRL["Master Controller (Vivo Y31 - Gujarat)<br/>(:8000)"]
        CTRL <-->|Lease Consensus & State Sync| STANDBY["Standby Controller (Realme 5 Pro - Gujarat)<br/>(:8000)"]
        CTRL -->|Dispatches Workloads / Telemetry| WORKER["Compute Worker (Realme 5 Pro)<br/>Node Agent (:8000)"]
        CTRL -->|Direct Video Proxy / API| EXP_NODE["Experimental CCTV Node (Vivo V2334 - Punjab)<br/>Node API (:8080)"]
        DEV["Dev Workstation (Windows 11)<br/>Build Worker (:8000)"] <--> CTRL
    end

    subgraph Android Node Architecture (Vivo V2334)
        EXP_NODE <-->|Local Loopback HTTP :8765| BRIDGE["Android Native Bridge<br/>(com.personalserver.bridge)"]
        EXP_NODE <-->|Raw MJPEG Stream :8766| MS["MediaStreamServer<br/>(Java Local Server)"]
        BRIDGE --> CAM2["Camera2 HAL<br/>(1280x720 @ 15fps)"]
        BRIDGE --> AUDIO["AudioRecord HAL<br/>(16kHz Mono PCM)"]
        BRIDGE --> WL["PowerManager WakeLock<br/>(PARTIAL_WAKE_LOCK)"]
        BRIDGE --> NOTIF["Persistent Foreground Notification"]
        TERMUX["Termux Supervisor<br/>(Python 3.11 Runtime)"] --> EXP_NODE
    end
```

---

## 2. Core Subsystems

### 2.1 Master Controller Plane (`controller/`)
- **REST API (`controller/controller.py`):** Authoritative cluster API for node enrollment, heartbeats, job dispatch, and deployment triggers.
- **Transactional JSON Stores (`controller/stores/`):** Atomic read-modify-write storage using standard library file operations (`jobs_store.json`, `nodes_store.json`, `apps_store.json`, `leases_store.json`).
- **High Availability Consensus (`config/ha_manager.py`):** Distributed master election, monotonic term tracking, atomic leader lease renewal (60s), and cluster state synchronization (`/ha/sync`).
- **Universal Scheduler (`scheduler/scheduler.py`):** 8-stage eligibility pipeline (liveness, capabilities, memory/CPU reservations, thermals, and affinity) with priority aging.

---

### 2.2 Node Agent & Node API (`agent/`, `services/node-api/`)
- **Node Agent (`agent/node-agent.py`):** Runs on worker nodes, emitting authenticated heartbeats every 10 seconds with dynamic battery, WiFi, CPU temperature, and load metrics.
- **Node API (`services/node-api/app.py`):** Exposes local node management, health checks, static file serving (`/`), standalone CCTV live viewer (`/cctv`), and streaming proxies (`/camera/stream`, `/camera/frame`).
- **Hardware Integration Layer (`agent/hardware/`):**
  - `camera.py`: Manages on-demand camera sessions and coordinates with the native bridge.
  - `live_call.py`: Handles streaming protocols, dynamic camera flipping, and frame buffering.
  - `microphone.py` & `speaker.py`: Manages optional audio capture and playback.

---

### 2.3 Android Native Bridge (`android/native-bridge/`)
The native companion app (`com.personalserver.bridge`) provides unprivileged Termux processes with safe access to restricted Android OS APIs:
- **`BridgeService.java`:** Android Foreground Service maintaining process priority, managing `FOREGROUND_SERVICE_TYPE_CAMERA`, and binding notification lifecycle.
- **`LiveMediaBridgeManager.java`:** Camera2 HAL manager:
  - Dynamically negotiates 1280x720 (720p) resolution via `chooseBestResolution`.
  - Enforces 15 FPS throttling via interval gating (`minFrameIntervalMs`) to bound bandwidth to 500–800 kbps.
  - Manages dedicated session `PowerManager.WakeLock` (`PersonalServer:CCTVSessionWakeLock`).
  - Implements clean hardware teardown (`abortCaptures()`, `stopRepeating()`, session close, device close, reader close, background thread shutdown).
- **`LocalBridgeServer.java`:** Local loopback HTTP server (`127.0.0.1:8765`) handling command dispatches (`/status`, `/start`, `/stop`, `/update`).
- **`MediaStreamServer.java`:** Dedicated loopback media server (`127.0.0.1:8766`) delivering low-latency MJPEG frame buffers to the Python Node API.
- **`NodeStartupStateMachine.java` & `TermuxRecoveryManager.java`:** Manages one-tap node startup by dispatching `RUN_COMMAND` intents to Termux and tracking startup milestones.

---

### 2.4 Universal Node Bootstrapper (`bootstrap/`)
A modular shell-based onboarding tool providing deterministic installation across heterogeneous platforms:
- `bootstrap/detect.sh`: Probes OS, platform, CPU architecture, core count, RAM, and available package managers (`pkg`, `apt`, `pacman`).
- `bootstrap/dependencies.sh`: Installs minimum runtime prerequisites (Python 3, Git, Curl, OpenSSL).
- `bootstrap/install.sh`: Creates standardized directory structures, virtual environments, and configuration templates.
- `bootstrap/configure.sh`: Applies node identity, controller pairing, and Tailscale binding.
- `bootstrap/uninstall.sh`: Safely cleans up services and unregisters the node.

---

## 3. Communication Protocols & Data Flows

### 3.1 Live Camera Streaming Flow
```text
Browser Client (Laptop)
  ↓ GET /camera/stream (or /nodes/<id>/proxy/camera/stream)
Controller / Ingress Router
  ↓ Proxied over Tailscale WireGuard Mesh
Node API (app.py :8080)
  ↓ POST http://127.0.0.1:8765/start (Camera on-demand activation)
Android Native Bridge (BridgeService.java)
  ↓ Acquires WakeLock & starts Camera2 Preview Session
Camera2 HAL Sensor (1280x720 @ 15fps)
  ↓ ImageReader delivers JPEG frames to MediaStreamServer (:8766)
Node API
  ↓ Delivers multipart/x-mixed-replace; boundary=frame stream
Browser Client displays live feed in Dashboard (<1.0s latency)
```

### 3.2 Session Teardown Flow
```text
Browser Client closes stream / tab
  ↓ Socket disconnected
Node API catches ConnectionResetError / BrokenPipeError
  ↓ POST http://127.0.0.1:8765/stop
Android Native Bridge
  ↓ abortCaptures() ➔ stopRepeating() ➔ close CameraDevice
  ↓ Releases WakeLock
  ↓ Updates state to IDLE
Hardware sensor powers down completely (Zero battery waste)
```

---

## 4. Security & Trust Boundaries

1. **Perimeter Boundary:** Only traffic arriving over Tailscale WireGuard (`100.x.y.z`) or authenticated via Cloudflare Zero Trust is accepted.
2. **Local IPC Boundary:** Native bridge servers (`:8765`, `:8766`) bind strictly to `127.0.0.1`. Remote devices cannot bypass Node API authentication to access hardware directly.
3. **Token Validation:** Inter-node heartbeats and state synchronization require valid cryptographic tokens stored in `config/secrets/auth.token`.
