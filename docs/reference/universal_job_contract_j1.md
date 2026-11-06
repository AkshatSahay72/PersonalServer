# PersonalServer Universal Job Contract (UJC) v1.0
## Milestone J1 Architectural Specification (Amended)

**Status:** Final Specification (J1 Frozen)  
**Author:** Antigravity Engineering  
**Version:** ujc.v1.0  
**Phase:** PersonalServer Phase 2 — Universal Job Execution  

---

## 1. Architecture Overview

### 1.1 Mission & Vision
PersonalServer successfully completed its application deployment foundation (Phases R1–R9), establishing a resilient cluster across Android Termux nodes (Vivo Y31 controller, Realme 5 Pro compute worker) with capability-aware scheduling, isolated artifact distribution, and zero-downtime reverse proxying.

Phase 2 evolves PersonalServer from an application deployment harness into a **universal, distributed workload execution platform**. The cluster must execute heterogeneous workloads—ranging from one-shot batch calculations, recurring scheduled jobs, and asynchronous build tasks to long-running services and multi-stage workflow DAGs—without coupling the core scheduler and node agents to runtime-specific quirks.

### 1.2 Core Architectural Principles
1. **Runtime-Agnostic Core Contract:** The scheduler, queue, and node agent operate on a uniform specification. The platform never alters its core scheduling logic for Python, Node.js, Shell, or Container workloads.
2. **Strict Separation of Specification and Execution State:** The immutable job definition (`spec`) is decoupled from its mutable runtime progress (`status`). The `spec` is cryptographically hashed to establish unique identity (`spec_hash`), enforce immutability, and guarantee idempotency.
3. **Artifact-Driven Input/Output Boundary:** Workloads consume declared input artifacts and produce declared output artifacts registered in the cluster's content-addressable `artifacts_db` via independent SHA-256 verification.
4. **Secret-Free Job Declarations:** Job specifications declare references to secrets (`secret_ref`), never plaintexts. Secret resolution and injection occur ephemeral in-memory at execution time on the assigned worker node.
5. **Mobile-First Resource Awareness:** Mobile Android devices have dynamic thermal, memory, and battery envelopes. The contract supports scheduling hints (e.g., battery constraints, thermal throttling avoidance, CPU limits).
6. **Authoritative Monotonic Fencing:** Execution leases carry strictly monotonic fencing tokens generated exclusively by the controller. All worker status updates, artifact uploads, and routing activations must validate the current fencing token to unconditionally reject zombie or stale worker attempts.
7. **Explicit Workload Modality:** The platform enforces strict semantic divergence between one-shot terminating workloads (`batch`, `step`) and supervised daemons (`service`).

---

## 2. Universal Job Schema (`ujc.v1.0`)

A Universal Job is represented as a structured JSON record containing an immutable root specification (`spec`), an authoritative cryptographic fingerprint (`spec_hash`), and a mutable execution state (`status`).

