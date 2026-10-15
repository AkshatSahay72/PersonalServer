# PersonalServer

> **A lightweight, robust, self-hosted personal server, container platform, and distributed compute cluster designed for heterogeneous edge devices (Android/Termux, Raspberry Pi, Linux servers, WSL, and Laptops).**

---

## 1. Highlights

* 👑 **High Availability (HA) & Master Failover**: Self-contained, zero-dependency consensus with leader leases, term tracking, continuous state sync, and transparent worker/client failover when a master goes down.
* ⚡ **1-Command Zero-Touch Onboarding**: Add any Android phone (via Termux), Raspberry Pi, or Linux machine to your cluster in seconds using short-lived, single-use pairing codes (`PS-XXXX-XXXX`) with automated hardware discovery.
* 📊 **Rich Device Telemetry**: Real-time cross-platform inspection of battery percentage, charging state, WiFi/Cellular signal strength (dBm), link speed, CPU temperature, and thermal throttling.
* 🐳 **Decoupled OCI Container Platform**: 8-stage image build, tagging with immutable `sha256:...` digests, architecture normalization (`arm64`/`amd64`), and sandboxed execution without exposing Docker sockets.
* 📦 **Multi-Node Decentralized Storage**: Browse, upload, download, and organize files across multiple physical storage nodes with sandboxed paths and strict path-traversal defenses.
* 🔄 **Reliable Workload Execution & Scheduling**: Resource-aware job placement, automatic lease-based failure recovery, and retries.
* 🖥️ **Technical Operations Console**: Compact, information-dense server administration interface accessible over HTTPS via Cloudflare Zero Trust without exposing internal ports.
* 🔒 **Private WireGuard Mesh**: All inter-node coordination, heartbeats, SSH administration (`:8022`), and Node APIs (`:8080`) run over a private Tailscale network.

---

## 2. Architecture Overview

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

## 3. Quick Start Guide

### Step 1: Start the Cluster Controller

On your controller host (e.g., laptop or home server):

```bash
# Start Controller service
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60
```

### Step 2: Onboard a New Node

Generate a one-time onboarding code on the Controller:

```bash
python controller/controller.py onboard-code
```
```text
==========================================
Onboarding code: PS-WYR9-3LYT
Expires in:      15 minutes
==========================================
```

Run onboarding on the new device (Android / Termux / Linux):

```bash
cd ~/PersonalServer
python agent/node-agent.py onboard \
  --controller http://<CONTROLLER_IP>:8000 \
  --code PS-WYR9-3LYT
```
The agent automatically detects hardware, configures identity, registers with the controller, starts local services, and begins heartbeats.

### Step 3: Manage Cluster & Workloads

```bash
# Check cluster inventory & rich telemetry
python controller/controller.py nodes

# Submit a workload job to the cluster
python controller/controller.py submit --node auto --type system-info

# Check execution logs
python controller/controller.py jobs
```

---

## 4. Multi-Node Storage Subsystem

All files reside securely in `~/PersonalServer/storage/` on each node. Access files locally or across remote nodes via the Web Console or REST API:

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/storage/nodes` | List all discovered cluster storage nodes & available space |
| `GET` | `/storage/list?node=<name>&path=<dir>` | List files and folders on target node |
| `GET` | `/storage/download?node=<name>&path=<file>` | Download file from target node |
| `POST` | `/storage/upload?node=<name>&path=<dir>` | Upload file to target node |
| `POST` | `/storage/mkdir?node=<name>` | Create new folder (`{"name": "...", "path": "..."}`) |
| `POST` | `/storage/rename?node=<name>` | Rename file or folder (`{"path": "...", "new_name": "..."}`) |
| `DELETE` | `/storage?node=<name>&path=<item>` | Delete file or directory |
| `GET` | `/storage/usage?node=<name>` | Storage telemetry and free space |

---

## 5. Operations Web Console

Access the high-density administration console at `http://<NODE_IP>:8080/` (or via Cloudflare Tunnel over HTTPS):

* **Dashboard**: Cluster metrics, online node count, active workloads, and HA Master badge.
* **Nodes**: Detailed hardware inventory, CPU cores, RAM, storage, battery %, WiFi dBm, and CPU temperature.
* **Jobs**: Dispatch allowlisted workloads and inspect execution logs.
* **Storage**: Multi-node switcher, directory browser, uploads, downloads, and file management.
* **Settings**: Cluster configuration, network routing, and HA failover simulation.

---

## 6. Complete Documentation

Comprehensive technical guides are maintained in the [`docs/`](file:///d:/Coding/Major%20Project/PersonalServer/docs/README.md) directory:

### Core Architecture & HA
* [Technical Documentation Catalog](file:///d:/Coding/Major%20Project/PersonalServer/docs/README.md)
* [System Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/architecture.md)
* [High Availability (HA) & Master Failover](file:///d:/Coding/Major%20Project/PersonalServer/docs/high_availability_failover.md)
* [Controller Reference & API](file:///d:/Coding/Major%20Project/PersonalServer/docs/controller.md)
* [Node Agent & Telemetry Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-agent.md)
* [Resource-Aware Scheduling](file:///d:/Coding/Major%20Project/PersonalServer/docs/scheduling.md)
* [Multi-Node Storage Subsystem](file:///d:/Coding/Major%20Project/PersonalServer/docs/storage.md)

### Workloads, Reliability & Containers
* [Job Reliability & Failure Recovery](file:///d:/Coding/Major%20Project/PersonalServer/docs/reliability.md)
* [Job Lifecycle FSM & Recovery Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/job_lifecycle_recovery.md)
* [Workload Jobs & Allowlist](file:///d:/Coding/Major%20Project/PersonalServer/docs/jobs.md)
* [OCI Image Build & Deployment Workflow](file:///d:/Coding/Major%20Project/PersonalServer/docs/image_build_deploy_workflow.md)
* [Application Routing & Gateway](file:///d:/Coding/Major%20Project/PersonalServer/docs/domain_migration.md)

### Setup, Security & Migration
* [Getting Started Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/getting-started.md)
* [Node Enrollment & Onboarding](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-enrollment.md)
* [Troubleshooting & Runbook](file:///d:/Coding/Major%20Project/PersonalServer/docs/troubleshooting.md)
* [Security Model & Threat Assessment](file:///d:/Coding/Major%20Project/PersonalServer/docs/security.md)
* [Networking & Ingress Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/networking.md)
* [Hardware Migration Plan (Raspberry Pi & Android)](file:///d:/Coding/Major%20Project/PersonalServer/docs/hardware_migration_plan.md)
* [Raspberry Pi OS Deployment & FHS Mapping](file:///d:/Coding/Major%20Project/PersonalServer/docs/raspberry_pi_migration.md)
* [Multi-Node Simulation Suite](file:///d:/Coding/Major%20Project/PersonalServer/docs/multi_node_simulation.md)
* [Reproducible Development, CI & Backup](file:///d:/Coding/Major%20Project/PersonalServer/docs/reproducible_setup.md)
* [Development & Testing Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/development.md)
* [Evolution & Roadmap](file:///d:/Coding/Major%20Project/PersonalServer/docs/roadmap.md)
