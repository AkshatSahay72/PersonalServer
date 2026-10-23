# PersonalServer Distributed Job Scheduler — Baseline & Audit Report

**Date**: 2026-10-24  
**Audit Target**: PersonalServer Job Lifecycle, Scheduling, Node Capabilities, and Execution Engine

---

## 1. Executive Summary

This baseline report documents the findings from the Phase 1 audit of the existing job submission, node registry, worker execution, and scheduler infrastructure.

The cluster currently connects:
1. `server-5387a86bf36116b1` (vivo-y31) — Android phone running Termux (Linux aarch64, 8 cores, ~5.7 GB RAM, compute/storage).
2. `server-95bad5ff01424d4c8d184330d6d2e394` (node-02) — Android phone running Termux (Linux aarch64, 6 cores, ~5.5 GB RAM, compute/storage).
3. `server-f8b1485ecf458f74` (Aki) — Windows PC (win32 AMD64, 12 cores, 4 GB RAM allocated, Docker container runtime enabled).

---

## 2. Confirmed Defects & Architecture Limitations

### Defect 1: Queued Jobs Stuck Indefinitely on Offline Nodes
- **Investigation**: In the Jobs page, Docker jobs (`docker-deploy`, etc.) targeted node `server-f8b1485ecf458f74` (Aki). Aki went offline (last heartbeat on 2026-10-07).
- **Root Cause**: `controller/lease_sweeper.py` only monitors jobs with status `CLAIMED` or `RUNNING`. It completely ignores jobs in `QUEUED`.
- **Result**: Because the pinned node is offline and never contacts `GET /nodes/<node_id>/jobs/next`, queued jobs sit in the database forever with no timeout, no queue age policy, and no operator notification.

### Defect 2: Docker Workload Capability Mismatch
- **Investigation**: Docker jobs require capability `container_runtime:docker`.
- **Root Cause**: The only node in the cluster registered with Docker is `server-f8b1485ecf458f74` (Windows AMD64). The two active phone nodes (`vivo-y31`, `node-02`) run Android/Termux without a Docker daemon.
- **Result**: When Aki is offline, no online worker can execute Docker workloads. Auto-placement failed at submission time with HTTP 400 rejection instead of entering an explainable queue waiting state.

### Defect 3: Incomplete Lifecycle State Machine
- **Existing States**: `QUEUED`, `CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `RECOVERING`, `REJECTED`.
- **Missing States**: `WAITING_FOR_DEPENDENCY`, `ASSIGNED`, `RETRY_WAIT`, `TIMED_OUT`, and `NEEDS_ATTENTION`.
- **Result**: Cannot represent workflow dependencies, retry backoff windows, or jobs targeting offline pinned nodes requiring operator intervention.

### Defect 4: Premature Node Pinning at Submission Time
- **Existing Behavior**: For `target: "auto"`, `ResourceScheduler.select_node` was called at HTTP POST submission time and hardcoded `job["target_node"] = selected_node`.
- **Defect**: If the selected node disconnects before claiming the job, the job is stranded. Resource reservations were not tracked across active concurrent jobs.

### Defect 5: Lack of General-Purpose Script Execution
- Workloads were limited to fixed test commands and inline Python strings.
- Missing structured ICMP ping checks (with Windows/Linux flag adaptations), HTTP/HTTPS health checks, DNS diagnostics, and approved shell scripts with argument arrays.

### Defect 6: Missing Artifact and Workflow Engines
- No mechanism to pass outputs of one job as inputs to another.
- No DAG validation for multi-step workflows.

---

## 3. Incremental Implementation Plan

- **Phase 2**: Implement the expanded 10-state lifecycle machine with backward-compatible aliases, lease expiry recovery, queue age limits, and `NEEDS_ATTENTION` for offline pinned nodes.
- **Phase 3**: Implement the Resource-Aware Scheduler with capacity vs reservation tracking, queue-time scheduling, and explainable decisions.
- **Phase 4**: Implement general-purpose script execution (`ping-check`, `http-check`, `dns-check`, `python-script`, `shell-script`) with argument validation and security sandboxing.
- **Phase 5**: Implement multi-node artifact transfer with SHA-256 integrity verification and atomic storage promotion.
- **Phase 6**: Implement DAG workflow decomposition with acyclic validation and parallel subtask dispatch.
- **Phase 7**: Upgrade the Jobs UI with script submission templates, status badges, waiting reasons, and artifact downloads.
- **Phase 8 & 9**: Add observability metrics, queue depth tracking, and comprehensive unit/integration test suites.
