# 07 — Monitoring & Troubleshooting Guide: Diagnostics & Self-Healing

> **Reading Order**: **Step 7 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [06 — Storage & Artifacts Guide](06_storage_and_artifacts.md)  
> **Next Guide**: [08 — Development Guide](08_development.md)  
> **Audience**: Operators diagnosing cluster health, resolving errors, and inspecting logs.  
> **Prerequisites**: A running PersonalServer controller or worker node.

---

## 1. Automated Diagnostics: The Doctor Utility

PersonalServer includes a comprehensive diagnostic and self-healing framework (`scripts/doctor.py`). It inspects the entire system and recommends or executes automated fixes.

### Running a Complete Health Diagnostic:
```bash
python scripts/doctor.py
```

**Diagnostic Checks Executed:**
1. **Platform & Termux Detection**: Verifies Android prefix, battery sensor APIs, and Linux architecture.
2. **Process Health & PID Liveness**: Validates PID files against actual OS process tables (detecting orphaned or recycled PIDs).
3. **Port & Socket Availability**: Checks whether ports 8000 (API), 8080 (UI), and 8088 (Proxy) are accessible.
4. **Tailscale Mesh Connectivity**: Tests ping and WireGuard handshake to peer controller nodes.
5. **Storage Sandbox & File Permissions**: Validates write permissions and detects primary database corruption.

### Automated Repair (Self-Healing):
If degraded or orphaned services are detected, run the doctor in repair mode:
```bash
python scripts/doctor.py --repair
```
**Automated Repairs Performed:**
- Cleans stale lock files and dead PID registrations.
- Adopts untracked running controller processes.
- Restores corrupted JSON databases from their automated timestamped backups (`*.bak`).

---

## 2. Inspecting System Logs

PersonalServer records structured logs across several dedicated log files located in `storage/logs/`:

| Log File | Component | Description |
|---|---|---|
| `storage/logs/controller.log` | Master Controller | API requests, job scheduling decisions, deployment state transitions |
| `storage/logs/ha.log` | HA Consensus Engine | Leader heartbeat renewals, term increments, election events |
| `storage/logs/node_agent.log` | Edge Node Agent | Local telemetry metrics, prober runs, worker dispatch |
| `storage/logs/router.log` | Reverse Proxy | Ingress requests, routing table updates, 502/503 upstream errors |

### Streaming Live Logs:
```bash
# On Linux / Termux:
tail -f storage/logs/controller.log

# On Windows (PowerShell):
Get-Content storage\logs\controller.log -Wait -Tail 30
```

---

## 3. Common Troubleshooting Scenarios

### Scenario 1: Node Appears "OFFLINE" After Device Reboot
- **Cause**: Android OS terminated Termux background processes upon reboot.
- **Resolution**:
  1. Open Termux on the phone.
  2. Run `termux-wake-lock`.
  3. Start the node agent: `python agent/node-agent.py start`.
  4. Confirm that battery optimization is disabled in Android Settings.

### Scenario 2: Application Returns HTTP 503 "Service Unavailable"
- **Cause**: The application either crashed during startup or failed its configured health check.
- **Resolution**:
  1. Inspect application logs: `python controller/cli.py apps logs <app_id> --tail 50`.
  2. Check if the application crashed on a missing dependency or invalid port binding.
  3. Verify that your application listens on `0.0.0.0` or `127.0.0.1` at the exact port declared in `app.json`.

### Scenario 3: Stale Leader Lease / Split-Brain Suspected
- **Cause**: Temporary network drop between two HA nodes.
- **Resolution**:
  1. Inspect HA status on both nodes: `curl -s http://<node_ip>:8000/ha/status`.
  2. Check the term counter. If one node shows a higher term, the other node will automatically synchronize and step down.
  3. If manual intervention is required, restart the standby node: `python controller/controller.py restart`.

---

## 4. Next Steps

Interested in extending PersonalServer, writing custom runtime adapters, or contributing code?

👉 **Continue to [08 — Development Guide](08_development.md)**
