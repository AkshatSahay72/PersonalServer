# 09 — High Availability Guide: Leader Leases & Failover

> **Reading Order**: **Step 9 of 12** in the PersonalServer Guide Series (Completes the Operator Journey).  
> **Previous Guide**: [08 — Monitoring & Recovery](08_monitoring_and_recovery.md)  
> **Next Guide**: [10 — Development](10_development.md) *(Begins the Engineer Journey)*  
> **Audience**: Operators configuring high availability, standby nodes, and failover consensus.  
> **Prerequisites**: Two or more connected nodes running PersonalServer.

---

## 1. High Availability (HA) in PersonalServer

Running server software on smartphones or single-board computers presents unique reliability challenges: thermal throttling, Android OS background task management, and unpredictable WiFi disconnections.

PersonalServer includes a built-in **High Availability Consensus Engine** (`config/ha_manager.py`) designed specifically for two-node and three-node edge clusters. It guarantees:
- **No External Quorum Software Required**: No ZooKeeper, etcd, or Consul needed; operates entirely using Python standard library sockets and file locks.
- **Lease-Based Leadership**: The active leader holds a renewable lease with an authoritative fencing token.
- **Split-Brain Immunity**: A partitioned standby node cannot usurp leadership unless the lease has definitively expired and the remote leader is confirmed unreachable.
- **Automatic State Synchronization**: The leader continuously streams cluster state snapshots (node inventory, job queue, active leases) to standby nodes.

---

## 2. Cluster Roles: Leader vs Standby

In an HA cluster:
- **Leader (Active Controller)**:
  - Serves HTTP REST API requests (`:8000`).
  - Schedules jobs and deploys applications.
  - Renews its leadership lease every 5 seconds (lease duration: 30s).
- **Standby Controller (Backup Leader)**:
  - Continuously polls the leader's `/ha/status` endpoint.
  - Synchronizes cluster stores (`apps_store`, `nodes_store`, `jobs_store`).
  - Evaluates failover criteria if 3 consecutive health checks fail.

---

## 3. Configuring an HA Cluster

To configure High Availability, define candidate peer controller URLs in your node configuration.

### Example Topology:
- **Node A (Vivo Y31)**: `http://100.85.108.5:8000`
- **Node B (Realme 5 Pro)**: `http://100.73.52.72:8000`

### Step 1: Configure Node A (Primary Leader)
On Node A, create or edit `config/node.conf`:
```ini
ROLE=leader
NODE_URL=http://100.85.108.5:8000
PERSONALSERVER_CONTROLLER_URLS=http://100.85.108.5:8000,http://100.73.52.72:8000
AUTO_PROMOTION=true
```

### Step 2: Configure Node B (Standby Controller)
On Node B, create or edit `config/node.conf`:
```ini
ROLE=standby
NODE_URL=http://100.73.52.72:8000
PERSONALSERVER_CONTROLLER_URLS=http://100.85.108.5:8000,http://100.73.52.72:8000
AUTO_PROMOTION=true
```

### Step 3: Start Services on Both Nodes
Start the controller daemon on both devices:
```bash
python controller/controller.py start
```

---

## 4. Verifying HA Cluster Health

From any computer on your Tailscale mesh, query the HA status endpoint:

```bash
curl -s http://100.85.108.5:8000/ha/status
```

**Expected JSON Response:**
```json
{
  "ha_enabled": true,
  "role": "leader",
  "leader_id": "server-vivo-y31",
  "term": 1,
  "lease_valid": true,
  "lease_remaining_sec": 28.4,
  "standbys_count": 1,
  "standbys": [
    {
      "node_id": "server-realme-5pro",
      "url": "http://100.73.52.72:8000",
      "last_sync": "2026-11-06T10:30:00Z"
    }
  ]
}
```

---

## 5. Controlled Leadership Handover

If you need to reboot or upgrade the active leader phone without causing cluster downtime, perform a **Controlled Handover**:

```bash
python controller/cli.py ha handover --target-node "server-realme-5pro"
```

**What Happens During Handover:**
1. Node A ceases scheduling new jobs and flushes in-flight state to disk.
2. Node A demotes itself to `STANDBY` and releases its lease lock.
3. Node B detects the released lease, increments the term counter ($T = T + 1$), and promotes itself to `LEADER`.
4. Ingress proxy routers automatically update their upstream target to Node B.

---

## 6. Automatic Failover & Recovery

If Node A abruptly powers off or crashes:
1. Node B misses 3 consecutive heartbeats (15 seconds total).
2. Node B verifies that Node A's lease has expired ($t_{now} > t_{expire}$).
3. Node B executes candidate ranking heuristics (evaluating CPU load, battery, and storage).
4. Node B promotes itself to active leader with a higher term counter.
5. All worker nodes receive HTTP 503 redirects and automatically reconnect their heartbeats to Node B.

---

## 7. Next Step

Congratulations! You have completed the **Operator Journey**. If you wish to inspect the codebase, run test suites, and write custom code:

👉 **Continue to [10 — Development Guide](10_development.md)**
