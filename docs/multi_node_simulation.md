# PersonalServer Multi-Node Worker Simulator & Integration Suite

## Overview

The PersonalServer Multi-Node Simulator allows developers to test multi-node cluster behavior, dynamic scheduling, lease timeouts, worker failures, network partitions, and retries on a single machine without requiring Android devices, virtual machines, or additional physical computers.

The simulator interacts exclusively at the **network protocol boundary** (real HTTP REST APIs). It uses the authentic controller, authentication system, database persistence, and resource scheduler without patching or bypassing internal controller code.

---

## Simulated Worker Architecture

Simulated workers are implemented in [`agent/simulated_worker.py`](file:///d:/Coding/Major%20Project/PersonalServer/agent/simulated_worker.py).

### Identity & Safety Guarantees
- **Logical Node ID**: Always prefixed with `sim-node-` (e.g. `sim-node-alpha`) to prevent collision with physical nodes.
- **Platform & OS**: Explicitly reported as `platform="simulated"` and `os="SimulatedOS"`.
- **Zero Local Footprint**: Simulated workers do not execute arbitrary OS shell commands, do not touch production file storage, and do not spawn host Docker containers unless configured to simulate specific protocol outcomes.

### Configurable Failure Modes (`WorkerBehavior`)

| Behavior Mode | Description |
|---|---|
| `NORMAL` | Full lifecycle compliance: authenticates, reports heartbeats, claims leases, and returns exit code 0. |
| `FAIL_EXECUTION` | Simulates a non-zero exit code (`exit_code: 1`, `status: FAILED`) to trigger controller retry policies. |
| `CRASH_ON_JOB` | Claims the lease on `/jobs/next`, then immediately terminates without submitting a result to test lease sweep and worker loss recovery. |
| `DISCONNECT_BEFORE_ACCEPT` | Drops connection when polled for work, simulating connection reset or network crash during assignment. |
| `TIMEOUT_EXECUTION` | Simulates a hung task exceeding the allowed timeout duration (`status: TIMEOUT`, `exit_code: -1`). |
| `UNSUPPORTED_CAPABILITY` | Rejects incompatible workloads if scheduled improperly (`status: REJECTED`). |
| `DUPLICATE_COMPLETION` | Submits the job result twice to test idempotent controller deduplication. |
| `DELAYED_HEARTBEAT` / `MISSING_HEARTBEAT` | Stops or delays liveness heartbeats to test offline state transitions. |

---

## Test Scenarios Covered

The integration suite in [`tests/test_multi_node_simulation.py`](file:///d:/Coding/Major%20Project/PersonalServer/tests/test_multi_node_simulation.py) covers:

1. **Multi-Node Registration**: Heterogeneous cluster topology with varying CPU, RAM, storage, architecture (`amd64`, `arm64`), and roles (`compute`, `storage`).
2. **Strict Authentication**: Rejection of forged enrollment tokens, auth tokens on heartbeats, job polling, and result posting.
3. **Heartbeat Expiry**: Deterministic transition of stale workers to `OFFLINE`.
4. **Capability-Aware Scheduling**: ResourceScheduler selecting only nodes with matching roles, architectures, and required capabilities (e.g., Docker container runtime).
5. **Unavailable Worker Protection**: Verification that `OFFLINE` or `DRAINING` workers cannot receive new workloads either through auto-scheduling or explicit target submission.
6. **Failure & Retry Lifecycle**: Step-by-step verification of attempt increments up to `max_attempts` before terminal failure.
7. **Controller Restart & Persistence**: Cold reboot of the controller HTTP process against persisted disk state, verifying worker authentication and job leases survive.
8. **Crash Recovery & Silent Success Prevention**: Proving that worker process death while holding a lease never marks a job as successful.
9. **Idempotent Duplicate Reports**: Replaying stale completion packets and verifying the terminal state and attempt history are preserved without duplication.
10. **Network Partition & Healing**: Simulating packet loss / unreachable host, verifying transition to `OFFLINE`, followed by partition recovery, heartbeat restoration, and subsequent workload execution.

---

## How to Run the Test Suite

### Run Multi-Node Simulation Tests Only
```powershell
pytest tests/test_multi_node_simulation.py -v
```

### Run Multi-Node Simulation + LocalWorker Tests
```powershell
pytest tests/test_multi_node_simulation.py tests/test_local_worker.py -v
```

### Run Full Regression Suite (59 Tests)
```powershell
pytest tests/test_multi_node_simulation.py tests/test_local_worker.py tests/test_platform_config.py tests/test_portable_config.py tests/test_phase14b_telemetry.py tests/test_phase14c_deboarding.py -v
```