```json
{
  "job_id": "job-ujc-a1b2c3d4e5f6",
  "schema_version": "ujc.v1.0",
  "spec_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "created_at": "2026-11-05T16:30:00.000000Z",
  "spec": {
    "metadata": {
      "name": "data-aggregation-task",
      "description": "Daily metrics rollup and analytical report compilation",
      "owner": "admin",
      "labels": {
        "tier": "analytics",
        "team": "core"
      },
      "annotations": {},
      "idempotency_key": "analytics-rollup:2026-11-05"
    },
    "workload": {
      "classification": "batch",
      "runtime": "native/python",
      "entrypoint": "aggregate.py",
      "arguments": ["--format", "json", "--window", "24h"],
      "working_directory": ".",
      "runtime_config": {
        "python_version": "3.13",
        "install_dependencies": true
      }
    },
    "inputs": [
      {
        "artifact_id": "art-raw-metrics-20261105",
        "name": "raw_metrics.csv.zip",
        "mount_path": "data/metrics.csv",
        "sha256": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
        "required": true
      }
    ],
    "outputs": [
      {
        "name": "summary_report",
        "path_pattern": "output/daily_summary.json",
        "destination": "storage/artifacts",
        "required": true,
        "max_size_bytes": 10485760,
        "format": "application/json"
      }
    ],
    "requirements": {
      "role": "compute",
      "capabilities": ["python3"],
      "verified_capabilities": [],
      "architecture": "arm64",
      "resources": {
        "min_cpu_cores": 1,
        "min_memory_mb": 256,
        "min_storage_mb": 512,
        "max_cpu_percent": 80,
        "max_memory_mb": 1024,
        "max_storage_mb": 2048
      },
      "hints": {
        "priority": "NORMAL",
        "preemption_policy": "PREEMPTIBLE",
        "node_affinity": ["role:compute"],
        "node_anti_affinity": [],
        "allow_on_battery": true,
        "min_battery_percent": 25
      }
    },
    "environment": {
      "variables": {
        "ENVIRONMENT": "production",
        "LOG_LEVEL": "INFO"
      },
      "secret_refs": {
        "DATABASE_CREDENTIALS": "secrets://databases/analytics/creds"
      },
      "config_refs": {}
    },
    "isolation": {
      "type": "virtualenv",
      "network_policy": "loopback_only",
      "umask": "0022"
    },
    "policy": {
      "timeout_sec": 300,
      "max_attempts": 3,
      "backoff_base_sec": 5.0,
      "backoff_max_sec": 60.0,
      "deadline": "2026-11-05T18:00:00Z",
      "auto_cleanup": true
    },
    "dependencies": [
      {
        "job_id": "job-ujc-raw-extract-001",
        "condition": "ON_SUCCESS"
      }
    ]
  },
  "status": {
    "phase": "QUEUED",
    "assigned_node": null,
    "lease": {
      "lease_token": null,
      "acquired_at": null,
      "expires_at": null,
      "fencing_token": 0
    },
    "attempt": 1,
    "timing": {
      "submitted_at": "2026-11-05T16:30:00.000000Z",
      "scheduled_at": null,
      "started_at": null,
      "finished_at": null,
      "duration_ms": null
    },
    "result": {
      "exit_code": null,
      "stdout": "",
      "stderr": "",
      "truncated": false
    },
    "produced_outputs": [],
    "metrics": {
      "peak_memory_mb": null,
      "cpu_time_ms": null,
      "wall_time_ms": null
    },
    "error": null,
    "history": [
      {
        "from_phase": "NONE",
        "to_phase": "QUEUED",
        "timestamp": "2026-11-05T16:30:00.000000Z",
        "actor": "controller",
        "reason": "Job accepted and queued"
      }
    ]
  }
}
```

### 2.1 Specification Field Dictionary (`spec`)

| Field Path | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| `spec_hash` | `string` | Yes | Immutable SHA-256 hex digest of canonicalized `spec` payload (RFC 8785). |
| `metadata.name` | `string` | Yes | Human-readable name of the job. |
| `metadata.owner` | `string` | Yes | Submitting identity/service (e.g. `cli`, `api`, `cron`). |
| `metadata.idempotency_key` | `string` | No | Client-supplied key preventing duplicate execution. |
| `workload.classification`| `enum` | Yes | `batch` (one-shot exit), `service` (supervised daemon), `step` (DAG step). |
| `workload.runtime` | `string` | Yes | Target adapter (`native/python`, `native/nodejs`, `native/static`, `container/docker`). |
| `workload.entrypoint` | `string` | Yes | Script path, executable, or command. |
| `workload.arguments` | `list[str]` | No | CLI arguments passed to entrypoint. |
| `workload.runtime_config` | `dict` | No | Runtime-specific non-standard configuration bucket. |
| `inputs[]` | `list[dict]` | No | Input artifact references required before execution. |
| `outputs[]` | `list[dict]` | No | Output artifacts expected to be captured after completion. |
| `outputs[].max_size_bytes`| `int` | No | Maximum allowed output payload size in bytes (default: 52428800 = 50 MB). |
| `requirements` | `dict` | Yes | Placement criteria matching `ResourceScheduler.select_node()`. |
| `requirements.resources` | `dict` | No | Soft scheduling floors (`min_*`) and hard worker limits (`max_*`). |
| `requirements.hints` | `dict` | No | Priority, preemption, battery threshold, node affinity. |
| `environment.variables` | `dict[str,str]`| No | Explicit non-sensitive environment variables. |
| `environment.secret_refs`| `dict[str,str]`| No | Vault/secret references mapped to env variable names. |
| `isolation.type` | `enum` | Yes | `process`, `virtualenv`, `npm_prefix`, `container`. |
| `isolation.network_policy`| `enum` | No | `unrestricted`, `loopback_only`, `isolated` (no network). |
| `policy.timeout_sec` | `int` | Yes | Hard execution timeout for batch jobs; startup readiness timeout for services. |
| `policy.max_attempts` | `int` | Yes | Maximum retry attempts on retryable failures. |
| `policy.deadline` | `string` | No | Absolute ISO 8601 deadline for queue dwell time (`QUEUED`, `WAITING_FOR_DEPENDENCY`). |
| `dependencies[]` | `list[dict]` | No | Upstream jobs that must conclude before this job queues. |

