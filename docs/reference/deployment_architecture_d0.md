# PersonalServer
## Deployment Architecture — D0 Architecture Freeze

**Document version:** 1.0  
**Date:** 5 November 2026  
**Status:** Architecture decisions frozen  
**Implementation status:** Not started  
**Scope:** Distributed application deployment platform

---

## 1. Executive Summary

PersonalServer Deployment is a distributed application deployment system designed to deploy, execute, monitor, and manage applications across heterogeneous computing nodes.

The platform must support native processes, containerized applications, static applications, and custom execution environments.

Users must be able to initiate deployments through the dashboard, CLI, API, or GitHub integration.

The deployment experience must remain simple even when the underlying execution infrastructure is distributed.

The system will use:

- A distributed control plane.
- A persistent job scheduling system.
- Flexible build placement.
- Hybrid artifact storage.
- Pluggable runtime adapters.
- Node capability discovery.
- Health-based release activation.
- Versioned release management.
- Recovery and rollback mechanisms.

The architecture is designed to operate across Android/Termux devices, Linux systems, Windows build workers, and future compatible nodes.

### Core design principle

**Infrastructure complexity belongs to PersonalServer, not to the application developer.**

A simple application should not require complicated infrastructure configuration to deploy.

---

## 2. Confirmed Architecture Decisions

The following decisions are frozen for D0.

| Architecture dimension | Decision |
|---|---|
| Deployment scope | Multi-node cluster |
| Runtime architecture | Hybrid runtime adapters |
| Deployment triggers | Dashboard, CLI, API, GitHub |
| Build execution | Flexible scheduling |
| Artifact storage | Hybrid shared registry and node-local cache |
| Control-plane availability | Distributed |
| Job execution | Scheduler-managed asynchronous execution |
| Application activation | Health-verified release activation |
| Rollback | Previous known-good release |
| Production dependency | No permanent dependency on Windows laptop |

These decisions establish architectural direction. They do not imply that every capability must be implemented in the first release.

---

## 3. System Architecture

The platform is divided into five logical layers.

### Layer 1 — Interface Layer

Responsibilities:

- Dashboard.
- Command-line interface.
- REST API.
- GitHub webhook integration.
- Deployment request validation.

All interfaces must use the same deployment API and lifecycle.

No interface may implement an independent deployment engine.

### Layer 2 — Distributed Control Plane

Responsibilities:

- Deployment orchestration.
- Cluster membership.
- Leader election and leadership coordination.
- Durable deployment state.
- Application registry.
- Release registry.
- Job coordination.
- Recovery after controller failure.

The control plane coordinates operations but does not directly execute application workloads.

Control-plane components must support distributed operation without allowing conflicting deployment decisions.

### Layer 3 — Scheduling Plane

Responsibilities:

- Persistent job queue.
- Job claiming.
- Node selection.
- Resource-aware scheduling.
- Capability matching.
- Lease management.
- Fencing tokens.
- Retry policies.
- Cancellation.
- Job recovery.

The scheduler must distinguish between execution compatibility and resource availability.

A node is eligible only if it satisfies the workload's hard requirements.

### Layer 4 — Execution Plane

Responsibilities:

- Node agents.
- Build execution.
- Runtime adapters.
- Application lifecycle management.
- Health checks.
- Process monitoring.
- Execution logs.

Execution must be independent of the control-plane request lifecycle.

### Layer 5 — Platform Services

Responsibilities:

- Artifact registry.
- Node-local artifact caches.
- Configuration and secret management.
- Application routing.
- Release storage.
- Logs and diagnostics.
- Networking.

These services provide reusable capabilities to the deployment system and other PersonalServer subsystems.

---

## 4. Distributed Control Plane

### 4.1 Architectural decision

The control plane must operate as a distributed system.

It must support:

- Multiple controller-capable nodes.
- Coordinated leadership.
- Persistent state.
- Controller recovery.
- Leadership transfer.
- Duplicate-operation protection.
- Split-brain prevention.

