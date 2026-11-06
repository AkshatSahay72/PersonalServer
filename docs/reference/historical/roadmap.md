# PersonalServer Evolution & Roadmap

PersonalServer has evolved from an initial edge access prototype into an enterprise-grade, high-availability distributed edge computing platform.

---

## 1. Milestone Summary

### v0.1 – v0.5: Edge Access & Supervisor Core (Completed)
- Unprivileged Android Termux environment supervisor.
- Tailscale WireGuard mesh networking & Cloudflare Tunnel ingress.
- Basic hardware discovery (`nproc`, `uptime`, `free`, `df`).

### v0.6 – v0.7: Multi-Node Clustering & Security (Completed)
- Multi-node registration and identity persistence.
- Zero-touch single-use onboarding pairing codes (`PS-XXXX-XXXX`).
- Cryptographic node auth tokens and secret redaction.

### v0.8 – v0.9: Workload Execution & Scheduling (Completed)
- Sandboxed allowlisted workload executor (`agent/job_executor.py`).
- Resource-aware multi-factor fitness scheduler (`scheduler/scheduler.py`).
- Atomic lease claiming and at-least-once failure recovery.

### v1.0: Operations Web Console & Multi-Node Storage (Completed)
- Modern, high-density Web Operations Console.
- Sandboxed decentralized multi-node storage manager with path-traversal defenses.
- Offline node protection and read-only fallback mode.

### v1.1: Container Platform & Reproducibility (Completed)
- Dynamic Application Router (`router/app_router.py`) with SSRF & traversal defenses.
- Decoupled 8-stage image build and deployment pipeline (`controller/image_inspector.py`).
- Architecture normalization (`arm64`/`amd64`) and Docker capability matching.
- Hardware-independent simulation suite (`agent/simulated_worker.py`).
- Portable FHS configuration mapping for Linux/Raspberry Pi OS (`config/platform_config.py`).
- State backup, verification, and restoration pipeline (`scripts/backup_restore.py`).

### v1.2: High Availability & Rich Node Telemetry (Current)
- Zero-external-dependency High Availability (HA) & Master Failover (`config/ha_manager.py`).
- Atomic on-disk & in-memory leader leases and term consensus.
- Automated master election and split-brain immunity.
- Full cluster database snapshot replication (`/ha/sync`).
- Transparent client and worker redirection on master failure.
- Cross-platform Rich Device Telemetry (`config/node_telemetry.py`): battery percentage, charging state, WiFi signal strength (dBm), link speed, CPU temperature, and thermal throttling detection.

---

## 2. Future Directions

1. **Distributed Shared Storage (Storage Federation)**:
   - Chunk-level file deduplication across heterogeneous storage nodes.
   - Erasure coding for high-redundancy backup across edge devices.
2. **WebSocket Real-Time Log Streaming**:
   - Streaming live stdout/stderr from executing containers and workloads into the Web UI.
3. **Hardware Acceleration Detection**:
   - Automated detection and scheduling of Android NNAPI, Qualcomm Hexagon DSP, and Raspberry Pi VideoCore GPU workloads.
4. **Peer-to-Peer Worker Gossip**:
   - Direct worker-to-worker data transfer for large image layers without routing through the master.
