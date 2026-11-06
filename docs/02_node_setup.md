# 02 — Node Setup Guide: Preparing & Enrolling Edge Devices

> **Reading Order**: **Step 2 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [01 — Setup Guide](01_setup.md)  
> **Next Guide**: [03 — Deployment Guide](03_deployment.md)  
> **Audience**: Operators setting up smartphones, Linux SBCs, or Windows workstations as cluster nodes.  
> **Prerequisites**: A running PersonalServer controller (from Guide 01) and network reachability.

---

## 1. Node Architecture & Roles

PersonalServer clusters consist of heterogeneous nodes collaborating across a private mesh:

1. **Active Controller (Leader)**: Manages cluster state, schedules workloads, coordinates deployments, and exposes the HTTP API.
2. **Standby Controller**: Synchronizes state snapshots from the leader and automatically assumes leadership if the primary node goes offline.
3. **Compute Worker**: Runs native Python, Node.js, static file servers, or container workloads dispatched by the controller.
4. **On-Demand Build Worker**: Intermittent workstation (e.g. Windows PC) used to package builds or run tests when online without stalling phone nodes.

---

## 2. Preparing an Android Smartphone (Termux)

Recycled Android phones offer outstanding energy efficiency (consuming under 3W idle) with built-in battery backup (UPS) and mobile data failover.

### Step 1: Install Termux & Prerequisites
1. Install **Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) (do not use Google Play Store version as it is deprecated).
2. Open Termux and update the package repositories:
   ```bash
   pkg update -y && pkg upgrade -y
   ```
3. Install required packages:
   ```bash
   pkg install -y python git curl proot iproute2 termux-api
   ```
4. Optional: Install Node.js if you want this Android device to host Node.js applications:
   ```bash
   pkg install -y nodejs
   ```

### Step 2: Prevent Android Battery Killing
Android aggressive power management kills background processes. Execute these essential steps:
- Run `termux-wake-lock` in Termux to acquire a CPU wake lock.
- In Android OS Settings, locate **Battery Optimization** / **Background Power Management** for Termux and set it to **"Don't optimize"** / **"Allow unrestricted background activity"**.

### Step 3: Clone PersonalServer on Android
```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

---

## 3. Preparing a Linux Node (Ubuntu / Debian / Raspberry Pi)

Any Linux machine with systemd and Python 3.9+ can join the cluster:

```bash
# Update and install dependencies
sudo apt-get update
sudo apt-get install -y python3 python3-pip git curl

# Optional: Install Docker if this node should execute containerized workloads
sudo apt-get install -y docker.io
sudo usermod -aG docker $USER
```

Clone the repository:
```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

---

## 4. Preparing a Windows Workstation (On-Demand Worker)

To use your Windows PC as an intermittent compute and build worker:
1. Ensure Python 3.10+ is in your Windows `PATH`.
2. Open PowerShell in your cloned `PersonalServer` repository.
3. PersonalServer provides a native launcher script:
   ```powershell
   .\scripts\ps-worker.cmd run
   ```
   Or run directly via Python:
   ```powershell
   python agent/local_worker.py run
   ```

---

## 5. Network Configuration: Private Mesh with Tailscale

PersonalServer nodes typically reside behind carrier-grade NAT (CGNAT) on mobile networks or home routers. We use **Tailscale** to create a zero-config WireGuard mesh network connecting all devices:

1. Install Tailscale on the Controller and each Worker node:
   - **Android**: Install Tailscale from Google Play or F-Droid and log in.
   - **Linux / Raspberry Pi**: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
   - **Windows**: Install the Tailscale Windows client and log in.
2. Verify node IP addresses on the Tailscale network (typically `100.x.y.z`):
   ```bash
   tailscale ip -4
   ```

*Example Topology*:
- Controller (Vivo Y31): `100.85.108.5:8000`
- Worker (Realme 5 Pro): `100.73.52.72:8000`
- Workstation: `100.64.1.1`

---

## 6. Register and Enroll Nodes (Zero-Touch Pairing)

PersonalServer features a secure, one-command pairing protocol using short-lived pairing tokens (`PS-XXXX-XXXX`).

### Step 1: Generate an Onboarding Pairing Code on the Controller
Run this command on your controller node:
```bash
python controller/cli.py onboarding create --ttl 600
```
**Output:**
```text
[OK] Generated pairing code: PS-8F2A-94B1 (Valid for 600 seconds)
```

### Step 2: Enroll the Worker Node
On the new worker node (e.g. the second Android phone or Raspberry Pi), run:
```bash
python agent/node-agent.py enroll \
  --controller-url http://100.85.108.5:8000 \
  --pairing-code PS-8F2A-94B1 \
  --name "server-realme-5pro" \
  --role worker
```

**What Happens Automatically:**
1. The agent exchanges the one-time code over HTTP with the controller.
2. The controller verifies the token, assigns a unique `node_id`, and issues a mutual SHA-256 node secret.
3. The node agent automatically executes local hardware capability probes (CPU, RAM, storage, battery, Python, Node.js, Docker).
4. The node agent begins emitting periodic heartbeats (every 10 seconds).

---

## 7. Verify Connectivity & Capabilities

Verify that the new node appears in the cluster and reports verified capabilities.

### List Nodes via Controller CLI:
```bash
python controller/cli.py nodes list
```
**Expected Output:**
```text
+---------------------+-------------------+---------+-----------------------+-------------+
| Node ID             | Name              | Status  | IP / URL              | Roles       |
+---------------------+-------------------+---------+-----------------------+-------------+
| server-vivo-y31     | Vivo Y31 (Master) | ONLINE  | http://100.85.108.5   | controller  |
| server-realme-5pro  | Realme 5 Pro      | ONLINE  | http://100.73.52.72   | worker      |
+---------------------+-------------------+---------+-----------------------+-------------+
```

### Inspect Verified Capabilities:
```bash
python controller/cli.py nodes inspect server-realme-5pro
```
**Expected Output:**
```json
{
  "node_id": "server-realme-5pro",
  "status": "ONLINE",
  "verified_capabilities": {
    "python3": true,
    "nodejs": true,
    "static_web": true,
    "container_runtime:docker": false,
    "thermal_sensors": true,
    "battery_telemetry": true
  },
  "battery": {
    "percentage": 94,
    "charging": true,
    "temperature_c": 31.2
  }
}
```

---

## 8. Next Steps

With your cluster nodes enrolled and reporting verified capabilities, you are ready to deploy your first distributed application:

👉 **Continue to [03 — Deployment Guide](03_deployment.md)**
