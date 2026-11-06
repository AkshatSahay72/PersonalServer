# PersonalServer — Raspberry Pi & Android Integration Readiness Plan

This document establishes the hardware migration plan for moving the PersonalServer cluster from the current **Windows development workstation** to a **64-bit Raspberry Pi OS controller** and **unrooted Android (Termux) worker nodes**.

---

## 1. Classification Matrix of Deliverables

Every architectural capability is categorized by its validation timeline:

| Capability / Deliverable | Classification | Validation Strategy Today |
| :--- | :--- | :--- |
| **Cross-Platform Path Resolution & FHS Mapping** | **Implement & test now on Windows** | Tested via `ControllerPaths` and `test_reproducibility.py` |
| **State Backup & Restoration Pipeline** | **Implement & test now on Windows** | Validated via `scripts/backup_restore.py` and SHA256 tests |
| **Deterministic Multi-Node Worker Simulator** | **Implement & test now on Windows** | Validated via `SimulatedWorker` protocol boundary tests |
| **Scheduler Capability Filtering (Docker vs Termux)** | **Implement & test now on Windows** | Validated via `ResourceScheduler` tests rejecting Termux for Docker |
| **Image Architecture Normalization (`arm64`/`amd64`)** | **Implement & test now on Windows** | Validated via `controller/image_inspector.py` unit tests |
| **Safe Sample Configuration & Secrets Isolation** | **Implement & test now on Windows** | Validated via `.env.example` and `.gitignore` audit |
| **Termux Provisioning Script (`install/termux.sh`)** | **Implement & test now on Windows** | Validated via bash syntax check and non-root permission tests |
| **Multi-Arch Image Cross-Building (`linux/arm64`)** | **Validate partially using Linux containers** | Testable on Windows with Docker Desktop Buildx / QEMU |
| **Systemd Service Unit sandboxing (`ProtectSystem`)** | **Validate partially using Linux containers** | Testable in Docker Ubuntu/Debian container |
| **Termux Wake-Lock & Process Killer Behaviors** | **Must wait for physical Android devices** | Requires physical Android 11+ device in power-save mode |
| **Android Wi-Fi Sleep & Reconnection Delays** | **Must wait for physical Android devices** | Requires physical radio transitions and Wi-Fi roaming |
| **Raspberry Pi OS GPIO / Hardware Peripherals** | **Must wait for the Raspberry Pi** | Requires physical board |
| **Raspberry Pi SD Card Wear & I/O Benchmark** | **Must wait for the Raspberry Pi** | Requires physical eMMC / MicroSD / NVMe media |
| **High Availability (HA) & Master Failover** | **Implemented & tested on Windows** | Validated via `tests/test_ha_failover.py` (Leader lease, term consensus, state sync) |

---

## 2. Controller Configuration Mapping (Windows Workstation to 64-Bit Raspberry Pi OS)

### 2.1. Filesystem Hierarchy Standard (FHS) Mapping
On the Windows development workstation, directories default relative to the repository workspace. On Raspberry Pi OS, paths map strictly to the Linux FHS:

| Subsystem | Windows Development Default | Raspberry Pi OS (FHS) | Environment Variable |
| :--- | :--- | :--- | :--- |
| **Application Code** | `<repo_root>` | `/opt/personalserver` | `PERSONALSERVER_BASE_DIR` |
| **Platform Config** | `<repo_root>/config` | `/etc/personalserver` | `PERSONALSERVER_CONFIG_DIR` |
| **Secrets & Tokens** | `<repo_root>/config/secrets` | `/etc/personalserver/secrets` | `PERSONALSERVER_SECRETS_DIR` |
| **Persistent Databases** | `<repo_root>/controller/data` | `/var/lib/personalserver/data` | `PERSONALSERVER_DATA_DIR` |
| **Application Storage** | `<repo_root>/storage` | `/var/lib/personalserver/storage` | `PERSONALSERVER_STORAGE_DIR` |
| **Ephemeral Runtime & PIDs** | `<repo_root>/runtime` | `/run/personalserver` (tmpfs) | `PERSONALSERVER_RUNTIME_DIR` |
| **Log Files** | `<repo_root>/logs` | `/var/log/personalserver` | `PERSONALSERVER_LOGS_DIR` |

