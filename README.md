# PersonalServer

> **A lightweight, robust, self-hosted personal server, container platform, and distributed compute cluster designed for heterogeneous edge devices (Android/Termux, Raspberry Pi, Linux servers, WSL, and Laptops).**

---

## 1. What is PersonalServer?

PersonalServer is a lightweight, zero-runtime-dependency distributed computing platform. It unites heterogeneous personal hardware—such as Android phones running Termux, Raspberry Pis, Linux boxes, and Windows development PCs—into a resilient personal compute cluster.

The core controller, agent, consensus engine, and storage subsystems run exclusively on the **Python 3.9+ standard library** without requiring external databases, message brokers, or heavy container orchestrators.

---

## 2. What Can It Currently Do?

* 👑 **Active-Standby High Availability (HA)**: Self-contained leader lease consensus, term tracking, state sync, and transparent worker/client failover when a master node goes down.
* ⚡ **1-Command Zero-Touch Onboarding**: Add any Android phone (via Termux), Raspberry Pi, or Linux machine in seconds using short-lived, single-use pairing codes (`PS-XXXX-XXXX`) with automated hardware discovery.
* 📊 **Rich Device Telemetry**: Real-time cross-platform monitoring of battery percentage, charging state, WiFi/Cellular signal strength (dBm), link speed, CPU temperature, and thermal throttling.
* 🔄 **Reliable Workload Execution & DAG Workflows**: Resource-aware job placement, automatic lease-based failure recovery, retries, and multi-step directed acyclic graph (DAG) workflow scheduling.
* 📦 **Cross-Worker Artifact Exchange**: Upload, verify (SHA-256), and exchange files and build bundles between workers and the controller.
* 🐳 **Decoupled OCI Container Platform**: Image build pipeline, tagging with immutable `sha256:...` digests, architecture normalization (`arm64`/`amd64`), and sandboxed execution on container-capable nodes.
* 🌐 **Dynamic Application Routing**: Integrated reverse proxy (`:8088`) providing path-based routing to containerized applications with traversal defenses.
* 🖥️ **Technical Operations Console**: Information-dense web dashboard (`:8080`) providing real-time cluster health, node statuses, job queues, workflows, and multi-node storage exploration.
* 🔒 **Private WireGuard Mesh**: All inter-node coordination, heartbeats, SSH administration, and Node APIs run over a private Tailscale network.

---

## 3. Supported Hardware & Operating Systems

PersonalServer is designed to run across diverse, asymmetric compute nodes without requiring root privileges or uniform hardware:

| Platform / Environment | Tested Role(s) | Python Workloads | Containers | Storage Service | Telemetry Engine | Verification Status |
|---|---|---|---|---|---|---|
| **Android / Termux** (unrooted) | Worker, Controller, Standby | ✅ Native Python 3.9+ | ❌ Kernel cgroup restricted | ✅ Sandboxed | ✅ Battery, WiFi dBm, thermals | Verified on physical hardware (Vivo Y31, Realme 5 Pro) |
| **Linux (Debian / Ubuntu / x86_64 / aarch64)** | Worker, Controller, Standby | ✅ Native Python 3.9+ | ✅ Native Docker / udocker | ✅ Sandboxed | ✅ System `/proc` & `sysfs` | Verified in CI & multi-node simulator |
| **Raspberry Pi OS (arm64 / armhf)** | Worker, Storage | ✅ Native Python 3.9+ | ✅ Docker engine | ✅ Sandboxed | ✅ `vcgencmd` & thermal sysfs | Architecture normalization verified in test suites |
| **Windows 10/11 (Development PC)** | On-Demand Build Worker | ✅ Native via `LocalWorker` | ⚠️ Docker Desktop (when running) | ❌ Excluded (Ephemeral workspace) | ✅ Win32 GlobalMemoryStatusEx | Verified on Windows 11 host & unit test suite |

> [!NOTE]
> The Windows PC operates as an optional **on-demand build worker**, not an always-on server. When the laptop is offline, jobs requiring the `build` capability remain safely queued on the controller and are not scheduled onto phone nodes. When started via `ps-worker run` or `python agent/local_worker.py run`, the worker processes pending build jobs, uploads artifacts, and shuts down safely.

---

## 4. Prerequisites

### Core Runtime Prerequisites
- **Python 3.9+** (Standard library only; zero third-party packages required for core services).
- **Git** (for code checkout).

### Optional / Feature-Specific Prerequisites
- **Tailscale**: Recommended for seamless private WireGuard mesh networking between mobile/edge nodes.
- **Docker / udocker**: Required only if you intend to run OCI container workloads on Linux or Raspberry Pi nodes.
- **Cloudflare Zero Trust / Cloudflared**: Required only if you want public HTTPS ingress without opening firewall ports.
- **Development & Testing**: `pip install -r requirements-dev.txt` (only needed for developer linting and test coverage tooling).

