# PersonalServer Distributed Job Scheduler & Multi-Node Execution Guide

## Overview

PersonalServer has been upgraded from single-node execution into a distributed computing platform designed for heterogeneous clusters (Android Termux nodes and Windows/Linux controllers).

This system provides:
1. **Reliable Job Lifecycle & Leases**: Cooperative cancellation, lease heartbeats, exponential backoff with jitter, and crash recovery.
2. **Resource-Aware Scheduler**: Active memory reservation, least-loaded concurrency penalties, and explainable placement.
3. **Safe General-Purpose Script Execution**: `ping-check`, `http-check`, `dns-check`, `python-script` (with CLI argument arrays), and `shell-script`.
4. **Multi-Node Artifact Management**: Atomic file promotion, streaming SHA-256 integrity verification, and directory traversal sandboxing.
5. **Multi-Step DAG Workflows**: Topological dependency graphs, cycle validation via Kahn's algorithm, independent parallel step dispatch, and automatic failure propagation.
6. **Offline Pinned Node Policy**: Distinguishes between automatic cluster scheduling and pinned nodes. Pinned workloads for offline nodes enter `NEEDS_ATTENTION` with explicit waiting reasons rather than being silently dropped or redispatched elsewhere.
7. **Enhanced Jobs UI**: Live workload submission templates, status badges, waiting reasons, scheduling rationale, and artifact inspection.

---

## 1. Job Lifecycle State Machine

The scheduler enforces strict transitions across 10 canonical job states, maintaining 100% backward compatibility with legacy aliases (`CLAIMED`, `RECOVERING`, `TIMEOUT`).

```
                +----------------------------+
                |    WAITING_FOR_DEPENDENCY  |
                +--------------+-------------+
                               | (upstream parents SUCCEEDED)
                               v
+-------------------------> QUEUED <-----------------------------------------+
|                              |                                             |
|                              v                                             |
|                           ASSIGNED / CLAIMED                               |
|                              |                                             |
|                              v                                             |
|                           RUNNING                                          |
|                         /    |    \                                        |
|                        /     |     \                                       |
|                       v      |      v                                      |
|                  SUCCEEDED   |    RETRY_WAIT / RECOVERING                  |
|                              |      |  (exponential backoff)               |
|                              |      +--------------------------------------+
|                              v
|                           FAILED / TIMED_OUT / CANCELLED
|
| (Pinned target offline >300s)
+-------------------------> NEEDS_ATTENTION
                               | (target node comes back ONLINE)
                               +---------------------------------------------> QUEUED
```

### Canonical States:
- **`QUEUED`**: Job is ready for dispatch to an assigned or eligible auto worker.
- **`WAITING_FOR_DEPENDENCY`**: Job step is waiting for parent workflow steps to succeed.
- **`ASSIGNED`** (alias `CLAIMED`): Worker holds a bounded, heartbeated lease.
- **`RUNNING`**: Worker is actively executing the workload process.
- **`RETRY_WAIT`** (alias `RECOVERING`): Non-terminal failure; waiting for backoff delay.
- **`SUCCEEDED`**: Workload exited with code 0 and reported results.
- **`FAILED`**: Workload exited with non-zero code or exhausted maximum attempts.
- **`TIMED_OUT`** (alias `TIMEOUT`): Execution exceeded configured job timeout.
- **`CANCELLED`**: Execution was cooperatively terminated by operator request.
- **`NEEDS_ATTENTION`**: Job is pinned to a node that has been offline for >300s.

---

## 2. Resource-Aware Scheduler Policy

The scheduler evaluates candidates using a multi-stage scoring algorithm:

1. **Eligibility Filter**:
   - Excludes nodes in `OFFLINE`, `DRAINING`, `DEBOARDING`, `REMOVED`, or `UNHEALTHY` states.
   - Enforces role requirements (`compute`, `storage`, `hybrid`).
   - Verifies required capabilities (e.g. `container_runtime:docker`, `python:3`).
   - Verifies architecture compatibility (`x86_64`, `aarch64`, `armv7l`).

2. **Resource Reservations & Concurrency**:
   - In-flight active jobs (`ASSIGNED`, `CLAIMED`, `RUNNING`) reserve their declared `min_memory_mb`.
   - The scheduler deducts active reservations from available RAM:
     $$\text{Effective Memory} = \text{Reported Available RAM} - \sum \text{Reserved In-Flight RAM}$$
   - Nodes with active jobs receive a concurrency load penalty (-5 points per active job) to achieve least-loaded load balancing across phones.
   - Nodes matching a job's `preferred_node` receive a +15 bonus.

3. **Pending Auto Queue**:
   - If a job requests automatic placement (`target: "auto"`) and no eligible worker currently qualifies, the job is accepted into `QUEUED` with an explicit `waiting_reason` explaining what requirement was unmet. When a qualifying worker connects, it automatically claims the pending job.

---

## 3. General-Purpose Script Workloads

Supported workloads run via safe argument arrays without shell interpolation:

