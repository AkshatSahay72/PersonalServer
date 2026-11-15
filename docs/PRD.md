# PersonalServer — Product Requirements Document (PRD)

**Document Version:** 1.0.0  
**Status:** Approved Baseline (Post-Interview)  
**Target Milestone:** M0 Completion / M1 Planning  
**Last Updated:** November 15, 2026  

---

## 1. Problem Statement

Personal computing hardware—especially retired Android smartphones—frequently ends up unused in drawers despite possessing multi-core processors, several gigabytes of RAM, high-resolution cameras, battery backups, and wireless networking. Meanwhile, self-hosters and developers face recurring cloud costs, vendor lock-in, and privacy concerns when deploying private services, remote inspection cameras, and automation.

Existing solutions either require specialized hardware (e.g. Raspberry Pis with scalped pricing), complex cloud setups (Kubernetes, AWS), or invasive proprietary camera apps that phone home to third-party cloud servers.

PersonalServer solves this by providing a lightweight, private, self-hosted platform that seamlessly bridges phone hardware into a secure compute cluster.

---

## 2. Target Users & Use Cases

### Target Users:
- **Self-Hosters & Tech Enthusiasts:** Users seeking private, sovereign cloud alternatives without subscription fees.
- **Hardware Recyclers:** Users who want to put old phones and laptops to practical, productive 24/7 use.
- **Privacy-Conscious Remote Observers:** Users requiring private live video inspection (CCTV) of remote locations (e.g. home, office, property) without exposing video feeds to external clouds.

### Core Use Cases:
1. **On-Demand Remote Inspection (CCTV Node):** The user opens their laptop browser from Punjab and views a crisp 720p 15 FPS live video feed from an Android phone situated in Gujarat, flips cameras, checks battery status, and closes the tab without leaving camera hardware active.
2. **Distributed Personal Server:** Running private APIs, light Python workloads, file sharing, and web apps across multiple edge nodes.
3. **One-Tap Node Management:** Installing the companion app on an Android phone, tapping "Start", and having the node automatically initialize Termux, register with the cluster, and run in the background.

---

## 3. Goals & Explicit Non-Goals

### Goals:
- **G-1:** Enable any supported Android device (Android 11 through Android 16) to act as a resilient cluster node with one-tap startup.
- **G-2:** Deliver on-demand 720p 15 FPS Camera2 live video streaming with sub-second latency over private Tailscale networking.
- **G-3:** Centralize multi-node monitoring and live camera inspection within the central Operations Dashboard.
- **G-4:** Protect hardware longevity via automated thermal and battery monitoring.
- **G-5:** Maintain zero external runtime dependencies for core cluster operations (Python 3 stdlib only).

### Explicit Non-Goals:
- **NG-1 (No 24/7 NVR Video Recording):** PersonalServer will not continuously record, transcode, or store multi-gigabyte video archives on phone storage. Streaming is strictly on-demand.
- **NG-2 (No External Database Engines):** PersonalServer will not require PostgreSQL, MySQL, Redis, or MongoDB for core state management.
- **NG-3 (No Docker on Android):** Docker will not be deployed or required on Android/Termux nodes.
- **NG-4 (No Public Port Forwarding):** PersonalServer will never rely on open router ports or UPnP.
- **NG-5 (No Cloud Vendor Dependencies):** Core operations must run independently of AWS, GCP, or Azure services.

---

## 4. Functional Requirements

### 4.1 Camera & CCTV Streaming Subsystem (`CAM-xxx`)

| ID | Priority | Requirement Description | Status |
| :--- | :--- | :--- | :--- |
| **CAM-001** | P0 | Node can capture and stream live video via Camera2 HAL at 720p (1280x720) @ 15 FPS. | **Verified (v1.0.8)** |
| **CAM-002** | P0 | Video streaming operates independently of microphone permissions (decoupled audio). | **Verified (v1.0.8)** |
| **CAM-003** | P0 | Hardware camera is released to `IDLE` immediately when a stream ends (zero resource leak). | **Verified (v1.0.8)** |
| **CAM-004** | P1 | Dynamic camera switching between rear (`camera_id: "0"`) and front (`camera_id: "1"`) during an active session. | **Verified (v1.0.8)** |
| **CAM-005** | P1 | Dedicated session `WakeLock` (`PARTIAL_WAKE_LOCK`) maintains streaming when screen turns off. | **Verified (v1.0.8)** |
| **CAM-006** | P0 | Live video feed and controls are directly embedded into the central Cluster Operations Dashboard. | **Planned (M1)** |
| **CAM-007** | P1 | Optional listen-in microphone audio toggle alongside the live video feed. | **Planned (M1)** |
| **CAM-008** | P2 | Real-time telemetry overlay on stream (battery %, CPU temperature, charging status). | **Planned (M1)** |

