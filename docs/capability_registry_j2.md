# PersonalServer Capability Registry (CR) v1.0
## Milestone J2 Architectural Specification (Amended)

**Status:** Final Specification (J2 Frozen)  
**Author:** Antigravity Engineering  
**Version:** cr.v1.0  
**Phase:** PersonalServer Phase 2 — Universal Job Execution, Milestone J2  

---

## 1. Architecture Overview

### 1.1 Mission & Scope
Phase 1 established the frozen universal job execution foundation (R1–R9 application deployment, J1 Universal Job Contract `ujc.v1.0`). To schedule universal workloads effectively across heterogeneous nodes, the cluster requires a **unified, runtime-independent Capability Registry**.

PersonalServer's physical topology consists of:
1. **Vivo Y31 (Android 11, Termux, aarch64, 8 cores, ~5.7 GB RAM, 106 GB storage):** Primary control plane, ingress, and Application Router.
2. **Realme 5 Pro (Android 11, Termux, aarch64, 8 cores, ~5.6 GB RAM, 50 GB storage, udocker):** Primary compute worker.
3. **Windows 11 Laptop (x86_64, 12 cores, 24 GB RAM, 1 TB SSD, NVIDIA RTX 3050 6 GB VRAM, Docker Engine):** Optional, opportunistic worker. Must remain non-essential; PersonalServer's primary operations must run 100% on Android nodes without the laptop online.

The Capability Registry provides a formal schema and lifecycle for discovering, registering, tracking, and querying the actual capabilities of every connected node without making assumptions or inventing specs.

### 1.2 Core Architectural Principles
1. **Node Authority with Zero Invention:** Nodes are authoritative regarding their locally discovered hardware and installed runtimes. The controller indexes and stores reports but never fabricates capabilities.
2. **Strict Separation of Static Hardware vs. Dynamic Telemetry:**
   - **Static Capabilities:** CPU architecture, physical core count, total RAM, GPU device model, supported runtimes, physical peripheral presence. These change rarely (typically only across reboots, package installations, or hardware reconfiguration).
   - **Dynamic Telemetry:** Instantaneous CPU utilization, available RAM, available disk space, battery level, charging status, thermal state, and network latency. These fluctuate continuously and are delivered via ephemeral heartbeats.
3. **Four-State Hardware Capability Lifecycle:** Explicitly separates physical discovery from operational access: `DETECTED`, `ACCESSIBLE`, `UNAVAILABLE`, and `UNKNOWN`. Hardware detection never implies application permission or capture availability.
4. **Verified vs. Advertised Capability Model:** Mere advertisement (e.g. in `node.json`) is not proof. Capabilities must be verified via harmless, bounded, worker-side smoke tests (`capability_prober.py`). The registry stores both advertised and verified states.
5. **Separation of Physical Inventory from Administrative Policy:** Administrative restrictions, draining, and temporary scheduling overrides reside in a separate controller policy layer and **never** rewrite the node's underlying physical capability inventory.
6. **Hardware Access Isolation & Gated Permissions:** Advertising physical peripherals (camera, microphone, speaker, GPU) does **not** grant workloads arbitrary access. Access requires explicit job declaration, operator access leases, and user-visible indicators.
7. **Offline Eligibility Invariant:** Stored capability inventory for an offline node (e.g., the Windows laptop) can **never** make the node eligible for scheduling. Liveness gating is absolute.
8. **Zero Disruption to R1–R9 & J1:** Existing scheduler matching (`ResourceScheduler.is_node_eligible`), node registration (`POST /register`), heartbeat endpoints (`POST /heartbeat`), and legacy deployment job requirements remain 100% backward-compatible.

---

## 2. Versioned Capability Taxonomy (`cr.v1.0`)

The capability model organizes node attributes into distinct namespaces:

