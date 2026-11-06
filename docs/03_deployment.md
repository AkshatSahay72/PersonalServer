# 03 — Deployment Guide: Applications & Workloads

> **Reading Order**: **Step 3 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [02 — Node Setup Guide](02_node_setup.md)  
> **Next Guide**: [04 — Cluster Management Guide](04_cluster_management.md)  
> **Audience**: Developers deploying services, microservices, and static websites to PersonalServer.  
> **Prerequisites**: Enrolled cluster nodes with verified capabilities (from Guide 02).

---

## 1. Application Deployment Model (R1–R9 Architecture)

PersonalServer features a multi-runtime deployment engine with automated health-check verification and atomic rollback. Applications run directly under native runtime adapters without requiring heavy virtualization on resource-constrained edge hardware.

### Supported Application Runtimes:
1. **Python (`python3`)**: Isolated virtual environments (`venv`) with dependencies installed via `pip`.
2. **Node.js (`nodejs`)**: Native Node.js processes with automated `npm install --omit=dev` lifecycle management.
3. **Static Web (`static`)**: High-performance static web hosting with SPA fallback and MIME type detection.
4. **OCI Containers (`container`)**: Full Docker container execution on nodes with genuine Docker daemon support.

---

## 2. The Application Manifest (`app.json`)

Every deployable application is defined by an `app.json` manifest located at the root of the application repository or artifact package.

### Example 1: Node.js Web API Manifest
```json
{
  "name": "portfolio-api",
  "version": "1.0.0",
  "runtime": "nodejs",
  "entrypoint": "server.js",
  "port": 3000,
  "health_check": {
    "path": "/health",
    "timeout_sec": 5,
    "max_retries": 3
  },
  "env": {
    "NODE_ENV": "production",
    "PORT": "3000"
  },
  "routing": {
    "subdomain": "portfolio",
    "path_prefix": "/api"
  }
}
```

### Example 2: Python Flask / FastAPI Microservice Manifest
```json
{
  "name": "sensor-collector",
  "version": "1.2.0",
  "runtime": "python3",
  "entrypoint": "app.py",
  "port": 5000,
  "dependencies": "requirements.txt",
  "health_check": {
    "path": "/status",
    "timeout_sec": 10
  }
}
```

### Example 3: Static Front-End SPA Manifest
```json
{
  "name": "dashboard-frontend",
  "version": "2.0.0",
  "runtime": "static",
  "static_root": "dist",
  "spa_fallback": true
}
```

---

## 3. Packaging & Building Artifacts

PersonalServer packages application source code into cryptographically hashed `.tar.gz` bundles:

```bash
# Packaging an application directory
python controller/cli.py apps package \
  --source ./my-portfolio-app \
  --output ./storage/artifacts/portfolio-v1.tar.gz
```

**Security & Sanitization Rules During Packaging:**
- Sensitive files (`.env`, `*.token`, `id_rsa`, `.git`) are automatically redacted from packages.
- SHA-256 checksums are computed upon creation and verified by the worker before extraction to prevent in-transit tampering.

---

## 4. Deploying Applications

You can trigger deployments using the Controller CLI, REST API, or the Web Dashboard.

### Method 1: Using the Controller CLI
```bash
python controller/cli.py apps deploy \
  --app-id "portfolio-api" \
  --artifact "./storage/artifacts/portfolio-v1.tar.gz" \
  --target-node "server-realme-5pro"
```

### Method 2: Automatic Node Scheduling
If you omit `--target-node`, the **Universal Scheduler (J3)** evaluates cluster nodes and places the application on the most suitable node based on available memory, CPU load, and runtime capability matching.

```bash
python controller/cli.py apps deploy \
  --app-id "portfolio-api" \
  --artifact "./storage/artifacts/portfolio-v1.tar.gz"
```

### Method 3: Deploying via Web Console
1. Navigate to `http://127.0.0.1:8080/#apps` in your browser.
2. Click **Deploy New Application**.
3. Select your application manifest or enter GitHub repository URL and branch.
4. Click **Submit Deployment**.
5. Watch live logs stream in real-time as the package builds, activates, and passes health checks.

---

## 5. Deployment Verification & Atomic Rollback

PersonalServer enforces a strict state machine during deployment:

```
QUEUED ➔ STAGING ➔ PREPARING ➔ ACTIVATING ➔ HEALTH_CHECKING ➔ RUNNING
                                        ↳ (on failure) ➔ ROLLING_BACK ➔ ROLLED_BACK
```

1. **Activation**: The worker launches the new process on an ephemeral port.
2. **Health Check Probe**: The controller probes the application's declared `health_check.path`.
3. **Traffic Cutover**: If the health check returns HTTP 200 within `timeout_sec`, the reverse proxy flips live traffic to the new process, and the old process is gracefully stopped (`SIGTERM` followed by `SIGKILL` after 10s).
4. **Automatic Rollback**: If the health check fails, PersonalServer immediately terminates the unhealthy process and **restores traffic to the previous healthy release** without downtime.

---

## 6. Inspecting Deployed Applications

### View Application Status:
```bash
python controller/cli.py apps status portfolio-api
```
**Output:**
```json
{
  "app_id": "portfolio-api",
  "status": "RUNNING",
  "active_deployment_id": "dep-8f9211ab",
  "node_id": "server-realme-5pro",
  "port": 3000,
  "pid": 14209,
  "health": "200 OK (latency: 14ms)",
  "uptime_sec": 3840
}
```

### View Application Logs:
```bash
python controller/cli.py apps logs portfolio-api --tail 50
```

---

## 7. Manual Rollback

If a newly deployed release introduces a logic bug, you can instantly roll back to any prior healthy release in the deployment history:

```bash
# List deployment history
python controller/cli.py apps history portfolio-api

# Rollback to specific release
python controller/cli.py apps rollback portfolio-api --deployment-id dep-10293847
```

---

## 8. Next Steps

Now that you know how to package and deploy applications, learn how to manage cluster resources, monitor nodes, and submit standalone compute jobs:

👉 **Continue to [04 — Cluster Management Guide](04_cluster_management.md)**
