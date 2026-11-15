# Project Context & Long-Term Vision

**Project:** PersonalServer  
**Owner:** Akshat Sahay  
**Scope:** Private, self-hosted, modular personal computing and distributed infrastructure.

---

## 1. Vision & Core Philosophy

The vision of PersonalServer is to build an autonomous personal infrastructure platform using hardware already owned by the user. Rather than relying on centralized third-party cloud VMs or proprietary IoT camera ecosystems, PersonalServer turns old smartphones, laptops, single-board computers, and workstations into an integrated, self-healing personal server cluster.

### Key Philosophy Points:
1. **Recycled Hardware as First-Class Citizens:** Android smartphones provide low power (<3W idle), built-in battery backup (UPS), and cellular/WiFi connectivity. They make exceptional edge servers when properly supervised.
2. **Modular Evolution:** The platform grows in deliberate, verified milestones rather than sweeping, disruptive overhauls.
3. **Zero External Runtime Dependencies:** Core services require only the standard library of Python 3.9+. No mandatory Docker, external database engines, or complex cloud orchestrators.
4. **Local-First & Sovereign:** All data, configurations, and video streams stay strictly within the owner's private network mesh.

---

## 2. Cluster Topology & Geographic Reality

The PersonalServer cluster currently spans multiple geographic locations across India:
- **Punjab Node (Active Testing):**
  - `server-v2334-a531` (Vivo V2334, Android 16 / API 36, ARM64): Active test node for Native Bridge, Camera2 720p streaming, and Universal Bootstrapper.
- **Gujarat Nodes (Production Controller Plane & Worker):**
  - `server-y31-b842` (Vivo Y31, Android 11, ARM64): Primary Active Controller / Leader (`100.85.108.5:8000`).
  - `server-r5p-c913` (Realme 5 Pro, Android 11, ARM64): Compute Worker & Standby Controller (`100.73.52.72:8000`).
- **Development Workstation:**
  - Windows 11 PC (`100.64.1.1`): On-Demand Build Worker and developer workstation.

### Network Backbone:
- **Tailscale WireGuard Mesh (`100.x.y.z`):** Encrypted peer-to-peer mesh between Punjab, Gujarat, and laptop.
- **Cloudflare Zero Trust Ingress (`server.akshatsahay.space`):** Authenticated web access from browsers without VPN client installation.

---

## 3. Product Roles & Terminology

- **Master Controller:** Central REST API, authoritative state stores, and scheduler.
- **Standby Controller:** HA replica that syncs state (`/ha/sync`) and takes over via term election if the leader drops off.
- **Node Agent:** Lightweight daemon (`agent/node-agent.py`) running on edge nodes, reporting battery/CPU telemetry and claiming scheduled jobs.
- **Android Native Bridge (`com.personalserver.bridge`):** Java foreground service exposing Android hardware (Camera2, AudioRecord, Sensors, WakeLock) over local loopback (`:8765`, `:8766`).
- **CCTV Capability:** On-demand live video streaming mode in the bridge and Node API. Camera is powered on only while viewing; hardware is immediately freed on disconnect.
- **Universal Bootstrapper:** Unified shell-based setup tool (`bootstrap/*.sh`) for onboarding Termux, Linux, and Raspberry Pi nodes.