```
Node Capability Document (`cr.v1.0`)
├── metadata    (Schema version, agent version, discovered_at, verified_at)
├── compute     (Architecture, cores, total RAM, GPU acceleration)
├── runtime     (Verified interpreters: Python, Node.js, Docker, udocker, Static)
├── storage     (Total capacity, filesystem types, writable mount paths)
├── network     (Interface type, IPs, reachability, ping latency, bandwidth)
├── hardware    (Physical camera, microphone, speaker, sensors with explicit 4-state flags)
└── mobile      (Battery %, charging source, thermal status, power-saving)
```

### 2.1 Hardware Capability States
Physical peripherals and sensors declare an explicit operational status:
- **`DETECTED`**: Physical device node or hardware enumeration is confirmed present by the operating system kernel or driver (e.g., `/dev/video0`, DirectShow camera enumerated), but OS/user permission has not yet been granted to the PersonalServer agent process.
- **`ACCESSIBLE`**: Hardware is present, OS-level application permission is confirmed granted to the agent process, and local probes confirm device open/query without error. *(Note: Accessible indicates agent-level viability; it does NOT grant workloads permission to capture without an explicit job lease).*
- **`UNAVAILABLE`**: Hardware is known to exist or was previously accessible, but is currently offline, disabled by physical hardware switch/privacy shutter, locked by an exclusive third-party process, or throttled by thermal policy.
- **`UNKNOWN`**: Hardware detection probe timed out, subsystem failed, or required platform libraries (e.g. optional Termux:API) are absent.

*Critical Invariant:* Hardware detection (`DETECTED`) MUST NOT imply application permission or capture availability.

---

## 3. Concrete Node Capability Document

Below is the concrete capability document for the **Realme 5 Pro (`server-95bad5ff01424d4c8d184330d6d2e394`)**:

```json
{
  "schema_version": "cr.v1.0",
  "node_id": "server-95bad5ff01424d4c8d184330d6d2e394",
  "name": "Realme 5 Pro",
  "role": "compute",
  "platform": "termux",
  "os": "Android 11",
  "metadata": {
    "agent_version": "1.4.2",
    "schema_version": "cr.v1.0",
    "discovered_at": "2026-11-05T16:50:00.000000Z",
    "verified_at": "2026-11-05T16:55:00.000000Z",
    "verification_ttl_sec": 86400,
    "is_stale_verification": false
  },
  "capabilities": {
    "compute": {
      "architecture": "arm64",
      "cpu_cores": 8,
      "cpu_model": "Qualcomm Snapdragon 712",
      "ram_total_mb": 5616,
      "gpu": {
        "status": "UNAVAILABLE",
        "vendor": "qualcomm",
        "model": "Adreno 616",
        "vram_total_mb": 0,
        "compute_apis": []
      }
    },
    "runtime": {
      "python": {
        "verified": true,
        "version": "3.14.6",
        "path": "/data/data/com.termux/files/usr/bin/python3",
        "last_verified_at": "2026-11-05T16:55:00Z"
      },
      "nodejs": {
        "verified": false,
        "version": null,
        "path": null,
        "last_verified_at": null
      },
      "container": {
        "docker_engine": {
          "verified": false,
          "error": "Docker daemon unreachable or not installed in Termux"
        },
        "udocker": {
          "verified": true,
          "version": "1.3.17",
          "last_verified_at": "2026-11-05T16:55:02Z"
        }
      },
      "static": {
        "verified": true,
        "last_verified_at": "2026-11-05T16:55:00Z"
      }
    },
    "storage": {
      "total_gb": 50.3,
      "filesystem": "f2fs",
      "writable_roots": [
        "/data/data/com.termux/files/home/PersonalServer/runtime"
      ]
    },
    "network": {
      "interface_type": "wifi",
      "tailscale_ip": "100.73.52.72",
      "local_ip": "192.168.1.105",
      "reachability": "mesh"
    },
    "hardware": {
      "camera": {
        "status": "DETECTED",
        "devices": [
          {"id": 0, "facing": "back", "resolution": "48MP"},
          {"id": 1, "facing": "front", "resolution": "16MP"}
        ],
        "requires_lease": true
      },
      "microphone": {
        "status": "DETECTED",
        "requires_lease": true
      },
      "speaker": {
        "status": "ACCESSIBLE",
        "requires_lease": false
      },
      "sensors": [
        {"name": "battery_temperature", "status": "ACCESSIBLE"},
        {"name": "accelerometer", "status": "DETECTED"}
      ]
    },
    "mobile": {
      "is_mobile": true
    }
  },
  "telemetry": {
    "timestamp": "2026-11-05T17:00:00.000000Z",
    "ttl_sec": 60,
    "is_stale": false,
    "cpu": {
      "utilization_percent": 4.2,
      "load_average": [0.15, 0.22, 0.18],
      "temperature_c": 33.1
    },
    "memory": {
      "total_mb": 5616.2,
      "used_mb": 2658.5,
      "available_mb": 2957.7,
      "used_percent": 47.3
    },
    "storage": {
      "total_gb": 50.3,
      "used_gb": 9.8,
      "available_gb": 40.4,
      "used_percent": 19.4
    },
    "mobile_state": {
      "battery_percentage": 82,
      "power_source": "BATTERY",
      "thermal_state": "Cool",
      "power_saving_mode": false
    },
    "network": {
      "ping_ms": 14.2,
      "reachability_state": "ONLINE"
    }
  }
}
```

