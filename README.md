# PersonalServer

> **A lightweight, robust, self-hosted personal server and distributed compute cluster designed for heterogeneous edge devices (Android/Termux, Raspberry Pi, Linux servers, WSL, and Laptops).**

---

## 1. Highlights

* ⚡ **1-Command Zero-Touch Onboarding**: Add any Android phone (via Termux), Raspberry Pi, or Linux machine to your cluster in seconds using short-lived, single-use pairing codes (`PS-XXXX-XXXX`) with automated hardware discovery.
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
# Check cluster inventory
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

* **Dashboard**: Cluster metrics, online node count, active workloads, and system stats.
* **Nodes**: Detailed hardware inventory, CPU cores, RAM, storage, and health.
* **Jobs**: Dispatch allowlisted workloads and inspect execution logs.
* **Storage**: Multi-node switcher, directory browser, uploads, downloads, and file management.
* **Settings**: Cluster configuration and network routing details.

---

## 6. Complete Documentation

Detailed technical guides are available in the [`docs/`](file:///d:/Coding/Major%20Project/PersonalServer/docs/) directory:

* [System Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/architecture.md)
* [Getting Started & Setup](file:///d:/Coding/Major%20Project/PersonalServer/docs/getting-started.md)
* [Node Enrollment & Onboarding](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-enrollment.md)
* [Controller Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/controller.md)
* [Node Agent & Hardware Discovery](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-agent.md)
* [Multi-Node Storage](file:///d:/Coding/Major%20Project/PersonalServer/docs/storage.md)
* [Workload Jobs & Allowlist](file:///d:/Coding/Major%20Project/PersonalServer/docs/jobs.md)
* [Resource-Aware Scheduling](file:///d:/Coding/Major%20Project/PersonalServer/docs/scheduling.md)
* [Reliability & Leases](file:///d:/Coding/Major%20Project/PersonalServer/docs/reliability.md)
* [Networking & Ingress](file:///d:/Coding/Major%20Project/PersonalServer/docs/networking.md)
* [Security & Threat Model](file:///d:/Coding/Major%20Project/PersonalServer/docs/security.md)
* [Troubleshooting Runbook](file:///d:/Coding/Major%20Project/PersonalServer/docs/troubleshooting.md)
