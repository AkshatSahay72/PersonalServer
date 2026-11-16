# Feature: Universal Node Bootstrapper

**Subsystem:** Automated Multi-Platform Provisioning  
**Location:** `bootstrap/`, `bootstrap.sh`  
**Current Version:** 0.1.0 (Verified)  

---

## 1. Overview & Capabilities

The Universal Node Bootstrapper transforms any supported edge device into a PersonalServer node with a single command. It automatically resolves system architecture, detects package managers, installs required runtime dependencies, configures the environment, and pairs with the controller.

---

## 2. Script Architecture

```text
bootstrap/
├── detect.sh         # Probes OS, platform, CPU architecture, RAM, and package managers
├── dependencies.sh   # Installs Python 3, Git, Curl, OpenSSL, and builds venv
├── install.sh        # Creates directory layouts, permissions, and log paths
├── configure.sh      # Applies node identity, pairing tokens, and network configs
└── uninstall.sh      # Cleanly tears down services, removes venvs, and unregisters node
```

### Supported Platforms:
1. **Android / Termux:** Unprivileged userspace detection; installs packages via `pkg`.
2. **Linux (Debian / Ubuntu / Raspberry Pi OS):** Detects `apt`; provisions systemd or user services.
3. **Arch Linux:** Detects `pacman`.

---

## 3. Usage & Verification

Run the unified entrypoint:
```bash
# Run automatic platform detection
bash bootstrap/detect.sh

# Complete one-command installation and enrollment
bash bootstrap.sh --pair <PAIRING_CODE> --controller <CONTROLLER_URL>
```

Verified via automated test suite: `tests/unit/test_bootstrapper.py` (6/6 tests passing).
