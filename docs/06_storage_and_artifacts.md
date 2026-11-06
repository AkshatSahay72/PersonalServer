# 06 — Storage & Artifacts Guide: File Exchange & Integrity

> **Reading Order**: **Step 6 of 9** in the PersonalServer User Guide sequence.  
> **Previous Guide**: [05 — High Availability Guide](05_high_availability.md)  
> **Next Guide**: [07 — Monitoring & Troubleshooting Guide](07_monitoring_and_troubleshooting.md)  
> **Audience**: Users managing application files, data persistence, and job build artifacts.  
> **Prerequisites**: A functional PersonalServer controller and storage directory.

---

## 1. Storage Architecture Overview

PersonalServer provides a decentralized, sandboxed storage subsystem (`storage/`) designed to safely manage files across heterogeneous edge devices without exposing the host operating system.

### Key Storage Subsystems:
1. **Application Storage (`storage/applications/<app_id>/`)**: Persistent volumes mounted into running applications (databases, static media, user uploads).
2. **Artifact Repository (`storage/artifacts/`)**: Cryptographically verified build packages, binary distributions, and job output archives.
3. **Multi-Node Storage Explorer (`/api/storage`)**: Distributed file management API enabling operators to browse and transfer files across phone storage via the Web UI.

---

## 2. Path Traversal & Security Defenses

Edge devices running servers must protect private phone files (photos, SMS, private keys). PersonalServer enforces strict directory boundaries:
- **Canonical Path Sandboxing**: All requested file paths are resolved against the sandbox root using `os.path.realpath()`. Any attempt to access `/etc/passwd`, `~/.ssh`, or `../` produces an immediate `403 Forbidden` response.
- **Symlink Jail**: Symlinks pointing outside the application storage root are automatically detected and rejected.

---

## 3. Managing Application Artifacts

When an application is packaged or a compute job generates outputs, PersonalServer tracks artifacts using an immutable cryptographic model:

```
[Artifact ID / Filename] ➔ [SHA-256 Digest] ➔ [Size Bound Verification] ➔ [Durable Commit]
```

### Uploading an Artifact:
```bash
python controller/cli.py artifacts upload \
  --file ./datasets/sensor_data.csv \
  --app-id "sensor-collector"
```
**Output:**
```text
[OK] Artifact uploaded successfully:
     SHA-256: d2d2d2d2f78b8bc9e1...
     Size: 4.2 MB
     Path: storage/artifacts/sensor_data.csv
```

### Downloading an Artifact:
```bash
python controller/cli.py artifacts download \
  --name "sensor_data.csv" \
  --output ./downloaded_sensor_data.csv
```

---

## 4. Job Output Artifact Discovery (J5 Result Management)

When running distributed batch jobs (from Guide 04), declared outputs are automatically ingested into cluster storage:

1. **Declared Match**: The worker monitors the job working directory for files matching declared output names or globs.
2. **Size Bounds Verification**: Files exceeding maximum bounds (50MB per individual file or 200MB per attempt) are rejected with a descriptive error to protect smartphone flash storage.
3. **Two-Phase CAS Promotion**: The worker computes the SHA-256 checksum locally, uploads the file to the controller's staging area, and promotes it atomically under database lock.

---

## 5. Storage Retention & Automated Cleanup

To prevent smartphone flash storage from filling up:
- **Completed Job Workspaces**: Ephemeral execution attempt folders (`runtime/attempts/attempt-*`) are automatically purged 1 hour after job completion.
- **Old Application Releases**: The deployment manager retains the **last 5 releases** for each application; older inactive release tarballs are purged during the next deployment.

### Running Manual Storage Cleanup:
```bash
python controller/cli.py storage cleanup --older-than 7d
```

---

## 6. Next Steps

Learn how to monitor node thermals, inspect logs, and use the automated self-healing diagnostic framework:

👉 **Continue to [07 — Monitoring & Troubleshooting Guide](07_monitoring_and_troubleshooting.md)**
