# PersonalServer Self-Healing Infrastructure & Diagnostic Engine (`doctor`)

## Overview

The PersonalServer Self-Healing Infrastructure and Unified Diagnostic Engine (`scripts/doctor.py`) is a cross-platform diagnostic, recovery, and watchdog system. It operates across Termux (Android nodes), Linux nodes/servers, and Windows controller hosts.

It provides a single, low-friction recovery command for non-technical users (such as someone physically near a Termux phone) while ensuring strict safety boundaries: no blind process kills (`pkill python` is strictly forbidden), process command-line identity validation, stale PID file reclamation, mutual exclusion locking, and a circuit breaker to prevent restart loops.

---

## 1. Quick Reference: The One-Command Workflows

### For the Brother on an Android Termux Node:
Open Termux and run:
```bash
bash scripts/doctor.sh fix
```
*(Or simply `bash scripts/doctor.sh` to check health without making any changes).*

- **What it does**: Diagnoses all services, acquires `termux-wake-lock`, starts or repairs missing/crashed services (Node API, Node Worker, Cloudflare Tunnel, SSH), validates process identity, verifies post-restart health, and prints a concise summary.
- **If everything is working**: Outputs `[HEALTHY]` or `[RECOVERED]`.
- **If human action is required**: Outputs `[NEEDS MANUAL ACTION]` with simple, step-by-step instructions (e.g. "Open Tailscale app and tap Connect").

### For the Windows Controller Host:
Open Command Prompt or PowerShell in the repository root and run:
```cmd
scripts\doctor.bat fix
```
*(Or `scripts\doctor.bat` for diagnostic-only).*

- Diagnoses and repairs Controller HTTP API (:8000), App Router (:8088), Tailscale reachability, and configuration integrity.

---

## 2. Command-Line Interface & Wrappers

| Platform | Diagnostic Only | Safe Auto-Recovery | Dry-Run Simulation |
| :--- | :--- | :--- | :--- |
| **Termux (Android)** | `bash scripts/doctor.sh` | `bash scripts/doctor.sh fix` | `bash scripts/doctor.sh dry-run` |
| **Linux** | `bash scripts/doctor.sh` | `bash scripts/doctor.sh fix` | `bash scripts/doctor.sh dry-run` |
| **Windows** | `scripts\doctor.bat` | `scripts\doctor.bat fix` | `scripts\doctor.bat dry-run` |
| **Direct Python** | `python scripts/doctor.py` | `python scripts/doctor.py --repair` | `python scripts/doctor.py --repair --dry-run` |

### Supported Flags

```bash
# Standard interactive health check (read-only, no side-effects)
python scripts/doctor.py

# Safe targeted self-healing and recovery
python scripts/doctor.py --repair

# Preview recovery actions without executing them
python scripts/doctor.py --repair --dry-run

# Output structured JSON (ideal for CI, pipelines, or scripting)
python scripts/doctor.py --json

# Save sanitized timestamped diagnostic reports (JSON + TXT) to runtime/reports/
python scripts/doctor.py --save-report

# Combine repair and report generation
python scripts/doctor.py --repair --save-report

# Run bounded watchdog monitoring (e.g., check every 30s for 10 iterations)
python scripts/doctor.py --watchdog --interval 30 --iterations 10

# Stop an actively running background watchdog
python scripts/doctor.py --stop-watchdog
```

---

## 3. Overall Status & Exit Codes

The engine outputs one of four standardized health states and sets standard POSIX exit codes:

| State | Exit Code | Meaning | Recovery Behavior |
| :--- | :---: | :--- | :--- |
| **`HEALTHY`** | `0` | All services, ports, mesh connections, and tokens are operational. | No actions taken. Healthy services are never touched. |
| **`RECOVERED`** | `0` | One or more unhealthy services were successfully repaired and verified healthy. | Logged recovery actions and post-restart verification passed. |
| **`DEGRADED`** | `1` | Non-critical warnings (e.g. peer count low, high disk usage 85-95%, wake-lock absent). | Warnings displayed; non-critical repairs attempted if safe. |
| **`NEEDS MANUAL ACTION`** | `2` | Critical failure requiring human intervention (e.g. missing enrollment token, Tailscale unauthenticated, circuit breaker tripped, or persistent crash loop). | Exact manual steps printed to terminal. |

