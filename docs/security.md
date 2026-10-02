# Security Model & Threat Assessment (v1.0)

This document outlines the security architecture, authentication mechanisms, and defenses implemented across PersonalServer.

---

## 1. Secrets Management

1. **No Tracked Secrets**:
   - `config/secrets/enrollment.token`
   - `config/secrets/auth.token`
   - `config/secrets/cloudflare.token`
   - `runtime/registration.json`
   - `runtime/*.pid`
   - `controller/data/`
   All sensitive files are strictly Git-ignored and excluded from version control.
2. **Permission Sandboxing**:
   Secrets directories are protected with `0700` and secret files with `0600` permissions.

---

## 2. Authentication & Authorization

- **Enrollment Token**: 48-character cryptographic hex token required for initial node registration and administrative job submissions.
- **Per-Node Tokens**: Upon enrollment, each node receives a unique 40-character token used for authenticating heartbeats and fetching assigned jobs.
- **Revocation**: Removing a node via `POST /nodes/<node_id>/remove` instantly wipes its token in the Controller.
- **Web UI & Storage Auth**: Protected via token authorization and compatible with Cloudflare Access identity headers.

---

## 3. Threat Matrix & Defenses

| Threat Vector | Defense Mechanism |
|---|---|
| **Path Traversal (`../`)** | Strict canonical normalization and `os.path.commonpath` verification against `STORAGE_ROOT`. |
| **Direct System Access** | Node API rejects any path outside `~/PersonalServer/storage/`. |
| **Command Injection in Workloads** | Strict allowlist of safe workload types (`agent/job_executor.py`). Arbitrary shell execution is forbidden. |
| **Public Port Scanning** | SSH (`:8022`) and Controller (`:8000`) are accessible only over Tailscale. |
| **Memory Exhaustion (DoS)** | 100MB upload limits, streaming disk writes, 64KB log buffer caps. |
