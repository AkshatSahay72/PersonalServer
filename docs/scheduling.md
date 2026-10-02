# Resource-Aware Scheduling (v1.0)

The PersonalServer Scheduler (`scheduler/scheduler.py`) provides intelligent, deterministic placement of workload jobs onto cluster nodes.

---

## 1. Scheduling Modes

### 1.1 Automatic Scheduling (`target: "auto"`)
When a job specifies `target: "auto"`, the Scheduler evaluates all registered nodes and selects the most capable candidate based on a multi-factor fitness score.

### 1.2 Explicit Scheduling (`target: "<node_id>"`)
When a client specifies an explicit target node ID:
- The Controller verifies node existence and `ONLINE` status.
- The job is pinned to that specific node.
- In accordance with the system policy, explicit-target jobs are **never silently migrated** to other nodes if the target node fails.

---

## 2. Evaluation & Scoring Algorithm

The scheduler applies a two-stage filter and score pipeline:

### Stage 1: Hard Constraint Filtering
Nodes are disqualified (`REJECTED`) if:
- Node status is not `ONLINE`.
- Node is marked `REMOVED`.
- Required role does not match (e.g. requires `compute`, but node is `storage`).
- Required hardware capabilities are missing (e.g. `compute: true`, `gpu: true`).
- Available CPU cores or free memory are below declared minimums.

### Stage 2: Multi-Factor Scoring (0 to 100)
For all eligible candidate nodes:
1. **CPU Load Factor (35%)**: Higher score for nodes with lower 1-minute load average relative to core count.
2. **Memory Availability Factor (35%)**: Higher score for nodes with higher percentage and absolute free RAM.
3. **Hardware Capacity Factor (20%)**: Higher score for nodes with more CPU cores and total RAM.
4. **Role Affinity Factor (10%)**: Bonus for dedicated `compute` nodes over general hybrid nodes.

The candidate with the highest score is selected deterministically. All decisions include human-readable explainability strings recorded in the job metadata.