### 2.2. Service Environment Configuration (`/etc/personalserver/controller.env`)
```bash
# /etc/personalserver/controller.env
PERSONALSERVER_BASE_DIR=/opt/personalserver
PERSONALSERVER_CONFIG_DIR=/etc/personalserver
PERSONALSERVER_SECRETS_DIR=/etc/personalserver/secrets
PERSONALSERVER_DATA_DIR=/var/lib/personalserver/data
PERSONALSERVER_STORAGE_DIR=/var/lib/personalserver/storage
PERSONALSERVER_RUNTIME_DIR=/run/personalserver
PERSONALSERVER_LOGS_DIR=/var/log/personalserver

# Bindings & Timings
PERSONALSERVER_CONTROLLER_HOST=0.0.0.0
PERSONALSERVER_CONTROLLER_PORT=8000
PERSONALSERVER_ROUTER_PORT=8088
PERSONALSERVER_HEARTBEAT_TIMEOUT=60
PERSONALSERVER_LEASE_GRACE_SEC=20
PERSONALSERVER_ONBOARDING_TTL_SEC=900
```

---

## 3. Platform & Architectural Differences

| Dimension | Windows Development PC | 64-Bit Raspberry Pi OS | Unrooted Android (Termux) |
| :--- | :--- | :--- | :--- |
| **Architecture** | `x86_64` (`amd64`) | `aarch64` (`arm64`) | `aarch64` (`arm64`) |
| **OS Kernel** | Windows NT kernel | Linux 6.x (Debian arm64) | Android Linux Kernel (vendor modified) |
| **User Privileges** | Administrator / Normal User | Root + unprivileged `personalserver` | Unprivileged app UID (e.g. `u0_a245`) |
| **Port Binding** | Any unallocated port | Ports < 1024 require root; > 1024 unprivileged | Strict: ports < 1024 prohibited |
| **Process Model** | Windows Processes & Services | Systemd cgroups v2 services | Termux sub-processes (subject to Phantom Killer) |
| **Container Engine** | Docker Desktop (WSL2 backend) | Native Docker Engine / containerd | **None** (Rootless PRoot userland only) |
| **Filesystem** | NTFS (case-insensitive) | ext4 / btrfs (case-sensitive) | ext4 (internal `/data`) / FUSE (sdcard) |
| **Sleep / Throttling**| S3/Modern Standby (when idle) | Always-on, no sleep throttling | Aggressive Doze, App Standby, WakeLock |

---

## 4. Safe Controller Provisioning Procedure (Raspberry Pi OS)

### Phase 1: Base OS Setup
1. Flash **Raspberry Pi OS (64-bit)** (Debian 12 Bookworm / Debian 13 Trixie) to an A2-rated MicroSD or NVMe SSD using Raspberry Pi Imager.
2. Enable SSH with public-key authentication during flashing; disable password authentication.
3. Boot the Pi, connect via Ethernet, and update packages:
   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo apt install -y python3 python3-venv python3-pip git curl
   ```

### Phase 2: Unprivileged System User & Hardened Hierarchy
```bash
# Create dedicated system account without login shell
sudo useradd -r -m -d /var/lib/personalserver -s /bin/false personalserver

# Create FHS directories
sudo mkdir -p /etc/personalserver/secrets
sudo mkdir -p /var/lib/personalserver/data
sudo mkdir -p /var/lib/personalserver/storage
sudo mkdir -p /var/log/personalserver
sudo mkdir -p /run/personalserver

# Restrict permissions
sudo chown -R personalserver:personalserver /etc/personalserver /var/lib/personalserver /var/log/personalserver /run/personalserver
sudo chmod 750 /etc/personalserver /var/lib/personalserver /var/log/personalserver
sudo chmod 700 /etc/personalserver/secrets
```

### Phase 3: Application Code & Virtual Environment
```bash
sudo mkdir -p /opt/personalserver
sudo git clone https://github.com/AkshatSahay72/PersonalServer.git /opt/personalserver
sudo chown -R root:root /opt/personalserver

