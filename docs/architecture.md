# PersonalServer System Architecture

PersonalServer is a distributed, self-hosted personal server and cluster management platform built for heterogeneous edge hardware (Android devices running Termux, Raspberry Pis, Linux servers, and desktop hosts).

---

## 1. High-Level System Architecture

```text
+─────────────────────────────────────────────────────────────────────────────+
|                                PUBLIC ACCESS                                |
+─────────────────────────────────────────────────────────────────────────────+
                                       │
                                       ▼ (HTTPS Ingress)
                      +─────────────────────────────────+
                      |     Cloudflare Zero Trust       |
                      +─────────────────────────────────+
                                       │
                                       ▼ (http://127.0.0.1:8080)
+─────────────────────────────────────────────────────────────────────────────+
|                              NODE 01 (VIVO Y31)                             |
|                                                                             |
|   +─────────────────────────────────────────────────────────────────────+   |
|   |                    Node API & Operations Web Console                |   |
|   |  - Cluster Summary Strip               - System Telemetry           |   |
|   |  - Nodes Table & Live Status           - Workload Job Execution UI  |   |
|   |  - Multi-Node Storage Browser          - Settings & Diagnostics     |   |
|   +─────────────────────────────────────────────────────────────────────+   |
|                                      │                                      |
|   +──────────────────────────────────┴─────+   +────────────────────────+   |
|   |         Local Storage Root             |   |       Node Agent       |   |
|   |     ~/PersonalServer/storage/          |   |  - Service Supervisor  |   |
|   |     (Sandboxed & Traversal Defended)   |   |  - Heartbeat Reporter  |   |
|   +────────────────────────────────────────+   |  - Job Executor        |   |
|                                                +────────────┬───────────+   |
+─────────────────────────────────────────────────────────────┼───────────────+
                                                              │
                                                Tailscale WireGuard Mesh
                                                              │
                 ┌────────────────────────────────────────────┴─────────────┐
                 │                                                          │
                 ▼                                                          ▼
+──────────────────────────────────────────+   +────────────────────────────┴─+
|               NODE 02                    |   |      LAPTOP CONTROLLER       |
|                                          |   |                              |
|   +──────────────────────────────────+   |   |   +───────────────────────+  |
|   |       Node API (:8080)           |   |   |   | PersonalServer        |  |
|   |   - Remote Storage Read/Write    |   |   |   | Controller (:8000)    |  |
|   +──────────────────────────────────+   |   |   | - Cluster Inventory   |  |
|   |       Node Agent                 |   |   |   | - Resource Scheduler  |  |
|   |   - Hardware Discovery           |   |   |   | - Lease Sweeper       |  |
|   |   - Workload Job Executor        |   |   |   | - Onboarding Store    |  |
|   +──────────────────────────────────+   |   |   +───────────────────────+  |
|   |       Storage Root               |   |   +──────────────────────────────+
|   |   ~/PersonalServer/storage/      |   |
|   +──────────────────────────────────+   |
+──────────────────────────────────────────+
```

---

## 2. Core Subsystems

### 2.1 Controller (`controller/controller.py`)
* **Cluster Inventory**: Maintains registered nodes, roles, capabilities, and liveness states in `controller/data/nodes.json`.
* **Resource-Aware Scheduler**: Selects optimal execution nodes evaluating CPU cores, load averages, memory availability, and hardware capabilities.
* **Lease-Based Failure Detection & Sweeper**: Automatically tracks job execution leases, detects worker disconnections, and initiates recovery.
* **One-Time Onboarding Store**: Generates temporary, single-use `PS-XXXX-XXXX` pairing codes with SHA-256 hashed storage in `controller/data/onboarding_codes.json`.

### 2.2 Node Agent (`agent/node-agent.py`)
* **Hardware Discovery**: Inspects CPU cores, RAM, storage, platform, and OS without manual configuration.
* **One-Command Onboarding**: Enrolls nodes with the controller via `onboard` CLI, synthesizing configuration files and starting services.
* **Liveness & Telemetry**: Sends periodic authenticated heartbeats reporting system load, memory, disk usage, and service states.
* **Job Executor (`agent/job_executor.py`)**: Executes allowlisted workloads under non-root user permissions with strict timeout enforcement.

### 2.3 Node API & Storage Manager (`services/node-api/app.py`)
* **Operations Web Console**: Modern, information-dense administration interface for cluster health, jobs, telemetry, and storage.
* **Multi-Node Decentralized Storage**: Exposes local and remote storage roots (`~/PersonalServer/storage/`) with strict path traversal defenses.
* **Remote Storage Proxy**: Routes storage requests transparently to remote node APIs over Tailscale.

### 2.4 Private Mesh & Ingress Networking
* **Tailscale WireGuard Mesh**: Encrypts and isolates all inter-node traffic (Controller `:8000`, Node APIs `:8080`, SSH `:8022`).
* **Cloudflare Tunnel**: Exposes the Web Console securely over HTTPS without requiring port forwarding or exposing internal services.
