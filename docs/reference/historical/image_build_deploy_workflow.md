# Reproducible Image Build & Deployment Workflow

This document specifies the 8-stage image build, tagging, verification, scheduling, execution, and rollback pipeline in **PersonalServer**, hardened for local development on Windows PCs and portable toward Raspberry Pi OS (ARM64) and compatible Termux workers.

---

## 1. Pipeline Architecture (Separated Stages)

In PersonalServer, image building and container execution are **strictly decoupled**. The pipeline does not conflate build and execution into an opaque operation.

```
[ Stage 1: Source Checkout / Snapshot ]
                  │
                  ▼
[ Stage 2: Decoupled Image Build ] ──► (Uses Buildx / Docker CLI / Deterministic Snapshot)
                  │
                  ▼
[ Stage 3: Image Tagging & Metadata ] ──► (Computes immutable sha256:... digest & architecture)
                  │
                  ▼
[ Stage 4: Publication to Store / Registry ] ──► (Persists to storage/images/ or remote registry)
                  │
                  ▼
[ Stage 5: Image Selection & Arch Check ] ──► (Validates OCI syntax & node compatibility)
                  │
                  ▼
[ Stage 6: Worker Scheduling ] ──► (Selects worker matching architecture & container runtime)
                  │
                  ▼
[ Stage 7: Pull & Sandboxed Execution ] ──► (Enforces no docker.sock, no host mounts, resource limits)
                  │
                  ▼
[ Stage 8: Health Check & Rollback ] ──► (Probes endpoint; on failure, preserves/restores previous deployment)
```

### Stage 1: Source Checkout & Snapshot
- Application sources reside under `storage/applications/<app_id>/source`.
- When fetching from GitHub or local snapshot, a deterministic directory tree is created with commit SHA metadata.
- Traversal outside the application sandbox is strictly blocked.

### Stage 2: Decoupled Image Build (`image-build` workload)
- The build stage is an independent, allowlisted workload (`image-build`).
- Parameters: `source_dir`, `dockerfile`, `image_tag`, `platform`, `build_args`.
- Executes `docker build` (or `docker buildx build`) with multi-platform flags when Docker is running, or computes a deterministic source snapshot when testing offline.
- **Never launches an application container.**

### Stage 3: Image Tagging & Immutable Metadata
- Generates versioned tags (e.g. `personalserver/<app_name>:v<num>`).
- Extracts or computes immutable cryptographic digests: `sha256:<64-hex-digits>`.
- Formulates the canonical immutable reference: `personalserver/<app_name>:v1@sha256:...`.
- Records architecture (`amd64`, `arm64`), created timestamp, and image layer size.

### Stage 4: Publication to Local Image Store or Registry
- **Offline / Local Development:** Saves image metadata to `storage/images/<sanitized_repo>_<tag>.json`.
- **Remote Registries (Docker Hub, GHCR):** Credentials are kept in the secure local environment (`config/secrets/registry_credentials.json` or local environment variables), **never** logged or committed to source control.