---

## 4. Static Freshness and Re-Discovery Protocol

### 4.1 Freshness Metadata
Every capability document maintains top-level timestamps and version provenance:
- `metadata.agent_version`: Installed agent build/commit string.
- `metadata.schema_version`: Registry specification version (`cr.v1.0`).
- `metadata.discovered_at`: Timestamp when the hardware inventory was enumerated.
- `metadata.verified_at`: Timestamp when bounded smoke tests completed.
- `metadata.verification_ttl_sec`: Window after which verified status is considered stale (default: 86,400s = 24 hours).

### 4.2 Inventory Preservation vs. Verification Expiry
- If `now - metadata.verified_at > verification_ttl_sec`, the controller flags `metadata.is_stale_verification = true`.
- **Preservation Invariant:** The controller **never erases** the last-known physical inventory (e.g. CPU cores, architecture, RAM) when verification expires. The hardware inventory remains visible in query catalogs, but the scheduler rejects jobs with `require_verified: true` until fresh verification completes.

### 4.3 Triggered Re-Discovery Lifecycle
Re-probing and capability refresh are triggered by four distinct lifecycle events:
1. **Agent Boot / Restart:** Always performs full hardware detection and smoke verification.
2. **Reconnection after Offline:** When a node reconnects after an `OFFLINE` period exceeding 5 minutes, it executes a background re-verification probe to detect packages installed or removed during the partition.
3. **Agent Software Upgrade (`agent_version` change):** If a heartbeat reports an `agent_version` mismatch against the registry, the controller issues a re-probe command to refresh runtime support.
4. **Administrative Refresh Request:** An operator can trigger `POST /api/v1/nodes/{id}/refresh` to force an immediate probe cycle.

---

## 5. Dynamic Telemetry, TTL, and Failure Modes

### 5.1 Strict Decoupling of Hardware Facts from Ephemeral Telemetry
Dynamic telemetry fluctuates continuously and is isolated into the `telemetry` payload:
- Static facts (Architecture, Total RAM, Total Storage) are never overwritten by instantaneous dynamic counters (Free RAM, Free Storage).
- If dynamic telemetry fails to deliver or becomes corrupted, the static capability inventory remains 100% intact.

### 5.2 Telemetry TTL and Stale Data Handling
- **Telemetry Heartbeat Frequency:** Every 15 seconds.
- **Telemetry TTL (`ttl_sec`):** 60 seconds (`DEFAULT_HEARTBEAT_TIMEOUT`).
- **Graceful Staleness (`15s < age <= 60s`):** Node remains `ONLINE`; telemetry is marked `is_stale: true`. Scheduler penalizes the node score but allows placement.
- **Expired Telemetry (`age > 60s`):**
  - Controller marks node liveness `OFFLINE`.
  - Dynamic telemetry metrics are marked unavailable (`ping_ms = null`, `available_mb = 0`).
  - **Static Inventory Persisted:** Static specifications remain recorded in `nodes_db`.

