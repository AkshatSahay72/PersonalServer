# Controller Reference & API

The Controller is the central orchestrator for node discovery, liveness tracking, resource scheduling, job persistence, lease management, container application deployment, and high availability (HA) master failover.

---

## 1. Controller CLI Commands

```bash
# Start the Controller service
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60

# View cluster overview
python controller/controller.py cluster

# List registered cluster nodes
python controller/controller.py nodes [--status online|offline|unhealthy|removed|all]

# Inspect detailed node telemetry (battery, network, CPU temperature)
python controller/controller.py node <node_id>

# Remove a node and invalidate its credentials
python controller/controller.py remove <node_id>

# Generate a single-use onboarding pairing code (15-min TTL)
python controller/controller.py onboard-code

# Submit a workload job
python controller/controller.py submit --node auto|<node_id> --type <type> [--timeout <sec>] [--max-attempts <n>] [--params '<json>'] [--requirements '<json>']

# Test scheduler evaluation without dispatching
python controller/controller.py schedule-test [--requirements '<json>']

# List jobs
python controller/controller.py jobs [--node <node_id>] [--status <state>]

# Inspect job details
python controller/controller.py job <job_id>

# Cancel a job
python controller/controller.py cancel <job_id>
```

---

## 2. HTTP API Reference

### 2.1 Cluster & Node Management
- `GET /cluster`: Returns cluster-wide metrics, active nodes count, running job counts, and storage usage.
- `GET /nodes`: List sanitized node records (auth tokens stripped). Includes hardware specifications and real-time telemetry.
- `GET /nodes/<node_id>`: Detailed telemetry of specific node, including battery level, network signal dBm, and thermal state.
- `POST /register`: Authenticated node registration (supports `Authorization: Bearer <token>` or `X-Onboarding-Code: PS-XXXX-XXXX`).
- `POST /heartbeat`: Node liveness and telemetry reporting. Accepts battery, network, and temperature metrics.
- `POST /nodes/<node_id>/remove`: Node removal, credential invalidation, and automatic task recovery.

### 2.2 High Availability (HA) & Master Failover
- `GET /ha/status` (or `GET /api/ha/status`): Returns HA state (`leader`, `standby`), current `term`, `state_version`, leader URL, lease validity, time remaining, and registered standbys.
- `GET /ha/leader`: Fast endpoint returning current leader URL and term.
- `GET /ha/sync` (or `GET /api/ha/sync`): Authenticated cluster state snapshot export for standby synchronization (returns atomic dump of `nodes`, `jobs`, `apps`, and `onboarding_codes`).
- `POST /ha/heartbeat`: Standby check-in endpoint registering standby liveness with the active master.
- `POST /ha/renew`: Renews leader lease.
- `POST /ha/failover`: Triggers manual or test failover. Demotes current leader or elects specified node.
- `POST /ha/promote`: Standby promotion endpoint.

### 2.3 Workload Jobs & Recovery
- `POST /jobs`: Submit a workload job with optional scheduler requirements (cores, memory, role, container runtime, architecture).
- `GET /jobs`: Query submitted workload jobs with optional filtering by status and target node.
- `GET /jobs/<job_id>`: Query detailed status, attempt history, scheduler decision, duration, and captured stdout/stderr.
- `GET /nodes/<node_id>/jobs/next`: Worker atomic job fetch and lease acquisition.
- `POST /jobs/<job_id>/result`: Worker execution result submission (exit code, output, error, duration).
- `POST /jobs/<job_id>/cancel`: Explicit administrative job cancellation.

### 2.4 Application Management & Deployment
- `GET /applications` (or `GET /api/applications`): List registered containerized applications, routes, targets, and health states.
- `POST /applications`: Register a new containerized application with image reference, port, route, and environment variables.
- `POST /applications/<name>/deploy`: Trigger image pull, container execution, and dynamic route registration.
- `POST /applications/<name>/stop`: Gracefully stop a running application container.
- `DELETE /applications/<name>`: Unregister application and remove dynamic route.

### 2.5 Diagnostics & Telemetry
- `GET /diagnostics` (or `GET /api/diagnostics`): In-memory ring-buffer diagnostic event logs with correlation IDs.
- `GET /health`: Basic controller liveness check.

---

## 3. Node Telemetry Payload Schema

During heartbeats, nodes report rich device telemetry via `POST /heartbeat`:

```json
{
  "node_id": "server-5387a86bf36116b1",
  "status": "ONLINE",
  "load_avg": [0.35, 0.42, 0.38],
  "free_ram_mb": 4210,
  "telemetry": {
    "battery": {
      "level": 88,
      "charging": true,
      "status": "Charging"
    },
    "network": {
      "type": "wifi",
      "signal_dbm": -54,
      "signal_quality": "excellent",
      "link_speed_mbps": 300,
      "ip": "100.85.108.5"
    },
    "thermal": {
      "cpu_temp_c": 38.5,
      "throttling": false
    }
  }
}
```