# Create venv and install dependencies
sudo python3 -m venv /opt/personalserver/.venv
sudo /opt/personalserver/.venv/bin/pip install --upgrade pip
sudo /opt/personalserver/.venv/bin/pip install -r /opt/personalserver/requirements.txt
```

### Phase 4: State Migration from Windows Development PC
1. On the Windows workstation, create a verified backup:
   ```powershell
   python scripts/backup_restore.py backup --output backups/windows_migration.tar.gz
   ```
2. Copy `windows_migration.tar.gz` to the Raspberry Pi:
   ```bash
   scp backups/windows_migration.tar.gz pi@<PI_IP>:/tmp/
   ```
3. Restore on the Raspberry Pi:
   ```bash
   sudo /opt/personalserver/.venv/bin/python3 /opt/personalserver/scripts/backup_restore.py restore /tmp/windows_migration.tar.gz \
       --data-dir /var/lib/personalserver/data \
       --secrets-dir /etc/personalserver/secrets \
       --storage-dir /var/lib/personalserver/storage
   sudo chown -R personalserver:personalserver /etc/personalserver /var/lib/personalserver
   ```

### Phase 5: Systemd Service Installation
Install [`docs/raspberry_pi_migration.md`](file:///d:/Coding/Major%20Project/PersonalServer/docs/raspberry_pi_migration.md) systemd units:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now personalserver-controller
sudo systemctl enable --now personalserver-router
```

---

## 5. Termux Worker Lifecycle (Unrooted Android)

### 5.1. Authentication & Registration Protocol
1. **Initial Pairing**:
   - The worker runs `install/termux.sh`, which configures wake-lock, verifies Python 3, and generates `config/node.json`:
     ```json
     {
       "node_id": "server-termux-vivo",
       "name": "vivo-y31",
       "role": "compute",
       "platform": "termux",
       "os": "Android Linux",
       "architecture": "aarch64",
       "capabilities": {
         "compute": true,
         "storage": true,
         "python_runtime": true,
         "container_runtime:docker": false,
         "container_runtime:proot": false
       }
     }
     ```
   - Registration request:
     ```http
     POST /register HTTP/1.1
     Host: <PI_IP>:8000
     Authorization: Bearer <ENROLLMENT_TOKEN>
     Content-Type: application/json

     { ...node.json payload... }
     ```
   - The controller validates the enrollment token, inserts the record into `nodes.json`, generates a cryptographic `auth_token`, and returns it.
   - The worker stores credentials in `runtime/registration.json` (mode `0600`).

### 5.2. Periodic Heartbeat Protocol
Every 30 seconds, the Termux worker sends:
```http
POST /heartbeat HTTP/1.1
Host: <PI_IP>:8000
Authorization: Bearer <AUTH_TOKEN>
Content-Type: application/json

{
  "node_id": "server-termux-vivo",
  "status": "online",
  "timestamp": "2026-12-01T12:00:00Z",
  "capabilities": {
    "compute": true,
    "storage": true,
    "python_runtime": true,
    "container_runtime:docker": false
  },
  "services": {
    "node_api": "running",
    "cloudflare": "stopped"
  },
  "workloads": {
    "active_jobs": 0,
    "running_jobs": 0,
    "running_containers": 0
  },
  "system": {
    "cpu_cores": 8,
    "load_average": [0.25, 0.18, 0.15],
    "cpu_percent": 8.5,
    "memory_details": {"total_mb": 5697, "used_mb": 2100, "available_mb": 3597, "used_percent": 36.9},
    "storage_details": {"total_gb": 106, "used_gb": 42, "available_gb": 64, "used_percent": 39.6},
    "uptime": "14 days, 3 hours"
  }
}
```

### 5.3. Disconnection & Reconnection Strategy
- **Controller Crash / Reboot**: The worker catches connection errors and enters a bounded exponential backoff (2s, 4s, 8s, up to 60s max) rather than exiting.
- **Node Power Loss / Network Drop**: The controller marks the node `OFFLINE` after `PERSONALSERVER_HEARTBEAT_TIMEOUT` (60s). Leased jobs enter `RECOVERING` and are reassigned after `PERSONALSERVER_LEASE_GRACE_SEC` (20s).
- **Session Expiry (401 Unauthorized)**: If the controller state was re-initialized and rejects the node's `auth_token`, the worker automatically re-initiates registration using its configured enrollment token.

---

## 6. Supported Android Workload Execution & Limits

### 6.1. Supported Execution Methods (Unrooted)
1. **Native Python Execution (`python-script`, `echo`, `system-info`, `health-check`)**:
   - Executes directly via Termux's standard Python interpreter.
   - Bounded by timeouts and output truncation (64 KB).
2. **Native AArch64 Binaries**:
   - Any CLI tool compiled for Android/Bionic (installed via `pkg install`, e.g., `git`, `curl`, `ffmpeg`, `sqlite`).