### 1. `ping-check`
Performs network latency probes with platform-specific flags (`ping -n` on Windows, `ping -c` on Linux/Termux):
```json
{
  "type": "ping-check",
  "target": "auto",
  "parameters": {
    "host": "8.8.8.8",
    "count": 4,
    "ping_timeout": 3
  }
}
```

### 2. `http-check`
Probes HTTP/HTTPS endpoints, capturing status code and round-trip duration in milliseconds:
```json
{
  "type": "http-check",
  "target": "auto",
  "parameters": {
    "url": "https://httpbin.org/status/200",
    "expected_status": 200,
    "http_timeout": 5,
    "method": "GET"
  }
}
```

### 3. `dns-check`
Resolves domain names and measures DNS latency:
```json
{
  "type": "dns-check",
  "target": "auto",
  "parameters": {
    "domain": "google.com"
  }
}
```

### 4. `python-script`
Executes user-approved Python scripts with structured CLI argument arrays:
```json
{
  "type": "python-script",
  "target": "auto",
  "parameters": {
    "script": "import sys, platform\nprint(f'Node: {platform.node()}, Python {sys.version}')",
    "args": ["cluster", "diag"]
  }
}
```

### 5. `shell-script`
Executes structured shell scripts on compatible hosts (PowerShell on Windows, Bash on Linux/Termux):
```json
{
  "type": "shell-script",
  "target": "auto",
  "parameters": {
    "script": "echo 'PersonalServer Cluster Node OK'"
  }
}
```

---

## 4. Multi-Node Artifacts & Data Transfer

Artifacts are securely exchanged between nodes using SHA-256 verification and atomic promotion:

- **Storage Location**: `storage/artifacts/<artifact_id>/`
- **Path Traversal Sandboxing**: Filenames containing `..`, `/`, or `\` are rejected with HTTP 400.
- **Atomic Promotion**: Content is written to `<filename>.tmp` and renamed atomically upon checksum validation.
- **API Endpoints**:
  - `POST /api/artifacts/upload`: Uploads base64 encoded payload with optional expected SHA-256.
  - `GET /api/artifacts`: Lists all cluster artifacts and metadata.
  - `GET /api/artifacts/<artifact_id>`: Streams artifact download.
  - `GET /api/artifacts/<artifact_id>/metadata`: Returns metadata and on-disk integrity status.
  - `POST /api/artifacts/<artifact_id>/verify`: Re-computes SHA-256 against recorded checksum.

---

## 5. Multi-Step DAG Workflows

Workflows decompose complex multi-step pipelines into directed acyclic graphs:

- **Graph Validation**: Kahn's algorithm validates that the graph has no circular dependencies before acceptance.
- **Parallel Dispatch**: All root steps (with 0 dependencies) transition immediately to `QUEUED`.
- **Dependency Fulfillment**: When parent steps succeed, child steps transition automatically from `WAITING_FOR_DEPENDENCY` to `QUEUED`.
- **Failure Propagation**: If any parent step fails or times out, downstream dependent steps transition to `FAILED` with an explicit reason citing the upstream failed step.

### Workflow Submission Example (`POST /api/workflows`):
```json
{
  "name": "Network Health & Diagnostic Pipeline",
  "description": "Multi-node parallel probe and aggregation",
  "steps": {
    "probe_ping": {
      "type": "ping-check",
      "target": "auto",
      "parameters": {"host": "8.8.8.8", "count": 3}
    },
    "probe_http": {
      "type": "http-check",
      "target": "auto",
      "parameters": {"url": "https://httpbin.org/status/200", "expected_status": 200}
    },
    "aggregate": {
      "type": "python-script",
      "target": "auto",
      "depends_on": ["probe_ping", "probe_http"],
      "parameters": {
        "script": "print('All diagnostic probes succeeded. Aggregating cluster report.')"
      }
    }
  }
}
```

---

## 6. Offline Pinned Node Policy

- When an explicit node target is specified with `pinned: true`, the scheduler honors the operator's placement.
- If the pinned node is offline or deboarded:
  - The job is queued with a visible reason: `"Pinned node '<node_id>' is currently OFFLINE; awaiting reconnection."`
  - If the node remains offline for >300 seconds, the background lease sweeper transitions the job to `NEEDS_ATTENTION`.
  - When the pinned node comes back online and sends heartbeats, the job automatically returns to `QUEUED` and executes.
  - Pinned jobs are **never** silently migrated to another node without operator consent.

---

## 7. Cluster Observability & Metrics

Operators can monitor real-time queue health via `GET /api/jobs/metrics`:
```json
{
  "queue_depth": 3,
  "total_jobs": 42,
  "status_counts": {
    "QUEUED": 2,
    "RUNNING": 1,
    "SUCCEEDED": 35,
    "FAILED": 2,
    "NEEDS_ATTENTION": 2
  },
  "active_leases": 1,
  "avg_duration_ms": 245.8,
  "total_workflows": 4,
  "total_artifacts": 12,
  "heartbeat_timeout_sec": 60
}
```
