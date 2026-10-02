# Workload Jobs & Safe Execution (v1.0)

PersonalServer executes workloads through a sandboxed, allowlisted execution model governed by `agent/job_executor.py`.

---

## 1. Workload Allowlist

To ensure security across heterogeneous personal devices, only predefined workload types are permitted for execution:

| Workload Type | Description | Permissions & Isolation |
|---|---|---|
| `system-info` | Queries `nproc`, `uptime`, `free`, `df` | Read-only system commands |
| `health-check` | Runs `scripts/health.sh` | Local diagnostic script |
| `node-status` | Runs `scripts/status.sh` | Local process status inspection |
| `echo` | Echoes input parameters | Pure in-memory echo |
| `python-script` | Executes inline Python scripts | Safe Python child process |
| `failing-test` | Simulates a non-zero exit failure | Controlled failure testing |
| `timeout-test` | Simulates a hanging command | Controlled timeout testing |

Any job submitted with a type not present in the allowlist is rejected immediately with state `REJECTED` and `HTTP 400 Bad Request`.

---

## 2. Job Parameters and Isolation

- Workloads execute with standard unprivileged user permissions (e.g. `u0_a244` in Termux).
- Standard output and standard error are captured with buffer limits (max 64KB) to avoid memory exhaustion.
- Execution duration is measured in milliseconds.
- Timeouts are strictly enforced via process killing and timeout signal propagation.