3. **PRoot Distros (`container_runtime:proot`)**:
   - Runs rootless Linux distributions (Alpine, Debian aarch64) using `ptrace` system-call translation.
   - Does **not** require device rooting.
   - Suitable for running packages not natively ported to Termux.

### 6.2. Concrete Limits of Unrooted Android
1. **No Docker Daemon (`dockerd`)**:
   - Docker requires root privileges, Linux kernel cgroups v1/v2, network namespaces, and overlayfs drivers. Android vendor kernels strip these or restrict them to `uid 0`.
   - The scheduler explicitly verifies `container_runtime:docker: false` and routes container workloads away from Termux nodes.
2. **Android Phantom Process Killer (Android 12+)**:
   - Android OS terminates an application if it spawns more than 32 total child processes across all forks.
   - Mitigation: Workers maintain strict process pooling and execute jobs sequentially.
3. **Battery Optimization & Doze Mode**:
   - Android aggressively suspends background CPU and network activity when the screen turns off.
   - Mitigation: Termux must be granted **Unrestricted Battery Usage** in Android settings, and `install/termux.sh` executes `termux-wake-lock`.
4. **Port Number Restrictions**:
   - Linux forbids non-root users from binding ports below 1024.
   - Node API binds to `8080`, SSH binds to `8022`.

---

## 7. Docker Multi-Architecture Build Matrix (AMD64 & ARM64)

PersonalServer decouples image building from container execution via `controller/image_inspector.py`.

### 7.1. Build & Execution Target Compatibility
| Workload Image Architecture | Windows PC Worker (AMD64) | Raspberry Pi Worker (ARM64) | Termux Worker (ARM64) |
| :--- | :--- | :--- | :--- |
| `linux/amd64` | **Native Execution** | Emulated (slow via QEMU) / Rejected | Incompatible (No Docker) |
| `linux/arm64` | Emulated (via QEMU) / Rejected | **Native Execution** | Incompatible (No Docker) |
| Multi-Arch (`amd64` + `arm64`) | **Native Execution** | **Native Execution** | Native Python only |

### 7.2. Multi-Arch Build Process via Buildx
When building images for deployment across the heterogeneous cluster:
```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t personalserver/demo-app:v1 \
  --output type=image,push=false .
```
- Each image receives an immutable cryptographic digest (`sha256:...`).
- The scheduler matches the node's `architecture` (`arm64` or `amd64`) and capability (`container_runtime:docker`) before assigning a job.

---

## 8. Network Segmentation Plan

PersonalServer establishes four isolated network planes:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        PLANE 4: PUBLIC INGRESS                         │
│  Internet -> Cloudflare Edge -> Cloudflare Tunnel (Outbound Encrypted) │
│  Exposes: Application Web Gateway (:8088), Public Storage (:8080)      │
│  Zero inbound ports open on home router. Zero firewall holes.          │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                   PLANE 3: REMOTE ADMINISTRATION                       │
│  Tailscale Mesh (100.x.y.z WireGuard)                                  │
│  Raspberry Pi SSH (:22) | Android Termux SSH (:8022)                   │
│  Authenticated via SSH keypairs only. Completely private.              │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                  PLANE 2: PRIVATE CLUSTER CONTROL                      │
│  Tailscale Mesh or Dedicated Local Subnet (e.g. 192.168.1.0/24)        │
│  Controller REST API (:8000)                                           │
│  Worker Heartbeats & Lease Checks                                      │
│  Requires Authorization: Bearer <TOKEN>                                │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                 PLANE 1: WORKER-TO-WORKER STORAGE                      │
│  Direct point-to-point transfers for storage files (:8080)             │
│  Never routed over public Internet.                                    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 9. Hardware Link Evaluation (Ethernet, Wi-Fi, USB, Cloudflare)

| Link Technology | Suitability | Prerequisites | Technical Assessment |
| :--- | :--- | :--- | :--- |
| **Ethernet (RJ-45)** | **Primary for Raspberry Pi Controller** | Router/switch port, Cat5e/6 cable | Zero latency jitter, immune to Wi-Fi sleep/interference, 1 Gbps throughput. |
| **Wi-Fi (802.11ac/ax)** | **Primary for Android Phones** | 2.4/5 GHz home Wi-Fi | Standard mobile connectivity. Requires static DHCP reservation or Tailscale mesh to handle IP changes. |
| **Cloudflare Tunnel** | **Primary for Public Application Ingress** | Domain registered on Cloudflare, `cloudflared` binary | Secure reverse proxy. Eliminates port forwarding and hides home IP address. |
| **USB Networking (RNDIS/CDC)** | **Conditional / Not Recommended** | OTG cable, host USB port, kernel driver | A USB hub **does not** provide IP networking. Requires explicit CDC-Ethernet/RNDIS drivers on both phone and Pi. Can cause battery degradation if connected continuously without smart power cutoff. |
| **ADB Reverse Tunneling** | **Development Only** | USB debugging enabled on phone, `adb` on PC | Requires host USB connection and continuous ADB daemon. Fragile for headless server operation. |

