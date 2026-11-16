# PersonalServer — Architecture Decision Records (ADRs)

This document records the foundational architectural decisions governing PersonalServer.  
**Rule:** Never silently reverse an established decision without an explicit ADR and owner approval.

---

## ADR-001: Zero External Runtime Dependencies for Core Services
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** Deploying external database engines (PostgreSQL, MySQL, Redis, MongoDB) and message brokers on recycled, memory-constrained Android phones creates high memory pressure, difficult process supervision, and complex recovery failure modes.
- **Decision:** All core controller state, scheduling, consensus, and storage mechanisms must be implemented exclusively using the **Python 3.9+ standard library** (`urllib`, `http.server`, `json`, `threading`, `socket`). Persistence is handled via atomic read-modify-write JSON stores.
- **Tradeoffs:** Requires custom lock management and transaction safety in Python, but delivers zero-install simplicity, sub-second startup, and negligible RAM consumption (<150 MB total).

---

## ADR-002: Private Mesh Exclusivity (Tailscale + Cloudflare Zero Trust)
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** Nodes are distributed across different states (Punjab and Gujarat) behind carrier-grade NATs (CGNAT) and mobile data connections. Port forwarding on home routers is insecure and often technically impossible.
- **Decision:** All inter-node communication, heartbeats, and media streams flow exclusively over an encrypted **Tailscale WireGuard Mesh** (`100.x.y.z`). Web ingress for operators without Tailscale is routed via Cloudflare Tunnel with Cloudflare Zero Trust Access authentication. Never open public router ports.
- **Tradeoffs:** Requires Tailscale on edge nodes, but eliminates firewall configuration, dynamic DNS complexity, and public attack surfaces.

---

## ADR-003: Unprivileged Android Native Bridge via Local Loopback IPC
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** Unrooted Android devices running Termux cannot directly access Android OS hardware APIs (Camera2, AudioRecord, SensorManager, PowerManager) via standard Linux device files (`/dev/video*`).
- **Decision:** Implement a lightweight Android companion app (`com.personalserver.bridge`) that exposes hardware APIs to Termux over local loopback HTTP (`127.0.0.1:8765`) and raw media streams (`127.0.0.1:8766`).
- **Tradeoffs:** Requires maintaining a small Java Android app alongside the Python codebase, but allows full hardware access on non-rooted Android 11 through Android 16 devices.

---

## ADR-004: On-Demand Camera2 Streaming vs. Continuous 24/7 NVR
- **Date:** November 2026
- **Status:** **Accepted (Confirmed in Owner Interview)**
- **Context:** Running phone camera sensors 24/7 generates continuous heat, accelerates Li-ion battery degradation, consumes internal flash storage with rolling video buffers, and strains battery longevity.
- **Decision:** CCTV video streaming is strictly an **on-demand feature**. The Camera2 HAL is initialized only when an authenticated viewing session starts and is immediately torn down to `IDLE` when the client disconnects. Continuous 24/7 video recording and motion detection pipelines are non-goals for V1.
- **Tradeoffs:** Does not provide retrospective surveillance footage, but guarantees device safety, low thermal load, and long-term phone longevity.

---

## ADR-005: Decoupled Video and Microphone Execution
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** In early prototypes, failure to acquire microphone permissions or audio hardware locks aborted the entire CCTV video session.
- **Decision:** Decouple camera and microphone permissions and lifecycles. Video streaming proceeds uninterrupted if microphone permissions are absent or denied. Audio is treated as an optional companion channel.
- **Tradeoffs:** Requires handling separate audio/video state machines, but prevents audio permission failures from blocking critical video feeds.

---

## ADR-006: Dedicated Session WakeLock & Foreground Notification
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** Android OS aggressively terminates background CPU tasks and cuts off camera sensors when the screen turns off to conserve battery.
- **Decision:** Acquire a dedicated session `PowerManager.WakeLock` (`PARTIAL_WAKE_LOCK`, tag `PersonalServer:CCTVSessionWakeLock`) for the duration of an active stream, accompanied by an Android Foreground Service notification declaring `FOREGROUND_SERVICE_TYPE_CAMERA`. Release the lock immediately on session termination.
- **Tradeoffs:** Modest battery consumption while actively streaming with screen off, but completely prevents stream freeze and process death.

---

## ADR-007: Modular Shell-Based Universal Bootstrapper
- **Date:** November 2026
- **Status:** **Accepted**
- **Context:** Onboarding heterogeneous devices previously required disparate, platform-specific manual setup instructions.
- **Decision:** Implement a unified, modular bootstrapper in `bootstrap/` (`detect.sh`, `dependencies.sh`, `install.sh`, `configure.sh`, `uninstall.sh`) capable of auto-detecting the environment (Termux vs. Debian/Ubuntu vs. Raspberry Pi) and configuring the node with zero manual configuration.
- **Tradeoffs:** Requires testing across multiple shell environments, but enables one-command node onboarding.