---

## 4. Autonomous Self-Healing Capabilities

### What the Engine Repairs Automatically

1. **Untracked Healthy Processes (`RUNNING_UNTRACKED`)**:
   - If the Node API or Worker is already running and responds healthy on `/health`, the engine does **not** terminate it. It safely adopts the running PID and rewrites `runtime/node-api.pid`.
2. **Stale PID Files (`STALE_PID`)**:
   - If a PID file references an operating system PID that is either dead or belongs to an unrelated command (e.g., text editor, system daemon), the engine removes the stale PID file and launches the correct service.
3. **Dead or Crashed Node API (:8080)**:
   - Verifies process identity, terminates any unresponsive remnant gracefully (SIGTERM followed by SIGKILL with a 3-second grace window), spawns a new detached instance using existing `scripts/start.sh` or Python module, and polls `http://127.0.0.1:8080/health` up to 5 seconds to verify recovery.
4. **Dead or Crashed Node Worker (`agent/node-agent.py work`)**:
   - Confirms `runtime/registration.json` exists, launches the detached worker daemon, and verifies its process identity.
5. **Termux Wake-Lock (`termux-wake-lock`)**:
   - Idempotently executes `termux-wake-lock` on Android/Termux to ensure background CPU execution is not frozen by Android Doze mode.
6. **Termux SSH Daemon (`sshd` on Port 8022)**:
   - If SSH port 8022 is closed on Termux, the engine starts `sshd` and confirms the socket is listening.
7. **Cloudflare Tunnel (`cloudflared`)**:
   - Inspects `runtime/cloudflared.pid` and matches process pattern.
   - Detects QUIC/UDP 7844 blocking in `logs/cloudflared.log` and automatically falls back to `--protocol http2` if edge connection retries are detected.
8. **Controller & App Router (on Controller hosts)**:
   - Detects stopped Controller (:8000) or Router (:8088), launches them via their native modules, and verifies HTTP `/health`.

---

## 5. Safety Boundaries & Guardrails

The recovery engine implements defense-in-depth safety rules:

1. **No Broad Process Kills**:
   - Commands like `pkill python` or `killall python` are **strictly forbidden**. The engine uses `verify_process_identity(pid, pattern)` before sending any signal. If the PID does not match the expected command-line, it is left untouched.
2. **Mutual Exclusion Recovery Locking**:
   - Recovery operations acquire `runtime/recovery.lock` containing the active PID and timestamp. If another recovery process is active, new runs abort cleanly. Stale locks (>180 seconds old from dead processes) are automatically reclaimed.
3. **Circuit Breaker Protection**:
   - Maintained in `runtime/recovery_state.json`. If a service fails recovery 3 times within a 600-second window, the circuit breaker **trips**. The engine ceases restart attempts to prevent battery drain or log flood, and reports `NEEDS MANUAL ACTION`.
4. **Data & Config Immutability**:
   - Never deletes jobs, user storage files, application databases, or configuration files.
5. **Secret Redaction**:
   - All diagnostic outputs, JSON reports, and text logs are scrubbed via `config/structured_logger.py`. Bearer tokens, tunnel tokens, private keys, and passwords are masked with `[REDACTED]`.

---

## 6. Guarded Boundaries (What Requires Human Intervention)

