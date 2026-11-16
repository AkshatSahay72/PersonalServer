# Workflow: Debug Issue

Use this workflow whenever a failure, crash, network disconnect, or unexpected behavior is reported.

---

## Steps

1. **Establish Ground Truth (Do Not Guess):**
   - Query the actual physical environment first:
     - Check process state: `pgrep -a python` or `ps -ef | grep python`.
     - Check network listener: `ss -tlpn` or `netstat -tlpn`.
     - Check HTTP response: `curl -v http://127.0.0.1:<port>/health`.
     - Inspect recent log files (`logs/`, `logcat`, or Termux output).

2. **Isolate the Defect:**
   - Trace the exact call path (Client ➔ Ingress ➔ Controller ➔ Node API ➔ Native Bridge).
   - Write a reproduction script or minimal failing unit test in `tests/unit/` or `tests/integration/`.

3. **Apply Minimal Sufficient Fix:**
   - Fix the underlying root cause with the smallest surgical change.
   - Do not perform broad refactoring while debugging.

4. **Verify the Fix:**
   - Re-run the reproduction test to prove the fix passes.
   - Execute the full test suite to guarantee no collateral damage.
   - Verify on the physical target device.
