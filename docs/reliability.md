# Job Reliability, Leases & Failure Recovery (v1.0)

PersonalServer v1.0 implements end-to-end reliability guarantees to ensure workloads do not get lost when nodes crash, disconnect, or experience transient failures.

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

## 2. Leases & Abandonment Detection

1. **Lease Granting**:
   When a node claims a job, the Controller sets:
   $$\text{lease\_expires\_at} = \text{now} + \text{job.timeout} + 20\text{s}$$
2. **Background Sweeper**:
   A dedicated daemon thread sweeps the database every 3 seconds.
3. **Lease Expiration / Node Disappearance**:
   If a worker holding a `CLAIMED` or `RUNNING` job crashes or goes `OFFLINE`:
   - The job is detected as abandoned.
   - The event is logged to `attempts_history`.
   - If `attempt < max_attempts`, the job enters `RECOVERING`.
   - For `target: auto`, the Scheduler reschedules onto an available online node.
   - For explicit target jobs, the job is re-queued for the assigned node (no silent migration).
   - If `attempt >= max_attempts`, the job is marked `FAILED`.

---

## 3. Controller Restart Recovery

Upon Controller startup, `sweep_expired_leases()` is immediately executed. Any jobs left in `CLAIMED` or `RUNNING` from a previous controller run are evaluated against their assigned node's liveness and recovered seamlessly without data loss.
