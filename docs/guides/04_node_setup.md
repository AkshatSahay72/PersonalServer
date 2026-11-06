# 04 — Node Setup Guide: Edge Device Preparation & Enrollment

> **Reading Order**: **Step 4 of 12** in the PersonalServer Guide Series.  
> **Previous Guide**: [03 — Installation](03_installation.md)  
> **Next Guide**: [05 — First Deployment](05_first_deployment.md)  
> **Audience**: Operators setting up secondary Android phones, Linux edge boxes, or Windows workers.  
> **Prerequisites**: A running PersonalServer controller (Guide 03) and local network or Tailscale mesh connectivity.

---

## 1. Node Roles in a PersonalServer Cluster

A PersonalServer cluster consists of specialized nodes collaborating across an encrypted mesh:
1. **Master Controller (Leader)**: Authoritative state, scheduling, and API server.
2. **Standby Controller**: Replicates state from the leader and assumes leadership if the master fails.
3. **Compute Worker**: Executes deployed applications, background batch jobs, and health checks.
4. **On-Demand Build Worker**: Workstation (e.g. Windows laptop) that connects intermittently to compile code or run tests without draining phone batteries.

---

## 2. Preparing an Android Smartphone (Termux)

Android phones make ideal edge server nodes: low power consumption (<3W idle), built-in battery backup, and integrated wireless radios.

### Step 1: Install Termux & Base Packages
1. Install **Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) (do not use the obsolete Google Play version).
2. Open Termux and update package mirrors:
   ```bash
   pkg update -y && pkg upgrade -y
   ```
3. Install Python, Git, and network utilities:
   ```bash
   pkg install -y python git curl proot iproute2 termux-api
   ```
4. Optional: If this phone will host Node.js web applications, install Node:
   ```bash
   pkg install -y nodejs
   ```

### Step 2: Prevent Background Termination
Android aggressive power management kills background processes when the screen turns off. Configure Termux:
1. Run `termux-wake-lock` inside Termux to acquire a permanent CPU wake lock.
2. Open Android OS **Settings ➔ Apps ➔ Termux ➔ Battery**. Set battery optimization to **"Unrestricted"** / **"Don't optimize"**.

### Step 3: Clone the Repository
```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

---

## 3. Preparing a Linux Node (Ubuntu / Debian / Raspberry Pi)

Any Linux machine with Python 3.9+ can join the cluster:
```bash
sudo apt-get update && sudo apt-get install -y python3 python3-pip git curl

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

To use your Windows development PC as an intermittent compute and build worker:
1. Ensure Python 3.10+ is installed and present in your `PATH`.
2. Open PowerShell in your cloned `PersonalServer` repository.
3. Start the worker using the native launcher:
   ```powershell
   .\scripts\ps-worker.cmd run
   ```
   Or via direct Python invocation:
   ```powershell
   python agent/local_worker.py run
   ```

---

## 5. Network Configuration: Private Mesh with Tailscale

PersonalServer nodes typically operate behind mobile NAT (CGNAT) or home routers. We use **Tailscale** to create a zero-config WireGuard mesh network:

1. Install Tailscale on the controller and all worker nodes:
   - **Android**: Install Tailscale from F-Droid or Play Store and sign in.
   - **Linux / Raspberry Pi**: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
   - **Windows**: Install the Tailscale Windows desktop client.
2. Note the Tailscale IP of each device (typically `100.x.y.z`):
   ```bash
   tailscale ip -4
   ```

*Example Operational Topology*:
- **Controller (Vivo Y31)**: `100.85.108.5:8000`
- **Worker (Realme 5 Pro)**: `100.73.52.72:8000`

---

## 6. Register and Enroll Nodes (Zero-Touch Pairing)

PersonalServer features a secure one-command enrollment protocol using short-lived pairing tokens (`PS-XXXX-XXXX`).

### Step 1: Generate a Pairing Code on the Controller
Run this command on your controller node:
```bash
python controller/cli.py onboarding create --ttl 600
```
**Output:**
```text
[OK] Generated pairing code: PS-8F2A-94B1 (Valid for 600 seconds)
```

### Step 2: Enroll the Worker Node
On the worker device (e.g. your secondary Android phone or Linux box), run:
```bash
python agent/node-agent.py enroll \
  --controller-url http://100.85.108.5:8000 \
  --pairing-code PS-8F2A-94B1 \
  --name "server-realme-5pro" \
  --role worker
```

**What Happens Automatically:**
1. The agent securely exchanges the pairing code with the controller.
2. The controller verifies the token, assigns a unique `node_id`, and issues mutual SHA-256 node credentials.
3. The worker executes automated local capability probes (CPU, RAM, storage, Python, Node.js, Docker).
4. The worker begins sending periodic heartbeats (every 10 seconds) with real-time battery and thermal metrics.

---

## 7. Verifying Node Connectivity & Capabilities

From the controller, verify that the new node appears in the cluster:

### List All Enrolled Nodes:
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

### Inspect Verified Hardware Capabilities:
```bash
python controller/cli.py nodes inspect server-realme-5pro
```
**Expected JSON Output:**
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

## 8. Next Step

With your cluster nodes enrolled and reporting verified capabilities, deploy your first web application:

👉 **Continue to [05 — First Deployment Guide](05_first_deployment.md)**
