# 12 — Protocols and Specifications Reference

> **Reading Order**: **Step 12 of 12** in the PersonalServer Guide Series (Completes the Engineer Journey).  
> **Previous Guide**: [11 — Architecture Reference](11_architecture.md)  
> **Audience**: Core contributors, protocol implementers, and security auditors.  
> **Prerequisites**: Familiarity with Guides 01 through 11.

---

## 1. Overview of Normative Specifications

PersonalServer architecture is governed by four frozen normative specifications located in `docs/reference/`. These documents define authoritative data models, state transitions, wire protocols, and error codes:

| Specification | Path | Scope |
|---|---|---|
| **Universal Job Contract (J1)** | [docs/reference/universal_job_contract_j1.md](../reference/universal_job_contract_j1.md) | Standardized job definition, execution environment, and artifact schema |
| **Capability Registry (J2)** | [docs/reference/capability_registry_j2.md](../reference/capability_registry_j2.md) | Static and dynamic hardware/software capability discovery |
| **Universal Scheduler (J3)** | [docs/reference/universal_scheduler_j3.md](../reference/universal_scheduler_j3.md) | 8-stage eligibility pipeline, scoring formula, and atomic reservations |
| **Deployment Architecture (D0)** | [docs/reference/deployment_architecture_d0.md](../reference/deployment_architecture_d0.md) | Universal multi-runtime application deployment lifecycle and rollback |

---

## 2. Core Protocol Summary

### A. Universal Job Contract (J1)
- **Top-Level Attributes**: `job_id`, `type`, `target`, `requirements`, `env`, `code`, `args`, `outputs`, `timeout_sec`.
- **Fencing Generations**: Strictly monotonic integer generation counter increments with each dispatch attempt. Stale workers attempting to report status or upload artifacts with older generation tokens are rejected with HTTP 409 Conflict.
- **Environment Bindings**: Injected execution environment variables include `PS_JOB_ID`, `PS_ATTEMPT_ID`, `PS_WORKSPACE_DIR`, `PS_ARTIFACTS_DIR`, `PS_FENCING_GENERATION`.

### B. Capability Registry (J2)
- **Namespace Structure**: Standardized taxonomy:
  - `compute` (CPU cores, architecture: `arm64`, `amd64`)
  - `python3`, `nodejs`, `container_runtime:docker`
  - `battery_telemetry`, `thermal_sensors`
- **Verification Guarantee**: Probers execute actual probe binaries (`python -V`, `node -v`, `docker info`). Unverified advertised claims are distinguished from physically verified capabilities.

### C. Universal Scheduler (J3)
- **8-Stage Ordered Pipeline**:
  1. *Liveness & Freshness*: Node last seen within heartbeat threshold ($TTL \le 45s$).
  2. *Administrative & Policy*: Maintenance mode, drain flags, admission controls.
  3. *Capability Matching*: Exact verified capability string matching.
  4. *Resource Reservations*: Memory capacity checks under lock.
  5. *Thermal & Power*: Battery percentage ($>15\%$) and thermal throttling limits ($<48^\circ\text{C}$).
  6. *Storage Space*: Disk space verification on candidate nodes.
  7. *Affinity & Roles*: Worker role matching.
  8. *Scoring & Placement*: Multi-factor fitness score.
- **Scoring Formula**:
  $$S = w_{mem} \cdot M_{free} + w_{therm} \cdot (T_{max} - T) + w_{batt} \cdot B_{pct}$$
- **Priority Aging**: Long-queued jobs have their effective priority incremented by $0.1$ points per second waiting in queue.

### D. Deployment Engine (D0 / R1–R9)
- **State Monotonicity**: Deployment statuses transition strictly forward:
  `QUEUED` ➔ `STAGING` ➔ `PREPARING` ➔ `ACTIVATING` ➔ `HEALTH_CHECKING` ➔ `RUNNING`
- **Health Check Guarantees**: HTTP GET against declared health check endpoint with retries and configurable timeout.
- **Rollback Guarantee**: Unhealthy releases automatically restore the prior verified release without service downtime.

---

## 3. Reference and Operations Links

- **Operational Runbooks**:
  - [Application Deployment Runbook](../operations/runbooks/application_deployment_runbook.md)
  - [Failover Recovery Runbook](../operations/runbooks/failover_recovery_runbook.md)
  - [Job Recovery Runbook](../operations/runbooks/job_recovery_runbook.md)
- **Operations & Security**:
  - [Operations Troubleshooting](../operations/troubleshooting.md)
  - [Maintenance & Doctor Guide](../operations/maintenance.md)
  - [Backup & Restore Guide](../operations/backup_and_restore.md)
  - [Security & Threat Model](../operations/security.md)
  - [Networking & Mesh Setup](../operations/networking.md)
- **API & Configuration Reference**:
  - [API Reference](../reference/api_reference.md)
  - [Configuration Reference](../reference/configuration_reference.md)
  - [Data Models](../reference/data_models.md)
  - [Runtime Adapters](../reference/runtime_adapters.md)

---

## 4. Conclusion

You have explored the full architectural and operational documentation of PersonalServer!

👉 **Return to [Documentation Index](../README.md)** or start the onboarding series at **[01 — Introduction](01_introduction.md)**.