---

## 10. Storage Reliability & Disconnection Recovery

1. **SD Card Endurance on Raspberry Pi**:
   - MicroSD cards degrade rapidly under constant database write cycles.
   - Mitigation: Mount `/run` and `/tmp` as `tmpfs` (in RAM). Set `noatime` in `/etc/fstab`. Recommended: boot directly from a USB 3.0 SSD / NVMe.
2. **Android Storage Durability**:
   - Internal flash is reliable, but battery swelling can occur if phones are kept permanently on unmanaged chargers.
   - Mitigation: Use battery charging limiters (e.g., smart plug cycling or 80% charge limiters if supported).
3. **Automated State Backups**:
   - Cron job runs `scripts/backup_restore.py backup` nightly on the controller.
   - Keeps rotating 7-day backups with SHA256 checksum verification.
4. **Node Disconnection Recovery**:
   - Phone disconnects $\rightarrow$ 60s heartbeat timeout passes $\rightarrow$ node marked `OFFLINE`.
   - Running jobs assigned to the node transition to `RECOVERING`.
   - After 20s grace period, jobs are rescheduled to surviving online nodes.
   - Stale status updates from revived nodes are rejected by state machine sequence checking.

---

## 11. Staged Hardware Acceptance Checklist

When physical hardware becomes available in December, execute this checklist step-by-step:

### Stage 1: Raspberry Pi OS Base Verification
- [ ] 64-bit Raspberry Pi OS boots cleanly from MicroSD/SSD.
- [ ] Static IP or Tailscale IP assigned and reachable via SSH key.
- [ ] Python 3.9+ installed and `python3 -m venv` functional.
- [ ] Docker Engine (`docker version`) functional and runs `hello-world`.

### Stage 2: Controller State Restoration
- [ ] Transfer `backup.tar.gz` from Windows development machine.
- [ ] Run `python3 scripts/backup_restore.py verify backup.tar.gz` (reports `VALID`).
- [ ] Run `python3 scripts/backup_restore.py restore backup.tar.gz` (extracts databases and secrets).
- [ ] Start controller systemd unit (`systemctl status personalserver-controller`).
- [ ] Query `curl -f http://127.0.0.1:8000/cluster` (returns HTTP 200 with cluster metadata).

### Stage 3: Termux Worker Setup (Unrooted Phone)
- [ ] Install latest Termux from F-Droid (avoid outdated Google Play release).
- [ ] Run `install/termux.sh` to generate node configuration and acquire wake-lock.
- [ ] Confirm Android Battery Optimization is set to "Unrestricted" for Termux.
- [ ] Run `python agent/node-agent.py register --controller http://<PI_IP>:8000 --token <TOKEN>`.
- [ ] Verify node shows `ONLINE` in controller `GET /cluster` response.

### Stage 4: Heartbeat & Telemetry Validation
- [ ] Run `python agent/node-agent.py heartbeat` on phone.
- [ ] Verify phone CPU cores, RAM, and storage appear accurately in controller inventory.
- [ ] Turn off phone Wi-Fi for 90 seconds; verify controller transitions node to `OFFLINE`.
- [ ] Turn on phone Wi-Fi; verify node transitions back to `ONLINE` within one heartbeat cycle.

### Stage 5: Workload Dispatch & Capability Safety
- [ ] Dispatch an `echo` job: verify Termux receives, executes, and returns result.
- [ ] Dispatch a `docker-deploy` job: verify controller **rejects** Termux and schedules to Raspberry Pi.
- [ ] Disconnect phone during long-running workload: verify lease expiry and controller job recovery.

### Stage 6: Public Gateway & Ingress
- [ ] Start Cloudflare Tunnel service (`cloudflared tunnel run`).
- [ ] Open public domain in an external browser (e.g. mobile cellular data).
- [ ] Verify operations console loads and storage explorer lists cluster files.
