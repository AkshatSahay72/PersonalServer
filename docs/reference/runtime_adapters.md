# Node Agent Reference & Guide

The Node Agent (`agent/node-agent.py`) is the resident supervisor and workload executor running on cluster edge devices (Android/Termux, Raspberry Pi OS, Linux servers, and Windows).

---

## 1. Responsibilities

1. **Service Supervision**: Starts, stops, restarts, and monitors local services (`Node API`, `Cloudflare Tunnel`).
2. **Rich Telemetry Collection (`config/node_telemetry.py`)**:
   - **Battery**: Percentage level, charging status (`Charging`, `Discharging`, `Full`).
   - **Network**: Connection type (`wifi`, `cellular`, `ethernet`), signal strength (dBm), signal quality label, and link speed (Mbps).
   - **Thermal**: CPU temperature (°C) and thermal throttling indicators.
3. **High Availability Controller Discovery**:
   - Resolves active master from `PERSONALSERVER_CONTROLLER_URLS`.
   - If the active master is unreachable, automatically fails over to standby candidates without dropping heartbeats or jobs.
4. **Cluster Onboarding**: Registers via single-use `PS-XXXX-XXXX` code or enrollment token, receiving a cryptographic node auth token stored in `runtime/registration.json`.
5. **Periodic Heartbeats**: Reports system metrics, load averages, RAM, disk, and rich telemetry to the controller.
6. **Sandboxed Job Execution**: Pulls assigned jobs from `/nodes/<node_id>/jobs/next`, executes allowlisted tasks via `agent/job_executor.py`, and posts results to `/jobs/<job_id>/result`.

---

## 2. CLI Usage

```bash
# Display node identity and hardware specs
python agent/node-agent.py info [--json]

# Display health and real-time telemetry (CPU, RAM, Battery, Thermal, Network)
python agent/node-agent.py health [--json]

# Display unified node and service status
python agent/node-agent.py status [--json]

# Service orchestration
python agent/node-agent.py start
python agent/node-agent.py stop
python agent/node-agent.py restart

# Enrollment and heartbeats
python agent/node-agent.py register [--controller <url>] [--token <token>]
python agent/node-agent.py registration-status [--json]
python agent/node-agent.py heartbeat [--controller <url>] [--json]

# Pull and execute a single assigned job from Controller
python agent/node-agent.py work [--controller <url>]

# Execute a workload locally without Controller (for testing)
python agent/node-agent.py exec-local --type <type> [--timeout <sec>] [--params '<json>']
```

---

## 3. Dedicated Background Worker (`agent/local_worker.py`)

For persistent background workload execution, use `agent/local_worker.py`:

```bash
# Start background worker daemon
python agent/local_worker.py --controller http://127.0.0.1:8000 --poll-interval 2.0
```

### Features:
- **Continuous Polling Loop**: Automatically polls for queued jobs matching the worker's capabilities.
- **Heartbeat Thread**: Issues background health pings every 10 seconds.
- **Lease Renewal**: Extends job lease on long-running tasks.
- **Transparent Controller Failover**: If the active controller disconnects, probes secondary candidate URLs and redirects without task interruption.
- **Graceful Shutdown**: Intercepts `SIGINT`/`SIGTERM` to complete in-flight tasks before disconnecting.

---

## 4. Telemetry Collection Mechanics

`config/node_telemetry.py` adapts transparently to host platforms:

| Platform | Battery Source | Network / Signal Source | Thermal Source |
|---|---|---|---|
| **Android (Termux)** | `termux-battery-status` CLI | `termux-wifi-connectioninfo` CLI | `/sys/class/thermal/thermal_zone*/temp` |
| **Linux (Raspberry Pi)** | `/sys/class/power_supply/` | `/proc/net/wireless` or `/sys/class/net/` | `/sys/class/thermal/thermal_zone0/temp` |
| **Windows** | `wmic path Win32_Battery` | `netsh wlan show interfaces` | `wmic /namespace:\\root\wmi path MSAcpi_ThermalZoneTemperature` |

If physical sensors or tools are unavailable, safe neutral fallbacks are returned without throwing exceptions or blocking heartbeats.
