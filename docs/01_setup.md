# 01 — Setup Guide: Getting Started with PersonalServer

> **Reading Order**: **Step 1 of 9** in the PersonalServer User Guide sequence.  
> **Next Guide**: [02 — Node Setup Guide](02_node_setup.md)  
> **Audience**: New users, system operators, developers.  
> **Prerequisites**: A computer/device with Python 3.9+ and Git installed.

---

## 1. What is PersonalServer?

**PersonalServer** is an autonomous, self-hosted, distributed compute cluster and edge application platform designed to run 24/7 on heterogeneous, recycled personal hardware. Instead of paying for expensive cloud VMs, PersonalServer turns idle Android smartphones, single-board computers (Raspberry Pis), old laptops, and Linux workstations into a unified personal cloud.

### Key Capabilities:
- **Zero Heavy Dependencies**: Core controller and agents run purely on the **Python 3.9+ standard library** (no external SQL servers, Redis, or Celery required).
- **Heterogeneous Edge Architecture**: Android smartphones (Termux) act as master controllers and compute workers alongside Linux edge boxes and Windows build workstations.
- **Active-Standby High Availability**: Consensus-driven leader lease election with automated failover and zero single points of failure.
- **Universal Multi-Runtime Deployment**: Direct hosting and auto-rollback for **Node.js**, **Python (venv)**, and **Static Web** applications, plus Docker container dispatch on capable nodes.
- **Secure Mesh Networking**: All inter-node communication, remote commands, and heartbeats flow over a private Tailscale WireGuard mesh with public ingress via Cloudflare Tunnels.

---

## 2. Supported Hardware and Platform Matrix

PersonalServer is built from the ground up for asymmetric edge hardware:

| Platform / Operating System | Supported Cluster Roles | Runtimes Supported | Verification Status |
|---|---|---|---|
| **Android (unrooted via Termux)** | Controller, Standby, Worker | Python, Static Web, Node.js | **Tier 1 (Production)** — Verified on Vivo Y31 & Realme 5 Pro |
| **Linux (Ubuntu / Debian / x86_64 / arm64)** | Controller, Standby, Worker | Python, Static Web, Node.js, Docker | **Tier 1 (Production)** — Verified on Linux edge nodes & CI |
| **Raspberry Pi OS (arm64)** | Worker, Storage Host | Python, Static Web, Node.js, Docker | **Tier 2 (Supported)** — Verified in hardware simulation |
| **Windows 10/11 (PowerShell)** | On-Demand Build Worker | Python, Node.js, Docker Desktop | **Tier 2 (Supported)** — Used as on-demand workstation worker |

> [!NOTE]
> Unrooted Android smartphones run in userspace via Termux. Android's Linux kernel restricts unprivileged userspace cgroups, meaning raw Docker daemons cannot run natively on Android. Containerized workloads are automatically routed to Linux/Windows nodes with Docker capability, while Python, Node.js, and static web apps run directly on Android.

---

## 3. Prerequisites and Dependencies

### Core Controller & Worker Requirements:
1. **Python**: Version `3.9` through `3.13` (Python 3.10+ recommended).
2. **Git**: Installed and available in your shell `PATH`.
3. **Network**: Local network or internet connectivity. For multi-node clustering, [Tailscale](https://tailscale.com/) is recommended for effortless mesh networking across firewalls.

### Verifying Prerequisites:
Run the following commands in your terminal:
```bash
python --version
# Output: Python 3.10.x (or higher)

git --version
# Output: git version 2.x.x
```

---

## 4. Clone the Repository

Clone PersonalServer from GitHub to your chosen installation path:

```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

Verify your working directory:
```bash
ls -la
# You should see: agent/, config/, controller/, docs/, router/, scripts/, services/
```

---

## 5. Initial Configuration

PersonalServer provides an automated environment initialization tool.

Run the diagnostic and setup bootstrapper:
```bash
python scripts/doctor.py --json
```

This command will:
1. Detect your host operating system and architecture (`aarch64`, `x86_64`, etc.).
2. Initialize runtime directories (`runtime/`, `storage/`, `storage/logs/`, `config/`).
3. Create local secrets and auth tokens (`runtime/secrets/master.token`).
4. Validate port availability (`8000` for Controller API, `8080` for UI Gateway, `8088` for Reverse Proxy).

### Inspecting Local Token
All administrative interactions require a master auth token. PersonalServer generates this during first boot:
```bash
# On Linux / Termux:
cat runtime/secrets/master.token

# On Windows (PowerShell):
Get-Content runtime\secrets\master.token
```
*Keep this token secure; it is required for enrolling nodes and executing privileged jobs.*

---

## 6. Start the Controller

To start PersonalServer in single-node/controller mode:

### On Linux or Android (Termux):
```bash
python controller/controller.py start
```

### On Windows (Development / Workstation):
```powershell
python controller/controller.py start
```

### Starting as a Background Daemon (Linux / Termux):
```bash
bash scripts/start.sh
```

---

## 7. Verify the Installation

Once started, confirm that the controller is online and healthy:

### 1. HTTP Health Endpoint:
```bash
curl -s http://127.0.0.1:8000/health
```
**Expected Output:**
```json
{"status": "HEALTHY", "node_id": "controller-...", "timestamp": "..."}
```

### 2. High Availability (HA) Status:
```bash
curl -s http://127.0.0.1:8000/ha/status
```
**Expected Output:**
```json
{
  "ha_enabled": true,
  "role": "leader",
  "term": 1,
  "lease_valid": true,
  "lease_remaining_sec": 30.0
}
```

### 3. Open the Web Operations Console:
Open your browser and navigate to:
```
http://127.0.0.1:8080
```
You will be greeted by the PersonalServer Web Console, showing node inventory, real-time CPU/thermal gauges, and active job queues.

---

## 8. Next Steps

Now that your primary controller is running, proceed to connect additional phones or computers to form a resilient distributed cluster:

👉 **Continue to [02 — Node Setup Guide](02_node_setup.md)**
