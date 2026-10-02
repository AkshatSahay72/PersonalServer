#!/usr/bin/env python3
"""
PersonalServer Job Executor
===========================
Executes safe, allowlisted workloads with timeout enforcement,
output truncation limits, and exit-code capture.
"""

import sys
import os
import json
import time
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
RUNTIME_DIR = BASE_DIR / "runtime"
JOBS_RUNTIME_DIR = RUNTIME_DIR / "jobs"

MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB limit

# Centralized Job States
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMEOUT = "TIMEOUT"
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_REJECTED = "REJECTED"

# Allowlisted Workload Types
ALLOWLISTED_WORKLOADS = {
    "system-info": "Gather system hardware and OS metrics",
    "health-check": "Run node health check script",
    "node-status": "Run node status check script",
    "python-script": "Execute safe inline python script or function",
    "echo": "Echo back test message",
    "failing-test": "Controlled error-handling test workload",
    "timeout-test": "Controlled timeout test workload"
}


def truncate_output(text, max_bytes=MAX_OUTPUT_BYTES):
    """Truncate output if it exceeds max_bytes to protect memory."""
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + "\n... [OUTPUT TRUNCATED]"
    return text


class JobExecutor:
    """Executes validated workloads under PersonalServer user permissions."""

    @staticmethod
    def is_allowed(job_type):
        return job_type in ALLOWLISTED_WORKLOADS

    @staticmethod
    def execute(job):
        """
        Execute a job dictionary:
        {
          "job_id": "...",
          "type": "...",
          "parameters": {},
          "timeout": 60
        }
        Returns result dictionary.
        """
        JOBS_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        job_id = job.get("job_id", "local-job")
        job_type = job.get("type", "")
        params = job.get("parameters", {}) or {}
        timeout_sec = int(job.get("timeout", 60))

        if not JobExecutor.is_allowed(job_type):
            return {
                "job_id": job_id,
                "status": JOB_STATE_REJECTED,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Error: Workload type '{job_type}' is not in the safe allowlist.",
                "duration_ms": 0,
                "started_at": None,
                "finished_at": None
            }

        start_time = time.time()
        started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time))

        cmd = []
        custom_script = None

        if job_type == "system-info":
            cmd = ["python3", str(BASE_DIR / "agent" / "node-agent.py"), "info", "--json"]

        elif job_type == "health-check":
            cmd = ["bash", str(SCRIPTS_DIR / "health.sh")]

        elif job_type == "node-status":
            cmd = ["bash", str(SCRIPTS_DIR / "status.sh")]

        elif job_type == "echo":
            msg = params.get("message", "PersonalServer workload echo")
            cmd = ["echo", str(msg)]

        elif job_type == "failing-test":
            err_msg = params.get("error_message", "Simulated workload failure")
            cmd = ["python3", "-c", f"import sys; sys.stderr.write('{err_msg}\\n'); sys.exit(1)"]

        elif job_type == "timeout-test":
            sleep_duration = int(params.get("sleep", timeout_sec + 10))
            cmd = ["python3", "-c", f"import time; time.sleep({sleep_duration})"]

        elif job_type == "python-script":
            code = params.get("code", "")
            if not code:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Missing 'code' in parameters for python-script workload.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["python3", "-c", code]

        # Execute process with strict timeout and output capture
        status = JOB_STATE_RUNNING
        stdout_str = ""
        stderr_str = ""
        exit_code = 0

        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(BASE_DIR)
            )
            raw_stdout, raw_stderr = proc.communicate(timeout=timeout_sec)
            exit_code = proc.returncode
            stdout_str = truncate_output(raw_stdout)
            stderr_str = truncate_output(raw_stderr)

            if exit_code == 0:
                status = JOB_STATE_SUCCEEDED
            else:
                status = JOB_STATE_FAILED

        except subprocess.TimeoutExpired:
            status = JOB_STATE_TIMEOUT
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                raw_stdout, raw_stderr = proc.communicate()
                stdout_str = truncate_output(raw_stdout)
                stderr_str = truncate_output(raw_stderr) + f"\nJob execution timed out after {timeout_sec} seconds."
            exit_code = -1

        except Exception as e:
            status = JOB_STATE_FAILED
            stderr_str = f"Execution Exception: {str(e)}"
            exit_code = 1

        end_time = time.time()
        finished_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_time))
        duration_ms = int((end_time - start_time) * 1000)

        result = {
            "job_id": job_id,
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout_str.strip(),
            "stderr": stderr_str.strip(),
            "duration_ms": duration_ms,
            "started_at": started_iso,
            "finished_at": finished_iso
        }

        # Save result to node-local runtime storage
        try:
            job_file = JOBS_RUNTIME_DIR / f"{job_id}.json"
            job_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass

        return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_type = sys.argv[1]
        print(f"Testing local execution of '{test_type}'...")
        res = JobExecutor.execute({"job_id": "cli-test", "type": test_type, "timeout": 5})
        print(json.dumps(res, indent=2))
    else:
        print("Usage: python job_executor.py <job_type>")
