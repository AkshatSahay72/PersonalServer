# Node Enrollment Workflow (v1.0)

PersonalServer v1.0 implements an authenticated, zero-manual-editing workflow for adding new devices (Android phones, Raspberry Pis, Linux servers, old laptops) to the cluster.

---

## 1. Enrollment Lifecycle

```text
+-------------------+
|    New Device     | (Termux / Linux / Raspberry Pi)
+-------------------+
          │
          │ 1. Read node configuration (node.conf / node.json)
          │ 2. Read enrollment token (from secrets/ or command line)
          ▼
+───────────────────────────────────────────────────────────+
| POST http://<controller_ip>:8000/register                 |
| Authorization: Bearer <enrollment_token>                  |
| Payload: { node_id, name, role, platform, os, arch, ... } |
+───────────────────────────────────────────────────────────+
          │
          ▼
+-----------------------------------------------------------+
|                    Laptop Controller                      |
| - Validates enrollment token                              |
| - Generates unique cryptographic node_auth_token          |
| - Records node in controller/data/nodes.json              |
| - Sets node status to ONLINE                              |
+-----------------------------------------------------------+
          │
          │ Returns: { status: "registered", auth_token: "<node_token>" }
          ▼
+-----------------------------------------------------------+
|                        Node Agent                         |
| - Saves credentials locally to runtime/registration.json  |
| - Uses node_auth_token for all future heartbeats & claims |
+-----------------------------------------------------------+
```

---

## 2. Enrolling a Device

### Step 1: Configure Node Identity
Edit `config/node.conf` or `config/node.json` on the new node:

```ini
NODE_ID=server-node-02
NODE_NAME=raspberry-pi-02
NODE_ROLE=compute
```

### Step 2: Run Registration Command
On the new node, run:

```bash
python agent/node-agent.py register --controller http://100.120.251.42:8000 --token <ENROLLMENT_TOKEN>
```

### Step 3: Check Registration Status
```bash
python agent/node-agent.py registration-status
```

### Step 4: Send Heartbeat
```bash
python agent/node-agent.py heartbeat
```

---

## 3. Node Removal & Invalidation

To permanently remove a node from the cluster:

```bash
# Run on Controller
python controller/controller.py remove <node_id>
```

Once removed:
- The node's `auth_token` is destroyed in `controller/data/nodes.json`.
- The node status transitions to `REMOVED`.
- Any subsequent heartbeats or job claims from the removed node will be rejected with `HTTP 403 Forbidden`.
- Any pending jobs assigned to that node are automatically swept and recovered.
