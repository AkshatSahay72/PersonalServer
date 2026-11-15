# PersonalServer — Coding Standards & Engineering Rules

These standards are mandatory for all modifications to the PersonalServer repository.

---

## 1. Minimal-Change Principle & Refactoring

1. **Targeted Bug Fixes:** When resolving a defect, identify the root cause and apply the minimal sufficient diff. Do not refactor adjacent code during a bug fix turn.
2. **Dedicated Modularization:** The user has explicitly authorized decomposing oversized modules (e.g. `services/node-api/app.py`). However, modularization must:
   - Extract single-responsibility modules (e.g. `camera_routes.py`, `auth_middleware.py`, `proxy_handler.py`).
   - Retain complete backward compatibility with existing route contracts and endpoints.
   - Run the full test suite (`pytest`) before and after refactoring to prove zero regressions.
3. **No Unneeded Dependencies:**
   - Standard library Python (`urllib.request`, `http.server`, `json`, `sqlite3` only if local, `subprocess`, `threading`, `socket`) is the core baseline.
   - Do not add third-party packages to `requirements.txt` without explicit architectural justification and user approval.

---

## 2. Python Standards

1. **Python Compatibility:** Code must execute cleanly on Python 3.9 through Python 3.13 across Linux (Termux/Ubuntu), macOS, and Windows.
2. **Explicit Type Hints & Docstrings:** All public functions, dataclasses, and class methods must include clear type hints and brief docstrings explaining input contracts and return values.
3. **Thread Safety & Atomic Persistence:**
   - State mutations in JSON stores must use atomic file swaps (write to `.tmp`, flush/fsync, then `os.replace`).
   - Synchronize concurrent access using `threading.Lock` with minimal lock hold times.
4. **Error Handling & Resilience:**
   - Never use bare `except:`. Catch specific exceptions (`ConnectionRefusedError`, `TimeoutError`, `KeyError`, etc.).
   - Log warnings with actionable diagnostic context (node ID, attempted IP, port, timestamp).

---

## 3. Android & Java Standards

1. **API Level Targets:**
   - Minimum SDK: API 26 (Android 8.0)
   - Target SDK: API 34+ (Android 14 / Android 16 compatibility)
2. **Hardware Resource Lifecycle:**
   - Never leave Camera2 devices or AudioRecord instances open in the background.
   - Always acquire WakeLocks with descriptive tags (`PersonalServer:CCTVSessionWakeLock`) and ensure `release()` is called in `finally` or explicit teardown blocks.
   - Guard asynchronous callbacks (`onOpened`, `onDisconnected`) against race conditions where a session has been cancelled prior to callback arrival.
3. **Foreground Service Declarations:**
   - Android 14+ requires explicit `foregroundServiceType` flags (`camera`, `microphone`, `specialUse`). Maintain proper declarations in `AndroidManifest.xml` and call `startForeground` with the required service type flags.

---

## 4. Shell & Scripting Standards

1. **POSIX & Termux Compatibility:**
   - Shell scripts in `scripts/` and `bootstrap/` must run under bash/sh across Termux (`/data/data/com.termux/files/usr/bin/bash`) and standard Linux (`/bin/bash`).
   - Use dynamic shebangs (`#!/usr/bin/env bash`) or detect Termux prefix via `$PREFIX`.
   - Never assume root (`sudo`) is available. Handle unprivileged userspace gracefully.
