# Workflow: Implement Feature

Use this workflow during approved feature implementation.

---

## Steps

1. **Environment & Branch Verification:**
   - Confirm active git branch and worktree.
   - Run baseline test suite (`pytest`) to verify tests pass prior to editing.

2. **Targeted Implementation:**
   - Implement the feature following `.agent/rules/coding-standards.md`.
   - Maintain modular structure; decompose oversized components where approved.
   - Avoid adding external dependencies.

3. **Automated Testing:**
   - Create or update unit tests under `tests/unit/`.
   - Run tests:
     ```bash
     python -m pytest tests/unit/
     ```
   - Ensure 100% pass rate without regressions.

4. **Physical / Integration Testing:**
   - Deploy code or APK to target test node (e.g. Vivo V2334).
   - Execute integration test or curl validation to prove live functionality.
   - Record exact command output, timestamps, and return codes.

5. **Documentation & Release:**
   - Update `docs/CURRENT-STATUS.md` and feature-specific documents in `docs/features/`.
   - Stage and commit changes with descriptive commit messages.
