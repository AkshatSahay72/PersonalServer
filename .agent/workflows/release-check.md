# Workflow: Release Quality Check

Use this workflow prior to tagging, releasing, or deploying a milestone or APK.

---

## Pre-Release Checklist

1. **Codebase Hygiene:**
   - [ ] No uncommitted or untracked temporary debug files.
   - [ ] No hardcoded passwords, tokens, or private keys.
   - [ ] Version numbers synchronized across `android/native-bridge/build.gradle` (`versionCode`, `versionName`) and `config/release.conf`.

2. **Automated Verification:**
   - [ ] All unit tests pass cleanly:
     ```bash
     python -m pytest tests/unit/
     ```
   - [ ] Code formatting and syntax checks pass without errors.

3. **Physical / Device Gate:**
   - [ ] APK compiled successfully via `gradlew assembleDebug`.
   - [ ] SHA-256 checksum recorded.
   - [ ] Physical installation and smoke test executed on the target device.
   - [ ] Node boots, joins Tailscale, and responds to controller heartbeats.

4. **Documentation Synchronization:**
   - [ ] `docs/CURRENT-STATUS.md` updated with latest build version and test proof.
   - [ ] `docs/ROADMAP.md` milestone status marked accurately.
