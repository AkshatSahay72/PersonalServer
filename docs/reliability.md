# Reliability, Failure Recovery & High Availability

PersonalServer implements end-to-end reliability guarantees at both the **workload execution level** (job leases, retry backoffs, crash detection) and the **cluster orchestration level** (High Availability master failover).

---

## 1. Job Lifecycle State Machine

```text
       [Submit Job]
            │
            ▼
        +────────+
        | QUEUED |
        +────────+
            │
            │ Node claims via /nodes/<id>/jobs/next
            ▼
        +─────────+
        | CLAIMED | ─── (Lease active: timeout + 20s)
        +─────────+
            │
            │ Worker executes
            ▼
        +─────────+
        | RUNNING |
        +─────────+
            │
      ┌─────┴──────────────────┬────────────────────────┐
      │                        │                        │
      ▼                        ▼                        ▼
+───────────+            +───────────+            +───────────+
| SUCCEEDED |            |  FAILED   |            |  TIMEOUT  |
+───────────+            +───────────+            +───────────+
                               │                        │
                               └───────────┬────────────┘
                                           │
                           (attempt < max_attempts?)
                                           │
                                           ▼
                                    +────────────+
                                    | RECOVERING |
                                    +────────────+
                                           │
                                           │ (Reschedule / Re-queue)
                                           ▼
                                       [ CLAIM ]
```

---

## 2. Leases & Worker Abandonment Detection

1. **Lease Granting**:
   When a node claims a job, the Controller computes an atomic lease:
   $$\text{lease\_expires\_at} = \text{now} + \text{job.timeout} + 20\text{s}$$
2. **Background Sweeper**:
   A dedicated daemon thread sweeps the database periodically (every 3 seconds).
3. **Lease Expiration / Worker Disappearance**:
   If a worker holding a `CLAIMED` or `RUNNING` job crashes, loses power, or goes `OFFLINE`:
   - The job is detected as abandoned.
   - The failure event is logged to `attempts_history`.
   - If `attempt < max_attempts`, the job enters `RECOVERING`.
   - For `target: auto`, the Scheduler reschedules onto an available online node.
   - For explicit target jobs, the job is re-queued for the assigned node (no silent migration).
   - If `attempt >= max_attempts`, the job is transitioned to terminal `FAILED`.

---

## 3. High Availability (HA) & Master Failover

Beyond worker failure recovery, PersonalServer protects against **Controller / Master device failure** via a zero-external-dependency consensus and replication engine (`config/ha_manager.py`).

```text
[ Primary Master (Active) ]               [ Standby Controller (Hot) ]
     │                                                │
     ├─ Holds Leader Lease (Term T, Version V)        ├─ Continuous Liveness Monitoring
     ├─ Orchestrates cluster databases                ├─ Atomic State Replication (/ha/sync)
     │                                                │
   [ CRASH / DISCONNECT ]                             │
     X                                                ├─ 3 missed pings or expired lease
                                                      ▼
                                              [ PROMOTES TO LEADER ]
                                              - Increments Term (T+1)
                                              - Assumes orchestration
                                              - Clients & Workers auto-redirect
```

### Guarantees & Mechanics:
- **Leader Lease**: Atomic lease tracking (`term`, `leader_id`, `leader_url`, `lease_expires_at`, `state_version`) saved in `data/controller/leader_lease.json`.
- **Automatic Standby Promotion**: Standbys monitor the active master via `/ha/status`. If the master misses 3 consecutive checks or its lease expires, the standby automatically promotes itself to `LEADER`.
- **Split-Brain Immunity**: Every election increments the logical term. An isolated or revived older leader that detects a higher term immediately steps down (`step_down()`).
- **State Replication**: Standbys continuously track `state_version` and pull complete atomic database snapshots (`nodes.json`, `jobs.json`, `apps.json`) via `/ha/sync`.
- **Transparent Redirection**: Node API gateway, Local Worker daemon, and Node Agent automatically switch target controllers when a master outage occurs.

For complete HA setup instructions, see the [High Availability Failover Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/high_availability_failover.md).