---

## 3. Specification Immutability and Fingerprinting

### 3.1 Canonical Serialization Standard (RFC 8785)
To eliminate serialization ambiguity across platforms, architectures, and programming languages, the job specification digest MUST be generated strictly using the **RFC 8785 JSON Canonicalization Scheme (JCS)**:
1. Object keys are sorted lexicographically by UTF-16 code units.
2. Whitespace outside strings is completely stripped (`separators=(',', ':')`).
3. Numbers and floats are normalized without leading zeros, exponential notations where unnecessary, or negative zero.
4. Unicode strings are normalized to UTF-8 without non-mandatory escaping.

The authoritative specification hash is computed as:
$$\text{spec\_hash} = \text{SHA256}(\text{JCS}(\text{job}.\text{spec}))$$

The resulting 64-character lowercase hexadecimal string is persisted at the root level of the record (`job.spec_hash`).

### 3.2 Immutability and Retry Invariance Guarantee
- **Zero In-Place Spec Mutation:** Once accepted and assigned a `spec_hash`, the `spec` sub-document is strictly read-only. No controller routine, lease sweeper, scheduler, or worker agent may mutate any key within `spec`.
- **Retry Invariance:** When a job experiences transient failure and transitions to `RETRY_WAIT` and back to `QUEUED`:
  - `status.attempt` increments monotonically ($1 \to 2 \to \dots$).
  - `status.history` appends transition audit entries.
  - `spec` and `spec_hash` remain 100% byte-for-byte identical to the original submission.
- **Workload Changes Require New Job Identity:**
  - Any change to `spec` (including command-line arguments, environment references, resource requests, or entrypoint) generates a completely different `spec_hash`.
  - The API and CLI reject any attempt to update an existing job's `spec` in-place with `409 Conflict` (`JOB_SPEC_IMMUTABLE`).
  - Clients wishing to alter a workload must submit a new job or specify an explicit new revision, creating a distinct `job_id`.

---

## 4. Authoritative Fencing and Stale-Worker Protection

### 4.1 Token Scope and Authoritative Ownership
To guarantee safety in distributed, mobile environments with intermittent connectivity, execution leases use strictly monotonic fencing tokens:
- **Authority:** The **Controller** (specifically the `ResourceScheduler` and `LeaseSweeper`) is the sole entity authorized to generate and increment fencing tokens.
- **Monotonicity:** Each job maintains an integer `fencing_token` initialized to `0`. Whenever the controller assigns a job to a node (`QUEUED -> ASSIGNED`) or reclaims an abandoned lease for retry (`RUNNING -> RETRY_WAIT -> QUEUED -> ASSIGNED`), the controller atomically increments:
  $$\text{fencing\_token}_{\text{new}} = \text{fencing\_token}_{\text{current}} + 1$$
- **Lease Payload:** The lease dispatched to the Node Agent binds `(job_id, lease_token, fencing_token, expires_at)`.

### 4.2 Status Update Protection
Every execution progress and terminal status update sent from a Node Agent back to the controller must transmit the assigned `fencing_token`.
The controller validates updates via an atomic condition:
```sql
UPDATE jobs_db 
SET status = :new_status, updated_at = :now
WHERE job_id = :job_id AND fencing_token = :reported_fencing_token;
```
If `reported_fencing_token < current_fencing_token`, the update is rejected with `412 Precondition Failed` (`STALE_LEASE_TOKEN`). Stale workers cannot overwrite modern job states.

### 4.3 Stale Output Artifact Protection
Before a worker uploads output artifacts to the controller's storage:
1. The upload request header must supply `X-PersonalServer-Fencing-Token: <fencing_token>`.
2. The controller's artifact upload handler verifies that the token strictly matches the current authoritative `status.lease.fencing_token` in `jobs_db`.
3. If a stale attempt (e.g. from an un-killed zombie process on a reconnected node) attempts to publish output files, the controller rejects the payload with `412 Precondition Failed` and discards all incoming bytes.

