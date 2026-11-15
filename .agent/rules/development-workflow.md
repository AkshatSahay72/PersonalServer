# PersonalServer — Development Workflow & Quality Gates

---

## 1. Feature & Bugfix Lifecycle

Every non-trivial modification to PersonalServer must adhere to this structured flow:

```text
1. IDEA
   Define the operational objective or problem statement.
   ↓
2. REQUIREMENTS DISCUSSION
   Clarify expectations with the project owner. Check existing ADRs in docs/DECISIONS.md.
   ↓
3. SPECIFICATION & PRD UPDATE
   Assign a traceable requirement ID (e.g. CAM-001, NODE-005) in docs/PRD.md and docs/REQUIREMENTS.md.
   ↓
4. ARCHITECTURE REVIEW
   Confirm changes respect stdlib limits, zero public ports, and Android foreground constraints.
   ↓
5. IMPLEMENTATION PLAN
   Draft a step-by-step technical plan before touching code.
   ↓
6. OWNER APPROVAL
   Obtain approval on proposed design changes.
   ↓
7. IMPLEMENTATION
   Write clean, modular code. Respect coding standards and minimal-change guidelines.
   ↓
8. AUTOMATED TESTING
   Execute offline unit & integration test suites:
   python -m pytest tests/unit/ tests/ha/
   ↓
9. PHYSICAL / INTEGRATION VERIFICATION
   Test on physical hardware (e.g. Vivo V2334 in Punjab or remote nodes in Gujarat).
   Record actual PIDs, open ports, HTTP status codes, and frame capture payloads.
   ↓
10. DOCUMENTATION UPDATE
    Update docs/CURRENT-STATUS.md, docs/ROADMAP.md, and relevant docs/features/*.md.
   ↓
11. RELEASE & COMMIT
    Commit with conventional commit message (feat, fix, test, docs). Verify working tree is clean.
```

---

## 2. Multi-Tier Acceptance Gate

Before declaring any feature, bug fix, or milestone complete, the following four evidence tiers **must be fulfilled**:

1. **Tier 1 — Offline Automated Tests:**
   All relevant unit tests pass with zero errors:
   ```bash
   pytest tests/unit/test_native_android_bridge_lifecycle.py tests/unit/test_bootstrapper.py
   ```
2. **Tier 2 — Physical Device Runtime Proof:**
   Demonstrate actual process execution on the target device:
   - Real PID running (verified via `ps -ef` or `pgrep`).
   - Real port listening (verified via `netstat -tlpn` or `ss -tlpn`).
   - Real HTTP 200 JSON payload returned with valid status.
3. **Tier 3 — Live End-to-End Verification:**
   For streaming features, retrieve live frame bytes (valid JPEG header `\xff\xd8` and trailer `\xff\xd9`) and multipart boundary detection.
4. **Tier 4 — Clean Git History:**
   Working tree is clean; commit message clearly describes changes.
