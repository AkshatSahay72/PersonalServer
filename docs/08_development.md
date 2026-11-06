# 08 — Development Guide: Architecture, Testing & Contributions

> **Reading Order**: **Step 8 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [07 — Monitoring & Troubleshooting Guide](07_monitoring_and_troubleshooting.md)  
> **Next Guide**: [09 — Architecture Reference](09_architecture_reference.md)  
> **Audience**: Contributors, core developers, and test engineers.  
> **Prerequisites**: Python 3.10+ development environment and a cloned repository.

---

## 1. Repository Layout

PersonalServer adheres to a modular, decoupled codebase architecture:

```
PersonalServer/
├── agent/                  # Edge node daemon, probers, and runtime execution adapters
│   ├── runtime_adapters/   # Python, Node.js, Static, and Container execution adapters
│   ├── capability_prober.py# Hardware and binary probe routines
│   ├── job_executor.py     # Attempt execution, process isolation, and timeout enforcement
│   └── node-agent.py       # Main worker supervisor daemon
├── config/                 # HA consensus manager, node telemetry, and system constants
├── controller/             # Master controller API, stores, CLI, and state machines
│   ├── http/               # Modular REST route handlers (jobs, apps, HA, nodes)
│   ├── stores/             # Durable JSON databases with atomic read-modify-write locks
│   └── deployment_manager.py # Application deployment state machine (R1-R9)
├── docs/                   # Numbered user guides and technical documentation
│   └── reference/          # Normative specifications (J1-J3, D0)
├── router/                 # Dynamic reverse proxy, subpath router, and ingress gateway
├── scheduler/              # Universal Scheduler (J3 8-stage eligibility pipeline)
├── scripts/                # Diagnostic doctor, test runner, and worker launchers
├── services/node-api/      # Web Operations Console (API backend and responsive UI)
└── tests/                  # Offline automated test suites (unit, deployment, HA, UI)
```

---

## 2. Setting Up the Development Environment

PersonalServer requires **zero third-party dependencies** for core operation, using only standard library modules.

For running test suites and linting:
```bash
# Create a local virtual environment
python -m venv .venv

# Activate virtual environment
# On Linux / macOS / Termux:
source .venv/bin/activate
# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# Install testing tools
pip install pytest
```

---

## 3. Running Automated Tests

All automated test suites run **completely offline** with isolated mock environments and loopback socket servers:

```bash
# Run fast unit tests (< 5s)
python -m pytest tests/unit/ -v

# Run deployment and runtime adapter tests
python -m pytest tests/deployment/ -v

# Run High Availability consensus and failover tests
python -m pytest tests/ha/ -v

# Run Web UI and Workload Control tests
python -m pytest tests/ui/ -v

# Run the unified test runner
python scripts/run_tests.py --all
```

---

## 4. Coding Standards & Principles

1. **Standard Library First**: Core controller, agent, and storage logic must remain functional without third-party pip dependencies.
2. **Android Termux Compatibility**: Never assume standard desktop Linux paths (`/bin/bash`, `/usr/bin/python`). Always use dynamic environment resolution (`shutil.which()`, `os.environ.get("PREFIX")`).
3. **Atomic File Persistence**: Never write directly to persistent JSON databases. Always use `atomic_save_json()` under `DB_LOCK` to ensure crash-safe atomic renaming.
4. **Process Group Supervision**: When terminating processes, always kill the entire process tree using `os.killpg()` or taskkill to prevent orphaned child daemons.

---

## 5. Adding a New Runtime Adapter

To support a new application runtime (e.g. Go, Rust, Ruby):
1. Create a new adapter in `agent/runtime_adapters/<runtime>_adapter.py`.
2. Inherit from `BaseRuntimeAdapter` in `agent/runtime_adapters/base.py`.
3. Implement `prepare_release()`, `start()`, `health_check()`, and `stop()`.
4. Register the adapter in `agent/runtime_adapters/__init__.py`.
5. Add unit and lifecycle tests under `tests/deployment/`.

---

## 6. Next Steps

Review the high-level architecture component map and links to frozen specifications:

👉 **Continue to [09 — Architecture Reference](09_architecture_reference.md)**
