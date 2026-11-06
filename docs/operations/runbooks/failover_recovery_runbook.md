# PersonalServer High Availability (HA) & Master Failover

## Overview

PersonalServer features a self-contained, zero-external-dependency High Availability (HA) and Master Failover subsystem. It eliminates the single-point-of-failure risk of running a centralized master/controller node by allowing one or more secondary devices (such as an Android phone running Termux, a Raspberry Pi, or another computer on the local network) to serve as **Hot Standby Controllers**.

If the primary Master server experiences hardware failure, power loss, or network disconnection, an active Standby automatically detects the outage, triggers an election, promotes itself to active Master (Leader), assumes cluster orchestration, and transparently redirects all connected workers and web clients.

```
       +---------------------------------------------+
       |             Active Master Node              |
       |  - Holds Leader Lease (Term T, Version V)   |
       |  - Orchestrates jobs, nodes, apps           |
       |  - Exposes /ha/status, /ha/sync             |
       +---------------------------------------------+
                              |
                     Lease Heartbeats & Sync
                              v
       +---------------------------------------------+
       |             Hot Standby Node                |
       |  - Monitors Master (/ha/status)             |
       |  - Replicates state (nodes, jobs, apps)     |
       |  - Auto-promotes to Master on 3 missed pings|
       +---------------------------------------------+
                              ^
            Auto-Redirect upon Controller Outage
                              |
       +---------------------------------------------+
       |       Workers (Local, Remote, Termux)       |
       |   Candidate URLs: [http://pc:8000, phone]   |
       +---------------------------------------------+
```

---

## Core Components

### 1. Leader Lease & Consensus (`config/ha_manager.py`)
- **`LeaderLease`**: Atomic lease object persisting lease expiration (`lease_expires_at`), term counter (`term`), leader identifier (`leader_id`), and state version (`state_version`) to disk (`data/controller/leader_lease.json`).
- **`HAManager`**: State machine managing node roles (`leader`, `standby`, `candidate`, `disabled`).
- **Heartbeat & Renewal**: The active leader renews its lease every `lease_duration_sec / 3` seconds. If the leader crashes, the lease expires naturally.
- **Split-Brain Immunity**: Every election increments the logical `term`. If a disconnected or recovered old master sees a peer claiming a higher term, it immediately yields leadership and demotes itself to `standby` (`step_down()`).

### 2. State Versioning & Snapshot Replication
- Whenever `save_nodes_db()`, `save_jobs_db()`, or `save_apps_db()` is called on the Master, the state version is automatically incremented.
- Standby nodes periodically poll `/ha/status` and compare their local `state_version` with the Master's.
- When the Master's state version is higher, the Standby pulls an atomic state snapshot from `/ha/sync`, updating local databases without stopping the service.

### 3. Worker & Client Auto-Redirection
Workers and clients are configured with a list of candidate controller URLs (e.g., `http://192.168.1.100:8000,http://192.168.1.101:8000`).
- **Node API Gateway (`services/node-api/app.py`)**: If the primary controller returns a connection error, the gateway probes candidate URLs via `find_active_leader()`, updates its active target, and retries the client request seamlessly.
- **Local Worker (`agent/local_worker.py`)**: Automatically switches active controller target during heartbeats, job polling, or result reporting if connection to the primary is lost.
- **Node Agent (`agent/node-agent.py`)**: Automatic controller candidate discovery and failover fallback during background heartbeats.

---

## Configuration & Deployment

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PERSONALSERVER_HA_ROLE` | `auto` | Node role: `leader` (force master), `standby` (hot standby), `auto` (auto-discover leader; become leader if none exists), `disabled` (standalone mode). |
| `PERSONALSERVER_CONTROLLER_URLS` | `http://127.0.0.1:8000` | Comma-separated list of candidate controller addresses across the local network. |
| `PERSONALSERVER_HA_LEASE_SEC` | `10.0` | Duration (seconds) of a leader lease before it is considered expired. |
| `PERSONALSERVER_HA_CHECK_SEC` | `2.5` | Interval (seconds) between standby health checks on the active leader. |
| `PERSONALSERVER_HA_MAX_MISSED` | `3` | Number of consecutive failed checks before standby initiates election. |

---

## Setting up a Multi-Device HA Cluster

### Step 1: Start Primary Master (e.g. Desktop PC)
Run the controller with default or explicit leader configuration:
```bash
python controller/controller.py --port 8000
```
Or with environment variables:
```bash
export PERSONALSERVER_HA_ROLE=leader
export PERSONALSERVER_CONTROLLER_URLS="http://192.168.1.100:8000,http://192.168.1.101:8000"
python controller/controller.py --port 8000
```

### Step 2: Start Hot Standby on Secondary Node (e.g. Android Termux or Raspberry Pi)
On the secondary device:
```bash
export PERSONALSERVER_HA_ROLE=standby
export PERSONALSERVER_CONTROLLER_URLS="http://192.168.1.100:8000,http://192.168.1.101:8000"
export PERSONALSERVER_ENROLLMENT_TOKEN="your-shared-cluster-token"
python controller/controller.py --port 8000
```
The standby will connect to the desktop master, download the latest nodes/jobs/apps state snapshot, and enter passive monitoring mode.

### Step 3: Configure Worker Nodes
Point worker nodes to all candidate master URLs:
```bash
export PERSONALSERVER_CONTROLLER_URLS="http://192.168.1.100:8000,http://192.168.1.101:8000"
python agent/local_worker.py
```

---

## Testing & Verifying Failover

### 1. Command-Line Status Check
Run the HA inspection tool to view active leader, term, and connected standbys:
```bash
./scripts/ha-status.sh http://127.0.0.1:8000
```

Sample output:
```
=====================================================
 PersonalServer High Availability (HA) Status
 Target: http://127.0.0.1:8000
=====================================================
Role:             leader
Node ID:          desktop-master-01
Active Leader:    desktop-master-01 (http://127.0.0.1:8000)
Term:             1
State Version:    14
Lease Valid:      true
Time Remaining:   8.7s
Connected Standbys: 1
  - node-android-standby (http://192.168.1.101:8000) | State Version: 14 | In Sync: true
=====================================================
```

### 2. Web UI Monitoring & Simulation
1. Navigate to the PersonalServer Web Dashboard (`http://localhost:8080`).
2. The top bar displays the **`👑 Master (Leader)`** badge with real-time term and heartbeat indicators.
3. In the **Nodes** view, the **HA Cluster Status Banner** displays active master URL, state version, and standby counts.
4. Click **"Simulate Failover"** to trigger a controlled leadership transition to test standby promotion without killing hardware.

---

## Verification Test Suite

All HA mechanisms are covered by automated integration tests in [`tests/test_ha_failover.py`](file:///d:/Coding/Major%20Project/PersonalServer/tests/test_ha_failover.py):
- `test_ha_manager_initialization_and_lease_renewal`: Validates lease creation, renewal, and in-memory/on-disk sync.
- `test_standby_state_synchronization`: Validates delta and snapshot export/import between leader and standby.
- `test_automatic_master_failover_when_master_goes_down`: Simulates abrupt master crash and validates standby automatic promotion within timeout.
- `test_split_brain_prevention_old_master_steps_down`: Verifies that an isolated leader automatically steps down when a higher-term leader is discovered.
- `test_worker_transparent_failover_redirection`: Verifies that worker nodes redirect requests to the new master without dropping jobs or returning errors.
- `test_find_active_leader_discovery`: Validates multi-candidate network probing.
- `test_leader_lease_lifecycle_and_persistence`: Tests lease expiry and file persistence safety.
