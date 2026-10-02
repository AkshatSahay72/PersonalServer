# PersonalServer Architecture (v1.0)

## 1. Architectural Philosophy

PersonalServer is designed around the concept of a **sovereign, multi-tier personal cloud** using inexpensive, repurposed edge devices. Rather than relying on heavy containerization engines or cloud orchestration layers, PersonalServer uses clean, language-level process abstractions and native OS tools.

```text
                     PERSONALSERVER TOPOLOGY
                                │
               ┌────────────────┼────────────────┐
               ▼                ▼                ▼
         [COMPUTE TIER]  [STORAGE TIER]   [CONTROL TIER]
           Vivo Y31        Storage Root      Laptop
           Workloads       File Server     Controller
           Jobs/Retry      Upload/Download Scheduler
               │                │                │
               └────────────────┼────────────────┘
                                │
                        Web Operations UI
                                │
                        Cloudflare Tunnel
                                │
                            Internet
```

---

## 2. Core Subsystems

### 2.1 The Laptop Controller (`controller/controller.py`)
- **Port**: `:8000` (Tailscale private).
- **Functions**:
  - Central inventory of cluster nodes (`controller/data/nodes.json`).
  - Persistent job database (`controller/data/jobs.json`).
  - Liveness monitoring with configurable heartbeat timeouts (default 60s).
  - Resource-Aware Scheduler with deterministic scoring algorithms.
  - Lease management and automatic recovery engine.
  - Token-based node enrollment and removal.

### 2.2 The Node Agent (`agent/node-agent.py`)
- Runs locally on each server node (e.g. Vivo Y31 under Termux).
- Performs service lifecycle management (`scripts/start.sh`, `stop.sh`, `restart.sh`, `status.sh`, `health.sh`).
- Collects system metrics (`nproc`, `uptime`, `free -h`, `df -h`).
- Sends authenticated periodic heartbeats to the Controller.
- Fetches and claims pending workload jobs via `/nodes/<node_id>/jobs/next`.
- Executes workloads safely via `agent/job_executor.py`.

### 2.3 The Node API & Storage Subsystem (`services/node-api/app.py`)
- **Port**: `:8080` (Localhost / Tailscale / Cloudflare Tunnel).
- Serves the Operations Web Interface (`services/node-api/static/`).
- Hosts the Storage Subsystem managing `~/PersonalServer/storage/`.
- Proxies cluster and workload requests to the Controller.

---

## 3. Network Architecture & Ingress

```text
Public Internet
       │
       ▼ (HTTPS :443)
Cloudflare Edge Network
       │
       ▼ (Cloudflare Tunnel)
Node 01 (Vivo Y31) -> Node API (:8080)
       │
       ▼ (Tailscale Private Network)
Laptop Controller (:8000)
```

- **Cloudflare Tunnel**: Provides public remote access to the Web Interface without exposing open inbound router ports or requiring static public IP addresses.
- **Tailscale Mesh**: Secure, encrypted private network for cluster node-to-controller communication and private SSH administration.
- **SSH (`:8022`)**: Private, accessible only via Tailscale.
- **Node API (`:8080`)**: Private cluster service, bridged to the public Internet exclusively through Cloudflare Tunnel.
