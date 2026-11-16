# PersonalServer — Agent Instructions & Operating Rules

> **Primary Instruction File for Antigravity & AI Development Agents**  
> **Status:** Active  
> **System Architecture:** Distributed Edge Computing & Personal Infrastructure Platform  
> **Current Milestone:** `M0 — Project Intelligence & Documentation Foundation` (Transitioning to `M1`)

---

## 1. What PersonalServer Is

PersonalServer is a private, lightweight, zero-external-dependency self-hosted personal infrastructure platform. It transforms heterogeneous personal devices—such as Android smartphones (via Termux), Raspberry Pis, Linux servers, and Windows development PCs—into an autonomous personal server cluster.

### Core Architecture Reality
- **Zero Heavy Runtimes:** Core controller, agent, consensus engine, and storage subsystems run exclusively on the **Python 3.9+ standard library**.
- **Geographically Distributed Topology:** Nodes operate across distant locations (e.g., test node Vivo V2334 in Punjab; active controller Vivo Y31 and worker Realme 5 Pro in Gujarat) connected via **Tailscale WireGuard Mesh** (`100.x.y.z`).
- **External Ingress:** Cloudflare Tunnel + Cloudflare Zero Trust Access (`*.akshatsahay.space`) provides secure operator access from any browser without exposing router ports.
- **Android Native Bridge (`com.personalserver.bridge`):** Java foreground service bridging Android platform capabilities (Camera2 720p streaming, audio, sensors, WakeLock) to unprivileged Termux Python processes over loopback (`:8765`, `:8766`).
- **CCTV Role:** On-demand live video streaming inspection feature within the server ecosystem. **Not** a 24/7 NVR recording appliance; hardware is strictly released to `IDLE` when not in use.

---

## 2. Master Principles & Non-Negotiable Rules

Every agent operating in this repository **must strictly observe** the following rules:

1. **Inspect Before Modifying:** Never assume the state of code, git branches, or physical devices. Always inspect files and verify real runtime processes first.
2. **Never Rewrite Working Code:** Prefer minimal, targeted changes. Do not discard working architecture without a demonstrated failure and user approval.
3. **No Heavy External Dependencies:** Never introduce external databases (PostgreSQL, MySQL, Redis, MongoDB) or heavy message queues into core cluster state. Use Python standard library atomic JSON stores.
4. **No Docker / Containers on Android:** Unrooted Android/Termux does not support kernel cgroups. Never attempt to run Docker daemons on Android. Containerized workloads are scheduled exclusively on Linux/Windows nodes.
5. **No Public Port Forwarding:** Never require opening home router WAN ports or UPnP. All networking must remain behind Tailscale and Cloudflare Zero Trust.
6. **No Cloud Vendor Lock-In:** Core cluster services must never depend on AWS, GCP, or Azure services to function.
7. **Strict Multi-Tier Verification Gate:** Never declare a feature or bug fix "Done" without test evidence:
   - Automated offline tests (`pytest`).
   - Physical device command verification (real PID, listening port, HTTP 200 payload).
   - End-to-end stream or payload confirmation with exact timestamps.
   - Clean git status and commit.
8. **Distinguish Verified Facts vs. Assumptions:** Clearly label what has been proven by physical device output versus what is theoretical.

---

## 3. Repository Structure & Key Locations

