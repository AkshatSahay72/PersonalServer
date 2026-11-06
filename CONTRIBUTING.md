# Contributing to PersonalServer

Thank you for your interest in contributing to PersonalServer!

PersonalServer is designed as a lightweight, robust, self-hosted personal computing and distributed cluster system. Because the project emphasizes reliability and clean agentic development, please adhere to the following guidelines:

---

## 1. Core Engineering Principles

1. **Zero External Runtime Dependencies**: The core controller, node agent, consensus manager, and storage subsystems run exclusively on the Python 3.9+ standard library. Do not add third-party runtime dependencies without prior discussion.
2. **Explicit Interfaces & Low-Cost Navigation**: Prefer modular, small files with explicit signatures and clear route registration. Avoid monolithic functions or hidden magic.
3. **Local-First & Privacy Conscious**: Never add telemetry or features requiring external third-party cloud services. Inter-node networking relies on private WireGuard / Tailscale meshes.
4. **Preserve Working Functionality**: Refactor incrementally in small, testable batches. Never break existing APIs, response formats, or serialization schemas.

---

## 2. Development & Verification Workflow

1. **Environment Setup**:
   ```bash
   python -m venv .venv
   # Windows: .\.venv\Scripts\Activate.ps1
   # Linux/macOS: source .venv/bin/activate
   pip install -r requirements-dev.txt
   ```

2. **Run Tests Offline**:
   All automated tests run completely offline without contacting physical nodes or external network resources.
   ```bash
   # Run fast unit tests
   python scripts/run_tests.py --unit

   # Run integration tests
   python scripts/run_tests.py --integration

   # Run full test suite
   python scripts/run_tests.py --all

   # Check AST syntax and code quality
   python scripts/run_tests.py --lint
   ```

3. **Detailed Guides**:
   - [Development & Testing Guide](docs/development.md)
   - [Reproducible Setup & Backups](docs/reproducible_setup.md)
   - [Troubleshooting Runbook](docs/troubleshooting.md)
