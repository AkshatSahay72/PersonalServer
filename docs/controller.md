# Controller Reference & API (v1.0)

The Controller is the central orchestrator for node discovery, liveness tracking, resource scheduling, job persistence, lease management, and failure recovery.

---

## 1. Controller CLI Commands

```bash
# Start the Controller service
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60

# View cluster overview
python controller/controller.py cluster

# List registered cluster nodes
python controller/controller.py nodes [--status online|offline|unhealthy|removed|all]

# Inspect node details
python controller/controller.py node <node_id>

# Remove a node
python controller/controller.py remove <node_id>

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

## 2. HTTP API Endpoints

### Cluster & Node Management
- `GET /cluster`: Returns cluster metrics, total node counts, active online/offline tallies.
- `GET /nodes`: List sanitized node records (auth tokens stripped).
- `GET /nodes/<node_id>`: Detailed telemetry of specific node.
- `POST /register`: Authenticated node registration (requires Enrollment Token).
- `POST /heartbeat`: Node liveness and metric reporting (requires Node Auth Token).
- `POST /nodes/<node_id>/remove`: Node removal and credential invalidation.

### Workload Jobs & Recovery
- `POST /jobs`: Submit a workload job (with scheduler requirements or target node).
- `GET /jobs`: List all submitted workload jobs.
- `GET /jobs/<job_id>`: Query status, attempt history, scheduler decision, and stdout/stderr of a job.
- `GET /nodes/<node_id>/jobs/next`: Node Agent job fetch and atomic lease claim.
- `POST /jobs/<job_id>/result`: Node Agent execution result submission and retry trigger.
- `POST /jobs/<job_id>/cancel`: Explicit admin cancellation.
