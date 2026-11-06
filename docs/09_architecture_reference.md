# 09 — Architecture Reference: Subsystems & Specifications

> **Reading Order**: **Step 9 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [08 — Development Guide](08_development.md)  
> **Audience**: System architects, security auditors, and core contributors.  
> **Prerequisites**: Familiarity with Guides 01 through 08.

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

## 3. Normative Technical Specifications (`docs/reference/`)

For exact technical contracts, state transition matrices, error codes, and mathematical formulas, consult the normative specifications:

| Specification | Path | Scope |
|---|---|---|
| **Universal Job Contract (J1)** | [docs/reference/universal_job_contract_j1.md](reference/universal_job_contract_j1.md) | Standardized job definition, execution environment, and artifact schema |
| **Capability Registry (J2)** | [docs/reference/capability_registry_j2.md](reference/capability_registry_j2.md) | Static and dynamic hardware/software capability discovery |
| **Universal Scheduler (J3)** | [docs/reference/universal_scheduler_j3.md](reference/universal_scheduler_j3.md) | 8-stage eligibility pipeline, scoring formula, and atomic reservations |
| **Deployment Architecture (D0)** | [docs/reference/deployment_architecture_d0.md](reference/deployment_architecture_d0.md) | Universal multi-runtime application deployment lifecycle and rollback |

---

## 4. Hardware Migration & Research Archives (`docs/reference/`)

Historical planning documents and research papers detailing the progression of PersonalServer:
- [Hardware Migration Plan](reference/hardware_migration_plan.md): Architectural roadmap transitioning from Windows development to dual-Android Termux and Raspberry Pi hardware.
- [Raspberry Pi Migration](reference/raspberry_pi_migration.md): Linux Filesystem Hierarchy Standard (FHS) mapping and systemd daemon units.
- [Multi-Node Simulation Suite](reference/multi_node_simulation.md): Offline loopback test harness for multi-worker fault injection.
- [Image Build & Deploy Workflow](reference/image_build_deploy_workflow.md): Decoupled 8-stage image pipeline and OCI digest verification.

---

## 5. Congratulations!

You have completed the entire PersonalServer documentation series. To review or start from the beginning:

👉 **Return to [01 — Setup Guide](01_setup.md)** or consult the [Documentation Index](README.md).
