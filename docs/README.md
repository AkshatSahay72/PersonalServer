# PersonalServer Technical Documentation

Welcome to the PersonalServer documentation catalog. All architectural specifications, operation runbooks, API references, and deployment guides are indexed below.

---

## 📑 Documentation Index

### 1. Architecture & Core Subsystems
- **[System Architecture](file:///docs/architecture.md)**: High-level system architecture, core subsystems, Tailscale mesh, and Cloudflare ingress.
- **[Controller Reference & API](file:///docs/controller.md)**: Master Controller CLI commands, REST endpoints, and telemetry payload schema.
- **[Node Agent & Supervisor](file:///docs/node-agent.md)**: Edge device agent, rich telemetry collection, local worker daemon, and CLI usage.
- **[Resource-Aware Scheduling](file:///docs/scheduling.md)**: Two-stage constraint filtering, Docker capability matching, architecture normalization, and fitness scoring.
- **[Multi-Node Storage Subsystem](file:///docs/storage.md)**: Sandboxed multi-node storage explorer, traversal defenses, and storage APIs.

### 2. High Availability & Reliability
- **[High Availability (HA) & Master Failover](file:///docs/high_availability_failover.md)**: Self-contained leader lease consensus, automated standby promotion, split-brain immunity, and transparent worker redirection.
- **[Job Reliability & Failure Recovery](file:///docs/reliability.md)**: Job state machine, atomic leases, worker abandonment detection, and recovery sweeps.
- **[Job Lifecycle & Recovery Guide](file:///docs/job_lifecycle_recovery.md)**: Deep dive into the finite state machine (FSM), transitions, and retry policies.
- **[Workload Jobs & Allowlist](file:///docs/jobs.md)**: Sandboxed allowlisted workload types, parameters, and execution isolation.

### 3. Container Platform & OCI Deployment
- **[Image Build & Deployment Workflow](file:///docs/image_build_deploy_workflow.md)**: Decoupled 8-stage image pipeline, tagging, verification, and rollback.
- **[Application Routing & Gateway](file:///docs/domain_migration.md)**: Domain migration, path-based routing, and Cloudflare tunnel configuration.

### 4. Operations, Setup & Troubleshooting
- **[Getting Started Guide](file:///docs/getting-started.md)**: Controller setup, device onboarding, service management, and Web UI overview.
- **[Node Enrollment & Onboarding](file:///docs/node-enrollment.md)**: Zero-touch `PS-XXXX-XXXX` pairing codes, SHA-256 validation, and credential security.
- **[Troubleshooting Runbook](file:///docs/troubleshooting.md)**: Diagnostics and resolution for offline nodes, Cloudflare tunnels, storage errors, and HA failover issues.
- **[Networking & Ingress](file:///docs/networking.md)**: Separation of private Tailscale mesh and public application ingress planes.
- **[Security & Threat Model](file:///docs/security.md)**: Authentication tokens, sandbox boundaries, path traversal defenses, and secrets isolation.

### 5. Hardware Migration & Testing
- **[Hardware Migration Plan](file:///docs/hardware_migration_plan.md)**: Roadmap for transitioning from Windows workstations to 64-bit Raspberry Pi OS and Android Termux.
- **[Raspberry Pi OS Deployment & FHS Mapping](file:///docs/raspberry_pi_migration.md)**: Linux Filesystem Hierarchy Standard (FHS) mapping, systemd service units, and security sandboxing.
- **[Multi-Node Simulation Suite](file:///docs/multi_node_simulation.md)**: Hardware-independent multi-worker simulation and fault injection testing on loopback.
- **[Reproducible Setup, CI & Backups](file:///docs/reproducible_setup.md)**: Clean checkout setup, virtual environment, backup/restore pipelines, and test execution.
- **[Development & Testing Guide](file:///docs/development.md)**: Core developer principles, quick testing commands, and backup workflows.
- **[Evolution & Roadmap](file:///docs/roadmap.md)**: Completed milestones (v0.1 through v1.2) and future directions.
