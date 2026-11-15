# PersonalServer — Verification & Test Plan

**Version:** 1.0.0  
**Status:** Mandatory Quality Gate  
**Last Updated:** November 15, 2026  

---

## 1. Test Strategy Overview

PersonalServer enforces a **Multi-Tier Quality Gate**. No feature, bug fix, or milestone can be marked complete based on automated tests alone; it must be backed by live hardware evidence.

| Test Tier | Scope | Target Environment | Frequency |
| :--- | :--- | :--- | :--- |
| **Tier 1: Unit & Offline Tests** | Module logic, schemas, algorithms, lifecycle stubs | Local workstation / CI runner | Every code modification |
| **Tier 2: Physical Device Smoke Test** | Real PID, listening socket, HTTP 200 payload | Physical Android phone (Vivo V2334) | Before APK release or deployment |
| **Tier 3: Live End-to-End Stream Test** | Multipart MJPEG stream, 720p JPEG payload, camera flip | Physical phone over Tailscale | Before milestone completion |
| **Tier 4: Cluster HA & Failover Test** | Master election, state replication, lease timeout | Remote cluster nodes (Y31 & R5P) | Major control plane releases |

---

## 2. Test Execution Commands

### 2.1 Unit & Offline Test Execution
Run the full unit test suite:
```bash
# Run core unit tests
python -m pytest tests/unit/ -v

# Run lifecycle, live call, and bootstrapper tests
python -m pytest tests/unit/test_native_android_bridge_lifecycle.py tests/unit/test_e07_live_call.py tests/unit/test_bootstrapper.py -v

# Run HA consensus tests
python -m pytest tests/ha/ -v
```

### 2.2 Physical Device Verification Commands
Execute on target Android node (Termux):
```bash
# 1. Verify process health
pgrep -fl python
# Expected: Node API and Node Worker PIDs active

# 2. Verify port listeners
ss -tlpn
# Expected: 8080 (Node API), 8765 (LocalBridgeServer), 8766 (MediaStreamServer)

# 3. Verify health endpoint
curl -s http://127.0.0.1:8080/health
# Expected: {"status": "ok", ...} with HTTP 200

# 4. Execute physical CCTV verification script
python tests/integration/test_vivo_cctv_streaming.py
# Expected: 7/7 checks passed (Start -> Status -> Frame -> Stream -> Flip -> Stop -> IDLE)
```

---

## 3. Physical Camera & CCTV Acceptance Checklist

For any camera streaming release, verify:
- [ ] Camera opens cleanly without `ERROR_CAMERA_IN_USE`.
- [ ] Stream delivers continuous multipart frames (`--frame`) without socket drop.
- [ ] Retrieved frames start with JPEG SOI (`\xff\xd8`) and end with EOI (`\xff\xd9`).
- [ ] Stream resolution is 1280x720 (or chosen preset) and bandwidth is bounded to 500–800 kbps.
- [ ] Camera switches between rear (`camera_id: "0"`) and front (`camera_id: "1"`) within 2.0s.
- [ ] Screen can be turned off while video continues streaming (WakeLock verified).
- [ ] When the viewer tab is closed, camera hardware releases to `IDLE` within 3.0s.
