# 05 — First Deployment Guide: Deploying Applications & Services

> **Reading Order**: **Step 5 of 12** in the PersonalServer Guide Series.  
> **Previous Guide**: [04 — Node Setup](04_node_setup.md)  
> **Next Guide**: [06 — Cluster Management](06_cluster_management.md)  
> **Audience**: Developers deploying services, microservices, and static websites.  
> **Prerequisites**: An enrolled worker node with verified capabilities (Guide 04).

---

## 1. Application Deployment Model (R1–R9 Architecture)

PersonalServer features an atomic multi-runtime deployment engine. Applications run directly under native runtime adapters without requiring heavy virtualization on resource-constrained edge hardware.

### Supported Runtimes:
1. **Python (`python3`)**: Isolated virtual environments (`venv`) with dependencies installed via `pip`.
2. **Node.js (`nodejs`)**: Native Node.js processes with automated `npm install --omit=dev` lifecycle management.
3. **Static Web (`static`)**: High-performance static web hosting with SPA fallback and MIME type detection.
4. **OCI Containers (`container`)**: Full Docker container execution on nodes with genuine Docker daemon support.

---

## 2. The Application Manifest (`app.json`)

Every deployable application is defined by an `app.json` manifest located at the root of the application repository or artifact package.

### Example: Python Flask Microservice Manifest
Create a file named `app.json` in your application directory:
```json
{
  "name": "hello-service",
  "version": "1.0.0",
  "runtime": "python3",
  "entrypoint": "app.py",
  "port": 5000,
  "health_check": {
    "path": "/health",
    "timeout_sec": 5,
    "max_retries": 3
  },
  "env": {
    "ENV": "production",
    "PORT": "5000"
  }
}
```

Accompanying `app.py`:
```python
import http.server
import json

class SimpleHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"UP"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Hello from PersonalServer!")

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 5000), SimpleHandler)
    server.serve_forever()
```

---

## 3. Packaging & Building Artifacts

PersonalServer packages application source code into cryptographically hashed `.tar.gz` bundles:

```bash
# Package the application directory into an artifact bundle
python controller/cli.py apps package \
  --source ./hello-service \
  --output ./storage/artifacts/hello-service-v1.tar.gz
```

**Security & Sanitization Rules During Packaging:**
- Sensitive files (`.env`, `*.token`, `id_rsa`, `.git`) are automatically excluded from the package.
- An immutable SHA-256 checksum is computed upon creation and verified by the worker before extraction to prevent tampering.

---

## 4. Deploying Applications

You can trigger deployments using the Controller CLI, REST API, or the Web Dashboard.

### Method 1: Using the Controller CLI
```bash
python controller/cli.py apps deploy \
  --app-id "hello-service" \
  --artifact "./storage/artifacts/hello-service-v1.tar.gz" \
  --target-node "server-realme-5pro"
```

### Method 2: Automatic Node Scheduling
If you omit `--target-node`, the **Universal Scheduler (J3)** evaluates cluster nodes and places the application on the most suitable node based on available memory, CPU load, and runtime capability matching.

```bash
python controller/cli.py apps deploy \
  --app-id "hello-service" \
  --artifact "./storage/artifacts/hello-service-v1.tar.gz"
```

### Method 3: Deploying via Web Console
1. Navigate to `http://127.0.0.1:8080/#apps` in your browser.
2. Click **Deploy New Application**.
3. Select your application package or enter your Git repository URL.
4. Click **Submit Deployment** and observe real-time build, activation, and health-check logs.

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
python controller/cli.py apps status hello-service
```
**Output:**
```json
{
  "app_id": "hello-service",
  "status": "RUNNING",
  "active_deployment_id": "dep-8f9211ab",
  "node_id": "server-realme-5pro",
  "port": 5000,
  "pid": 14209,
  "health": "200 OK (latency: 12ms)",
  "uptime_sec": 420
}
```

### View Application Logs:
```bash
python controller/cli.py apps logs hello-service --tail 50
```

---

## 7. Next Step

Learn how to manage cluster workloads, inspect resource allocations, and submit asynchronous batch jobs:

👉 **Continue to [06 — Cluster Management Guide](06_cluster_management.md)**
