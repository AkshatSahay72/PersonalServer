# Feature: Android Companion App & Native Bridge

**Package:** `com.personalserver.bridge`  
**Location:** `android/native-bridge/`  
**Current Version:** 1.0.8 (versionCode 9)  
**Binary:** `PersonalServerNativeBridge-debug.apk`  

---

## 1. Overview & Purpose

The Android Companion App bridges Android OS platform capabilities to unprivileged Termux Python environments. It acts as a supervisor and hardware bridge, allowing phones running Android 11 through Android 16 to function as resilient server nodes without rooting.

---

## 2. Key Architecture Components

- **`BridgeActivity.java`:** The single-activity user interface providing a "Start PersonalServer" button, real-time node status indicators (Termux RUN_COMMAND status, Node API health, Tailscale connection), and permission request prompts.
- **`BridgeService.java`:** An Android Foreground Service that keeps the application alive in the background. It displays a persistent OS notification with quick actions and declares `FOREGROUND_SERVICE_TYPE_CAMERA` and `FOREGROUND_SERVICE_TYPE_MICROPHONE`.
- **`NodeStartupStateMachine.java`:** A deterministic state machine coordinating node startup:
  1. `UNINITIALIZED` ➔ checks permissions.
  2. `STARTING_TERMUX` ➔ issues `RUN_COMMAND` intent to Termux to execute `scripts/start.sh`.
  3. `AWAITING_TAILSCALE` ➔ verifies Tailscale VPN interface is up (`100.x.y.z`).
  4. `VERIFYING_NODE_API` ➔ polls `http://127.0.0.1:8080/health`.
  5. `ONLINE` ➔ reports node healthy.
- **`TermuxCommandResultReceiver.java`:** Broadcast receiver capturing exit codes and stdout/stderr from Termux `RUN_COMMAND` actions.
- **`LocalBridgeServer.java`:** Lightweight HTTP server on port 8765 handling local IPC requests from Python.
- **`MediaStreamServer.java`:** High-throughput loopback streaming socket on port 8766 delivering JPEG frames to the Node API.
- **Tailscale Zero-Touch Auto-Recovery:** Integrates automated UI toggling for Tailscale VPN: detects stalled or disconnected mesh interfaces, launches the Tailscale intent, and dispatches a tap event to the fixed top-left toggle button coordinates to re-enable VPN without manual operator intervention (see [Failsafe & Self-Healing](failsafe-recovery.md)).

---

## 3. Supported Android Versions

| Android Version | API Level | Tested Devices | Notes |
| :--- | :--- | :--- | :--- |
| **Android 16** | API 36 | Vivo V2334 (Experimental) | Verified baseline for Camera2 720p, WakeLock, and one-tap startup. |
| **Android 11** | API 30 | Vivo Y31 (Controller), Realme 5 Pro (Worker) | Target for M2 cross-device rollout. |
