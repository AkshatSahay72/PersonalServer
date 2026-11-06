# Resource-Aware Scheduling

The PersonalServer Scheduler (`scheduler/scheduler.py`) provides intelligent, deterministic placement of workload jobs and containerized applications onto cluster nodes.

---

## 1. Scheduling Modes

### 1.1 Automatic Scheduling (`target: "auto"`)
When a job specifies `target: "auto"`, the Scheduler evaluates all registered nodes and selects the most capable candidate based on hard constraint filtering followed by a multi-factor fitness score.

### 1.2 Explicit Scheduling (`target: "<node_id>"`)
When a client specifies an explicit target node ID:
- The Controller verifies node existence and `ONLINE` status.
- The job is pinned to that specific node.
- In accordance with system policy, explicit-target jobs are **never silently migrated** to other nodes if the target node fails.

---

## 2. Evaluation & Scoring Pipeline

The scheduler applies a two-stage filter and score pipeline:

```
[ All Registered Nodes ]
          │
          ▼
┌──────────────────────────────────────────────┐
│       Stage 1: Hard Constraint Filtering     │
│  - Node status must be ONLINE                │
│  - Node must not be REMOVED or DRAINING      │
│  - Required role must match (compute/storage)│
│  - Architecture compatibility (arm64/amd64)  │
│  - Required capabilities (e.g. docker: true) │
│  - Free RAM >= min_ram_mb                    │
│  - CPU cores >= min_cores                    │
└──────────────────────┬───────────────────────┘
                       │ Eligible Nodes
                       ▼
┌──────────────────────────────────────────────┐
│       Stage 2: Multi-Factor Scoring (0-100)  │
│  1. CPU Load Factor (35%)                    │
│  2. Free Memory Factor (35%)                 │
│  3. Hardware Capacity Factor (20%)           │
│  4. Role Affinity Factor (10%)               │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
        [ Highest Scoring Candidate Selected ]
```

### Stage 1: Hard Constraint Filtering
Nodes are disqualified (`REJECTED`) if:
1. **Liveness**: Node status is not `ONLINE`.
2. **Lifecycle State**: Node is marked `REMOVED` or `DRAINING`.
3. **Role**: Node role does not satisfy the requirement (e.g., job requires `compute`, but node is dedicated `storage`).
4. **Container Runtime Capability**: If a job specifies `"requires_docker": true` or deploys a container, nodes lacking the Docker engine (e.g. Termux without root) are filtered out.
5. **Architecture Normalization**: Evaluates target image architecture against node architecture using `controller/image_inspector.py`. Aliases (`x86_64` -> `amd64`, `aarch64` -> `arm64`) are normalized. An `amd64` container is never scheduled onto an `arm64` Raspberry Pi or phone.
6. **Hardware Minimums**: Available CPU cores or free RAM are below declared thresholds (`min_cores`, `min_ram_mb`).

### Stage 2: Multi-Factor Scoring (0 to 100)
For all eligible candidate nodes:
1. **CPU Load Factor (35%)**: Higher score for nodes with lower 1-minute load average relative to core count.
2. **Memory Availability Factor (35%)**: Higher score for nodes with higher percentage and absolute free RAM.
3. **Hardware Capacity Factor (20%)**: Higher score for nodes with more CPU cores and total RAM.
4. **Role Affinity Factor (10%)**: Bonus for dedicated `compute` nodes over general hybrid nodes.

The candidate with the highest score is selected deterministically. All decisions include human-readable explainability strings recorded in the job metadata.
