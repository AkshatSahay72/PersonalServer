# Node Agent Reference & Guide (v1.0)

The Node Agent (`agent/node-agent.py`) is the resident supervisor running on each server node (e.g. Vivo Y31 Android/Termux, Linux).

---

## 1. Responsibilities

1. **Service Supervision**: Starts, stops, restarts, and monitors local services (`Node API`, `Cloudflare Tunnel`).
2. **Telemetry Collection**: Collects hardware cores (`nproc`), CPU load averages (`uptime`), RAM usage (`free -h`), and disk metrics (`df -h`).
3. **Cluster Enrollment**: Registers with the Controller, receives a cryptographic node token, and persists it to `runtime/registration.json`.
4. **Heartbeat Reporting**: Sends periodic health pings to `POST /heartbeat`.
5. **Workload Polling & Execution**: Pulls assigned jobs from `GET /nodes/<node_id>/jobs/next`, executes them via `agent/job_executor.py`, and posts results to `POST /jobs/<job_id>/result`.

---

## 2. CLI Usage

```bash
# Display node identity
python agent/node-agent.py info [--json]

# Display health and system metrics
python agent/node-agent.py health [--json]

# Display unified node and service status
python agent/node-agent.py status [--json]

# Service orchestration
python agent/node-agent.py start
python agent/node-agent.py stop
python agent/node-agent.py restart

# Enrollment and heartbeats
python agent/node-agent.py register [--controller <url>] [--token <token>]
python agent/node-agent.py registration-status [--json]
python agent/node-agent.py heartbeat [--controller <url>] [--json]

# Pull and execute assigned jobs from Controller
python agent/node-agent.py work [--controller <url>]

# Local direct workload execution test
python agent/node-agent.py exec-local --type <type> [--timeout <sec>] [--params '<json>']
```
