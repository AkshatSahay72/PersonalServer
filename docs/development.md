# Development & Testing Guide

This guide covers reproducible local development workflows, code standards, automated verification, and testing for PersonalServer.

For complete, detailed instructions on clean checkout setup, dependency pinning, cross-platform paths, CI, and state backup/restoration, see the comprehensive [Reproducible Development, CI, and Backup Guide](file:///d:/Coding/Major%20Project/PersonalServer/docs/reproducible_setup.md).

---

## 1. Development Principles

1. **Dependency-Free Core**: The controller and agent nodes run purely on the Python 3.9+ standard library. Never introduce heavy third-party frameworks (Redis, Celery, Kubernetes) when standard Python libraries suffice.
2. **Safety & Zero Secrets**: Never expose private credentials, hardcode tokens, or open public unauthenticated ports.
3. **Deterministic & Isolated Testing**: All tests must run in isolated temporary sandboxes with dynamic ephemeral ports and deterministic mock workers without depending on physical hardware.

---

## 2. Quick Local Commands

### Environment Setup
```bash
python -m venv .venv
# Activate on Windows: .\.venv\Scripts\Activate.ps1
# Activate on Linux:   source .venv/bin/activate
pip install -r requirements-dev.txt

# Copy safe configuration templates
cp .env.example .env
mkdir -p config/secrets
cp config/secrets/enrollment.token.example config/secrets/enrollment.token
```

### Automated Testing
```bash
# 1. Fast isolated unit tests (pure in-memory, < 2s)
python scripts/run_tests.py --unit

# 2. Multi-node worker simulation & integration tests
python scripts/run_tests.py --integration

# 3. Complete test suite
python scripts/run_tests.py --all

# 4. Static code quality and syntax checks
python scripts/run_tests.py --lint
```

### State Backup and Restoration
```bash
# Create state backup archive
python scripts/backup_restore.py backup --output backups/backup-$(date +%Y%m%d).tar.gz

# Verify backup integrity (SHA256 & manifest)
python scripts/backup_restore.py verify backups/backup-$(date +%Y%m%d).tar.gz

# Restore state
python scripts/backup_restore.py restore backups/backup-$(date +%Y%m%d).tar.gz
```
