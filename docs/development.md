# Development & Testing Guide (v1.0)

This guide covers local development workflows, code standards, and verification testing for PersonalServer.

---

## 1. Development Principles

1. **Keep it Incremental**: Never introduce large third-party frameworks (Docker, Redis, Celery, Kubernetes) when standard Python libraries suffice.
2. **Safety First**: Never expose private credentials or open public SSH ports.
3. **Deterministic Testing**: All scheduler and lease algorithms must produce explainable, testable outcomes.

---

## 2. Running Test Suites

PersonalServer includes automated test suites in `scratch/`:

```bash
# Run full v1.0 end-to-end verification (Real Vivo Y31 + Controller + Storage + Web UI)
python scratch/test_v10_full.py

# Run multi-node lease recovery and token protection test
python scratch/test_v10_multinode_recovery.py
```
