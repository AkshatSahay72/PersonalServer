# Getting Started with PersonalServer

This guide provides step-by-step instructions for running PersonalServer, starting the cluster controller, and onboarding server nodes (Android smartphones running Termux, Raspberry Pis, Linux PCs, or virtual machines).

---

## 1. Prerequisites

1. **Host Requirements**:
   * Python 3.9+ installed on all machines.
   * Tailscale VPN installed and authenticated on all cluster nodes (creates private WireGuard mesh network).
   * Termux on Android devices (with `python`, `openssh`, `curl`, and `git` packages).
   * Optional: Cloudflare Tunnel (`cloudflared`) on gateway node for public HTTPS web console access.

2. **Network Topology**:
   * All inter-node communication (Controller API `:8000`, Node API `:8080`, Node SSH `:8022`) operates securely over Tailscale private IPs.

---

## 2. Setting Up the Controller

The Controller manages node inventory, workload scheduling, lease-based failure recovery, and onboarding credentials.

```bash
# Clone or navigate to the repository
cd PersonalServer

# Start the cluster controller service
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60
```

The controller will initialize:
* Node database: `controller/data/nodes.json`
* Workload database: `controller/data/jobs.json`
* Onboarding database: `controller/data/onboarding_codes.json`
* Background lease-sweeper thread for automatic recovery.

---

## 3. Onboarding a New Node (Android / Termux / Linux)

Adding a new device takes a single command:

### 3.1 Generate a Pairing Code on the Controller

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

### 3.2 Run Onboarding on the New Node

On the target node (e.g., inside Termux or Linux):

```bash
cd ~/PersonalServer
python agent/node-agent.py onboard \
  --controller http://<CONTROLLER_TAILSCALE_IP>:8000 \
  --code PS-XXXX-XXXX
```

The agent will automatically:
1. Detect hardware (CPU cores, RAM, Storage, OS, Arch).
2. Generate a unique `node_id` (`server-<16 hex chars>`).
3. Save local configuration to `config/node.json` and `config/controller.json`.
4. Register with the Controller using the one-time code.
5. Save node authentication credentials to `runtime/registration.json`.
6. Start background services via `scripts/start.sh`.
7. Send initial heartbeat.

---

## 4. Managing Node Services

On any cluster node, control local services using the unified management scripts:

```bash
# Start services (Node API + optional Cloudflare Tunnel)
./scripts/start.sh

# Check process status and PIDs
./scripts/status.sh

# Run comprehensive health diagnostic
./scripts/health.sh

# Restart all services
./scripts/restart.sh

# Stop all services
./scripts/stop.sh
```

---

## 5. Verifying Cluster Status & Submitting Workloads

### Check Cluster Health from Controller

```bash
# View summary metrics
python controller/controller.py cluster

# View all active nodes
python controller/controller.py nodes
```

### Submit Workload Jobs

```bash
# Submit resource-aware auto-scheduled job
python controller/controller.py submit --node auto --type system-info --timeout 60

# Submit explicit job to a specific node
python controller/controller.py submit --node <node_id> --type echo --params '{"message": "ping"}'

# List active and completed jobs
python controller/controller.py jobs
```

---

## 6. Accessing the Web Dashboard

Open the Operations Web Console in your browser:
* Local / Private: `http://<NODE_TAILSCALE_IP>:8080/`
* Public Gateway (if configured): `https://api.akshatsahay.space/`

From the Web Console, you can:
* **Dashboard**: Monitor online nodes, running workloads, system telemetry, and recent jobs.
* **Nodes**: Inspect detailed hardware metrics and service states per node.
* **Jobs**: Dispatch allowlisted workloads and view execution output.
* **Storage**: Multi-node decentralized file manager (upload, download, organize, and manage files across all cluster storage nodes).