### 5.3 Offline Node vs. Individual Capability Unavailability
The registry strictly distinguishes:
- **Node Offline:** The entire host is unreachable. No workload of any kind may be scheduled on this node, regardless of its hardware specifications.
- **Individual Capability Unavailable:** The node is `ONLINE` and running jobs, but an individual runtime or peripheral is temporarily down (e.g., Docker daemon crashed while Python continues running; or thermal throttling temporarily masks udocker). Only workloads requiring that specific capability are blocked.

---

## 6. Ownership, Policy, and Administrative Overrides

### 6.1 Authority Boundary
- **Node Agent Authority:** The node agent is the sole source of truth for physical hardware detection and local runtime availability.
- **Controller Authority:** The controller stores reported facts and is authoritative for scheduling, lease allocation, fencing tokens, and administrative policies.

### 6.2 Separation of Inventory from Policy Overrides
Administrative restrictions (e.g., node draining, blacklisting a runtime, or reserving resources) are maintained in a dedicated **`policy_overrides`** block.
**Under no circumstances may an administrative override or controller action modify the node's reported physical capability inventory.**

```
Controller Node State:
┌─────────────────────────────────────────────────────────────┐
│ node_id: "server-95bad5ff01424d4c8d184330d6d2e394"          │
├─────────────────────────────────────────────────────────────┤
│ 1. Physical Inventory (Node-Reported, Immutable by Admin)    │
│    - capabilities.compute, runtime, storage, hardware        │
├─────────────────────────────────────────────────────────────┤
│ 2. Dynamic Telemetry (Ephemeral Heartbeat Stream)           │
│    - telemetry.cpu, memory.available_mb, mobile_state       │
├─────────────────────────────────────────────────────────────┤
│ 3. Policy & Scheduling Overrides (Controller-Maintained)    │
│    - status: "DRAINING"                                     │
│    - disabled_capabilities: ["container_runtime:udocker"]   │
│    - override_reason: "Thermal mitigation by operator"      │
│    - expires_at: "2026-11-05T18:00:00Z"                     │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 Temporary Override Expiry & Recovery
- Overrides support an optional `expires_at` ISO 8601 timestamp.
- The Controller Lease Sweeper evaluates expired overrides during its periodic cycle.
- Once expired, the override is automatically purged, restoring normal scheduling eligibility without requiring agent re-registration.

---

## 7. Offline Windows Node Invariant

PersonalServer's architecture enforces complete Android autonomy:
- **Physical Reality:** The primary cluster operates on the Vivo Y31 (controller) and Realme 5 Pro (worker).
- **The Windows Laptop:** Has 24 GB RAM and an RTX 3050 GPU, and its physical capabilities remain recorded in `nodes.json`.
- **Absolute Invariant:** **Stored capability inventory for an offline node CANNOT make the laptop eligible for scheduling.**
- **Enforcement Mechanics (`ResourceScheduler.is_node_eligible`):**
  1. Scheduler checks `compute_node_liveness(node)`.
  2. If `liveness != STATE_ONLINE`, eligibility returns `(False, "Node status is OFFLINE")` **before** any capability, GPU, or memory requirements are evaluated.
  3. No capability matrix, priority score, or preferred node hint can bypass the liveness gate. The cluster will safely queue or reject workloads rather than routing to an offline machine.

---

## 8. Security and Hardware Permission Model

Advertising physical hardware capabilities creates a risk that untrusted workloads might capture unauthorized audio, video, or sensor streams.

```
+─────────────────────────────────────────────────────────────+
| THREE-TIER HARDWARE ACCESS PERMISSION MODEL                 |
+─────────────────────────────────────────────────────────────+
| Tier 1: Discovery & Registration (Zero Permission)          |
| - Probes detect camera/mic device nodes.                    |
| - Capabilities advertised in registry as hardware.available.|
| - NO CAPTURE or recording streams initiated.                |
+──────────────────────────────┬──────────────────────────────+
                               ▼
