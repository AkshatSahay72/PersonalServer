# Workflow: Plan Feature

Use this workflow whenever the owner requests a new capability, architectural adjustment, or significant subsystem enhancement.

---

## Steps

1. **Inspect Existing State:**
   - Review `docs/CURRENT-STATUS.md` and `docs/PRD.md` to identify existing related capabilities.
   - Inspect active branch and worktrees (`git worktree list`).
   - Check `docs/DECISIONS.md` to ensure the proposal does not violate established ADRs.

2. **Scope & Requirement Assignment:**
   - Formulate traceable IDs (e.g. `CAM-005`, `TELE-002`).
   - Define exact functional and non-functional requirements.
   - Explicitly list non-goals for this feature.

3. **Technical Architecture Review:**
   - Map impact across subsystems (Node Agent, Controller, Native Bridge, Web UI).
   - Verify compatibility with Python 3.9+ standard library, Android 11, and Android 16.
   - Evaluate network overhead and battery impact.

4. **Draft Implementation Plan:**
   - Present proposed file modifications and additions.
   - Define automated test strategy (`pytest`).
   - Define physical device verification steps.

5. **Submit for User Approval:**
   - Present the plan to the project owner and await explicit authorization before writing code.