### 4.4 Stale Service Activation Protection
For workloads with `classification: "service"`:
1. When a service becomes healthy on a worker node, the agent submits an activation readiness report carrying its assigned `fencing_token`.
2. The Controller and `ApplicationRouter` verify that `fencing_token == current_authoritative_token`.
3. If a superseding deployment or newer lease has been scheduled, the activation request is rejected, preventing stale or zombie daemon instances from hijacking live traffic on the reverse proxy.

---

## 5. Workload Classification and Lifecycle State Machine

PersonalServer strictly separates terminating tasks from supervised daemons via `spec.workload.classification`.

### 5.1 Batch vs. Service Operational Semantics

| Semantic Dimension | Batch Workload (`classification: "batch"` or `"step"`) | Long-Running Service (`classification: "service"`) |
| :--- | :--- | :--- |
| **Primary Goal** | Run to finite completion and produce artifacts. | Run indefinitely to serve inbound network traffic. |
| **Exit Code 0** | **Normal Success:** Transitions job to `SUCCEEDED`. Sandbox cleaned. | **Unexpected Termination:** Process exited. Triggers crash supervisor / restart. |
| **Exit Code != 0** | **Failure:** Evaluated for retry or transitioned to `FAILED`. | **Crash:** Process restarted up to backoff limit by `SafeProcessLifecycleManager`. |
| **Timeout (`timeout_sec`)** | **Total Execution Budget:** Hard wall-clock limit for total task run. | **Readiness Budget:** Maximum time allowed for initial startup and `/health` passing. |
| **Router Interaction** | None. Pure compute workload. | Registers with `ApplicationRouter` upon readiness; unregisters on termination. |
| **Outputs** | Extracts and registers declared output files into `artifacts_db`. | None (logs captured; persistent state managed via dedicated volumes). |

### 5.2 Canonical State Graph

```
                                [SUBMISSION]
                                     |
                                     v
                                [SUBMITTED]
                                     |
                            (Validation Engine)
                                     |
                       +-------------+-------------+
                       |                           |
                  (Invalid)                     (Valid)
                       |                           |
                       v                           v
                   [REJECTED]                 [VALIDATED]
                                                   |
                                         (Dependency Engine)
                                                   |
                                    +--------------+--------------+
                                    |                             |
                          (Pending Upstream)              (Dependencies Met)
                                    |                             |
                                    v                             v
                        [WAITING_FOR_DEPENDENCY]               [QUEUED]
                                    |                             |
                                    +--------------+--------------+
                                                   |
                                          (ResourceScheduler)
                                                   |
                                                   v
                                               [ASSIGNED]
                                                   |
                                             (Node Agent)
                                                   |
                                                   v
                                               [RUNNING]
                                                   |
          +----------------------------------------+----------------------------------------+
          |                                                                                 |
   [BATCH / STEP]                                                                    [SERVICE]
          |                                                                                 |
  +-------+-------+-------+                                                        +--------+--------+
  |               |       |                                                        |                 |
(Exit 0 &       (Exit   (Timeout >                                            (Health Pass)    (Health Fail /
Outputs OK)     != 0)   timeout_sec)                                               |           Timeout > timeout_sec)
  |               |       |                                                        v                 |
  v               v       v                                                    [HEALTHY /            v
[SUCCEEDED]    [FAILED] [TIMED_OUT]                                             SERVING]          [FAILED]
                  |       |                                                        |
                  +---+---+                                                  (Crash / Term)
                      |                                                            |
              (Policy Evaluator)                                                   v
                      |                                                       [SUPERVISED
              +-------+-------+                                                RESTART]
              |               |
       (Attempts < Max) (Max Exhausted)
              |               |
              v               v
        [RETRY_WAIT]      [FAILED]
              |
              v
           [QUEUED]
```

### 5.3 Cancellation, Timeout, Retry, and Expiration Rules

#### 1. Cancellation Semantics
- **Batch Jobs:** If cancelled via API/CLI (`POST /api/v1/jobs/{id}/cancel`):
  - Controller marks job `CANCELLED` and bumps `fencing_token`.
  - Node Agent sends `SIGTERM` to process group, waiting up to 10s grace period before `SIGKILL`.
  - Sandbox attempt directories are purged.
- **Service Workloads:** If cancelled:
  - `ApplicationRouter` immediately removes upstream endpoints from proxy routes to stop new traffic.
  - Node Agent executes graceful shutdown sequence (`SIGTERM` -> drain -> `SIGKILL`).
  - Active release pointer is cleared or rolled back.