```text
PersonalServer/
├── AGENTS.md                                # This authoritative instruction file
├── README.md                                # Public project overview & quickstart
├── pyproject.toml                           # Python project configuration & pytest settings
│
├── .agent/                                  # Machine-readable development policies
│   ├── rules/                               # Rules for context, coding, security, and workflow
│   └── workflows/                           # Standard operational workflows
│
├── docs/                                    # Authoritative Project Documentation
│   ├── PROJECT.md                           # Long-term vision, philosophy, and boundaries
│   ├── PRD.md                               # Master Product Requirements Document
│   ├── ARCHITECTURE.md                      # System architecture, data flows, and subsystem maps
│   ├── ROADMAP.md                           # Milestone tracking (M0 through M5)
│   ├── CURRENT-STATUS.md                    # Verified capabilities, test evidence, known gaps
│   ├── DECISIONS.md                         # Architecture Decision Records (ADRs)
│   ├── REQUIREMENTS.md                      # Traceability matrix (CAM-xxx, NODE-xxx)
│   ├── SECURITY.md                          # Threat model, token handling, network boundaries
│   ├── TEST-PLAN.md                         # Automated, physical, and integration test protocols
│   └── features/                            # Deep-dive feature specifications
│
├── agent/                                   # Node Agent & runtime executors
│   ├── device_runtime/                      # Android & Linux hardware capability providers
│   ├── hardware/                            # Camera, microphone, speaker, live call managers
│   └── node-agent.py                        # Node lifecycle, heartbeats, and metrics reporter
│
├── android/                                 # Android companion applications
│   └── native-bridge/                       # Active companion app (com.personalserver.bridge)
│
├── bootstrap/                               # Universal Node Bootstrapper V0.1 (detect, install, config)
│
├── config/                                  # Configuration, HA manager, platform mappings
│   ├── ha_manager.py                        # Active-Standby consensus & term election
│   ├── node_telemetry.py                    # Battery, CPU thermal, WiFi telemetry
│   └── release.conf                         # Staged release definitions
│
├── controller/                              # Master Controller Plane
│   ├── controller.py                        # REST API, node registry, and state management
│   ├── deployment_manager.py                # Multi-runtime application deployment lifecycle
│   ├── leader_ranking.py                    # HA performance scoring & cooperative handover
│   └── stores/                              # Atomic read-modify-write JSON stores
│
├── router/                                  # Reverse Proxy Router (:8088 / :8080)
├── scheduler/                               # Universal Scheduler (8-stage eligibility pipeline)
├── scripts/                                 # Lifecycle scripts (start.sh, status.sh, doctor.py)
├── services/node-api/                       # Local Node API server & Web Console (:8080)
│   ├── app.py                               # Node API routes & media stream proxy
│   └── static/                              # Web Operations Console & CCTV viewer
│
└── tests/                                   # Test suites (unit, integration, canary, ha)
```

---

## 4. Current Milestone Priorities

1. **M0 — Project Intelligence & Documentation Foundation:**
   - Establish `AGENTS.md`, `.agent/rules/`, `.agent/workflows/`, and `docs/`.
   - Lock in requirements from the user interview.
2. **M1 — CCTV & Camera Dashboard Integration (Completed & Stabilized on V2334):**
   - Embed live 720p/480p Camera2 video feed and controls directly into the central Operations Dashboard (`server.akshatsahay.space` / `:8080`).
   - Integrated front/rear camera flip, strict on-demand auto-release, and real-time telemetry overlay.
3. **M3 — Node Health, Thermal Protection & Auto-Recovery (Completed):**
   - Enforce active battery thermal throttling (> 42°C) and background process resurrection.
   - Guard mobile hardware longevity and self-healing across nodes before multi-device rollout.
4. **M4 — Deployment Pipeline Fixes (Current Focus):**
   - Reconfigure and resolve application deployment bugs in `controller/deployment_manager.py`.
   - Ensure clean atomic rollbacks when health checks fail across multi-runtime applications.
5. **M2 — Cross-Device APK Stabilization & Multi-Node Rollout:**
   - Deploy and physically validate unified Native Bridge APK v1.0.9 on Gujarat nodes (Vivo Y31, Realme 5 Pro).
   - Cross-verify Android 11 vs Android 16 platform differences after health & deployment systems are solid.

---

## 5. Development Workflow Overview

Every significant feature must follow the 11-stage pipeline:
```text
IDEA ➔ REQUIREMENTS ➔ PRD SPEC ➔ ARCHITECTURE REVIEW ➔ IMPLEMENTATION PLAN
  ➔ USER APPROVAL ➔ IMPLEMENTATION ➔ AUTOMATED TEST ➔ PHYSICAL TEST
    ➔ DOCS UPDATE ➔ RELEASE
```

For bug fixes and small improvements, always isolate the bug with an automated test before applying minimal, targeted code changes.

Refer to:
- `.agent/rules/development-workflow.md`
- `docs/PRD.md`
- `docs/ARCHITECTURE.md`
- `docs/CURRENT-STATUS.md`
