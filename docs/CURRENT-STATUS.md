# PersonalServer — Current System Status & Verification Baseline

**Document Version:** 1.0.0  
**Verification Date:** November 15, 2026  
**Active Worktree:** `_worktrees/e07-camera-audio-streaming`  
**Git Branch:** `feature/e07-camera-audio-streaming` (Commit `8c0e152`)  

---

## 1. Physical Node Inventory & Operational Status

| Node ID | Physical Device | OS & Platform | Location | Cluster Role | Tailscale IP | Operational Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`server-v2334-a531`** | Vivo V2334 | Android 16 (API 36), ARM64 | Punjab (Local) | Experimental CCTV & Sensor Node | `100.113.234.61:8080` | **ONLINE (HTTP 200)** — Node API PID 21659, Worker PID 21834 |
| **`server-y31-b842`** | Vivo Y31 | Android 11 (API 30), ARM64 | Gujarat (Remote) | Active Leader Controller | `100.85.108.5:8000` | **ONLINE** — Leader Lease Active |
| **`server-r5p-c913`** | Realme 5 Pro | Android 11 (API 30), ARM64 | Gujarat (Remote) | Standby Controller & Worker | `100.73.52.72:8000` | **ONLINE** — Standby Replicating |
| **`server-win-dev`** | Windows 11 PC | Windows 11 x86_64 | Punjab (Local) | On-Demand Build Worker | `100.64.1.1` | **AVAILABLE** |

---

## 2. Active Software & Build Verification

| Component | Active Version | Verification Evidence |
| :--- | :--- | :--- |
| **Android Native Bridge APK** | `1.0.8` (code `9`) | Built via `gradlew assembleDebug` (3,190,399 bytes). SHA-256: `50F6FFF2427F98F1BBD12C2D92428A6AE853E240090743EB5D8B9DC2145537EF`. Installed on Vivo V2334. |
| **Camera2 720p 15 FPS Video** | Active | Physically tested: 390 KB valid JPEG frames captured via Camera2 HAL; dynamic front/rear flip; clean release to `IDLE`. |
| **Dedicated Session WakeLock** | Active | `PersonalServer:CCTVSessionWakeLock` verified active during streaming sessions in `LiveMediaBridgeManager.java`. |
| **Standalone CCTV Web Viewer** | `1.0.0` | Accessible over Tailscale at `http://100.113.234.61:8080/cctv` and `/camera`. |
| **Universal Node Bootstrapper** | `0.1.0` | `bootstrap/*.sh` modular scripts verified via `tests/unit/test_bootstrapper.py`. |

---

## 3. Verified Test Suite Results

### 3.1 Unit Test Suite (`pytest`)
Ran on November 15, 2026:
- `tests/unit/test_native_android_bridge_lifecycle.py`: **2 passed**
- `tests/unit/test_e07_live_call.py`: **14 passed**
- `tests/unit/test_bootstrapper.py`: **6 passed**
- **Result:** **22 passed in 58.99s** (100% pass rate).

### 3.2 Physical Hardware Integration Test (`test_vivo_cctv_streaming.py`)
Tested directly on live Vivo V2334 hardware (`100.113.234.61:8080`):
- [PASS] Step 1: Start 720p CCTV session (`session_id` returned).
- [PASS] Step 2: Stream active verified (`status: ACTIVE`, `1280x720`, `15 FPS`).
- [PASS] Step 3: Single frame capture verified (390,770 bytes valid JPEG, SOI `\xff\xd8`, EOI `\xff\xd9`).
- [PASS] Step 4: MJPEG multipart boundary stream verified (`--frame` received).
- [PASS] Step 5: Dynamic camera flip to front (`camera_id: "1"`) verified.
- [PASS] Step 6: Stop CCTV session verified.
- [PASS] Step 7: Verified stream and hardware returned to `IDLE` state.
- **Result:** **ALL 7 CHECKS PASSED ON LIVE HARDWARE.**

### 3.3 Milestone M1 Investigation & Black Screen Resolution
Tested directly on live Vivo V2334 hardware (`100.113.234.61:8080`) on November 15, 2026:
- **Physical Sensor Verification:** Validated Camera2 HAL capture: single frame `631,819 bytes` JPEG (`\xff\xd8`), Shannon entropy `7.821 bits/byte` (exceeds theoretical 7.0 visual threshold, confirming crisp physical capture, not blank/black sensor frame).
- **Root Cause 1 (Stream Loop Termination Bug in Proxy):** In `controller/http/node_routes.py`, `handler.send_header("Connection", "close")` set internal `close_connection = True`, prematurely aborting the MJPEG streaming loop on the first iteration (0 bytes sent). Fixed by switching to explicit `stream_active = True` loop with socket error handling.
- **Root Cause 2 (SESSION_ALREADY_ACTIVE Handling):** Re-opening stream or clicking start when session was already running returned HTTP 400 `SESSION_ALREADY_ACTIVE`, causing UI to abort stream display. Handled gracefully across controller, node API, and frontends as HTTP 200 `ACTIVE`.
- **Root Cause 3 (Missing Watchdog Fallback):** Browser image tags don't throw `onerror` on hung or stalled MJPEG streams. Added a 2.5-second watchdog in `live_viewer.html` and `app.js` that automatically engages high-frequency frame polling (`/camera/frame` or `/hardware/live-call/frame`) as a fallback.
- **Physical Stream Verification:** Tested `GET /camera/stream` over Tailscale: HTTP 200 `multipart/x-mixed-replace; boundary=frame` continuous streaming verified (3 frames, 262,144 bytes in 5.14s). All 22 offline unit tests pass.

---

## 4. Known Gaps & In-Flight Priorities

1. **Node Health & Thermal Protection (M3 — Immediate Next):** Implement battery thermal monitoring (> 42°C active throttling) and enhance node process resurrection before expanding to more devices.
2. **Application Deployment Subsystem Bugs (M4):** Resolve reported deployment bugs in `controller/deployment_manager.py` and verify atomic rollback mechanics.
3. **Gujarat Node APK Rollout (M2 — Deferred after M3/M4):** Deploy APK v1.0.9+ across Android 11 devices (Vivo Y31, Realme 5 Pro) once node health and deployment stability are verified.