#### 2. Timeout Semantics
- **Batch Jobs:** `policy.timeout_sec` represents maximum allowable wall-clock duration from `started_at` to process exit. If exceeded, agent terminates process group and reports `TIMED_OUT`.
- **Service Workloads:** `policy.timeout_sec` applies strictly to initial startup readiness. The service must bind its port and pass health checks within `timeout_sec`. Once healthy, the service runs indefinitely without timer-based eviction.

#### 3. Queue Expiration (`policy.deadline`)
- `policy.deadline` enforces maximum queue dwell time.
- If a job remains in `WAITING_FOR_DEPENDENCY` or `QUEUED` past its deadline timestamp, the Lease Sweeper transitions it to `EXPIRED`.
- **Safety Invariant:** A running, healthy service (`classification: "service"`) is **never** expired by `policy.deadline`. Once in `RUNNING`/`HEALTHY`, production services are protected from queue expiration sweeps.

---

## 6. Output Integrity and Artifact Registration Protocol

### 6.1 Path Validation and Resource Limits
To protect node and controller storage against unbounded growth or directory traversal attacks:
1. **Path Traversal Guard:** `outputs[].path_pattern` must be relative to the sandbox working directory. Paths containing leading slashes, Windows drive letters (`C:`), or parent navigation components (`..`) are rejected during schema validation.
2. **Per-Output Size Limit:** Each declared output specification supports `max_size_bytes` (default: 50 MB; platform ceiling: 500 MB).
3. **Aggregate Sandbox Output Ceiling:** If total captured output artifacts for any single execution attempt exceed `spec.requirements.resources.max_storage_mb`, the agent aborts packaging and marks the attempt failed with `OUTPUT_SIZE_LIMIT_EXCEEDED`.

### 6.2 Required vs. Optional Output Handling
When a batch workload exits with code `0`, the Node Agent matches captured files against declared `outputs[]`:
- **`required: true` (Default):** The `path_pattern` must match at least one file. If zero files match, the job MUST immediately fail with error code `MISSING_REQUIRED_OUTPUT`, regardless of process exit code 0.
- **`required: false`:** If zero files match, the agent records an empty match in `status.produced_outputs` and proceeds without error.

### 6.3 Attempt Isolation & Directory Namespacing
To prevent file collision and contamination between retries, worker scratch spaces MUST be strictly namespaced by job ID and attempt number:
$$\text{Sandbox Root} = \langle\text{runtime\_dir}\rangle/\text{jobs}/\langle\text{job\_id}\rangle/\text{attempts}/\langle\text{attempt}\rangle/$$
- Execution working directory: `.../attempts/<attempt>/work/`
- Output capture staging directory: `.../attempts/<attempt>/outputs/`
When an attempt fails or is retried, its sandbox directory is isolated. Subsequent attempts start with an empty, dedicated directory.

### 6.4 Two-Sided Checksum Verification Protocol
1. **Worker-Side Computation:**
   - The worker locates matching output files in `.../attempts/<attempt>/outputs/`.
   - The worker packages the files into an archive (or individual file payload) and computes the SHA-256 digest: `local_sha256 = SHA256(payload)`.
   - The worker dispatches the upload to the controller:
     ```http
     POST /api/v1/jobs/{job_id}/outputs/{output_name}
     X-PersonalServer-Fencing-Token: 2
     X-PersonalServer-Attempt: 2
     X-PersonalServer-SHA256: <local_sha256>
     ```
2. **Controller-Side Independent Verification:**
   - The controller verifies that the fencing token matches the active lease.
   - The controller streams the incoming bytes directly to temporary staging storage while simultaneously computing an independent SHA-256 digest (`computed_sha256`).
   - If `computed_sha256 != header_sha256`, the controller aborts the upload, discards the temporary file, and responds with `400 Bad Request` (`CHECKSUM_MISMATCH`).

### 6.5 Recoverable Two-Phase Atomic Commit Protocol
A distributed system cannot treat filesystem operations and relational database updates as a single native ACID transaction. PersonalServer uses a **Recoverable Two-Phase Commit Protocol** to register outputs and complete jobs:

```
[Worker Agent]                    [Controller Storage]               [Controller DB]
      |                                    |                                |
      |--- 1. Upload Staged Output ------->| (Stream to tmp file)           |
      |    (with fencing & attempt)        |                                |
      |                                    |--- 2. Verify SHA-256 --------->|
      |                                    |                                |
      |                                    |=== 3. Stage in Content Store ==|
      |                                    |    (Rename to final CAS path)  |
      |                                    |                                |
      |<-- 4. Upload Confirmed (200 OK) ---|                                |
      |                                                                     |
      |--- 5. Submit Execution Report (Success) --------------------------->|
                                                                            |--- 6. BEGIN DB TX
                                                                            |    - Register Artifact Records
                                                                            |    - Update Job to SUCCEEDED
                                                                            |    - Release Leases
                                                                            |--- 7. COMMIT DB TX
```

- **Phase 1 (Staged CAS Promotion):** The artifact is saved to immutable content-addressable storage using its SHA-256 digest (`storage/artifacts/<sha256>`). If the server crashes here, orphaned CAS files are harmlessly garbage-collected by the storage sweeper.
- **Phase 2 (Atomic Status & Catalog Commit):** The controller executes an atomic database transaction:
  1. Inserts records into `artifacts_db` referencing the verified CAS files.
  2. Updates `status.phase = 'SUCCEEDED'`.
  3. Updates `status.produced_outputs` with registered artifact metadata.
- **Recovery Invariant:** If Phase 2 fails or the connection drops before commit, the job remains in its prior state. The agent or sweeper can safely replay the transition because artifact CAS promotion is completely idempotent.

---

## 7. Concrete Workload Examples

### 7.1 Example 1: Native Python Batch Processing Job
*Scenario: Compiling aggregate statistics from an uploaded data zip, saving output JSON.*

```json
{
  "job_id": "job-ujc-py-data-8812",
  "schema_version": "ujc.v1.0",
  "spec_hash": "a4f89d38c11e74b28169e55d216f45bb6293998b9e8471b0593bcf91438a2091",
  "created_at": "2026-11-05T16:35:00.000000Z",
  "spec": {
    "metadata": {
      "name": "sensor-telemetry-cruncher",
      "owner": "iot-monitor",
      "idempotency_key": "sensor-rollup:20261105:h16"
    },
    "workload": {
      "classification": "batch",
      "runtime": "native/python",
      "entrypoint": "crunch_telemetry.py",
      "arguments": ["--input", "sensor_data.csv", "--output", "report.json"],
      "runtime_config": {
        "requirements_file": "requirements.txt"
      }
    },
    "inputs": [
      {
        "artifact_id": "art-telemetry-source-v1",
        "name": "telemetry_pkg.zip",
        "sha256": "9f83c60517b4d0715b3e9da0b4357271ce0d625e29f264e475b61fb3001ea74b",
        "required": true
      }
    ],
    "outputs": [
      {
        "name": "telemetry_summary",
        "path_pattern": "report.json",
        "destination": "storage/artifacts",
        "required": true,
        "max_size_bytes": 1048576,
        "format": "application/json"
      }
    ],
    "requirements": {
      "capabilities": ["python3"],
      "architecture": "any",
      "resources": {
        "min_memory_mb": 128,
        "min_cpu_cores": 1
      },
      "hints": {
        "priority": "HIGH",
        "allow_on_battery": true
      }
    },
    "environment": {
      "variables": {
        "SENSOR_CLUSTER": "lab-west"
      },
      "secret_refs": {
        "TELEMETRY_API_KEY": "secrets://tokens/sensor_ingest"
      }
    },
    "isolation": {
      "type": "virtualenv",
      "network_policy": "unrestricted"
    },
    "policy": {
      "timeout_sec": 120,
      "max_attempts": 2
    }
  },
  "status": {
    "phase": "QUEUED",
    "lease": {
      "fencing_token": 0
    },
    "attempt": 1
  }
}
```

### 7.2 Example 2: Native Node.js Asset Bundling Job
*Scenario: Compiling TypeScript/CSS source bundles via esbuild into production assets.*

