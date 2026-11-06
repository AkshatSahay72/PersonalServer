# 03 — Installation Guide: Setting Up the Controller

> **Reading Order**: **Step 3 of 12** in the PersonalServer Guide Series.  
> **Previous Guide**: [02 — Hardware & Requirements](02_requirements.md)  
> **Next Guide**: [04 — Node Setup](04_node_setup.md)  
> **Audience**: Operators setting up the primary PersonalServer controller.  
> **Prerequisites**: System prerequisites verified in Guide 02.

---

## 1. Overview

The **Master Controller** is the central brain of a PersonalServer cluster. It exposes the REST management API, hosts the Web Operations Console, coordinates high availability consensus, and executes the Universal Scheduler to dispatch workloads.

In this guide, you will clone the repository, run the automated diagnostic bootstrapper, launch the controller service, and verify that the API and Web UI are functioning.

---

## 2. Clone the Repository

Clone PersonalServer from GitHub onto your chosen controller device (e.g. your primary Android phone or Linux server):

```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

Verify your directory structure:
```bash
ls -la
# Verified directories: agent/, config/, controller/, docs/, router/, scripts/, services/
```

---

## 3. Initial Configuration & Diagnostic Bootstrapping

PersonalServer includes an automated diagnostic bootstrapper (`scripts/doctor.py`) that sets up directories, checks port availability, and generates local authentication tokens.

Run the diagnostic check:
```bash
python scripts/doctor.py
```

### What Happens Automatically:
1. Detects platform characteristics (Linux, Android/Termux, or Windows).
2. Creates runtime storage directories (`runtime/`, `storage/`, `storage/logs/`, `config/`).
3. Generates the master administrative token (`runtime/secrets/master.token`).
4. Verifies socket availability on ports `8000` (Controller API), `8080` (Web Console), and `8088` (Reverse Proxy).

### Inspect Your Master Authentication Token:
Administrative requests to the controller require a bearer authentication token:
```bash
# On Linux / Termux:
cat runtime/secrets/master.token

# On Windows (PowerShell):
Get-Content runtime\secrets\master.token
```
*Keep this token safe. You will need it to authorize nodes and submit privileged jobs.*

---

## 4. Starting the Controller

You can run the controller interactively in your foreground shell or as a background service:

### Foreground Launch (Recommended for first run):
```bash
python controller/controller.py start
```

### Background Daemon Launch (Linux / Termux):
```bash
bash scripts/start.sh
```

---

## 5. Verifying Installation

Verify that the controller is online and responding to requests:

### 1. Query the Health Endpoint:
```bash
curl -s http://127.0.0.1:8000/health
```
**Expected JSON Response:**
```json
{"status": "HEALTHY", "node_id": "controller-...", "timestamp": "..."}
```

### 2. Verify High Availability (HA) Status:
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
You should see the PersonalServer Dashboard displaying real-time cluster health, node inventory, and active job queues.

---

## 6. Common Installation Failures & Recovery

- **Port Already in Use (Error: Address already in use: 8000)**:  
  Another service or previous controller process is running. Run `python scripts/doctor.py --repair` to terminate stale PIDs and free ports.
- **Permission Denied on Storage Directory**:  
  Ensure your user has write access to the working directory: `chmod -R u+rw .`.

---

## 7. Next Step

Now that your primary controller is running, connect worker devices (additional phones, Raspberry Pis, or workstations) to form a distributed cluster:

👉 **Continue to [04 — Node Setup Guide](04_node_setup.md)**