---

### 4.2 Node Lifecycle & Companion App (`NODE-xxx`)

| ID | Priority | Requirement Description | Status |
| :--- | :--- | :--- | :--- |
| **NODE-001** | P0 | One-tap node startup from companion app (`Start PersonalServer` triggers Termux via `RUN_COMMAND`). | **Verified (v1.0.8)** |
| **NODE-002** | P0 | Background execution persistence via Android Foreground Service (`BridgeService`). | **Verified (v1.0.8)** |
| **NODE-003** | P0 | Tailscale VPN connectivity detection and automatic node registration resumption. | **Verified (v1.0.8)** |
| **NODE-004** | P0 | Unified compatibility across Android 11 (API 30) and Android 16 (API 36). | **In Progress (M2)** |
| **NODE-005** | P1 | Active thermal protection: throttle or pause camera streams if battery exceeds 42°C. | **Planned (M3)** |
| **NODE-006** | P1 | Automated node supervisor recovery: restart failed Node API or worker processes. | **Verified (v1.0.8)** |

---

### 4.3 Cluster Control Plane & Networking (`CTRL-xxx`)

| ID | Priority | Requirement Description | Status |
| :--- | :--- | :--- | :--- |
| **CTRL-001** | P0 | Active-Standby High Availability consensus and term election across mobile nodes. | **Verified** |
| **CTRL-002** | P0 | Secure inter-node communication strictly over private Tailscale WireGuard mesh. | **Verified** |
| **CTRL-003** | P0 | Public web ingress via Cloudflare Tunnel with Zero Trust Access policies. | **Verified** |
| **CTRL-004** | P1 | Universal Node Bootstrapper (`bootstrap/*.sh`) for one-command onboarding. | **Verified (v0.1)** |
| **CTRL-005** | P1 | Reverse proxy routing (`:8088`) for deployed applications and camera streams. | **Verified** |

---

## 5. Non-Functional Requirements

1. **Performance & Latency:**
   - Live camera streaming glass-to-glass latency must remain under 1.0s over Tailscale.
   - Stream bandwidth must be constrained to 500–800 kbps for 720p @ 15 FPS.
2. **Resource Efficiency:**
   - Standby CPU consumption on Android nodes must remain below 3% when idle.
   - Memory footprint of Node API and Worker combined must remain below 150 MB RAM.
3. **Hardware Longevity:**
   - Thermal limits enforced to prevent Li-ion battery swelling on 24/7 plugged-in devices.
4. **Resilience & Fault Tolerance:**
   - Automatic leader election failover completed within 15 seconds of leader loss.
   - Transparent stream reconnection if network jitter causes momentary packet drop.

---

## 6. Acceptance Criteria & Quality Gates

A requirement is only considered **Done** when:
1. Automated unit test suite passes with zero regressions (`pytest`).
2. Physical hardware execution is verified with actual process IDs, open listening ports, and valid HTTP responses.
3. End-to-end user experience matches the specification on live devices.
4. Relevant documentation in `docs/` is updated and synchronized.
5. Changes are committed cleanly with conventional commit messages.

---

## 7. Risks & Mitigation Strategies

| Risk | Impact | Mitigation Strategy |
| :--- | :--- | :--- |
| **OEM Background Killing (FuntouchOS, ColorOS)** | High | User is guided to whitelist app in battery settings; app acquires `PARTIAL_WAKE_LOCK` and runs `FOREGROUND_SERVICE`. |
| **Thermal Accumulation on Charging Phones** | Medium | Active thermal monitor pauses high-load tasks and camera when battery temp exceeds 42°C. |
| **Geographic Partition (Punjab ↔ Gujarat)** | Medium | Tailscale mesh handles NAT traversal; High Availability consensus allows nodes to operate safely during partitions. |
| **Large Monolithic Code Files** | Medium | Incremental decomposition of `app.py` into dedicated route handlers with full regression test coverage. |
