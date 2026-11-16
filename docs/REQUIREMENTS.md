# PersonalServer — Requirements Traceability Matrix

This matrix maps all functional and operational requirements to their milestone, priority, implementing files, and verification test suites.

---

## 1. Camera & CCTV Subsystem (`CAM-xxx`)

| ID | Description | Priority | Milestone | Implementing Files | Verification Test | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **CAM-001** | Stream 720p 15 FPS video via Camera2 HAL | P0 | M0 | `LiveMediaBridgeManager.java`, `services/node-api/app.py` | `tests/integration/test_vivo_cctv_streaming.py` | **Verified** |
| **CAM-002** | Decouple video from microphone permissions | P0 | M0 | `LiveMediaBridgeManager.java` | `tests/unit/test_native_android_bridge_lifecycle.py` | **Verified** |
| **CAM-003** | Immediate hardware release to `IDLE` on stop | P0 | M0 | `LiveMediaBridgeManager.java` | `tests/integration/test_vivo_cctv_streaming.py` (Step 7) | **Verified** |
| **CAM-004** | Dynamic front/rear camera flip during stream | P1 | M0 | `LiveMediaBridgeManager.java`, `agent/hardware/live_call.py` | `tests/integration/test_vivo_cctv_streaming.py` (Step 5) | **Verified** |
| **CAM-005** | Session WakeLock for screen-off streaming | P1 | M0 | `LiveMediaBridgeManager.java`, `BridgeService.java` | Code audit & physical verification | **Verified** |
| **CAM-006** | Embed live CCTV viewer in main dashboard | P0 | M1 | `services/node-api/static/index.html`, `app.js` | Browser inspection | **Planned** |
| **CAM-007** | Optional microphone audio listen toggle | P1 | M1 | `app.js`, `services/node-api/app.py` | Unit / integration test | **Planned** |
| **CAM-008** | Real-time telemetry overlay on stream | P2 | M1 | `app.js`, `live_viewer.html` | UI visual check | **Planned** |

---

## 2. Node Lifecycle & Android Companion App (`NODE-xxx`)

| ID | Description | Priority | Milestone | Implementing Files | Verification Test | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **NODE-001** | One-tap startup via RUN_COMMAND intent | P0 | M0 | `BridgeActivity.java`, `NodeStartupStateMachine.java` | Physical tap validation | **Verified** |
| **NODE-002** | Persistent foreground service lifecycle | P0 | M0 | `BridgeService.java`, `AndroidManifest.xml` | Foreground notification check | **Verified** |
| **NODE-003** | Tailscale detection & zero-touch coordinate toggle | P0 | M3 | `TailscaleRecoveryManager.java`, `docs/features/failsafe-recovery.md` | VPN disconnect & tap test | **In Progress** |
| **NODE-004** | Cross-device Android 11 & 16 compatibility | P0 | M2 | `build.gradle`, `BridgeService.java` | Physical test on Y31 & R5P | **Scheduled** |
| **NODE-005** | Thermal throttling when battery > 42°C | P1 | M3 | `agent/node-agent.py`, `node_telemetry.py` | Unit test with simulated thermal | **Verified** |
| **NODE-006** | Auto-resurrection via active doctor repair (`doctor.sh fix`) | P0 | M3 | `scripts/doctor.sh`, `scripts/doctor.py` | Process kill recovery test | **In Progress** |

---

## 3. Cluster Control Plane & Ingress (`CTRL-xxx`)

| ID | Description | Priority | Milestone | Implementing Files | Verification Test | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **CTRL-001** | HA active-standby term election & sync | P0 | M0 | `config/ha_manager.py` | `tests/ha/` test suite | **Verified** |
| **CTRL-002** | Inter-node mesh strictly over Tailscale | P0 | M0 | `controller/controller.py`, `node.conf` | Network interface binding audit | **Verified** |
| **CTRL-003** | Public ingress via Cloudflare Zero Trust | P0 | M0 | `router/app_router.py`, Cloudflare Tunnel | Public domain access check | **Verified** |
| **CTRL-004** | Universal Node Bootstrapper v0.1 | P1 | M0 | `bootstrap/*.sh` | `tests/unit/test_bootstrapper.py` | **Verified** |
| **CTRL-005** | Reverse proxy routing & media stream proxy | P1 | M0 | `controller/http/node_routes.py` | `test_e07_live_call.py` | **Verified** |

---

## 4. Telemetry & Sensor Subsystem (`TELE-xxx`)

| ID | Description | Priority | Milestone | Implementing Files | Verification Test | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TELE-001** | Real-time battery %, charging state, dBm | P0 | M0 | `config/node_telemetry.py` | Heartbeat payload test | **Verified** |
| **TELE-002** | CPU temperature & thermal status reporting | P0 | M0 | `config/node_telemetry.py` | Heartbeat payload test | **Verified** |
| **TELE-003** | Ambient light & motion sensor bridging | P2 | M5 | `SensorBridgeManager.java` | Sensor provider unit test | **Partial** |
| **TELE-004** | Low battery emergency alert broadcast | P1 | M3 | `controller/notifications.py` | Alert dispatcher test | **Scheduled** |