### Stage 5: Image Selection & Architecture Compatibility
- Subsystem `controller/image_inspector.py`:
  - `validate_image_reference`: Validates syntax, parses repository, tag, and immutable digest. Rejects shell injection tokens (`$`, `;`, `|`, `` ` ``, `<`, `>`, leading `-it`).
  - `is_architecture_compatible`: Normalizes aliases (`aarch64` -> `arm64`, `x86_64` -> `amd64`) and checks compatibility.
  - `detect_image_architectures`: Checks local image store first, then registry API, with safe fallback.

### Stage 6: Worker Scheduling Based on Runtime Capabilities
- Subsystem `scheduler/scheduler.py`:
  - `ResourceScheduler.select_node`:
    - Evaluates node liveness, CPU cores, memory, and storage.
    - Matches required capabilities: `container_runtime:docker` or `container_runtime:udocker`.
    - Matches required image architecture: Rejects worker if node architecture does not support the image.
    - Excludes draining, deboarding, offline, or removed workers.

### Stage 7: Pull & Sandboxed Execution (`docker-deploy` workload)
- Parameters: `image`, `image_digest`, `architecture`, `container_name`, `host_port`, `container_port`, `env`, `cpu_limit`, `memory_limit`, `health_check`.
- **Security Boundary**:
  - Arbitrary host volume mounts (`volumes`, `mounts`) are rejected with `ValueError`.
  - Privileged execution (`--privileged`) is disabled.
  - Environment variables containing `/var/run/docker.sock` or `docker.sock` are blocked.
  - Sanitized environment variables passed via `-e KEY=VALUE` without shell interpolation.

### Stage 8: Health Checks, Logs, Stop/Restart, and Rollback
- **Health Checks:** Probes container endpoint (`http://127.0.0.1:<host_port><path>`). If the container crashes or health check times out:
  - Captures container logs for diagnostics.
  - Automatically cleans up the failed container (`docker rm -f`).
  - Marks deployment `FAILED`.
- **Rollback Strategy:**
  - Preserves the previous working deployment (`status: "SUCCESS"`).
  - Automatically identifies `previous_working_deployment` so service state is not lost.
  - Provides endpoint `POST /apps/<app_id>/rollback`:
    - Can target a specific deployment ID or automatically roll back to the last known-good deployment.
    - Re-deploys the previous immutable image digest and configuration.

---

## 2. Runtime Capability & Security Guidelines

| Target Environment | Runtime Support | Architecture | Security & Capability Notes |
|---|---|---|---|
| **Windows Dev PC** | Real Docker CLI / Buildx / LocalWorker | `amd64` | Native Docker if engine active. Fast local image store for offline testing. |
| **Raspberry Pi OS** | Docker Engine | `arm64` | Native Linux containers, true namespaces, cgroups, bridge networking. |
| **Termux (Android)** | `udocker` (PRoot / userspace) | `arm64` / `armv7` | **Not equivalent to native Docker.** Runs unprivileged userspace emulation. Cannot bind ports <1024. Ordinary Docker images with kernel requirements are **not guaranteed** to run on unrooted Android. |

> [!CAUTION]
> **Docker Socket Access:** Never mount `/var/run/docker.sock` into application containers. Doing so grants root host execution rights and bypasses all sandbox boundaries.

---

## 3. End-to-End Local Development Workflow

### Step 1: Build the Image from Source
```bash
# Queue image-build job (decoupled stage)
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "build-sample-app",
    "type": "image-build",
    "parameters": {
      "app_id": "sample-app",
      "app_name": "sample-app",
      "source_dir": "storage/applications/sample-app/source",
      "dockerfile": "Dockerfile",
      "image_tag": "personalserver/sample-app:v1.0.0",
      "platform": "linux/amd64"
    }
  }'
```
*Output metadata includes immutable digest:* `sha256:4c72e276...`.

### Step 2: Deploy Image with Health Check
```bash
curl -X POST http://127.0.0.1:8000/apps/sample-app/deploy \
  -H "Authorization: Bearer <TOKEN>"
```

### Step 3: Rollback a Failed Deployment
```bash
# Rollback to the previous successful deployment
curl -X POST http://127.0.0.1:8000/apps/sample-app/rollback \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## 4. Test Execution & Reporting Matrix

The test suite in [`tests/test_image_build_deploy.py`](file:///d:/Coding/Major%20Project/PersonalServer/tests/test_image_build_deploy.py) tests all failure and success paths:

| Test Case | Scope | Execution Mode | Verification |
|---|---|---|---|
| `test_01_image_reference_and_immutable_digest_validation` | Stage 3, 5 | Real unit parser | Validates tags, sha256 digests, and rejects shell injection. |
| `test_02_decoupled_image_build_workload` | Stage 2, 4 | Real Docker / Local Store | Verifies build runs independently; computes digest & metadata. |
| `test_03_registry_failure_handling` | Stage 4, 5 | Real network & mock | Gracefully handles offline or unreachable registries. |
| `test_04_architecture_compatibility_and_rejection` | Stage 5, 6 | Real executor | Rejects ARM64 image on AMD64 worker node with clear error. |
| `test_05_missing_container_runtime_rejection` | Stage 6, 7 | Real executor | Returns clear diagnostic when no container runtime exists. |
| `test_06_docker_socket_and_host_mount_protection` | Stage 7 (Security) | Real executor | Rejects volume mounts, privileged mode, and `docker.sock`. |
| `test_07_env_var_injection_and_secret_masking` | Stage 7 | Real parser | Injects env vars safely and masks secret values in logs & APIs. |
| `test_08_health_check_timeout_handling` | Stage 8 | Real HTTP probe | Verifies failing health probe returns HTTP 503 and times out. |
| `test_09_container_stop_and_restart_operations` | Stage 8 | Real executor | Validates container identifier safety during stop/restart. |
| `test_10_rollback_strategy_preserves_previous_working_deployment` | Stage 8 | Real controller logic | Preserves previous working deployment and restores image/digest. |
| `test_11_multi_platform_build_metadata_and_arm64_selection` | Architecture | Real Scheduler & Metadata | Tests ARM64 selection vs AMD64 rejection on multi-node cluster. |
| `test_12_execution_strategy_report` | Diagnostic | Reporting | Formulates JSON execution report of real vs simulated tooling. |
