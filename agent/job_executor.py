#!/usr/bin/env python3
"""
PersonalServer Job Executor
===========================
Executes safe, allowlisted workloads with timeout enforcement,
output truncation limits, and exit-code capture.
"""

import sys
import os
import re
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
    "timeout-test": "Controlled timeout test workload",
    "docker-deploy": "Pull image and safely launch containerized application",
    "docker-build-deploy": "Build image from source and launch containerized application",
    "docker-stop": "Stop running containerized application",
    "docker-restart": "Restart containerized application",
    "docker-remove": "Remove containerized application",
    "docker-logs": "Fetch bounded container logs"
}


def is_docker_daemon_ready():
    try:
        res = subprocess.run(["docker", "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        return res.returncode == 0
    except Exception:
        return False


def validate_docker_identifier(val, name="Identifier"):
    if not val or not isinstance(val, str):
        raise ValueError(f"Invalid {name}: Value must be a non-empty string.")
    import re
    if not re.match(r'^[a-zA-Z0-9_./:-]+$', val.strip()):
        raise ValueError(f"Invalid {name}: Contains invalid characters.")
    return val.strip()


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

        elif job_type in ("docker-deploy", "docker-build-deploy"):
            try:
                # Security: Explicitly reject arbitrary host filesystem mounts
                if params.get("volumes") or params.get("mounts"):
                    raise ValueError("Host volume mounts are not allowed in application deployment.")

                app_id = params.get("app_id", "app")
                app_name = validate_docker_identifier(params.get("app_name") or params.get("name") or "app", "Application Name")
                container_name = validate_docker_identifier(params.get("container_name") or f"ps-{app_name}", "Container Name")

                image = params.get("image")
                source_dir = params.get("source_dir") or params.get("build_context")

                # If source-based build is requested
                if job_type == "docker-build-deploy" or source_dir:
                    src_path = Path(source_dir).resolve()
                    # Security check: must reside within application storage
                    app_storage_root = (BASE_DIR / "storage" / "applications").resolve()
                    try:
                        src_path.relative_to(app_storage_root)
                    except ValueError:
                        raise ValueError("Access Denied: Source directory escapes application storage sandbox.")

                    if not src_path.exists() or not src_path.is_dir():
                        raise ValueError(f"Source directory not found: {source_dir}")

                    dockerfile_rel = params.get("dockerfile") or "Dockerfile"
                    dockerfile_path = (src_path / dockerfile_rel).resolve()
                    try:
                        dockerfile_path.relative_to(src_path)
                    except ValueError:
                        raise ValueError("Access Denied: Dockerfile escapes source directory sandbox.")

                    if not dockerfile_path.exists() or not dockerfile_path.is_file():
                        raise ValueError(f"Dockerfile not found at {dockerfile_rel}")

                    build_tag = validate_docker_identifier(params.get("image_tag") or f"personalserver/{app_name}:latest", "Image Tag")
                else:
                    image = validate_docker_identifier(image or "python:3.11-slim", "Docker Image")

                host_port = int(params.get("host_port") or 8000)
                container_port = int(params.get("container_port") or params.get("target_port") or params.get("port", 8000))
                if not (1 <= host_port <= 65535) or not (1 <= container_port <= 65535):
                    raise ValueError(f"Invalid port configuration: host_port={host_port}, container_port={container_port}")

                if not is_docker_daemon_ready():
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Docker runtime error: Docker daemon is unavailable or not responding on this node.",
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }

                if job_type == "docker-build-deploy" or source_dir:
                    src_path = Path(source_dir).resolve()
                    # Security check: must reside within application storage
                    app_storage_root = (BASE_DIR / "storage" / "applications").resolve()
                    try:
                        src_path.relative_to(app_storage_root)
                    except ValueError:
                        raise ValueError("Access Denied: Source directory escapes application storage sandbox.")

                    if not src_path.exists() or not src_path.is_dir():
                        raise ValueError(f"Source directory not found: {source_dir}")

                    dockerfile_rel = params.get("dockerfile") or "Dockerfile"
                    dockerfile_path = (src_path / dockerfile_rel).resolve()
                    try:
                        dockerfile_path.relative_to(src_path)
                    except ValueError:
                        raise ValueError("Access Denied: Dockerfile escapes source directory sandbox.")

                    if not dockerfile_path.exists() or not dockerfile_path.is_file():
                        raise ValueError(f"Dockerfile not found at {dockerfile_rel}")

                    build_tag = validate_docker_identifier(params.get("image_tag") or f"personalserver/{app_name}:latest", "Image Tag")
                    build_cmd = ["docker", "build", "-t", build_tag, "-f", str(dockerfile_path), str(src_path)]

                    build_res = subprocess.run(build_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
                    if build_res.returncode != 0:
                        return {
                            "job_id": job_id,
                            "status": JOB_STATE_FAILED,
                            "exit_code": build_res.returncode,
                            "stdout": truncate_output(build_res.stdout),
                            "stderr": f"Docker build failed for '{app_name}':\n{truncate_output(build_res.stderr.strip())}",
                            "duration_ms": int((time.time() - start_time) * 1000),
                            "started_at": started_iso,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }
                    image = build_tag
                else:
                    image = validate_docker_identifier(image or "python:3.11-slim", "Docker Image")
                    # Check if image exists locally first, otherwise pull image
                    image_inspect = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if image_inspect.returncode != 0:
                        pull_res = subprocess.run(["docker", "pull", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
                        if pull_res.returncode != 0:
                            return {
                                "job_id": job_id,
                                "status": JOB_STATE_FAILED,
                                "exit_code": pull_res.returncode,
                                "stdout": truncate_output(pull_res.stdout),
                                "stderr": f"Failed to pull image '{image}': {pull_res.stderr.strip()}",
                                "duration_ms": int((time.time() - start_time) * 1000),
                                "started_at": started_iso,
                                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            }

                # Clean up existing container with same name if any
                subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # Construct safe run arguments (no shell=True, no host root mounts)
                run_args = [
                    "docker", "run", "-d",
                    "--name", container_name,
                    "-p", f"127.0.0.1:{host_port}:{container_port}",
                    "--restart", "unless-stopped"
                ]

                # Conservative resource limits where specified
                mem_limit = params.get("memory_limit", "256m")
                if mem_limit:
                    run_args.extend(["--memory", str(mem_limit)])
                cpu_limit = params.get("cpu_limit", "0.5")
                if cpu_limit:
                    run_args.extend(["--cpus", str(cpu_limit)])

                # Environment variables: Passed safely as discrete command arguments
                env_vars = params.get("env", {})
                if isinstance(env_vars, dict):
                    for k, v in env_vars.items():
                        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(k)):
                            run_args.extend(["-e", f"{k}={v}"])

                run_args.append(image)
                cmd = run_args

            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Validation Error in docker deployment: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }

        elif job_type == "docker-stop":
            try:
                container_name = validate_docker_identifier(params.get("container_name") or params.get("container_id"), "Container name")
            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Invalid container name: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if not is_docker_daemon_ready():
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Docker daemon is unavailable on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["docker", "stop", container_name]

        elif job_type == "docker-restart":
            try:
                container_name = validate_docker_identifier(params.get("container_name") or params.get("container_id"), "Container name")
            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Invalid container name: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if not is_docker_daemon_ready():
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Docker daemon is unavailable on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["docker", "restart", container_name]

        elif job_type == "docker-remove":
            try:
                container_name = validate_docker_identifier(params.get("container_name") or params.get("container_id"), "Container name")
            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Invalid container name: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if not is_docker_daemon_ready():
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Docker daemon is unavailable on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["docker", "rm", "-f", container_name]

        elif job_type == "docker-logs":
            try:
                container_name = validate_docker_identifier(params.get("container_name") or params.get("container_id"), "Container name")
            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Invalid container name: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if not is_docker_daemon_ready():
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Docker daemon is unavailable on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["docker", "logs", "--tail", "100", container_name]

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