```json
{
  "job_id": "job-ujc-node-build-9041",
  "schema_version": "ujc.v1.0",
  "spec_hash": "c80211a774bd01f78e4d29f8f41656b69b61d2d3a24b172a12b77546a36f6104",
  "created_at": "2026-11-05T16:36:00.000000Z",
  "spec": {
    "metadata": {
      "name": "client-assets-bundler",
      "owner": "ci-pipeline",
      "idempotency_key": "build-client:git-sha-77a1c"
    },
    "workload": {
      "classification": "batch",
      "runtime": "native/nodejs",
      "entrypoint": "build.js",
      "arguments": ["--minify", "--outdir=dist"],
      "runtime_config": {
        "package_manager": "npm"
      }
    },
    "inputs": [
      {
        "artifact_id": "art-src-client-77a1c",
        "name": "client_source.zip",
        "sha256": "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
        "required": true
      }
    ],
    "outputs": [
      {
        "name": "compiled_assets",
        "path_pattern": "dist/**/*",
        "destination": "storage/artifacts",
        "required": true,
        "max_size_bytes": 26214400,
        "format": "application/zip"
      }
    ],
    "requirements": {
      "capabilities": ["nodejs"],
      "architecture": "arm64",
      "resources": {
        "min_memory_mb": 512,
        "min_cpu_cores": 2
      },
      "hints": {
        "priority": "NORMAL",
        "allow_on_battery": false,
        "min_battery_percent": 50
      }
    },
    "environment": {
      "variables": {
        "NODE_ENV": "production"
      }
    },
    "isolation": {
      "type": "npm_prefix",
      "network_policy": "unrestricted"
    },
    "policy": {
      "timeout_sec": 180,
      "max_attempts": 2
    }
  },
  "status": {
    "phase": "QUEUED",
    "lease": {
      "fencing_token": 0
    },
    "attempt": 1
  }
}
```

### 7.3 Example 3: Static Data Processing & Markdown Rendering Job
*Scenario: Generating HTML reports from structured markdown fixtures with zero external network access.*

```json
{
  "job_id": "job-ujc-static-gen-4120",
  "schema_version": "ujc.v1.0",
  "spec_hash": "63d762955f2a96a9d70034a713997e07662886f45a6c3f3bf78b4aa036d0fa72",
  "created_at": "2026-11-05T16:37:00.000000Z",
  "spec": {
    "metadata": {
      "name": "markdown-documentation-renderer",
      "owner": "docs-engine",
      "idempotency_key": "docs-build:v2.1"
    },
    "workload": {
      "classification": "batch",
      "runtime": "native/static",
      "entrypoint": "render_docs.py",
      "arguments": ["--src", "docs/", "--dest", "public/"]
    },
    "inputs": [
      {
        "artifact_id": "art-docs-markdown-raw",
        "name": "docs_raw.zip",
        "sha256": "5d41402abc4b2a76b9719d911017c592fdcfbf8ac5df6365b9eb4718427b9c9e",
        "required": true
      }
    ],
    "outputs": [
      {
        "name": "static_site",
        "path_pattern": "public/**/*",
        "destination": "storage/artifacts",
        "required": true,
        "max_size_bytes": 5242880,
        "format": "application/zip"
      }
    ],
    "requirements": {
      "capabilities": ["compute"],
      "resources": {
        "min_memory_mb": 64,
        "min_cpu_cores": 1
      },
      "hints": {
        "priority": "LOW"
      }
    },
    "isolation": {
      "type": "process",
      "network_policy": "isolated"
    },
    "policy": {
      "timeout_sec": 60,
      "max_attempts": 1
    }
  },
  "status": {
    "phase": "QUEUED",
    "lease": {
      "fencing_token": 0
    },
    "attempt": 1
  }
}
```

---

## 8. State Transition Matrix and Component Ownership

