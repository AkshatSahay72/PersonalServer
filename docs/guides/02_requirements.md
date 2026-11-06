# 02 — Requirements: Hardware, Operating Systems & Dependencies

> **Reading Order**: **Step 2 of 12** in the PersonalServer Guide Series.  
> **Previous Guide**: [01 — Introduction](01_introduction.md)  
> **Next Guide**: [03 — Installation](03_installation.md)  
> **Audience**: Operators planning hardware and system preparation.  
> **Prerequisites**: Access to at least one computer, single-board computer, or smartphone.

---

## 1. Supported Hardware & Platform Matrix

PersonalServer is designed to run across diverse, asymmetric hardware platforms:

| Platform / Environment | Cluster Roles | Native Python | Native Node.js | Native Containers | Telemetry Sensors | Verification Status |
|---|---|---|---|---|---|---|
| **Android (Termux unrooted)** | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ❌ Kernel cgroup restricted | ✅ Battery %, WiFi dBm, thermals | **Tier 1 (Production)** — Physically verified on Vivo Y31 & Realme 5 Pro |
| **Linux (Ubuntu / Debian / x86_64 / arm64)** | Controller, Standby, Worker | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ `/proc` & `sysfs` thermals | **Tier 1 (Production)** — Verified in CI & hardware environments |
| **Raspberry Pi OS (arm64)** | Worker, Storage Host | ✅ Python 3.9+ | ✅ Node.js 18+ | ✅ Docker Engine | ✅ `vcgencmd` thermals | **Tier 2 (Supported)** — Verified in hardware simulation |
| **Windows 10/11 (Development PC)** | On-Demand Build Worker | ✅ Python 3.10+ | ✅ Node.js 18+ | ⚠️ Docker Desktop | ✅ Win32 GlobalMemoryStatus | **Tier 2 (Supported)** — Verified on Windows 11 host |

> [!IMPORTANT]
> **Container Limitations on Android**: Unrooted Android smartphones run in userspace via Termux. Android's Linux kernel restricts unprivileged userspace cgroups, meaning raw Docker Engine daemons cannot run natively on Android. Containerized workloads (`container_runtime:docker`) are automatically scheduled onto Linux/Windows nodes with Docker capability, while Python, Node.js, and static web apps run natively on Android.

---

## 2. Minimum Hardware Requirements

### Minimum Requirements for Master Controller (e.g. Android phone or Pi):
- **CPU**: 64-bit ARM (`aarch64` / `arm64`) or x86_64 dual-core processor.
- **RAM**: Minimum 1 GB available RAM (2 GB+ recommended for running multiple apps).
- **Storage**: 2 GB free internal flash storage.
- **Network**: WiFi or Ethernet with stable local connectivity.

### Minimum Requirements for Compute Worker:
- **CPU**: Single-core 1.0 GHz or higher.
- **RAM**: 512 MB available RAM (1 GB+ recommended).
- **Storage**: 1 GB free flash storage.

---

## 3. Operating System & Software Prerequisites

### On Linux or Android (Termux):
- **Python**: Version `3.9` through `3.13` (Python 3.10+ recommended).
- **Git**: Installed and available in your shell `PATH`.
- **Curl**: For health checking and API probing.

### Optional Packages (Depending on your workloads):
- **Node.js & npm**: If you plan to deploy Node.js web applications (`pkg install nodejs` or `apt install nodejs npm`).
- **Tailscale**: Recommended for connecting nodes across different networks without port forwarding.

---

## 4. Hardware Verification Commands

Verify your hardware and software setup by running the following commands in your shell:

```bash
# Verify Python version
python3 --version || python --version
# Output should be Python 3.9.x or higher

# Verify Git
git --version
# Output should be git version 2.x.x

# Check CPU Architecture
uname -m
# Output: aarch64 (ARM 64-bit) or x86_64 (Intel/AMD)
```

---

## 5. Next Step

With your hardware and system prerequisites confirmed, proceed to clone the repository and start the controller:

👉 **Continue to [03 — Installation Guide](03_installation.md)**