---

## 5. Installation and Getting Started

### Step 1: Clone the Repository
```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

### Step 2: Configure Environment and Secrets
```bash
# Copy environment configuration
cp .env.example .env

# Create secrets directory and set enrollment token
mkdir -p config/secrets
cp config/secrets/enrollment.token.example config/secrets/enrollment.token
```

### Step 3: Start Services

#### On Linux or Android (Termux):
```bash
# Start Controller and Web Operations Console
bash scripts/start.sh

# Check service status
bash scripts/status.sh
```

#### On Windows:
```powershell
# Start Controller in the foreground or background
python controller/controller.py start

# Verify cluster status
python controller/controller.py nodes
```

Expected Result:
- Controller HTTP API starts listening on port `8000` (e.g., `http://127.0.0.1:8000`).
- Web Operations Console starts listening on port `8080` (e.g., `http://127.0.0.1:8080`).

---

## 6. Accessing the Dashboard

Once the services are running, the Web Operations Console can be accessed via:

* **Local Network / Host Access**:
  Open your web browser and navigate to:
  ```text
  http://localhost:8080
  ```
  *(Or replace `localhost` with your node's LAN or Tailscale IP address, e.g., `http://100.85.108.5:8080`)*

* **Public Ingress (if configured via Cloudflare Tunnel)**:
  ```text
  https://server.akshatsahay.space
  ```
  *(Or your configured apex/subdomain in `config/platform.yaml`)*

---

## 7. Canonical Service Commands

| Lifecycle Action | Shell Script (Linux / Termux) | Windows / Direct Python Command |
|---|---|---|
| **Start Services** | `bash scripts/start.sh` | `python controller/controller.py start` |
| **Stop Services** | `bash scripts/stop.sh` | Terminate terminal process / `Ctrl+C` |
| **Restart Services** | `bash scripts/restart.sh` | Restart process |
| **Service Status** | `bash scripts/status.sh` | `python controller/controller.py nodes` |
| **Health Check** | `bash scripts/health.sh` | `python scripts/doctor.py` |
| **HA Status** | `bash scripts/ha-status.sh` | `curl -s http://127.0.0.1:8000/ha/status` |
| **On-Demand Worker** | `python agent/local_worker.py run` | `python agent/local_worker.py run` |

---

## 8. Automated Testing & Verification

All test suites run **completely offline** with isolated mock environments:

```bash
# Fast isolated unit tests (< 3s)
python scripts/run_tests.py --unit

# Multi-node simulation, lifecycle, routing, and HA integration tests
python scripts/run_tests.py --integration

# Complete automated test suite (all 29 suites, 296 tests)
python scripts/run_tests.py --all

# Code syntax and AST quality checks
python scripts/run_tests.py --lint
```

---

## 9. Detailed Documentation Index

For in-depth operational runbooks, architecture specifications, and API guides, consult the [`docs/`](docs/README.md) directory.

### User Guides (Follow in numerical order):
1. **[01 — Setup Guide](docs/01_setup.md)**: Hardware overview, prerequisites, cloning, initial configuration, starting the controller.
2. **[02 — Node Setup Guide](docs/02_node_setup.md)**: Preparing Android/Termux, Linux, and Windows nodes; private mesh setup; zero-touch pairing.
3. **[03 — Deployment Guide](docs/03_deployment.md)**: Application manifests (`app.json`), packaging, deploying Node.js/Python/Static apps, auto-rollback.
4. **[04 — Cluster Management](docs/04_cluster_management.md)**: Node roles, capability inspection, submitting batch jobs, scheduler decisions.
5. **[05 — High Availability](docs/05_high_availability.md)**: Active-standby consensus, leader leases, automated failover, split-brain immunity.
6. **[06 — Storage & Artifacts](docs/06_storage_and_artifacts.md)**: Sandboxed application storage, artifact transfer, SHA-256 integrity verification.
7. **[07 — Monitoring & Troubleshooting](docs/07_monitoring_and_troubleshooting.md)**: Health checks, log streaming, automated self-healing doctor utility.
8. **[08 — Development Guide](docs/08_development.md)**: Repository layout, running offline test suites, contribution principles.
9. **[09 — Architecture Reference](docs/09_architecture_reference.md)**: Subsystem component map, data flows, and links to normative specifications.

### Normative Specifications & Technical Reference:
- **[Universal Job Contract (J1)](docs/reference/universal_job_contract_j1.md)**
- **[Capability Registry (J2)](docs/reference/capability_registry_j2.md)**
- **[Universal Scheduler (J3)](docs/reference/universal_scheduler_j3.md)**
- **[Deployment Architecture (D0)](docs/reference/deployment_architecture_d0.md)**