| Component | Why Auto-Repair is Restricted | Exact Instructions Provided by Doctor |
| :--- | :--- | :--- |
| **Tailscale on Android** | The Android OS enforces sandbox boundaries that prevent unprivileged shell scripts from toggling the system VPN tunnel. | **"Open Tailscale Android app and tap Connect"** |
| **Tailscale Auth / Keys** | Running `tailscale up --reset` or reauthenticating could revoke host credentials or drop active cluster mesh routes. | **"Run `tailscale up` to authenticate with your tailnet"** |
| **Missing Secrets** | If `config/secrets/enrollment.token` or `cloudflared.token` is missing, auto-generating it would break cluster trust. | **"Generate or copy `config/secrets/enrollment.token` from your controller administrator"** |
| **Disk Exhaustion (≥95%)** | Automatically deleting files risks data loss in storage directories or application jobs. | **"Free up space in the storage directory or remove old logs in `logs/`"** |
| **Hardware / OS Crash** | Device powered off, Wi-Fi disabled, or OS battery optimization active. | **"Disable Android Battery Optimization for Termux"** (see below). |

---

## 7. Android & Termux:Boot Setup

To ensure PersonalServer survives Android reboots and background sleeping, configure the following on the Android device once:

### 1. Disable Android Battery Optimization (One-Time Setup)
1. On the Android phone, go to **Settings** → **Apps** → **Termux**.
2. Tap **App battery usage** (or **Battery**).
3. Select **Unrestricted** (do not use "Optimized" or "Restricted").
4. Under **Permissions**, ensure **Notifications** are Allowed.

### 2. Auto-Start on Reboot via Termux:Boot
1. Install the **Termux:Boot** add-on APK from F-Droid.
2. Open Termux once and grant boot permissions:
   ```bash
   mkdir -p ~/.termux/boot
   cat << 'EOF' > ~/.termux/boot/start-personalserver.sh
   #!/data/data/com.termux/files/usr/bin/bash
   termux-wake-lock
   cd ~/PersonalServer || cd /data/data/com.termux/files/home/PersonalServer
   bash scripts/doctor.sh fix >> runtime/reports/boot.log 2>&1
   EOF
   chmod +x ~/.termux/boot/start-personalserver.sh
   ```
3. Launch the **Termux:Boot** app once to register it with Android. Now, whenever the phone restarts, PersonalServer will self-diagnose and start all services automatically.

---

## 8. Bounded Watchdog Mode

For semi-autonomous monitoring without relying on heavy system services, `doctor.py` includes a bounded watchdog:

```bash
# Monitor every 30 seconds for 20 iterations (10 minutes total), repairing issues safely
python scripts/doctor.py --watchdog --interval 30 --iterations 20

# Run in background on Termux/Linux:
nohup python scripts/doctor.py --watchdog --interval 60 > logs/watchdog.log 2>&1 &

# Stop the watchdog cleanly at any time:
python scripts/doctor.py --stop-watchdog
```

- **Safety**: The watchdog tracks its PID in `runtime/watchdog.pid`, respects the circuit breaker, and releases the recovery lock between check cycles.

---

## 9. Diagnostic Report Files

When invoked with `--save-report` (or during recovery), sanitized timestamped reports are stored under `runtime/reports/`:

- `runtime/reports/doctor_<YYYYMMDD_HHMMSS>.json`: Complete machine-readable diagnostics, recovery history, and verification states.
- `runtime/reports/doctor_<YYYYMMDD_HHMMSS>.txt`: Clean, human-readable terminal table companion.

---

## 10. Summary for Non-Technical Operators

```text
=================================================================
             PersonalServer Quick Recovery Card
=================================================================

1. Problem on phone?
   -> Open Termux
   -> Type: bash scripts/doctor.sh fix
   -> Press Enter

2. What to look for at the end:
   - [HEALTHY] or [RECOVERED] : You are all set! Close Termux.
   - [NEEDS MANUAL ACTION]    : Read the 1-2 lines printed at the
                                bottom and follow them.

3. Most common manual step:
   - Tailscale disconnected:
     Open the Tailscale app on your phone screen and tap Connect.
=================================================================
```