| Tier 2: Job Declaration & Controller Lease Grant             |
| - Job spec explicitly requests hardware: ["camera"].        |
| - Scheduler checks node capabilities + operator authorization.|
| - Controller issues signed, time-bounded Hardware Access Lease.|
+──────────────────────────────┬──────────────────────────────+
                               ▼
| Tier 3: Worker-Local Sandboxed Execution & Visible Indicator|
| - Node Agent validates lease signature and fencing token.    |
| - Hardware access wrapper grants ephemeral access.           |
| - Mandatory User-Facing Notification / Indicator:            |
|   * Termux Notification: "PersonalServer camera active"     |
|   * Windows: OS Tray privacy icon.                          |
| - Revoked immediately upon job exit or timeout.              |
+─────────────────────────────────────────────────────────────+
```

*Invariant:* Under Milestone J2, this specification governs **discovery and registry only**. Hardware capture, audio streaming, or video streaming implementations are explicitly deferred.

---

## 9. Registry API Specification

### 9.1 Endpoints

#### 1. Register / Update Node Capabilities
- **Method / Path:** `POST /api/v1/nodes/{node_id}/capabilities`
- **Auth:** Node Auth Token or Enrollment Token.
- **Payload:** `{"metadata": <metadata>, "capabilities": <cr.v1.0 object>}`
- **Response `200 OK`:** Acknowledges registration and verification count.

#### 2. Query Nodes by Capability Matrix
- **Method / Path:** `POST /api/v1/nodes/query`
- **Auth:** Admin / Operator Token.
- **Behavior:** Filters candidate nodes by combining static inventory, active policy overrides, and live telemetry. Automatically excludes any node that is not `ONLINE`.

#### 3. Update Dynamic Telemetry
- **Method / Path:** `POST /nodes/{node_id}/heartbeat` *(Existing endpoint extended)*
- **Payload:** Fast JSON with `system`, `memory_details`, `mobile_state`, `thermals`.
- **Response `200 OK`:** `{"status": "ok", "ack": true, "timestamp": "..."}`

#### 4. Administrative Override
- **Method / Path:** `POST /api/v1/nodes/{node_id}/overrides`
- **Auth:** Admin Token.
- **Payload:** `{"disabled_capabilities": ["container_runtime:udocker"], "expires_in_sec": 3600, "reason": "Operator throttle"}`
- **Response `200 OK`:** Overrides applied in controller policy layer.

---

## 10. Milestone J2 Verification Checklist

- [x] **Hardware States:** Defined `DETECTED`, `ACCESSIBLE`, `UNAVAILABLE`, and `UNKNOWN`. Hardware detection explicitly decoupled from capture permission.
- [x] **Static Freshness:** Included `discovered_at`, `verified_at`, `agent_version`, `schema_version`, and TTL re-probing rules.
- [x] **Preserved Inventory:** Established that stale verification or missed heartbeats never erase static inventory.
- [x] **Dynamic Telemetry Decoupling:** Separated ephemeral metrics (TTL 60s) from immutable hardware facts.
- [x] **Ownership Separation:** Enforced node authority over physical inventory while controller manages policy overrides separately.
- [x] **Offline Laptop Protection:** Formally proved that offline Windows inventory cannot be scheduled.
- [x] **Safety Compliance:** Confined strictly to `docs/capability_registry_j2.md`; no source code touched; no production services interrupted.

---

## 11. Open Decisions Deferred to J3/J4 Implementation

1. **GPU Allocation Granularity:** When multiple batch jobs require GPU acceleration on the Windows laptop, should VRAM be partitioned via fractional limits or scheduled via exclusive node locking?
2. **Sensor Stream Transport:** Should future sensor telemetry (e.g., live accelerometer or microphone decibel levels) be polled via HTTP or pushed over a persistent WebSocket sidecar?
3. **Termux:API Fallback:** Graceful handling when optional Termux:API packages are absent on Android.
