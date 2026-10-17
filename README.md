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

## 3. Platform Capability & Support Matrix

PersonalServer is designed to run across diverse, asymmetric compute nodes without requiring root privileges or uniform hardware.

| Platform / Environment | Tested Role(s) | Python Workloads | Docker Containers | Storage Service | Telemetry Engine | Verification Status |
|---|---|---|---|---|---|---|
| **Android / Termux** (unrooted) | Worker, Controller | ✅ Native Python 3.9+ | ❌ Kernel cgroup restricted | ✅ Sandboxed | ✅ Battery, WiFi dBm, thermals | Verified on physical hardware (Vivo Y31, Realme 5 Pro) |
| **Linux (Debian / Ubuntu / x86_64 / aarch64)** | Worker, Controller, Standby | ✅ Native Python 3.9+ | ✅ Native Docker / udocker | ✅ Sandboxed | ✅ System `/proc` & `sysfs` | Verified in CI (Ubuntu-latest) & multi-node simulator |
| **Raspberry Pi OS (arm64 / armhf)** | Worker, Storage | ✅ Native Python 3.9+ | ✅ Docker engine | ✅ Sandboxed | ✅ `vcgencmd` & thermal sysfs | Architecture normalization verified in test suites |
| **Windows 10/11 (Development PC)** | Dev Controller, Local Worker | ✅ Native via `LocalWorker` | ⚠️ Docker Desktop required | ✅ Sandboxed | ✅ Win32 GlobalMemoryStatusEx | Verified in CI (windows-latest) & `test_local_worker.py` |

> [!NOTE]
> When a node lacks Docker capabilities (such as unrooted Android/Termux), PersonalServer advertises `container_runtime:docker: false`. The resource-aware scheduler routes container tasks only to nodes with verified Docker capability, and workers reject unsupported container jobs with clear status errors rather than crashing.

---

## 4. Repository Structure & Directory Catalog

```text
PersonalServer/
├── agent/                  # Node agent daemon, LocalWorker, and sandboxed JobExecutor
├── common/                 # Shared, dependency-free utilities (time, crypto, arch, storage)
├── config/                 # Centralized configuration, HA consensus manager, telemetry
│   ├── platform.yaml       # Platform public hostnames, routing domain, scheme
│   ├── node.example.json   # Template node identity and capability configuration
│   └── secrets/            # Local node tokens and secrets (strictly git-ignored)
├── controller/             # Master Controller HTTP service, scheduler, and API routes
│   └── stores/             # Atomic JSON persistence stores (nodes, jobs, apps, onboarding)
├── docs/                   # Architectural guides, operational runbooks, and threat models
├── install/                # Hardware detection (`detect.sh`) and Termux setup (`termux.sh`)
├── router/                 # Application reverse proxy and path-based router (:8088)
├── scheduler/              # Constraint-based and resource-aware workload scheduler
├── scripts/                # Canonical service management scripts and test runner CLI
├── scratch/                # Development-only live hardware diagnostic scripts (non-CI)
├── services/node-api/      # Node API web service and Web Operations Console dashboard
├── storage/                # Node application sandbox storage and OCI image cache
└── tests/                  # Deterministic offline unit and integration test suites
```

---

## 5. Canonical Service Management & Startup

PersonalServer uses standardized shell scripts on Linux and Termux, and direct Python commands on Windows:

| Lifecycle Action | Canonical Shell Command (Linux / Termux) | Native Windows Command |
|---|---|---|
| **Start Services** | `bash scripts/start.sh` | `python controller/controller.py start` |
| **Stop Services** | `bash scripts/stop.sh` | Terminate background process / Ctrl+C |
| **Restart Services** | `bash scripts/restart.sh` | Restart command |
| **Service Status** | `bash scripts/status.sh` | `python controller/controller.py nodes` |
| **Health Check** | `bash scripts/health.sh` | `python controller/controller.py health` |
| **HA Cluster Status** | `bash scripts/ha-status.sh` | `python controller/controller.py ha` |
| **Node Telemetry** | `bash scripts/node-info.sh` | `python agent/node-agent.py status` |

