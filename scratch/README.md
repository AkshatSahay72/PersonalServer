# Scratch & Live Diagnostic Scripts
===================================

The `scratch/` directory contains development-only scripts and live hardware diagnostic utilities used during testing on physical nodes (such as Android devices running Termux over Tailscale).

> [!WARNING]
> Scripts in this directory target live physical cluster endpoints (e.g. `100.85.108.5:8000`) and are intended exclusively for manual hardware verification.
> **They are not part of the automated unit or integration test suites.**

---

### File Inventory & Purpose

| Script | Purpose |
|---|---|
| `check_cluster.py` | Quick HTTP connectivity check against direct controller endpoint. |
| `claim_job.py` | Manual worker job lease claim and polling diagnostic. |
| `test_live_disposable_node.py` | Manual live-node lifecycle test (enrollment, heartbeat, deboarding). |
| `test_phase10b_onboarding.py` | Manual onboarding code verification against live controller. |
| `test_phone_storage.py` | Manual storage endpoint test on mobile node. |
| `test_remote_security.py` | Manual traversal and unauthenticated endpoint probe on remote node. |
| `test_remote_writes.py` | Manual multi-node write test. |
| `test_v10_full.py` | Full live cluster integration check across physical devices. |
| `test_v10_multinode_recovery.py` | Multi-node failover and recovery test on physical cluster. |

---

### Automated Test Suites

For automated testing, use the canonical test runner:

```bash
# Fast unit tests (pure in-memory, < 3s)
python scripts/run_tests.py --unit

# Simulated multi-node and lifecycle integration tests
python scripts/run_tests.py --integration

# Complete automated test suite
python scripts/run_tests.py --all
```
