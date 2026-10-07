# Node Enrollment & Onboarding Workflow

PersonalServer features an automated, zero-manual-configuration onboarding workflow designed to add new devices (Android smartphones running Termux, Raspberry Pis, Linux servers, old laptops, or WSL instances) to the cluster securely in seconds.

---

## 1. Onboarding Architecture

```text
[ Controller (Laptop / Central Host) ]
  │
  ├─ 1. Operator generates short-lived one-time pairing code:
  │     $ python controller/controller.py onboard-code
  │     => Code: "PS-WYR9-3LYT" (15m TTL, stored as SHA-256 hash)
  │
  ▼
[ New Device (Termux / Linux / Raspberry Pi) ]
  │
  ├─ 2. Run single onboarding command:
  │     $ python agent/node-agent.py onboard \
  │         --controller http://<controller_ip>:8000 \
  │         --code PS-WYR9-3LYT
  │
  ├─ 3. Automatic Hardware Discovery:
  │     - Detects CPU cores, total RAM, and storage capacity
  │     - Detects platform (Termux/Linux), OS, and architecture
  │     - Generates secure random ID (server-<16 hex chars>)
  │     - Synthesizes config/node.json and config/controller.json
  │
  ├─ 4. Authenticated Registration:
  │     - Sends POST /register with header: X-Onboarding-Code: PS-WYR9-3LYT
  │
  ▼
[ Controller ]
  │
  ├─ 5. Validation & Single-Use Invalidation:
  │     - Validates code hash, expiration, and unused status
  │     - Atomically consumes code (cannot be replayed or reused)
  │     - Generates unique cryptographic node_auth_token
  │     - Adds node to cluster inventory with status ONLINE
  │
  ▼
[ New Device ]
  │
  ├─ 6. Service Startup & Heartbeat:
  │     - Saves node_auth_token to runtime/registration.json
  │     - Launches background node services via scripts/start.sh
  │     - Issues initial heartbeat to controller
  │     - Node is now active in the cluster!
```

---

## 2. Step-by-Step Device Onboarding

### Step 1: Generate Onboarding Code (on Controller)

Run on the controller machine:

```bash
python controller/controller.py onboard-code
```

Output:
```text
==========================================
Onboarding code: PS-XXXX-XXXX
Expires in:      15 minutes
==========================================
```

### Step 2: Run Onboard Command (on New Device)

On the new device (within Termux or a Linux terminal):

```bash
cd ~/PersonalServer
python agent/node-agent.py onboard \
  --controller http://<CONTROLLER_TAILSCALE_IP>:8000 \
  --code PS-XXXX-XXXX
```

Output:
```text
==========================================
Onboarding successful
Node ID:    server-fc6125d69a24dee0
Controller: http://100.120.251.42:8000
Status:     registered
==========================================
```

### Step 3: Verify Registration

On the new device:

```bash
python agent/node-agent.py registration-status
```

On the controller:

```bash
python controller/controller.py nodes
```

---

## 3. Security & Anti-Replay Model

1. **Short-Lived One-Time Codes**:
   * Format: `PS-XXXX-XXXX` using cryptographically secure character selection.
   * Default TTL: 15 minutes.
   * Stored strictly as SHA-256 hashes in `controller/data/onboarding_codes.json` (never in plaintext).
2. **Atomic Consumption**:
   * Thread-safe validation via mutex locks prevents concurrent race conditions.
   * Marked as `used: true` immediately upon successful registration.
   * Attempting to reuse an onboarded code immediately returns `HTTP 401 Unauthorized`.
3. **Per-Node Authentication**:
   * Once onboarded, each node uses its own 40-character `node_auth_token` stored locally in `runtime/registration.json` with restricted permissions.
   * Plaintext credentials and tokens are never printed to stdout or written to persistent application logs.
4. **Legacy Static Enrollment Token (Backward Compatibility)**:
   * Automated tests and legacy automation can still register using `Authorization: Bearer <static_enrollment_token>`.

---

## 4. Node Removal & Invalidation

To remove any node from the cluster:

```bash
# Run on Controller
python controller/controller.py remove <node_id>
```

Once removed:
* The node's `auth_token` is invalidated in `controller/data/nodes.json`.
* The node transitions to `REMOVED` status.
* Any future heartbeats or job requests from that node are rejected with `HTTP 403 Forbidden`.
* Any pending jobs assigned to that node are automatically swept and rescheduled.
