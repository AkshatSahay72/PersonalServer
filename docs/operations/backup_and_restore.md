# PersonalServer — Reproducible Development, CI, and Backup Guide

This document is the single source of truth for setting up, developing, testing, backing up, and running CI for PersonalServer on **Windows workstations today** and **Linux/Termux environments later**.

---

## 1. Quick Start: Clean Local Setup from a Fresh Checkout

Follow these steps from a freshly cloned checkout to configure a working environment:

### Step 1: Clone the Repository
```bash
git clone https://github.com/AkshatSahay72/PersonalServer.git
cd PersonalServer
```

### Step 2: Create and Activate a Python Virtual Environment
Requires **Python 3.9 or higher** (Python 3.11–3.13 recommended).

- **Windows (PowerShell):**
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  ```
- **Linux / macOS / Termux (Bash):**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

### Step 3: Install Pinned Dependencies
PersonalServer core runtime is deliberately dependency-free. Install development and testing dependencies:
```bash
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### Step 4: Configure Safe Sample Environment & Credentials
Initialize local configuration from safe templates (contains zero real secrets):

- **Windows (PowerShell):**
  ```powershell
  Copy-Item .env.example .env
  New-Item -ItemType Directory -Force -Path config/secrets
  Copy-Item config/secrets/enrollment.token.example config/secrets/enrollment.token
  ```
- **Linux / Bash:**
  ```bash
  cp .env.example .env
  mkdir -p config/secrets
  cp config/secrets/enrollment.token.example config/secrets/enrollment.token
  ```

To generate a real cryptographically secure enrollment token locally:
```bash
python -c "import secrets; print(secrets.token_hex(32))" > config/secrets/enrollment.token
```

### Step 5: Verify Setup with Automated Test Suite
```bash
# 1. Run fast unit tests (< 2s)
python scripts/run_tests.py --unit

# 2. Run multi-node simulation and protocol integration tests
python scripts/run_tests.py --integration
```

---

## 2. Dependency Management Audit & Architecture

### Core Design Philosophy: Zero External Runtime Dependencies
PersonalServer's controller, node agent, and job executor are engineered to run on Python's built-in standard library (`http.server`, `urllib`, `socket`, `subprocess`, `pathlib`, `json`, `sqlite3`/file persistence).
This eliminates dependency drift, enables lightweight deployment on Termux/Android and resource-constrained edge hardware, and ensures offline reproducibility.