---

## 6. Verification & Automated Testing

All automated tests run completely offline without contacting physical nodes or external network resources.

### Canonical Test Commands

```bash
# 1. Fast isolated unit tests (pure in-memory, < 3s)
python scripts/run_tests.py --unit

# 2. Multi-node simulation, lifecycle, routing, and HA integration tests
python scripts/run_tests.py --integration

# 3. Complete automated test suite (all 29 suites, 296 tests)
python scripts/run_tests.py --all

# 4. Standard unittest discovery (matches scripts/run_tests.py --all)
python -m unittest discover -s tests -p "test_*.py"

# 5. Static code syntax & AST lint checks
python scripts/run_tests.py --lint

# 6. Web Console JavaScript syntax check
node -c services/node-api/static/app.js
```

---

## 7. Device Configuration Without Hardcoding

PersonalServer avoids hardcoding device hostnames, Tailscale IPs, or tokens into source files. Every node dynamically resolves its configuration using:

1. **Environment Variables**:
   - `PERSONALSERVER_CONTROLLER_URL`: URL of the active cluster controller (e.g. `http://100.85.108.5:8000`).
   - `PERSONALSERVER_BASE_DIR`: Base installation directory (defaults to `~/PersonalServer`).
   - `STORAGE_INTEGRATION_URL`: Configurable target for storage UI integration tests.
2. **Configuration Files** (git-ignored for machine-specific deployment):
   - `config/node.json`: Auto-generated on onboarding via `detect.sh` or `node-agent.py onboard`.
   - `config/secrets/enrollment.token`: Local enrollment authentication secret.
   - `config/platform.yaml`: Centralized public domain and ingress routing specification.

---

## 8. Complete Documentation

Comprehensive technical guides are maintained in the [`docs/`](file:///docs/README.md) directory:

### Core Architecture & HA
* [Technical Documentation Catalog](file:///docs/README.md)
* [System Architecture](file:///docs/architecture.md)
* [High Availability (HA) & Master Failover](file:///docs/high_availability_failover.md)
* [Controller Reference & API](file:///docs/controller.md)
* [Node Agent & Telemetry Guide](file:///docs/node-agent.md)
* [Resource-Aware Scheduling](file:///docs/scheduling.md)
* [Multi-Node Storage Subsystem](file:///docs/storage.md)

### Workloads, Reliability & Containers
* [Job Reliability & Failure Recovery](file:///docs/reliability.md)
* [Job Lifecycle FSM & Recovery Guide](file:///docs/job_lifecycle_recovery.md)
* [Workload Jobs & Allowlist](file:///docs/jobs.md)
* [OCI Image Build & Deployment Workflow](file:///docs/image_build_deploy_workflow.md)
* [Application Routing & Gateway](file:///docs/domain_migration.md)

### Setup, Security & Migration
* [Getting Started Guide](file:///docs/getting-started.md)
* [Node Enrollment & Onboarding](file:///docs/node-enrollment.md)
* [Troubleshooting & Runbook](file:///docs/troubleshooting.md)
* [Security Model & Threat Assessment](file:///docs/security.md)
* [Networking & Ingress Architecture](file:///docs/networking.md)
* [Hardware Migration Plan (Raspberry Pi & Android)](file:///docs/hardware_migration_plan.md)
* [Raspberry Pi OS Deployment & FHS Mapping](file:///docs/raspberry_pi_migration.md)
* [Multi-Node Simulation Suite](file:///docs/multi_node_simulation.md)
* [Reproducible Development, CI & Backup](file:///docs/reproducible_setup.md)
* [Development & Testing Guide](file:///docs/development.md)
* [Evolution & Roadmap](file:///docs/roadmap.md)
