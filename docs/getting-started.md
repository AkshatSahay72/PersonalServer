# Getting Started with PersonalServer v1.0

This guide walks through starting, operating, and managing PersonalServer across both the Laptop development environment and the Vivo Y31 Android/Termux server node.

---

## 1. Prerequisites

1. **Laptop Environment**:
   - Python 3.9+ installed.
   - Tailscale connected (e.g. `100.120.251.42`).
   - Repository checked out.

2. **Vivo Y31 Server Node**:
   - Termux installed.
   - OpenSSH running on port `8022` over Tailscale (e.g. `100.85.108.5`).
   - Cloudflared installed and configured.

---

## 2. Starting the Controller

From the laptop development environment:

```bash
# Start Controller with 60-second heartbeat timeout
python controller/controller.py start --host 0.0.0.0 --port 8000 --timeout 60
```

The Controller will initialize:
- Cluster database: `controller/data/nodes.json`
- Jobs database: `controller/data/jobs.json`
- Enrollment token: `config/secrets/enrollment.token`
- Background lease recovery sweeper thread.

---

## 3. Starting the Server Node (Vivo Y31)

SSH into the Vivo Y31:

```bash
ssh -p 8022 u0_a244@100.85.108.5
```

Within Termux:

```bash
cd ~/PersonalServer

# Start all managed services (Node API + Cloudflare Tunnel)
bash scripts/start.sh

# Verify running processes and PIDs
bash scripts/status.sh
```

---

## 4. Verifying Health & Heartbeat

On the Vivo Y31:

```bash
# Send an authenticated heartbeat to the Controller
python agent/node-agent.py heartbeat
```

On the Laptop:

```bash
# Check cluster status
python controller/controller.py cluster

# View registered nodes
python controller/controller.py nodes
```

---

## 5. Accessing the Web Interface

Open your web browser (on any device, with or without Tailscale) to:

```text
https://api.akshatsahay.space/
```

From the Web Interface you can:
- View cluster status and node metrics.
- Browse, upload, download, and manage personal files.
- Submit new workload jobs.
- Inspect job execution logs and attempt history.
