# 11 — Architecture Reference: Subsystems & Data Flows

> **Reading Order**: **Step 11 of 12** in the PersonalServer Guide Series.  
> **Previous Guide**: [10 — Development](10_development.md)  
> **Next Guide**: [12 — Protocols and Specifications](12_protocols_and_specifications.md)  
> **Audience**: System architects, security auditors, and core contributors.  
> **Prerequisites**: Familiarity with Guides 01 through 10.

---

## 1. High-Level Subsystem Architecture

PersonalServer operates as a decentralized, resource-aware personal compute cluster:

```mermaid
flowchart TD
    subgraph Client & Ingress Plane
        CF["Cloudflare Tunnel<br/>(*.akshatsahay.space)"] --> R["Reverse Proxy Gateway<br/>(:8088 / :8080)"]
        CLI["Admin CLI & Web UI"] --> CTRL["Master Controller<br/>(:8000)"]
    end

    subgraph Controller Plane (Vivo Y31)
        CTRL --> SCHED["Universal Scheduler (J3)<br/>8-Stage Eligibility Pipeline"]
        CTRL --> HA["HA Consensus Engine<br/>Lease & State Sync"]
        CTRL --> DM["Deployment Manager (R1-R9)<br/>Lifecycle State Machine"]
        CTRL --> STORES["Transactional Stores<br/>(Jobs, Apps, Nodes, Leases)"]
    end

    subgraph Mesh & Execution Plane (Tailscale WireGuard)
        SCHED -->|Dispatches Jobs| W1["Worker Node (Realme 5 Pro)<br/>Node Agent (:8000)"]
        SCHED -->|Dispatches Builds| W2["Workstation (Windows)<br/>On-Demand Build Worker"]
        HA <-->|Heartbeats & Sync| SB["Standby Controller<br/>(Realme 5 Pro)"]
        DM -->|Artifacts & Probes| W1
    end
```

---

## 2. Core Subsystems

### A. Master Controller (`controller/`)
- Exposes modular REST endpoints for node registration, job submission, deployment triggers, and artifact management.
- Maintains authoritative cluster state using atomic, single-lock read-modify-write transactional databases (`controller/stores/`).
- Operates crash-recovery sweeps on startup to reconcile stale reservations, abandoned worker leases, and in-flight deployment attempts.

### B. Universal Scheduler (`scheduler/`)
- Evaluates candidate nodes through an **ordered 8-stage eligibility pipeline** (liveness, administrative policies, capability matching, RAM/CPU reservations, thermals, storage space, and affinity).
- Implements **priority aging** to guarantee starvation immunity for long-pending batch jobs.
- Reserves memory and execution slots under database locks with short commit windows.

### C. Node Agent & Prober (`agent/`)
- Executes periodic hardware and software capability discovery (Python, Node.js, Docker, battery, CPU temperature, storage).
- Emits authenticated heartbeats (every 10s) to the active leader with dynamic load metrics.
- Manages local runtime execution adapters (`agent/runtime_adapters/`) with process group isolation.

### D. Reverse Proxy Router (`router/`)
- Listens on port `8088` (and forwards from `8080`).
- Performs dynamic subpath routing (`/apps/<app_id>`) and apex domain routing (`<app_id>.akshatsahay.space`).
- Intercepts 502/503 upstream failures and provides automatic failover routing when the cluster leader migrates.

---

## 3. Data Flows

### A. Job Submission & Execution Flow:
1. Client submits job via `POST /api/jobs` with optional output declarations.
2. Controller normalizes payload, generates monotonic fencing generation token, and writes state to `jobs_store.json`.
3. Universal Scheduler evaluates cluster nodes and creates an atomic resource reservation.
4. Assigned worker receives dispatch, validates lease and fencing token, and executes code in an isolated attempt directory.
5. Upon exit, worker hashes output artifacts (SHA-256) and uploads them to the controller staging area.
6. Controller executes two-phase CAS promotion, updates job status to `SUCCEEDED`, and releases reservations.

### B. Deployment & Rollback Flow:
1. Operator triggers deployment via CLI or Web UI.
2. Deployment manager writes deployment record (`QUEUED`) and packages artifact bundle.
3. Candidate node unpacks release into versioned directory (`releases/<deployment_id>`).
4. Runtime adapter boots process on ephemeral port and controller conducts health checks.
5. If health check succeeds, reverse proxy updates upstream pointer to new port.
6. If health check times out, process is killed and previous healthy release is restored.

---

## 4. Next Step

Inspect the authoritative normative specifications, protocol schemas, and mathematical algorithms:

👉 **Continue to [12 — Protocols and Specifications](12_protocols_and_specifications.md)**