| From State | To State | Trigger / Condition | Authoritative Component | Fencing Guard Rule |
| :--- | :--- | :--- | :--- | :--- |
| `NONE` | `SUBMITTED` | Ingress receives raw submission | Controller HTTP Ingress | None |
| `SUBMITTED` | `VALIDATED` | Schema, RFC 8785 canonicalization & security pass | Job Validation Engine | Generates immutable `spec_hash` |
| `SUBMITTED` | `REJECTED` | Malformed schema, invalid secrets, or missing capabilities | Job Validation Engine | Immutable rejection |
| `VALIDATED` | `WAITING_FOR_DEP`| Upstream dependency job is still unresolved | Dependency Resolver | Checked against upstream status |
| `VALIDATED` | `QUEUED` | Dependencies resolved or no dependencies | Dependency Resolver | `fencing_token = 0` |
| `WAITING_FOR_DEP`| `QUEUED` | Upstream dependencies succeed | Dependency Resolver | Transitions to runnable pool |
| `WAITING_FOR_DEP`| `FAILED` | Upstream dependency failed permanently | Dependency Resolver | Cascade failure |
| `QUEUED` | `ASSIGNED` | `ResourceScheduler.select_node()` picks candidate worker | ResourceScheduler | Bumps `fencing_token = fencing_token + 1` |
| `ASSIGNED` | `RUNNING` | Worker agent acquires lease and spawns process | Node Agent Worker | Requires `lease.fencing_token == current` |
| `ASSIGNED` | `QUEUED` | Worker rejection or worker offline before lease acquisition | Controller Lease Sweeper | Bumps `fencing_token` on reclaim |
| `RUNNING` (Batch) | `SUCCEEDED` | Subprocess exit 0 and required outputs captured & verified | Node Agent & Controller | Commits via Recoverable Two-Phase Commit |
| `RUNNING` (Batch) | `FAILED` | Subprocess exit != 0 or `MISSING_REQUIRED_OUTPUT` | Node Agent Worker | Evaluates retry policy |
| `RUNNING` (Batch) | `TIMED_OUT` | Execution wall clock exceeds `policy.timeout_sec` | Node Agent / Watchdog | Enforces hard termination |
| `RUNNING` (Service)| `HEALTHY` | Port bound and `/health` probe succeeds | Node Agent Worker | Verifies `fencing_token` for proxy routing |
| `RUNNING` | `CANCELLED` | Operator abort requested via API or CLI | Controller / Node Agent | Terminates process; removes proxy routing |
| `FAILED`/`TIMED_OUT`| `RETRY_WAIT`| `attempt < max_attempts` and failure is retryable | Controller Lease Sweeper | Preserves immutable `spec` and `spec_hash` |
| `RETRY_WAIT` | `QUEUED` | Exponential backoff delay elapses | Controller Lease Sweeper | `attempt = attempt + 1`; bumps `fencing_token` |
| Queue States Only | `EXPIRED` | Dwell time exceeds `policy.deadline` | Controller Lease Sweeper | **Never evicts active healthy services** |

---

## 9. Backward Compatibility Mapping for R1–R9 Deployments

Legacy deployment jobs from R1–R9 map cleanly into the Universal Job Contract without code divergence:

```
+------------------------------------+      +------------------------------------------+
| Existing Deployment Job (Legacy)   |      | Universal Job Contract (ujc.v1.0)        |
+------------------------------------+      +------------------------------------------+
| type: "app-deploy"                 | ---> | spec.workload.classification: "service"  |
| parameters.project_type: "python"  | ---> | spec.workload.runtime: "native/python"   |
| parameters.app_id                  | ---> | spec.metadata.labels.app_id              |
| parameters.deployment_id           | ---> | spec.metadata.labels.deployment_id       |
| parameters.port                    | ---> | spec.workload.runtime_config.port        |
| parameters.health_check            | ---> | spec.workload.runtime_config.health      |
| input_artifacts                    | ---> | spec.inputs                              |
| requirements                       | ---> | spec.requirements                        |
| timeout                            | ---> | spec.policy.timeout_sec (readiness limit)|
| fencing_token                      | ---> | status.lease.fencing_token               |
+------------------------------------+      +------------------------------------------+
```

---

## 10. Open Architectural Decisions for J2–J4

The following items are deferred to upcoming milestones:

1. **J2 (Artifact Transfer & Storage Engine):**
   - Protocol for chunked / resumable uploads of large multi-gigabyte outputs on mobile worker connections.
2. **J3 (Dynamic Secret Resolution):**
   - Controller-held vault token resolution vs. worker-local secure keyrings.
3. **J4 (Workflow DAG Execution Engine):**
   - Dynamic workflow evaluation, parameter passing between DAG steps, and partial DAG retries.

---

## 11. Milestone J1 Final Verification Checklist

- [x] **Spec Immutability:** Formally specified RFC 8785 JSON Canonicalization Scheme (JCS) and persistent `spec_hash`.
- [x] **Retry Invariance:** Enforced that retries never alter `spec`; workload changes reject in-place edits and require new job identity.
- [x] **Authoritative Fencing:** Defined controller-owned strictly monotonic `fencing_token` governing status updates, output uploads, and service activations.
- [x] **Divergent Lifecycles:** Explicitly segregated terminating batch workloads from supervised long-running daemons.
- [x] **Output Integrity:** Defined path traversal guards, output size caps, required vs. optional resolution, attempt directory isolation, and two-sided SHA-256 verification.
- [x] **Recoverable Atomic Commit:** Modeled output artifact catalog registration and status finalization as a recoverable two-phase commit protocol.
- [x] **No Unrelated Code Modifications:** Confined strictly to `docs/universal_job_contract_j1.md`.