### 4.2 Leadership model

Use a leader-coordinated distributed control plane.

The elected leader coordinates state-changing deployment operations.

Other controller-capable members may provide standby coordination and recovery capabilities.

This does not require unrestricted active-active writes.

There must be one authoritative leader for deployment-state transitions at a time.

### 4.3 Consistency requirements

The system must use:

- Durable replicated metadata.
- Quorum-based leadership decisions.
- Monotonically increasing leadership generations.
- Fencing of stale leaders.
- Idempotent state transitions.
- Recovery from interrupted operations.

A node must not activate a release using stale leadership authority.

### 4.4 Critical quorum constraint

The current always-on PersonalServer hardware consists of:

- Vivo Y31.
- Realme 5 Pro.

The Windows laptop is an on-demand worker and cannot be counted as a permanent cluster voter.

Two voting members alone cannot guarantee safe automatic failover during a network partition while preserving availability on both sides.

Therefore, safe automatic control-plane failover requires one of the following:

1. A third always-available voting member.
2. An independent quorum witness.
3. A deliberately fail-closed two-node configuration without automatic partition-time leadership takeover.

**This is an implementation prerequisite, not an optional optimization.**

The third voter or witness must not depend on the Windows laptop.

D0 does not select a specific consensus implementation. That decision belongs to the technical design stage.

---

## 5. Flexible Build Execution

### 5.1 Architectural decision

Build execution is scheduler-controlled and flexible.

The build location and execution location are independent decisions.

A build may execute:

- On the selected application node.
- On a dedicated build worker.
- On another compatible cluster node.

### 5.2 Placement requirements

The scheduler must evaluate:

- Operating system.
- CPU architecture.
- Runtime compatibility.
- Dependency availability.
- Memory availability.
- CPU availability.
- Disk capacity.
- Network availability.
- Build duration estimates where available.

### 5.3 Build compatibility

Build artifacts must carry compatibility metadata.

Required metadata includes:

- Target architecture.
- Operating system.
- Runtime version.
- Build configuration.
- Dependency information.
- Artifact digest.
- Source commit.
- Build environment metadata.

A build produced on Windows must not automatically be assumed compatible with Android ARM64.

### 5.4 Laptop independence

The Windows laptop may participate as an optional build worker.

It must never become a mandatory production dependency.

If the laptop is offline, deployments must still be possible on eligible always-on nodes.

---

## 6. Hybrid Artifact Storage

### 6.1 Architectural decision

Use a shared artifact registry combined with node-local caches.

### 6.2 Shared artifact registry

Responsibilities:

- Store immutable build artifacts.
- Maintain artifact metadata.
- Record checksums.
- Associate artifacts with application releases.
- Support artifact retrieval from eligible nodes.
- Support release recovery.

### 6.3 Node-local cache

Responsibilities:

- Cache frequently used artifacts.
- Reduce repeated downloads.
- Support execution during temporary connectivity issues.
- Retain locally necessary release files.
- Avoid unnecessary cross-node transfers.

### 6.4 Artifact integrity

Every artifact must have a content digest.

Artifact retrieval must verify integrity before execution.

Artifacts must be immutable after publication.

The system must distinguish:

- Source archive.
- Build output.
- Runtime configuration.
- Deployment metadata.
- Release directory.

### 6.5 Storage independence

A release must not become dependent on temporary build workspace files.

The active application must retain access to its required runtime files independently of the build worker.

---

## 7. Hybrid Runtime Architecture

### 7.1 Runtime adapters

The system will use a common runtime adapter contract.

Initial adapters:

1. Native Python.
2. Native Node.js.
3. Docker.
4. Static application hosting.
5. Custom command execution.

Additional adapters may be introduced later.

### 7.2 Adapter responsibilities

Each adapter must provide:

- Project detection.
- Configuration validation.
- Environment preparation.
- Build execution.
- Application startup.
- Application shutdown.
- Process inspection.
- Log retrieval.
- Runtime health reporting.

