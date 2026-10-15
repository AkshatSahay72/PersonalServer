# PersonalServer System Architecture

PersonalServer is a modular, lightweight, distributed edge computing and cluster management platform built for heterogeneous personal hardware (Android smartphones running Termux, Raspberry Pis, Linux servers, Windows workstations, and container runtimes).

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
                                       ▼ (http://127.0.0.1:8080 or :8088)
+─────────────────────────────────────────────────────────────────────────────+
|                        ACTIVE CONTROLLER / MASTER NODE                      |
|                                                                             |
|   +───────────────────────────────────+   +─────────────────────────────+   |
|   |   Node API & Web Operations UI    |   |     Cluster Controller      |   |
|   |  - Real-time Cluster Summary      |   |  - Node Inventory & Health  |   |
|   |  - Rich Telemetry (Battery/WiFi)  |   |  - Resource Scheduler       |   |
|   |  - Workload Job Management        |   |  - Lease Sweeper            |   |
|   |  - Storage Explorer (Multi-Node)  |   |  - Onboarding Store         |   |
|   |  - HA Master Status & Failover    |   |  - App Registry & Deployer  |   |
|   +───────────────────────────────────+   +──────────────┬──────────────+   |
|                                                          │                  |
|   +───────────────────────────────────+                  │                  |
|   |     High Availability Manager     |◄─────────────────┘                  |
|   |  - Leader Lease & Term Consensus  |                                     |
|   |  - State Versioning & Snapshots   |                                     |
|   +─────────────────┬─────────────────+                                     |
+─────────────────────┼───────────────────────────────────────────────────────+
                      │ Heartbeats, Sync & WireGuard Tailscale Mesh
       ┌──────────────┴───────────────────────────────┐
       ▼                                              ▼
+─────────────────────────────────────────+  +────────────────────────────────+
|        HOT STANDBY CONTROLLER           |  |       APPLICATION ROUTER       |
|  - Passive Liveness Monitor             |  |  router/app_router.py (:8088)  |
|  - Synchronized State (nodes/jobs/apps) |  |  - Dynamic Path-Based Routing  |
|  - Auto-Election upon Master Crash      |  |  - SSRF & Traversal Defenses   |
|  - Seamless Worker & Client Failover    |  |  - Container Reverse Proxy     |
+─────────────────────────────────────────+  +────────────────────────────────+
       │
       ▼ WireGuard Tailscale Mesh (:8000, :8022, :8080)
+─────────────────────────────────────────────────────────────────────────────+
|                     HETEROGENEOUS WORKER / STORAGE NODES                    |
|                                                                             |
|   +─────────────────────────────────+     +─────────────────────────────+   |
|   |     Node 01: Android (Termux)   |     |    Node 02: Raspberry Pi    |   |
|   |  - Node Agent & Supervisor      |     |  - Docker Container Runtime |   |
|   |  - Rich Telemetry Engine        |     |  - Multi-Arch OCI Images    |   |
|   |    (Battery, WiFi dBm, Thermal) |     |  - High-Capacity Storage    |   |
|   |  - Sandboxed Job Executor       |     |  - Node Agent & Worker      |   |
|   |  - Sandboxed File Storage       |     |  - SSH Administration       |   |
|   +─────────────────────────────────+     +─────────────────────────────+   |
+─────────────────────────────────────────────────────────────────────────────+
```

---

## 2. Core Subsystems

### 2.1 Cluster Controller & High Availability (`controller/controller.py`, `config/ha_manager.py`)
* **Cluster Inventory**: Maintains registered nodes, roles (`compute`, `storage`), hardware specs, runtime capabilities (`docker`, `python`), and liveness status in `controller/data/nodes.json`.
* **High Availability & Master Failover**:
  * **Leader Lease**: Atomic lease tracking (`term`, `leader_id`, `lease_expires_at`, `state_version`) saved safely on disk and held in memory.
  * **Hot Standby**: Secondary controllers monitor the primary via `/ha/status`, replicate database state via `/ha/sync`, and automatically promote to Leader if the primary fails.
  * **Split-Brain Immunity**: Higher terms supersede older terms; disconnected or recovered old masters step down immediately.
* **Resource-Aware Scheduler (`scheduler/scheduler.py`)**: Filters candidates by hard constraints (status, architecture, Docker runtime capability) and scores eligible nodes across CPU load (35%), free RAM (35%), total capacity (20%), and role affinity (10%).
* **Lease-Based Failure Detection**: Dedicated sweeper daemon identifies abandoned jobs from crashed workers and triggers at-least-once recovery.
* **Single-Use Onboarding Store**: Generates temporary, single-use `PS-XXXX-XXXX` pairing codes stored as SHA-256 hashes in `controller/data/onboarding_codes.json`.

### 2.2 Node Agent & Telemetry (`agent/node-agent.py`, `config/node_telemetry.py`)
* **Hardware Discovery**: Detects CPU cores, architecture, RAM, storage, platform, and OS without manual intervention.
* **Rich Telemetry Collector**: Inspects battery percentage, charging state, WiFi RSSI (dBm), link speed (Mbps), CPU temperature (°C), and thermal throttling alerts across Android Termux, Linux sysfs, and Windows.
* **Multi-Master Discovery**: Maintains candidate controller URLs and transparently redirects heartbeats and job queries if the primary master fails.
* **Job Executor (`agent/job_executor.py`)**: Sandboxed, allowlisted workload runner with millisecond duration tracking, 64KB log buffers, and strict timeout enforcement.
* **Local Worker Daemon (`agent/local_worker.py`)**: Dedicated continuous background worker process for polling, lease renewal, and execution.

### 2.3 Application Router & Gateway (`router/app_router.py`)
* **Dynamic Path-Based Routing**: Exposes registered containerized applications under canonical paths (e.g., `https://server.domain/apps/<app-name>/`).
* **Security Defenses**: Built-in SSRF protection, path traversal blocking, private subnet isolation, and header sanitization.

### 2.4 Container Engine & OCI Deployment (`controller/image_inspector.py`)
* **Decoupled 8-Stage Image Pipeline**: Source checkout -> Image build (`image-build`) -> Cryptographic digest tagging (`sha256:...`) -> Local image store (`storage/images/`) -> Arch normalization (`arm64`/`amd64`) -> Worker scheduling -> Sandboxed container execution -> Health verification and rollback.
* **Container Isolation**: Rejects dangerous mount points (`/var/run/docker.sock`, host root), enforces CPU/memory limits, and binds unprivileged ports.

### 2.5 Multi-Node Decentralized Storage (`services/node-api/app.py`)
* **Storage Sandbox**: All operations are strictly constrained to `~/PersonalServer/storage/` via canonical resolution and `os.path.commonpath` verification.
* **Transparent Multi-Node Proxy**: Node API discovers remote storage nodes across Tailscale and proxies directory listing, downloads, uploads, and file management.
* **Operations Web Console**: Modern, responsive dashboard with summary strip, node inventory, rich telemetry badges, storage file manager, job logs, and manual HA failover simulation.

### 2.6 Private Mesh & Ingress Networking
* **Tailscale WireGuard Mesh**: Encrypts and isolates all inter-node traffic (Controller `:8000`, Node APIs `:8080`, App Router `:8088`, SSH `:8022`).
* **Cloudflare Zero Trust**: Exposes Web Console and designated application routes securely over HTTPS without port forwarding or exposing internal services.
