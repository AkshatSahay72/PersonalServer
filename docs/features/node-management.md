# Feature: Node Management & High Availability

**Subsystem:** Node Registration, Heartbeats, Consensus & Supervision  
**Implementation:** `controller/node_manager.py`, `config/ha_manager.py`, `agent/node-agent.py`  

---

## 1. Zero-Touch Node Enrollment

PersonalServer enables simple, cryptographically secure device onboarding:
1. Operator generates a single-use pairing code on the Controller: `POST /api/nodes/enrollment-token` (format `PS-XXXX-XXXX`, valid for 15 minutes).
2. The new node executes the bootstrapper:
   ```bash
   bash bootstrap.sh --pair PS-1234-5678 --controller http://100.85.108.5:8000
   ```
3. Controller verifies the token, records node capabilities (RAM, CPU architecture, platform), creates an entry in `nodes_store.json`, and returns a persistent cryptographic `auth.token`.

---

## 2. Active-Standby High Availability (HA)

PersonalServer implements a zero-external-dependency consensus mechanism:
- **Leader Leases:** The active master holds an atomic lease with a 60-second validity window.
- **Heartbeat & Renewal:** The leader continuously renews its lease every 15 seconds.
- **Standby Monitoring & Term Election:** Standby nodes (e.g. Realme 5 Pro) monitor leader leases. If the leader fails to renew within the timeout, standbys conduct a randomized-jitter election and promote a new leader.
- **Database Replication:** Active leaders stream incremental database state updates (`/ha/sync`) to standbys, ensuring zero data loss during failovers.
