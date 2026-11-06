# PersonalServer Technical Documentation

> **Project Status**: Production Ready (Milestones J1–J5, R1–R9, HA Cluster Active)  
> **Architecture Target**: Android (Vivo Y31 Controller, Realme 5 Pro Compute Worker), Linux & Edge  
> **Last Updated**: November 2026

Welcome to **PersonalServer** — an autonomous, self-hosted, distributed compute cluster and edge application platform designed to run 24/7 on heterogeneous, recycled personal hardware (Android phones via Termux, Raspberry Pis, Linux servers, and Windows workstations).

---

## 🧭 Navigating the Documentation

PersonalServer features dual documentation journeys designed for different audiences:
1. **The Beginner / Operator Journey (Guides 01–09)**: Step-by-step onboarding to set up hardware, configure private networking, install the controller, enroll worker devices, deploy web applications, and configure high availability.
2. **The Engineer / Contributor Journey (Guides 10–12)**: In-depth codebase exploration, offline test suites, system architecture, and normative technical specifications.

---

## 📚 Numbered User Guides (Sequential Reading Order)

| Step | Guide | Purpose | Intended Audience | Expected Outcome |
|---|---|---|---|---|
| **01** | [01 — Introduction](guides/01_introduction.md) | Platform overview, philosophy, architecture principles | All users | Conceptual understanding of PersonalServer |
| **02** | [02 — Requirements](guides/02_requirements.md) | Hardware matrix, operating systems, prerequisites | Operators | Hardware and system preparation |
| **03** | [03 — Installation](guides/03_installation.md) | Cloning repository, running doctor, launching controller | Operators | Running controller & verified web dashboard |
| **04** | [04 — Node Setup](guides/04_node_setup.md) | Termux setup, Linux nodes, Tailscale mesh, zero-touch pairing | Operators | Enrolled cluster nodes reporting capabilities |
| **05** | [05 — First Deployment](guides/05_first_deployment.md) | Application manifests (`app.json`), packaging, deploying apps | Developers | Live applications running with health checks |
| **06** | [06 — Cluster Management](guides/06_cluster_management.md) | Managing workloads, submitting batch jobs, scheduler decisions | Operators / Users | Dispatched compute jobs with captured outputs |
| **07** | [07 — Storage & Artifacts](guides/07_storage_and_artifacts.md) | Application storage, cryptographic verification, file exchange | Developers / Operators | Sandboxed storage and automated artifact collection |
| **08** | [08 — Monitoring & Recovery](guides/08_monitoring_and_recovery.md) | Self-healing doctor utility, system logs, common error recovery | Operators | Verified cluster health and diagnostic mastery |
| **09** | [09 — High Availability](guides/09_high_availability.md) | Active-standby consensus, leader leases, automated failover | Architects / Operators | Redundant cluster with zero single points of failure |
| **10** | [10 — Development](guides/10_development.md) | Repository structure, offline test running, contribution rules | Contributors | Configured dev environment & passing test suite |
| **11** | [11 — Architecture](guides/11_architecture.md) | Subsystem component map, data flows, controller state machine | Architects | Comprehensive understanding of cluster internals |
| **12** | [12 — Protocols & Specs](guides/12_protocols_and_specifications.md) | Normative specifications, protocol schemas, wire contracts | Core Engineers | Protocol mastery and implementation reference |

---

## 🛠️ Operational Runbooks & Procedures (`docs/operations/`)

For day-to-day cluster administration, disaster recovery, and security:
- **[Operations Troubleshooting](operations/troubleshooting.md)**: Diagnostic decision trees for node connectivity, lease timeouts, and proxy errors.
- **[Maintenance & Doctor Utility](operations/maintenance.md)**: Automated self-healing routines, process adoption, and database repair.
- **[Backup & Restore Guide](operations/backup_and_restore.md)**: Automated state snapshotting, disaster recovery, and reproducible checkouts.
- **[Security & Threat Model](operations/security.md)**: Token authentication, sandbox boundaries, path traversal defenses, secrets isolation.
- **[Networking & Mesh Ingress](operations/networking.md)**: Tailscale WireGuard private mesh setup and Cloudflare Tunnel public routing.
- **Runbooks**:
  - [Application Deployment Runbook](operations/runbooks/application_deployment_runbook.md)
  - [Failover & Recovery Runbook](operations/runbooks/failover_recovery_runbook.md)
  - [Job Recovery Runbook](operations/runbooks/job_recovery_runbook.md)

---

## 🏛️ Normative Specifications & Technical Reference (`docs/reference/`)

Authoritative architectural specifications governing cluster contracts:
- **[Universal Job Contract (J1)](reference/universal_job_contract_j1.md)**: Standardized job definition, execution environment, and artifact schema.
- **[Capability Registry (J2)](reference/capability_registry_j2.md)**: Static and dynamic hardware/software capability discovery.
- **[Universal Scheduler (J3)](reference/universal_scheduler_j3.md)**: 8-stage eligibility pipeline, scoring formula, and atomic reservations.
- **[Deployment Architecture (D0)](reference/deployment_architecture_d0.md)**: Universal multi-runtime application deployment lifecycle and rollback.
- **[API Reference](reference/api_reference.md)**: REST endpoints, HTTP status codes, and JSON response schemas.
- **[Configuration Reference](reference/configuration_reference.md)**: `node.conf`, environment variables, and tunable thresholds.
- **[Data Models](reference/data_models.md)**: Durable store schemas (`jobs_store.json`, `apps_store.json`, `nodes_store.json`).
- **[Runtime Adapters](reference/runtime_adapters.md)**: Runtime process isolation, probers, and supervision adapters.
- **[Historical Archives](reference/historical/)**: Evolution roadmap, hardware migration plans, and baseline prototypes.

---

## 💻 Hardware Platform Compatibility Matrix

| Platform / Environment | Cluster Roles | Native Python | Native Node.js | Native Containers | Telemetry Sensors | Verification Status |
|---|---|---|---|---|---|---|
| **Android (Termux unrooted)** | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ❌ Kernel cgroup restricted | ✅ Battery %, WiFi dBm, thermals | **Tier 1 (Production)** — Physically verified on Vivo Y31 & Realme 5 Pro |
| **Linux (Debian / Ubuntu / x86_64 / arm64)** | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ `/proc` & `sysfs` thermals | **Tier 1 (Production)** — Verified in CI & hardware environments |
| **Raspberry Pi OS (arm64)** | Worker, Storage Host | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ `vcgencmd` thermals | **Tier 2 (Supported)** — Verified in hardware simulation |
| **Windows 10/11 (Development PC)** | On-Demand Build Worker | ✅ Python 3.10+ | ✅ Node.js 18+ | ⚠️ Docker Desktop | ✅ Win32 GlobalMemoryStatus | **Tier 2 (Supported)** — Verified on Windows 11 host |

---

## 🔒 Physical Production Hardware Topology

The operational cluster runs in a dedicated physical topology:
- **Primary Controller / Leader**: Vivo Y31 (Android Termux) — `100.85.108.5:8000`
- **Compute Worker / Standby Controller**: Realme 5 Pro (Android Termux) — `100.73.52.72:8000`
- **On-Demand Build Worker**: Windows 11 Workstation — `100.64.1.1`
- **Ingress**: Cloudflare Tunnel routing public domains (`akshatsahay.space`, `server.akshatsahay.space`) directly to the cluster.
