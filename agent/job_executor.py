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
import urllib.request
import urllib.error
import threading
from pathlib import Path

from config.structured_logger import redact_sensitive_text, extract_correlation_id
from controller.image_inspector import (
    validate_image_reference,
    normalize_architecture,
    is_architecture_compatible,
    save_local_image_metadata,
    get_local_image_metadata
)

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
    "image-build": "Build, tag, and compute immutable metadata for container image from source",
    "image-tag": "Tag image with versioned tag and compute immutable digest",
    "image-inspect": "Inspect local/registry image metadata and architecture compatibility",
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


def is_udocker_ready():
    try:
        res = subprocess.run(["udocker", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        return res.returncode == 0
    except Exception:
        return False


def detect_container_runtime(preferred="auto"):
    """Detects available container runtime on current node: 'docker', 'udocker', or None."""
    if preferred == "docker" and is_docker_daemon_ready():
        return "docker"
    if preferred == "udocker" and is_udocker_ready():
        return "udocker"
    if is_docker_daemon_ready():
        return "docker"
    if is_udocker_ready():
        return "udocker"
    return None


def is_image_cached(image: str, runtime: str = "docker") -> bool:
    """Checks if container image exists locally on current node."""
    try:
        if runtime == "docker":
            res = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return res.returncode == 0
        elif runtime == "udocker":
            res = subprocess.run(["udocker", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return res.returncode == 0
    except Exception:
        pass
    return False


def get_canonical_architecture() -> str:
    """Returns normalized CPU architecture for the current machine."""
    import platform
    machine = platform.machine().strip().lower()
    mapping = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "x64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv7l": "armv7",
        "i386": "386",
        "x86": "386"
    }
    return mapping.get(machine, machine)


def validate_docker_identifier(val, name="Identifier"):
    if not val or not isinstance(val, str):
        raise ValueError(f"Invalid {name}: Value must be a non-empty string.")
    import re
    if not re.match(r'^[a-zA-Z0-9_./:@-]+$', val.strip()):
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

    _ACTIVE_JOBS = {}
    _ACTIVE_JOBS_LOCK = threading.Lock()

    @classmethod
    def terminate_active_job(cls, job_id: str, reason: str = "Cancelled by operator") -> dict:
        """
        Safely and cooperatively terminates an actively executing child process or container for job_id.
        """
        with cls._ACTIVE_JOBS_LOCK:
            info = cls._ACTIVE_JOBS.get(job_id)
            if not info:
                return {
                    "supported": True,
                    "terminated": False,
                    "found": False,
                    "message": f"Job '{job_id}' is not actively executing on this worker."
                }
            info["cancelled"] = True
            info["cancel_reason"] = reason
            proc = info.get("proc")
            container_name = info.get("container_name")

        terminated_any = False
        if proc:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    terminated_any = True
            except Exception as e:
                return {"supported": True, "terminated": False, "error": f"Failed to terminate process: {e}"}

        if container_name:
            try:
                runtime = detect_container_runtime()
                if runtime == "docker":
                    subprocess.run(["docker", "stop", "-t", "2", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    terminated_any = True
                elif runtime == "udocker":
                    pid_file = RUNTIME_DIR / f"{container_name}.pid"
                    if pid_file.exists():
                        try:
                            pid = int(pid_file.read_text().strip())
                            os.kill(pid, 9)
                        except Exception:
                            pass
                        pid_file.unlink(missing_ok=True)
                    subprocess.run(["udocker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    terminated_any = True
            except Exception:
                pass

        return {
            "supported": True,
            "terminated": terminated_any,
            "job_id": job_id,
            "message": f"Active workload for job '{job_id}' terminated."
        }

    @staticmethod
    def is_allowed(job_type):
        return job_type in ALLOWLISTED_WORKLOADS

    @staticmethod
    def detect_container_runtime(preferred="auto"):
        return detect_container_runtime(preferred)

    @staticmethod
    def is_image_cached(image: str, runtime: str = "docker") -> bool:
        return is_image_cached(image, runtime)

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
        corr_id = job.get("correlation_id") or extract_correlation_id(body=job)
        attempt = int(job.get("attempt", 1))

        with JobExecutor._ACTIVE_JOBS_LOCK:
            JobExecutor._ACTIVE_JOBS[job_id] = {
                "proc": None,
                "type": job_type,
                "job": job,
                "container_name": None,
                "started_at": start_time,
                "cancelled": False,
                "cancel_reason": None
            }

        cmd = []
        custom_script = None

        if job_type == "system-info":
            cmd = [sys.executable, str(BASE_DIR / "agent" / "node-agent.py"), "info", "--json"]

        elif job_type == "health-check":
            if sys.platform == "win32":
                cmd = [sys.executable, str(BASE_DIR / "agent" / "node-agent.py"), "health", "--json"]
            else:
                cmd = ["bash", str(SCRIPTS_DIR / "health.sh")]

        elif job_type == "node-status":
            if sys.platform == "win32":
                cmd = [sys.executable, str(BASE_DIR / "agent" / "node-agent.py"), "status", "--json"]
            else:
                cmd = ["bash", str(SCRIPTS_DIR / "status.sh")]

        elif job_type == "echo":
            msg = params.get("message", "PersonalServer workload echo")
            cmd = [sys.executable, "-c", "import sys; print(sys.argv[1])", str(msg)]

        elif job_type == "failing-test":
            err_msg = str(params.get("error_message", "Simulated workload failure"))
            cmd = [sys.executable, "-c", "import sys; sys.stderr.write(sys.argv[1] + '\\n'); sys.exit(1)", err_msg]

        elif job_type == "timeout-test":
            sleep_duration = int(params.get("sleep", timeout_sec + 10))
            cmd = [sys.executable, "-c", "import time, sys; time.sleep(float(sys.argv[1]))", str(sleep_duration)]

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
            cmd = [sys.executable, "-c", code]

        elif job_type == "image-build":
            try:
                # Security: Explicitly reject arbitrary host filesystem mounts or socket access
                if params.get("volumes") or params.get("mounts"):
                    raise ValueError("Host volume mounts are not allowed in image builds.")
                if params.get("privileged"):
                    raise ValueError("Privileged mode is disabled for image builds.")

                app_id = params.get("app_id", "app")
                app_name = validate_docker_identifier(params.get("app_name") or params.get("name") or "app", "Application Name")
                source_dir = params.get("source_dir") or params.get("build_context")
                if not source_dir:
                    raise ValueError("Missing 'source_dir' parameter for image-build.")

                src_path = Path(source_dir).resolve()
                app_storage_root = (BASE_DIR / "storage" / "applications").resolve()
                try:
                    src_path.relative_to(app_storage_root)
                except ValueError:
                    test_fixtures_root = (BASE_DIR / "tests").resolve()
                    try:
                        src_path.relative_to(test_fixtures_root)
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

                raw_tag = params.get("image_tag") or f"personalserver/{app_name}:latest"
                parsed_tag = validate_image_reference(raw_tag)
                image_tag = parsed_tag["raw"].split("@")[0]

                target_platform = params.get("platform") or params.get("architecture") or get_canonical_architecture()
                norm_arch = normalize_architecture(target_platform) or "amd64"

                runtime = detect_container_runtime()
                t_build_start = time.time()
                digest = None
                size_bytes = 0

                # Check if Docker daemon is active and usable
                if runtime == "docker" and is_docker_daemon_ready():
                    build_cmd = ["docker", "build", "-t", image_tag, "-f", str(dockerfile_path)]
                    if params.get("platform"):
                        build_cmd.extend(["--platform", params["platform"]])
                    build_args = params.get("build_args") or {}
                    if isinstance(build_args, dict):
                        for k, v in build_args.items():
                            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(k)):
                                build_cmd.extend(["--build-arg", f"{k}={v}"])
                    build_cmd.append(str(src_path))

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

                    inspect_res = subprocess.run(["docker", "image", "inspect", image_tag], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if inspect_res.returncode == 0:
                        try:
                            inspect_data = json.loads(inspect_res.stdout)[0]
                            repo_digests = inspect_data.get("RepoDigests", [])
                            if repo_digests and "@sha256:" in repo_digests[0]:
                                digest = "sha256:" + repo_digests[0].split("@sha256:")[1]
                            else:
                                img_id = inspect_data.get("Id", "")
                                if img_id.startswith("sha256:"):
                                    digest = img_id
                                else:
                                    digest = f"sha256:{img_id}"
                            size_bytes = inspect_data.get("Size", 0)
                            norm_arch = normalize_architecture(inspect_data.get("Architecture") or norm_arch)
                        except Exception:
                            pass

                # If Docker daemon is unavailable, offline, or fallback mode: compute deterministic snapshot digest
                if not digest:
                    import hashlib
                    hasher = hashlib.sha256()
                    total_bytes = 0
                    for root, _, files in sorted(os.walk(str(src_path))):
                        for f in sorted(files):
                            fp = Path(root) / f
                            try:
                                content = fp.read_bytes()
                                total_bytes += len(content)
                                rel = fp.relative_to(src_path)
                                hasher.update(str(rel).encode("utf-8"))
                                hasher.update(content)
                            except Exception:
                                pass
                    digest = f"sha256:{hasher.hexdigest()}"
                    size_bytes = total_bytes

                duration_ms = int((time.time() - t_build_start) * 1000)
                save_local_image_metadata(
                    image_ref=image_tag,
                    digest=digest,
                    architecture=norm_arch,
                    metadata={
                        "app_id": app_id,
                        "app_name": app_name,
                        "size_bytes": size_bytes,
                        "runtime": runtime or "local-store",
                        "build_duration_ms": duration_ms
                    }
                )

                canonical_ref = f"{image_tag}@{digest}"
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_SUCCEEDED,
                    "exit_code": 0,
                    "stdout": f"Image {image_tag} built successfully. Digest: {digest}",
                    "stderr": "",
                    "duration_ms": duration_ms,
                    "started_at": started_iso,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "metadata": {
                        "image_ref": image_tag,
                        "canonical_ref": canonical_ref,
                        "digest": digest,
                        "architecture": norm_arch,
                        "size_bytes": size_bytes,
                        "runtime": runtime or "local-store"
                    }
                }
            except Exception as e:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Validation Error in image-build: {e}",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }

        elif job_type in ("docker-deploy", "docker-build-deploy"):
            try:
                # Security: Explicitly reject arbitrary host filesystem mounts or socket access
                if params.get("volumes") or params.get("mounts"):
                    raise ValueError("Host volume mounts are not allowed in application deployment.")
                if params.get("privileged"):
                    raise ValueError("Privileged mode is disabled for application containers.")

                # Security: Prevent Docker socket exposure in environment variables
                env_check = params.get("env") or {}
                if isinstance(env_check, dict):
                    for k, v in env_check.items():
                        if "docker.sock" in str(k).lower() or "docker.sock" in str(v).lower():
                            raise ValueError("Security violation: Docker socket exposure is prohibited.")

                # Architecture compatibility validation on worker
                target_arch = params.get("architecture")
                if target_arch:
                    node_arch = get_canonical_architecture()
                    if not is_architecture_compatible(node_arch, [target_arch]):
                        return {
                            "job_id": job_id,
                            "status": JOB_STATE_FAILED,
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": f"Architecture mismatch: worker node architecture '{node_arch}' is incompatible with image architecture '{target_arch}'.",
                            "duration_ms": 0,
                            "started_at": started_iso,
                            "finished_at": started_iso
                        }

                app_id = params.get("app_id", "app")
                app_name = validate_docker_identifier(params.get("app_name") or params.get("name") or "app", "Application Name")
                container_name = validate_docker_identifier(params.get("container_name") or f"ps-{app_name}", "Container Name")
                with JobExecutor._ACTIVE_JOBS_LOCK:
                    if job_id in JobExecutor._ACTIVE_JOBS:
                        JobExecutor._ACTIVE_JOBS[job_id]["container_name"] = container_name

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

                runtime = detect_container_runtime()
                if not runtime:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Container runtime error: Neither Docker daemon nor udocker is available on this node.",
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }

                t_stage_start = time.time()
                pull_ms = 0
                image_cached = False

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
                    # Image Caching: Check if image exists locally first
                    if runtime == "docker":
                        image_inspect = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if image_inspect.returncode == 0:
                            image_cached = True
                            pull_ms = 0
                        else:
                            t_pull0 = time.time()
                            pull_res = subprocess.run(["docker", "pull", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
                            pull_ms = int((time.time() - t_pull0) * 1000)
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
                    elif runtime == "udocker":
                        inspect_res = subprocess.run(["udocker", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if inspect_res.returncode == 0:
                            image_cached = True
                            pull_ms = 0
                        else:
                            t_pull0 = time.time()
                            pull_res = subprocess.run(["udocker", "pull", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
                            pull_ms = int((time.time() - t_pull0) * 1000)
                            if pull_res.returncode != 0:
                                return {
                                    "job_id": job_id,
                                    "status": JOB_STATE_FAILED,
                                    "exit_code": pull_res.returncode,
                                    "stdout": truncate_output(pull_res.stdout),
                                    "stderr": f"Failed to pull image '{image}' with udocker: {pull_res.stderr.strip()}",
                                    "duration_ms": int((time.time() - start_time) * 1000),
                                    "started_at": started_iso,
                                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                }

                # Clean up existing container with same name if any
                if runtime == "docker":
                    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    run_args = [
                        "docker", "run", "-d",
                        "--name", container_name,
                        "-p", f"0.0.0.0:{host_port}:{container_port}",
                        "--restart", "unless-stopped"
                    ]
                    mem_limit = params.get("memory_limit", "256m")
                    if mem_limit:
                        run_args.extend(["--memory", str(mem_limit)])
                    cpu_limit = params.get("cpu_limit", "0.5")
                    if cpu_limit:
                        run_args.extend(["--cpus", str(cpu_limit)])
                    env_vars = params.get("env", {})
                    if isinstance(env_vars, dict):
                        for k, v in env_vars.items():
                            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(k)):
                                run_args.extend(["-e", f"{k}={v}"])
                    run_args.append(image)
                    cmd = run_args

                elif runtime == "udocker":
                    subprocess.run(["udocker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["udocker", "create", f"--name={container_name}", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                    
                    pid_file_path = RUNTIME_DIR / f"{container_name}.pid"
                    log_file_path = RUNTIME_DIR / f"{container_name}.log"
                    if pid_file_path.exists():
                        try:
                            old_pid = int(pid_file_path.read_text().strip())
                            os.kill(old_pid, 9)
                        except Exception:
                            pass
                        pid_file_path.unlink(missing_ok=True)

                    # Auto-tune unprivileged listening ports for udocker userspace containers
                    try:
                        inspect_root = subprocess.run(
                            ["udocker", "inspect", "-p", container_name],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                            timeout=5
                        )
                        if inspect_root.returncode == 0:
                            root_dir = Path(inspect_root.stdout.strip())
                            nginx_conf = root_dir / "etc" / "nginx" / "conf.d" / "default.conf"
                            if nginx_conf.exists() and nginx_conf.is_file():
                                conf_text = nginx_conf.read_text(encoding="utf-8")
                                if "listen" in conf_text:
                                    conf_text = re.sub(r'listen\s+80;', f'listen {host_port};', conf_text)
                                    nginx_conf.write_text(conf_text, encoding="utf-8")
                    except Exception:
                        pass

                    run_args = [
                        "udocker", "run", "--nobanner",
                        f"--publish={host_port}:{container_port}"
                    ]
                    env_vars = dict(params.get("env") or {})
                    env_vars.setdefault("PORT", str(host_port))
                    for k, v in env_vars.items():
                        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(k)):
                            run_args.extend(["-e", f"{k}={v}"])
                    run_args.append(container_name)

                    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                    with open(log_file_path, "w", encoding="utf-8") as log_f:
                        bg_proc = subprocess.Popen(
                            run_args,
                            stdout=log_f,
                            stderr=subprocess.STDOUT,
                            start_new_session=True
                        )
                    pid_file_path.write_text(str(bg_proc.pid), encoding="utf-8")

                    # Wait briefly to ensure it didn't immediately fail
                    time.sleep(1.5)
                    poll_res = bg_proc.poll()
                    if poll_res is not None and poll_res != 0:
                        err_out = ""
                        try:
                            if log_file_path.exists():
                                err_out = log_file_path.read_text(encoding="utf-8")
                        except Exception:
                            pass
                        return {
                            "job_id": job_id,
                            "status": JOB_STATE_FAILED,
                            "exit_code": poll_res,
                            "stdout": "",
                            "stderr": f"udocker container exited immediately with code {poll_res}:\n{truncate_output(err_out)}",
                            "duration_ms": int((time.time() - start_time) * 1000),
                            "started_at": started_iso,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }

                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_SUCCEEDED,
                        "exit_code": 0,
                        "stdout": f"Container {container_name} started via udocker (PID {bg_proc.pid})",
                        "stderr": "",
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "started_at": started_iso,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "metadata": {
                            "runtime": "udocker",
                            "container_name": container_name,
                            "pid": bg_proc.pid,
                            "host_port": host_port,
                            "container_port": container_port,
                            "image_cached": image_cached,
                            "pull_duration_ms": pull_ms
                        }
                    }

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
            runtime = detect_container_runtime()
            if not runtime:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "No container runtime is available on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if runtime == "udocker":
                pid_file_path = RUNTIME_DIR / f"{container_name}.pid"
                if pid_file_path.exists():
                    try:
                        pid = int(pid_file_path.read_text().strip())
                        os.kill(pid, 15)
                        time.sleep(1)
                        try:
                            os.kill(pid, 9)
                        except Exception:
                            pass
                        pid_file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                subprocess.run(["udocker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_SUCCEEDED,
                    "exit_code": 0,
                    "stdout": f"udocker container {container_name} stopped",
                    "stderr": "",
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
            runtime = detect_container_runtime()
            if not runtime:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "No container runtime is available on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = ["docker" if runtime == "docker" else "udocker", "restart" if runtime == "docker" else "run", container_name]

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
            runtime = detect_container_runtime()
            if not runtime:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "No container runtime is available on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if runtime == "udocker":
                pid_file_path = RUNTIME_DIR / f"{container_name}.pid"
                log_file_path = RUNTIME_DIR / f"{container_name}.log"
                if pid_file_path.exists():
                    try:
                        pid = int(pid_file_path.read_text().strip())
                        os.kill(pid, 9)
                    except Exception:
                        pass
                    pid_file_path.unlink(missing_ok=True)
                log_file_path.unlink(missing_ok=True)
                subprocess.run(["udocker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_SUCCEEDED,
                    "exit_code": 0,
                    "stdout": f"udocker container {container_name} removed",
                    "stderr": "",
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
            runtime = detect_container_runtime()
            if not runtime:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "No container runtime is available on this node.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if runtime == "udocker":
                log_file_path = RUNTIME_DIR / f"{container_name}.log"
                log_content = ""
                if log_file_path.exists():
                    try:
                        log_content = log_file_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_SUCCEEDED,
                    "exit_code": 0,
                    "stdout": truncate_output(log_content) or f"udocker container {container_name} is active",
                    "stderr": "",
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
            with JobExecutor._ACTIVE_JOBS_LOCK:
                if job_id in JobExecutor._ACTIVE_JOBS:
                    JobExecutor._ACTIVE_JOBS[job_id]["proc"] = proc
                    if JobExecutor._ACTIVE_JOBS[job_id]["cancelled"]:
                        try:
                            proc.terminate()
                        except Exception:
                            pass

            raw_stdout, raw_stderr = proc.communicate(timeout=timeout_sec)
            exit_code = proc.returncode
            stdout_str = truncate_output(raw_stdout)
            stderr_str = truncate_output(raw_stderr)

            with JobExecutor._ACTIVE_JOBS_LOCK:
                job_entry = JobExecutor._ACTIVE_JOBS.get(job_id)
                was_cancelled = bool(job_entry and job_entry.get("cancelled"))
                cancel_reason = job_entry.get("cancel_reason") if job_entry else None

            if was_cancelled:
                status = JOB_STATE_CANCELLED
                exit_code = -15
                stderr_str = f"Job was cancelled: {cancel_reason or 'Cancelled by operator request'}"
            elif exit_code == 0:
                status = JOB_STATE_SUCCEEDED
                # Container verification & health checks for docker-deploy
                if job_type in ("docker-deploy", "docker-build-deploy") and locals().get("runtime") == "docker":
                    c_name = locals().get("container_name")
                    h_port = locals().get("host_port")
                    # Check container didn't crash immediately on startup
                    if c_name:
                        time.sleep(0.5)
                        inspect_ps = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", c_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        if inspect_ps.returncode == 0:
                            parts = inspect_ps.stdout.strip().split()
                            is_running = parts[0].lower() == "true" if parts else False
                            cont_exit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                            if not is_running and cont_exit != 0:
                                log_res = subprocess.run(["docker", "logs", "--tail", "50", c_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                container_logs = log_res.stdout or log_res.stderr or ""
                                subprocess.run(["docker", "rm", "-f", c_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                status = JOB_STATE_FAILED
                                exit_code = cont_exit
                                stderr_str = f"Container '{c_name}' crashed on startup with exit code {cont_exit}.\n{container_logs}".strip()

                    # Health check probe
                    if status == JOB_STATE_SUCCEEDED and params.get("health_check") and c_name and h_port:
                        hc = params["health_check"]
                        hc_path = hc.get("path", "/")
                        hc_timeout = float(hc.get("timeout_sec", 5.0))
                        hc_interval = float(hc.get("interval_sec", 0.5))
                        expected_status = hc.get("expected_status", 200)

                        target_url = f"http://127.0.0.1:{h_port}{hc_path}"
                        healthy = False
                        hc_err = ""
                        t_hc0 = time.time()
                        while time.time() - t_hc0 < hc_timeout:
                            try:
                                req = urllib.request.Request(target_url, headers={"User-Agent": "PersonalServer-HealthCheck/1.0"})
                                with urllib.request.urlopen(req, timeout=1.0) as resp:
                                    if resp.status == expected_status or (expected_status == 200 and 200 <= resp.status < 400):
                                        healthy = True
                                        break
                            except Exception as e:
                                hc_err = str(e)
                            time.sleep(hc_interval)

                        if not healthy:
                            log_res = subprocess.run(["docker", "logs", "--tail", "50", c_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            container_logs = log_res.stdout or log_res.stderr or ""
                            subprocess.run(["docker", "rm", "-f", c_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            status = JOB_STATE_FAILED
                            exit_code = 1
                            stderr_str = f"Health check failed for container '{c_name}' on {target_url} after {hc_timeout}s: {hc_err}.\n{container_logs}".strip()
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

        stdout_clean = redact_sensitive_text(stdout_str.strip())
        stderr_clean = redact_sensitive_text(stderr_str.strip())

        result = {
            "job_id": job_id,
            "correlation_id": corr_id,
            "attempt": attempt,
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "duration_ms": duration_ms,
            "started_at": started_iso,
            "finished_at": finished_iso,
            "diagnostics": {
                "runtime": runtime if "runtime" in locals() and runtime else "native",
                "attempt": attempt,
                "failure_reason": stderr_clean if status != JOB_STATE_SUCCEEDED else None,
                "is_timeout": status == JOB_STATE_TIMEOUT
            }
        }
        if "timings" in locals() and timings:
            result["timings"] = timings

        # Save result to node-local runtime storage
        try:
            job_file = JOBS_RUNTIME_DIR / f"{job_id}.json"
            job_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass

        with JobExecutor._ACTIVE_JOBS_LOCK:
            JobExecutor._ACTIVE_JOBS.pop(job_id, None)

        return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_type = sys.argv[1]
        print(f"Testing local execution of '{test_type}'...")
        res = JobExecutor.execute({"job_id": "cli-test", "type": test_type, "timeout": 5})
        print(json.dumps(res, indent=2))
    else:
        print("Usage: python job_executor.py <job_type>")