### 7.3 Runtime portability

Runtime availability must be discovered from node capabilities.

The platform must not assume Docker, Python versions, Node.js, or operating-system packages are available on every node.

Android/Termux nodes must support native execution without requiring Docker.

---

## 8. Deployment Job Architecture

### 8.1 Job categories

Deployment jobs:

- Source acquisition.
- Project detection.
- Build.
- Artifact publication.
- Application startup.
- Health verification.
- Release activation.

Runtime jobs:

- Start.
- Stop.
- Restart.
- Inspect.
- Retrieve logs.

Maintenance jobs:

- Health monitoring.
- Recovery.
- Artifact maintenance.
- Node capability refresh.

All categories may use shared scheduling infrastructure while retaining category-specific policies.

### 8.2 Job requirements

Every job must contain:

- Job ID.
- Job category.
- Application ID.
- Release ID where applicable.
- Source identifier.
- Target constraints.
- Resource requirements.
- Priority.
- Deadline.
- Retry policy.
- Idempotency key.
- Current state.
- Execution owner.
- Lease generation.
- Creation and completion timestamps.

### 8.3 Job delivery semantics

The system must assume at-least-once job delivery.

It must not rely on exactly-once execution across network failures and process crashes.

Operations must therefore be idempotent.

Release activation must have a single logically committed result.

### 8.4 Scheduler guarantees

- Durable job persistence.
- Lease-based job claiming.
- Lease expiry.
- Stale worker fencing.
- Bounded retries.
- Backoff.
- Cancellation.
- Per-application deployment coordination.
- Structured failure reporting.
- Restart recovery.

A completed deployment job must not own the lifetime of the application process.

Application workloads remain independently managed.

---

## 9. Deployment Lifecycle

The canonical deployment state machine is:

1. QUEUED
2. FETCHING
3. DETECTING
4. SCHEDULING
5. BUILDING
6. PUBLISHING
7. STARTING
8. HEALTH_CHECK
9. ACTIVATING
10. SUCCEEDED

Failure states:

- FAILED.
- CANCELLED.
- RECOVERY_REQUIRED.

Each stage must record its outcome and diagnostic information.

### Activation rule

A new release may become active only after:

1. Build or artifact preparation succeeds.
2. Runtime compatibility is verified.
3. Application startup succeeds.
4. Readiness health check succeeds.
5. The deployment coordinator still holds valid authority.

The active release must remain available during build and startup of a replacement release.

---

## 10. Release Management

Every application must support isolated, versioned releases.

Illustrative structure:

applications/
  application-id/
    releases/
      release-001/
      release-002/
    metadata/
    current-release

Requirements:

- Immutable release contents.
- Explicit active release reference.
- Release history.
- Health status.
- Source commit association.
- Artifact digest.
- Runtime configuration reference.
- Deployment metadata.
- Rollback capability.

Rollback must activate an existing known-good release when compatible.

It should not require rebuilding the previous release.

---

## 11. Application Routing

The router must remain independent of deployment execution.

Responsibilities:

- Stable application URLs.
- Application-to-release mapping.
- Release-to-node mapping.
- Runtime port mapping.
- Health-aware routing.
- Atomic routing updates.

Routing must change only after a release has passed health verification.

Application URLs must remain stable when an application moves between eligible nodes.

---

## 12. Project Configuration and Detection

The platform should support three configuration modes.

### Automatic mode

Detect common project structures and provide sensible defaults.

### Manifest mode

Read an application manifest describing runtime, build, execution, health, resources, and routing requirements.

### Custom mode

Allow explicitly defined build and runtime commands.

The manifest must remain optional for common application types.

Illustrative configuration:

name: example-app

runtime:
  type: python

build:
  command: pip install -r requirements.txt

run:
  command: python -u app.py

health:
  path: /health

resources:
  memory: 256MB

The final schema must be established during detailed API and data-model design.