### Dependency Manifests
| Manifest | Purpose | Key Constraints |
|---|---|---|
| [`requirements.txt`](file:///d:/Coding/Major%20Project/PersonalServer/requirements.txt) | Core runtime constraints | `pyyaml>=6.0.1,<7.0.0` (optional parser; built-in zero-dependency parser fallback included in `config/platform_config.py`) |
| [`requirements-dev.txt`](file:///d:/Coding/Major%20Project/PersonalServer/requirements-dev.txt) | Testing, linting, and CI | `pytest>=8.0.0,<10.0.0`, `ruff>=0.8.0,<1.0.0` |
| [`pyproject.toml`](file:///d:/Coding/Major%20Project/PersonalServer/pyproject.toml) | Modern project metadata & tool config | Python `>=3.9`, pytest markers (`unit`, `integration`, `slow`), ruff lint rules |

---

## 3. Automated Test Suite Commands

PersonalServer provides a unified CLI test runner [`scripts/run_tests.py`](file:///d:/Coding/Major%20Project/PersonalServer/scripts/run_tests.py):

### Unit Tests
Fast, pure in-memory tests with zero socket binding, no background daemon threads, and no external service requirements.
```bash
python scripts/run_tests.py --unit
```
*Coverage*: Configuration parsing, Windows/Linux path normalization, environment variable overrides, telemetry math, blueprint validation, image reference checking, and backup archive integrity.

### Integration Tests
Protocol boundary, lifecycle, and simulated multi-node tests.
```bash
python scripts/run_tests.py --integration
```
*Coverage*:
- Deterministic multi-node worker simulation (`SimulatedWorker` testing heartbeats, crashes, lease sweeps, and reconnections).
- Reliable job state machine transitions, crash recovery, and bounded exponential backoff.
- Image build, tagging, digest calculation, and deployment pipelines.
- Deboarding and node draining lifecycles.
- Storage routing and path traversal defense vectors.

### Run All Tests
```bash
python scripts/run_tests.py --all
```

### Test Isolation Principles
1. **Dynamic Ephemeral Ports (`port=0`)**: Test servers bind to OS-allocated ephemeral ports to eliminate `EADDRINUSE` port collisions.
2. **Temporary Sandboxes (`tempfile.mkdtemp`)**: All test databases (`nodes.json`, `jobs.json`) write into temporary directories cleaned up upon completion.
3. **Graceful Skip on External Dependencies**: Tests requiring a live physical device (e.g. Vivo Y31 phone on `100.85.108.5:8080`) or live production controller automatically detect reachability and report `SKIPPED` rather than failing.

---

## 4. Code Quality & Static Checks

Static checks focus on high-value safety (syntax errors, undefined variables, unused imports) without cosmetic noise:
```bash
python scripts/run_tests.py --lint
```
- If `ruff` is installed, it runs linting according to `[tool.ruff]` in `pyproject.toml`.
- If `ruff` is not installed, the runner automatically falls back to an embedded Python AST compilation check across all project Python files.

---

## 5. Separation of Runtime State, Logs, and Persistent Data

PersonalServer enforces a strict separation between source code, persistent configuration, and ephemeral runtime state:

```
PersonalServer/
├── controller/data/      <- [PERSISTENT] nodes.json, jobs.json, apps.json
├── config/secrets/       <- [SECRETS] enrollment.token, registry_credentials.json (gitignored)
├── storage/              <- [PERSISTENT] User uploaded files and application artifacts
├── runtime/              <- [EPHEMERAL] PIDs, runtime sockets, temporary worker state (gitignored)
├── logs/                 <- [EPHEMERAL] Controller and agent rotating log files (gitignored)
├── backups/              <- [BACKUPS] Compressed state tarballs (gitignored)
└── .pytest_cache/        <- [TEST ARTIFACTS] Pytest and linter caches (gitignored)
```

All directory paths can be redirected via environment variables (`PERSONALSERVER_BASE_DIR`, `PERSONALSERVER_DATA_DIR`, `PERSONALSERVER_SECRETS_DIR`, `PERSONALSERVER_RUNTIME_DIR`, `PERSONALSERVER_LOGS_DIR`, `PERSONALSERVER_STORAGE_DIR`).

---

## 6. State Backup, Verification, and Restoration

PersonalServer provides an automated backup and restoration utility [`scripts/backup_restore.py`](file:///d:/Coding/Major%20Project/PersonalServer/scripts/backup_restore.py).

### Create a Backup
Archives persistent databases, secrets, and storage into a compressed `.tar.gz` and writes a SHA256 sidecar:
```bash
python scripts/backup_restore.py backup --output backups/backup-$(date +%Y%m%d).tar.gz
```
*Note*: Automatically excludes runtime sockets, `.pid` files, logs, and temporary caches.

To exclude large application storage files:
```bash
python scripts/backup_restore.py backup --no-storage
```

### Verify an Archive
Verifies archive structure, embedded `manifest.json`, and computes SHA256 hashes of all internal members:
```bash
python scripts/backup_restore.py verify backups/backup-20261015.tar.gz
```

### Restore State
Restores persistent files into target directories:
```bash
# Test extraction safely (dry run)
python scripts/backup_restore.py restore backups/backup-20261015.tar.gz --dry-run

# Perform real restoration
python scripts/backup_restore.py restore backups/backup-20261015.tar.gz
```
*Security Defense*: The restore engine inspects all archive paths and strictly rejects path traversal sequences (`../`, absolute paths) attempting to escape destination directories.

---

## 7. Continuous Integration (CI)

Continuous integration is configured in [`.github/workflows/ci.yml`](file:///d:/Coding/Major%20Project/PersonalServer/.github/workflows/ci.yml).

### CI Matrix
- **Operating Systems**: `ubuntu-latest`, `windows-latest`
- **Python Versions**: `3.9`, `3.11`, `3.13`
- **Runner Secrets**: Zero credentials required; runner initializes safe `.example` templates.
- **Workflow Pipeline**:
  1. Dependencies installation (`pip install -r requirements-dev.txt`)
  2. Safe sample config generation
  3. Static code quality checks (`python scripts/run_tests.py --lint`)
  4. Unit test suite (`python scripts/run_tests.py --unit -v`)
  5. Multi-node simulation integration test suite (`python scripts/run_tests.py --integration`)
  6. Backup archive creation, verification, and dry-run restore validation

---

## 8. Concise Troubleshooting Guide

### 1. Port Conflict (`[WinError 10048]` / `EADDRINUSE`)
- **Symptom**: Controller fails to bind `0.0.0.0:8000` or router fails on `8088`.
- **Resolution**: Check running processes with `Get-Process python` (Windows) or `lsof -i :8000` (Linux). Terminate orphaned processes or override ports via `.env`:
  ```bash
  PERSONALSERVER_CONTROLLER_PORT=8001
  PERSONALSERVER_ROUTER_PORT=8089
  ```

### 2. Node Shows `OFFLINE` Immediately
- **Symptom**: Node registers successfully but scheduler marks it `OFFLINE` during job dispatch.
- **Resolution**: Check system clock synchronization. If node and controller have clock skew exceeding `PERSONALSERVER_HEARTBEAT_TIMEOUT` (default: 60s), the liveness check calculates an expired delta. Ensure NTP is active.

### 3. API Returns `401 Unauthorized`
- **Symptom**: `{"error": "Unauthorized"}` on controller endpoints.
- **Resolution**: Ensure `config/secrets/enrollment.token` exists and matches the `Authorization: Bearer <token>` header supplied in API calls or agent configurations.

### 4. Tests Skip with "PersonalServer controller is not running"
- **Symptom**: Legacy phase tests (`test_phase11b`, `test_phase12`, `test_phase13`) report `SKIPPED`.
- **Resolution**: This is expected behavior during offline or CI test execution. Those specific tests exercise a live cluster. To run them, launch the controller in a separate terminal (`python -m controller.controller`) before executing tests.
