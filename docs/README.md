# PersonalServer Technical Documentation

> **Project Status**: Production Ready (Milestones J1–J5, R1–R9, HA Cluster Active)  
> **Architecture Target**: Android (Vivo Y31 Controller, Realme 5 Pro Compute Worker), Linux & Edge  
> **Last Updated**: November 2026

Welcome to PersonalServer — an autonomous, self-hosted, distributed compute cluster and edge application platform designed to run 24/7 on heterogeneous, recycled personal hardware (Android phones via Termux, Raspberry Pis, Linux servers, and Windows workstations).

---

## 🚀 New User? Start Here

Follow our numbered user guides in numerical order to go from a clean checkout to a fully deployed and monitored distributed server:

| Step | Guide | Purpose | Intended Audience | Expected Outcome |
|---|---|---|---|---|
| **01** | [01 — Setup Guide](01_setup.md) | Platform overview, prerequisites, repository cloning, initial config | All users | Running controller & verified web dashboard |
| **02** | [02 — Node Setup Guide](02_node_setup.md) | Prepare Android/Linux/Windows nodes, network mesh, zero-touch pairing | Operators | Enrolled cluster nodes reporting capabilities |
| **03** | [03 — Deployment Guide](03_deployment.md) | Application manifests, packaging, deploying Node.js/Python/Static apps | Developers | Live applications running with health checks |
| **04** | [04 — Cluster Management](04_cluster_management.md) | Managing workloads, submitting batch jobs, scheduler decisions | Operators / Users | Dispatched compute jobs with captured outputs |
| **05** | [05 — High Availability](05_high_availability.md) | Active-standby consensus, leader leases, automated failover | Architects / Operators | Redundant cluster with zero single points of failure |
| **06** | [06 — Storage & Artifacts](06_storage_and_artifacts.md) | Application storage, cryptographic verification, file exchange | Developers / Operators | Sandboxed storage and automated artifact collection |
| **07** | [07 — Monitoring & Troubleshooting](07_monitoring_and_troubleshooting.md) | Self-healing doctor utility, system logs, common error recovery | Operators | Verified cluster health and diagnostic mastery |
| **08** | [08 — Development Guide](08_development.md) | Repository structure, offline test running, contribution rules | Contributors | Configured dev environment & passing test suite |
| **09** | [09 — Architecture Reference](09_architecture_reference.md) | Subsystem component map, data flows, normative specifications | Architects | Comprehensive understanding of cluster internals |

---

## 🏛️ Advanced Technical Reference & Normative Specifications

For in-depth protocol definitions, mathematical formulas, state machine transition tables, and error code contracts, consult the authoritative specifications in [`docs/reference/`](reference/):

- **[Universal Job Contract (J1)](reference/universal_job_contract_j1.md)**: Standardized job definition, execution environment, and artifact schema.
- **[Capability Registry (J2)](reference/capability_registry_j2.md)**: Static and dynamic hardware/software capability discovery.
- **[Universal Scheduler (J3)](reference/universal_scheduler_j3.md)**: 8-stage eligibility pipeline, scoring formula, and atomic reservations.
- **[Deployment Architecture (D0)](reference/deployment_architecture_d0.md)**: Universal multi-runtime application deployment lifecycle and rollback.
- **[Hardware Migration Plan](reference/hardware_migration_plan.md)**: Roadmap transitioning from workstation development to mobile hardware.
- **[Raspberry Pi Migration](reference/raspberry_pi_migration.md)**: Linux Filesystem Hierarchy Standard (FHS) mapping and systemd daemon units.
- **[Multi-Node Simulation Suite](reference/multi_node_simulation.md)**: Hardware-independent multi-worker loopback test harness.
- **[Image Build & Deploy Workflow](reference/image_build_deploy_workflow.md)**: Decoupled 8-stage image pipeline and OCI digest verification.

---

## 💻 Supported Hardware & Operating Systems Matrix

| Platform / Environment | Supported Roles | Native Python | Native Node.js | Native Containers | Storage Explorer | Telemetry Sensors |
|---|---|---|---|---|---|---|
| **Android / Termux** (unrooted) | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ❌ Kernel cgroup restricted | ✅ Sandboxed | ✅ Battery, WiFi dBm, thermals |
| **Linux (Debian / Ubuntu / x86_64 / arm64)** | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ Sandboxed | ✅ System `/proc` & `sysfs` |
| **Raspberry Pi OS (arm64)** | Worker, Storage Host | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ Sandboxed | ✅ `vcgencmd` & thermal sysfs |
| **Windows 10/11 (Development PC)** | On-Demand Build Worker | ✅ Python 3.10+ | ✅ Node.js 18+ | ⚠️ Docker Desktop | ❌ Excluded (Ephemeral) | ✅ Win32 GlobalMemoryStatus |

---

## 🔒 Physical Production Hardware Topology

The operational cluster runs in a dedicated physical topology:
- **Primary Controller / Leader**: Vivo Y31 (Android Termux) — `100.85.108.5:8000`
- **Compute Worker / Standby Controller**: Realme 5 Pro (Android Termux) — `100.73.52.72:8000`
- **On-Demand Build Worker**: Windows 11 Workstation — `100.64.1.1`
- **Ingress**: Cloudflare Tunnel routing public domains (`akshatsahay.space`, `server.akshatsahay.space`) directly to the cluster.