---

## 13. Security Requirements

- Secrets must not be included in source archives or build artifacts unnecessarily.
- Credentials must not appear in deployment logs.
- GitHub webhook signatures must be validated.
- Repository and branch policies must be enforced.
- Runtime commands must be validated against execution policy.
- Node agents must authenticate with the control plane.
- Worker leases must be protected against stale ownership.
- Artifacts must be integrity-verified.
- TLS certificate verification must remain enabled.
- Administrative deployment operations must be auditable.

---

## 14. Failure and Recovery Requirements

The architecture must explicitly handle:

| Failure | Expected behavior |
|---|---|
| Controller restart | Recover durable deployment state |
| Leader failure | Elect a valid replacement when quorum exists |
| Network partition | Prevent conflicting leadership |
| Worker failure | Expire lease and recover or reschedule job |
| Build failure | Preserve active release |
| Startup failure | Do not activate new release |
| Health failure | Retain previous healthy release |
| Artifact retrieval failure | Retry within policy, report exact cause |
| Router update failure | Preserve previous routing target |
| Stale worker reconnects | Reject stale state-changing operations |
| Laptop offline | Continue using eligible always-on nodes |

---

## 15. Non-Functional Requirements

The platform must provide:

- ARM64 and x86-64 compatibility.
- Native process support.
- Optional container support.
- Production independence from the laptop.
- Durable deployment history.
- Structured logs and diagnostics.
- Health-based activation.
- Recovery after restart.
- Application isolation.
- Resource-aware placement.
- Minimal configuration for ordinary applications.
- Explicit failure reasons.

Performance targets and resource thresholds will be established after measuring the actual hardware.

---

## 16. Existing PersonalServer Component Strategy

No existing implementation is automatically approved for reuse merely because its name matches the new architecture.

Existing components must be inspected and classified.

| Existing component | D0 direction |
|---|---|
| Job scheduler | Reuse if durability, leases and fencing requirements are satisfied |
| Node registration | Reuse and extend capabilities |
| Node agent | Reuse as execution-plane foundation |
| Process manager | Reuse after ownership and lifecycle validation |
| Artifact storage | Reuse or adapt to hybrid registry/cache design |
| Application router | Retain stable routing responsibility |
| Deployment manager | Refactor into orchestration responsibilities |
| Webhook manager | Restrict to validation and deployment-job submission |
| Health checks | Standardize around readiness |
| Release management | Establish canonical release lifecycle |

No duplicate scheduler, process manager, artifact registry, or node agent should be created without a documented reason.

---

## 17. Implementation Roadmap

### D0 — Architecture Freeze
Completed by approval of this document.

### D1 — Existing Architecture Audit
- Map current components.
- Inspect actual persistence and execution paths.
- Identify reusable primitives.
- Identify architectural conflicts.
- Establish distributed control-plane feasibility.
- Determine quorum witness strategy.

### D2 — Contracts and Data Model
- Deployment request schema.
- Job schema.
- Node capability schema.
- Artifact metadata.
- Release schema.
- Runtime adapter contract.
- Control-plane coordination contract.

### D3 — Distributed Scheduling Foundation
- Durable jobs.
- Node capabilities.
- Placement.
- Leases.
- Fencing.
- Recovery.

### D4 — Deployment Execution
- Source acquisition.
- Project detection.
- Flexible build execution.
- Artifact publication.
- Runtime adapters.

### D5 — Release and Routing
- Isolated releases.
- Readiness checks.
- Atomic activation.
- Rollback.
- Stable application URLs.

### D6 — User Interfaces
- Dashboard.
- CLI.
- REST API.
- GitHub webhook integration.

### D7 — Reliability Verification
- Controller restart.
- Leader failure.
- Network partition.
- Worker failure.
- Build failure.
- Startup failure.
- Artifact failure.
- Rollback.

---

## 18. Explicitly Out of Scope

The initial redesign does not require:

- Kubernetes compatibility.
- Mandatory Docker.
- Multi-region deployment.
- Automatic horizontal scaling.
- Service mesh.
- AI-driven scheduling.
- Runtime marketplace.
- Complex distributed storage.
- Multiple independent deployment engines.

These features may be considered after the foundational deployment system is reliable.

---

## 19. Architecture Acceptance Criteria

D0 is considered complete when the architecture is approved and its unresolved implementation constraints are recorded.

The implementation will be considered successful when:

1. A user can deploy a basic Flask application through the CLI or dashboard.
2. A GitHub push can initiate the same deployment pipeline.
3. The scheduler selects a compatible cluster node.
4. Builds can run on a suitable worker independently of execution placement.
5. Artifacts can be published and retrieved through the hybrid storage model.
6. A failed deployment does not destroy the active release.
7. The application becomes accessible through a stable URL after passing health checks.
8. A controller restart does not corrupt deployment state.
9. Distributed leadership prevents conflicting deployment activation.
10. Production does not depend on the Windows laptop.

---

## 20. Architecture Freeze Rules

After approval:

- No implementation should contradict these architectural decisions without an explicit design change.
- Existing components must be audited before replacement.
- New subsystems require a documented responsibility.
- Runtime adapters must use common contracts.
- Deployment triggers must share one pipeline.
- Job scheduling must remain separate from application lifecycle management.
- Control-plane consistency and fencing are mandatory.
- Simple deployments must remain simple.
- Reliability takes precedence over feature count.

**Final architectural statement:**

PersonalServer will be a distributed, hybrid-runtime deployment platform built around durable scheduling, flexible execution, hybrid artifact storage, coordinated control-plane leadership, and health-verified release management.

The platform should make distributed infrastructure invisible to the ordinary application deployment workflow.

**D0 Architecture Freeze — Version 1.0**

---

## Critical Architecture Constraint — Laptop Independence

### Principle

PersonalServer is an Android-first, laptop-optional distributed computing platform.

The Windows laptop is an opportunistic compute resource. It is not a permanent infrastructure component.

The always-on Android cluster must independently operate all critical production services.

### Always-on responsibilities

The Android cluster must provide:

- Control-plane availability.
- Durable job state.
- Scheduling and node coordination.
- Production application execution.
- Routing and networking.
- Artifact registry availability.
- Runtime lifecycle management.
- Health monitoring and recovery.

### Optional laptop responsibilities

The laptop may provide:

- Resource-intensive builds.
- Compatible x86-64 workloads.
- GPU-assisted execution.
- Development and testing.
- Temporary compute capacity.

### Scheduling requirements

1. The scheduler must dynamically detect laptop availability.
2. Laptop availability must never be assumed.
3. Critical jobs must not depend on laptop execution.
4. Laptop workloads must use leases and recoverable execution.
5. If the laptop disconnects, the system must safely recover, retry, reschedule or wait.
6. Build placement and application execution placement must remain independent.
7. Workloads requiring unavailable laptop-only capabilities must remain queued with a meaningful reason.
8. The scheduler should use laptop resources only when they offer a genuine advantage.

### Storage requirements

- Authoritative deployment state must reside on always-on infrastructure.
- The shared artifact registry must remain available without the laptop.
- Node-local caches must support independent execution.
- The laptop must not be the only copy of an active release or deployment artifact.

### Control-plane requirements

- Laptop membership is optional.
- Laptop availability must not affect voting quorum.
- Leadership and recovery must operate independently of the laptop.
- No critical control-plane service may be hosted exclusively on the laptop.

### Acceptance test

With the Windows laptop completely powered off, PersonalServer must:

1. Maintain control-plane operation.
2. Schedule eligible jobs.
3. Deploy compatible applications.
4. Maintain existing production applications.
5. Serve applications through their stable URLs.
6. Recover from eligible node and controller failures according to the supported quorum model.

The laptop may improve performance, but its absence must not reduce the platform's fundamental production capabilities.

