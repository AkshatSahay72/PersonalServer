# PersonalServer — Raspberry Pi OS Deployment & FHS Mapping Guide

## Overview

PersonalServer is designed to be fully portable between local Windows development and Linux production deployments (Raspberry Pi OS / Debian aarch64 or x86_64).

The controller, application router, and node agent make **no assumptions about operating system, user paths, or hardware addresses**. All directories, network addresses, and security boundaries are configured centrally via `config/platform.yaml` and environment variables.

---

## 1. Directory Mapping (Windows Dev vs. Raspberry Pi OS FHS)

On Windows development, paths default relative to the project workspace. On Raspberry Pi OS, paths map cleanly to the **Linux Filesystem Hierarchy Standard (FHS)**:

| Component | Windows Dev Default | Raspberry Pi OS (FHS) | Environment Variable |
| :--- | :--- | :--- | :--- |
| **Base / Code** | `<repo_root>` | `/opt/personalserver` | `PERSONALSERVER_BASE_DIR` |
| **Configuration** | `<repo_root>/config` | `/etc/personalserver` | `PERSONALSERVER_CONFIG_DIR` |
| **Secrets & Tokens** | `<repo_root>/config/secrets` | `/etc/personalserver/secrets` | `PERSONALSERVER_SECRETS_DIR` |
| **Persistent Databases** | `<repo_root>/controller/data` | `/var/lib/personalserver/data` | `PERSONALSERVER_DATA_DIR` |
| **Application Storage** | `<repo_root>/storage` | `/var/lib/personalserver/storage` | `PERSONALSERVER_STORAGE_DIR` |
| **Ephemeral Runtime/PIDs**| `<repo_root>/runtime` | `/run/personalserver` | `PERSONALSERVER_RUNTIME_DIR` |
| **Logs** | `<repo_root>/logs` | `/var/log/personalserver` | `PERSONALSERVER_LOGS_DIR` |

---

## 2. Raspberry Pi OS Environment Configuration

Create the system environment configuration file at `/etc/personalserver/controller.env`:

```bash
# /etc/personalserver/controller.env
PERSONALSERVER_BASE_DIR=/opt/personalserver
PERSONALSERVER_CONFIG_DIR=/etc/personalserver
PERSONALSERVER_SECRETS_DIR=/etc/personalserver/secrets
PERSONALSERVER_DATA_DIR=/var/lib/personalserver/data
PERSONALSERVER_STORAGE_DIR=/var/lib/personalserver/storage
PERSONALSERVER_RUNTIME_DIR=/run/personalserver
PERSONALSERVER_LOGS_DIR=/var/log/personalserver

# Network Bindings
PERSONALSERVER_CONTROLLER_HOST=0.0.0.0
PERSONALSERVER_CONTROLLER_PORT=8000
PERSONALSERVER_HEARTBEAT_TIMEOUT=60
PERSONALSERVER_LEASE_GRACE_SEC=20
PERSONALSERVER_ONBOARDING_TTL_SEC=900
```

---

## 3. Raspberry Pi OS Setup Instructions

### 3.1. Install Prerequisites
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 3.2. Dedicated System User
Run services under an unprivileged system user:
```bash
sudo useradd -r -m -d /var/lib/personalserver -s /bin/false personalserver
```

### 3.3. Create FHS Directory Hierarchy and Hardened Permissions
```bash
# Create FHS directories
sudo mkdir -p /etc/personalserver/secrets
sudo mkdir -p /var/lib/personalserver/data
sudo mkdir -p /var/lib/personalserver/storage/applications
sudo mkdir -p /var/log/personalserver
sudo mkdir -p /run/personalserver

# Hardened permissions: secrets readable strictly by personalserver user
sudo chown -R personalserver:personalserver /etc/personalserver
sudo chmod 750 /etc/personalserver
sudo chmod 700 /etc/personalserver/secrets

sudo chown -R personalserver:personalserver /var/lib/personalserver
sudo chmod 750 /var/lib/personalserver

sudo chown -R personalserver:personalserver /var/log/personalserver
sudo chmod 750 /var/log/personalserver

sudo chown -R personalserver:personalserver /run/personalserver
sudo chmod 755 /run/personalserver
```

### 3.4. Deploy Application Code
```bash
sudo mkdir -p /opt/personalserver
sudo cp -r . /opt/personalserver/
sudo chown -R root:root /opt/personalserver
sudo chmod -R 755 /opt/personalserver

# Create Python virtual environment
sudo python3 -m venv /opt/personalserver/.venv
sudo /opt/personalserver/.venv/bin/pip install --upgrade pip
if [ -f /opt/personalserver/requirements.txt ]; then
    sudo /opt/personalserver/.venv/bin/pip install -r /opt/personalserver/requirements.txt
fi
```

### 3.5. Copy Configuration
```bash
sudo cp /opt/personalserver/config/platform.yaml /etc/personalserver/platform.yaml
sudo chown personalserver:personalserver /etc/personalserver/platform.yaml
```

---

## 4. Systemd Service Unit Files

### 4.1. Controller Service (`/etc/systemd/system/personalserver-controller.service`)

```ini
[Unit]
Description=PersonalServer Cluster Controller & Scheduler
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=personalserver
Group=personalserver
EnvironmentFile=/etc/personalserver/controller.env
WorkingDirectory=/opt/personalserver
ExecStart=/opt/personalserver/.venv/bin/python3 /opt/personalserver/controller/controller.py start
Restart=always
RestartSec=5
StandardOutput=append:/var/log/personalserver/controller.log
StandardError=append:/var/log/personalserver/controller.log

# Security Sandboxing
ProtectSystem=full
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 4.2. Application Router Service (`/etc/systemd/system/personalserver-router.service`)

```ini
[Unit]
Description=PersonalServer Application Gateway & Dynamic Path Router
After=personalserver-controller.service
Wants=personalserver-controller.service

[Service]
Type=simple
User=personalserver
Group=personalserver
EnvironmentFile=/etc/personalserver/controller.env
WorkingDirectory=/opt/personalserver
ExecStart=/opt/personalserver/.venv/bin/python3 /opt/personalserver/router/app_router.py --port 8088
Restart=always
RestartSec=5
StandardOutput=append:/var/log/personalserver/router.log
StandardError=append:/var/log/personalserver/router.log

ProtectSystem=full
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

---

## 5. Enabling and Starting Services on Raspberry Pi OS

```bash
# Reload systemd configuration
sudo systemctl daemon-reload

# Enable services to launch on boot
sudo systemctl enable personalserver-controller
sudo systemctl enable personalserver-router

# Start the services
sudo systemctl start personalserver-controller
sudo systemctl start personalserver-router

# Check status
sudo systemctl status personalserver-controller
sudo systemctl status personalserver-router
```

---

## 6. Verification and Health Checks

### 6.1. Controller Liveness
```bash
curl -f http://127.0.0.1:8000/cluster
```
Expected response: JSON with cluster status and registered nodes.

### 6.2. Router Liveness
```bash
curl -f http://127.0.0.1:8088/health
```
Expected response: HTTP 200 `{"status": "ok", "router": "running"}`.

### 6.3. Secret Redaction & Token Audit
Verify that secrets are not disclosed in logs or responses:
```bash
# Verify controller log contains no tokens
grep -i "token" /var/log/personalserver/controller.log
```
The enrollment token remains strictly confined to `/etc/personalserver/secrets/enrollment.token` (permissions `0600`).
