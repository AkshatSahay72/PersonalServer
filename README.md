# PersonalServer v1.0

> **A practical, lightweight, self-hosted personal server and cluster management platform designed for heterogeneous edge devices (Android/Termux, Linux, Raspberry Pi, Laptop).**

---

## 1. Overview

PersonalServer transforms everyday personal devices—such as an Android smartphone running Termux or single-board computers—into a coordinated personal server and compute cluster.

```text
                    PERSONALSERVER
                          │
          ┌───────────────┼────────────────┐
          │               │                │
        COMPUTE         STORAGE          CONTROL
          │               │                │
        Jobs            Files            Nodes
        Retry           Upload           Scheduler
        Queue           Download         Monitoring
        Scheduler       Browse           Recovery
          │               │                │
          └───────────────┼────────────────┘
                          │
                    Web Interface (:8080)
                          │
                    Cloudflare Tunnel
                          │
                       Internet
```

PersonalServer v1.0 provides:
1. **Reliable Workload Execution**: Persistent lifecycle states (`QUEUED`, `CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `RECOVERING`, `REJECTED`), automatic lease monitoring, failure recovery, and retry policies.
2. **Resource-Aware Scheduling**: Dynamic node selection evaluating CPU cores, system load, available memory, roles, and hardware capabilities.
3. **Personal Storage Subsystem**: Restricted filesystem root (`~/PersonalServer/storage/`) providing file listing, streaming uploads/downloads, directory management, renaming, deletion, and strict path traversal defenses.
4. **Operations Web Interface**: Compact, high-density server administration panel accessible over HTTPS via Cloudflare Tunnel without requiring Tailscale on client devices.
5. **Private Cluster Mesh**: Tailscale private networking for controller coordination, node heartbeats, SSH administration (`:8022`), and node APIs (`:8080`), keeping all internal ports off the public Internet.

---

## 2. Architecture

```text
+-------------------------------------------------------------------------+
|                              INTERNET                                   |
+-------------------------------------------------------------------------+
                                    │
                                    ▼ (HTTPS Ingress)
                    +───────────────────────────────+
                    |       Cloudflare Tunnel       |
                    +───────────────────────────────+
                                    │
                                    ▼ (http://127.0.0.1:8080)
+─────────────────────────────────────────────────────────────────────────+
|                         NODE 01: VIVO Y31                               |
|                                                                         |
|   +─────────────────────────────────────────────────────────────────+   |
|   |                  Node API & Operations Web UI                   |   |
|   |  - Dashboard Overview         - Workload Execution UI           |   |
|   |  - Cluster Nodes Monitor      - Storage Subsystem File Browser  |   |
|   +─────────────────────────────────────────────────────────────────+   |
|                                   │                                     |
|   +───────────────────────────────┴───+   +─────────────────────────+   |
|   |         Storage Root              |   |       Node Agent        |   |
|   |   ~/PersonalServer/storage/       |   |   - Service Orchestration|  |
|   |   (Path Traversal Protected)      |   |   - Heartbeat Reporter  |   |
|   +───────────────────────────────────+   |   - Job Executor        |   |
|                                           +───────────┬─────────────+   |
|                                                       │                 |
|   +───────────────────────────────────+               │                 |
|   |   SSH Server (:8022, Private)     |               │                 |
|   +───────────────────────────────────+               │                 |
+───────────────────────────────────────────────────────┼─────────────────+
                                                        │
                                          Tailscale Private Network
                                                        │
                                                        ▼
+─────────────────────────────────────────────────────────────────────────+
|                          LAPTOP CONTROLLER                              |
|                                                                         |
|   +─────────────────────────────────────────────────────────────────+   |
|   |                  PersonalServer Controller (:8000)              |   |
|   |  - Central Node Inventory      - Resource-Aware Scheduler       |   |
|   |  - Authenticated Registration  - Job Lease Sweeper & Recovery   |   |
|   |  - Liveness Heartbeat Monitor  - Retry & Migration Engine       |   |
|   +─────────────────────────────────────────────────────────────────+   |
+─────────────────────────────────────────────────────────────────────────+
```

---

## 3. Quick Start

### 3.1 Start the Laptop Controller

```bash
# Start Controller on Laptop development environment
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60
```

### 3.2 Start Node Services (on Server Node / Vivo Y31)

```bash
# SSH into the server node
ssh -p 8022 u0_a244@100.85.108.5

# Start managed services
cd ~/PersonalServer
bash scripts/start.sh
```

### 3.3 Verify Cluster Health & Node Heartbeat

```bash
# Report heartbeat from Node Agent
python agent/node-agent.py heartbeat

# Inspect cluster overview from Controller
python controller/controller.py cluster
python controller/controller.py nodes
```

---

## 4. Personal Storage Subsystem

The storage subsystem operates out of `~/PersonalServer/storage/`. It is guarded against path traversal, absolute path escape, symlink redirection, and forbidden directories.

### Available Operations

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/storage/list?path=<dir>` | List files and folders in directory |
| `GET` | `/storage/download?path=<file>` | Stream and download file |
| `POST` | `/storage/upload?path=<dir>` | Multipart/stream file upload |
| `POST` | `/storage/mkdir` | Create new directory (`{"name": "...", "path": "..."}`) |
| `POST` | `/storage/rename` | Rename file/directory (`{"path": "...", "new_name": "..."}`) |
| `DELETE` | `/storage?path=<item>` | Delete file or directory recursively |
| `GET` | `/storage/usage` | Total storage bytes, file count, free space |

---

## 5. Workload Execution & Reliable Lifecycle

Workloads are submitted to the Controller and executed by Node Agents under sandboxed allowlisted executors.

### Supported Workload Types
- `system-info`: Hardware diagnostics, CPU cores, load average, memory, storage.
- `health-check`: Subsystem health verification.
- `node-status`: Process and PID status verification.
- `echo`: Lightweight parameter echo.
- `python-script`: Controlled inline Python execution.
- `failing-test` / `timeout-test`: Resilience testing.

### Submitting a Job

```bash
# Auto-scheduled job with max 3 retry attempts
python controller/controller.py submit --node auto --type system-info --timeout 60 --max-attempts 3

# Explicit target node job
python controller/controller.py submit --node server-5387a86bf36116b1 --type echo --params '{"message": "Hello"}'
```

---

## 6. Remote Access & Security Model

- **Public Internet**: Reachable only via Cloudflare Tunnel (`https://api.akshatsahay.space`) connecting to the Operations Web UI.
- **Node API (`:8080`)**: Bound to localhost/private network, exposed only via Cloudflare Tunnel.
- **SSH (`:8022`)**: Private, accessible only over Tailscale.
- **Controller (`:8000`)**: Private, accessible only over Tailscale.
- **Node Tokens**: Each node receives a cryptographically generated auth token upon enrollment.

---

## 7. Documentation Directory

- [System Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/architecture.md)
- [Getting Started Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/getting-started.md)
- [Node Enrollment Workflow](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-enrollment.md)
- [Controller Architecture](file:///d:/Coding/Major%20Project/PersonalServer/docs/controller.md)
- [Node Agent Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/node-agent.md)
- [Workload Jobs & Allowlist](file:///d:/Coding/Major%20Project/PersonalServer/docs/jobs.md)
- [Resource-Aware Scheduling](file:///d:/Coding/Major%20Project/PersonalServer/docs/scheduling.md)
- [Reliability & Leases](file:///d:/Coding/Major%20Project/PersonalServer/docs/reliability.md)
- [Storage Subsystem](file:///d:/Coding/Major%20Project/PersonalServer/docs/storage.md)
- [Networking & Ingress](file:///d:/Coding/Major%20Project/PersonalServer/docs/networking.md)
- [Security & Threat Model](file:///d:/Coding/Major%20Project/PersonalServer/docs/security.md)
- [Troubleshooting Runbook](file:///d:/Coding/Major%20Project/PersonalServer/docs/troubleshooting.md)
- [Development Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/development.md)
- [Future Roadmap](file:///d:/Coding/Major%20Project/PersonalServer/docs/roadmap.md)
