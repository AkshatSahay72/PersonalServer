#!/usr/bin/env python3
"""
PersonalServer Container Runtime Adapter
========================================
Implements the RuntimeAdapter interface for containerized workloads.
Supports Dockerfile-based applications and explicit image references.

Key architectural features:
- Capability discovery: Distinguishes genuine Docker Engine from udocker wrappers or missing daemons.
- Isolation: Unique container identity per deployment (ps-{app_id}-{dep_id[:8]}).
- Security: Strictly binds host ports to 127.0.0.1 (never 0.0.0.0). No arbitrary host volume mounts.
  Strips dangerous Docker socket/host control variables.
- Resource constraints: Bounded memory (--memory) and CPU (--cpus) limits.
- Release safety: Isolated cleanup per deployment without interfering with other containers or prior releases.
- Health probe integration: Validates container state before router activation.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from common.process_manager import ProcessIdentityRecord
from common.time_utils import get_current_iso_timestamp
from agent.runtime_adapters.base import RuntimeAdapter


class ContainerRuntimeAdapter(RuntimeAdapter):
    """
    Adapter for containerized workloads using Docker Engine.
    Handles image builds, image pulls, container execution, resource bounding,
    and safe lifecycle management.
    """

    def __init__(self, docker_bin: Optional[str] = None):
        self._docker_bin = docker_bin or shutil.which("docker")
        self.is_container = True

    @property
    def runtime_name(self) -> str:
        return "docker"

    def _resolve_docker_bin(self) -> Optional[str]:
        if self._docker_bin and Path(self._docker_bin).exists():
            return self._docker_bin
        return shutil.which("docker")

    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        """
        Validates that the host execution node possesses a functional, accessible
        Docker Engine daemon. Explicitly rejects missing Docker, unreachable daemons,
        and udocker shims.
        """
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return False, "Docker executable not found on PATH"

        # Explicitly reject if binary name contains udocker
        if "udocker" in Path(docker_cmd).name.lower():
            return False, "udocker detected: Docker Engine daemon is required for container runtime"

        try:
            # 1. Check version output to ensure genuine Docker Engine CLI
            v_res = subprocess.run(
                [docker_cmd, "version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5.0
            )
            combined_v = (v_res.stdout + " " + v_res.stderr).lower()
            if "udocker" in combined_v:
                return False, "Binary is a udocker wrapper; genuine Docker Engine daemon required"

            # 2. Check daemon connectivity via docker info
            info_res = subprocess.run(
                [docker_cmd, "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5.0
            )
            if info_res.returncode != 0:
                err = (info_res.stderr or info_res.stdout or "daemon not running").strip().splitlines()[0]
                return False, f"Docker daemon unreachable: {err}"

            return True, None
        except subprocess.TimeoutExpired:
            return False, "Docker daemon connection probe timed out"
        except Exception as e:
            return False, f"Docker environment validation error: {e}"

    def prepare_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        env_vars: Dict[str, str],
        timeout_sec: float = 120.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Prepares the container image for execution:
        - If Dockerfile is present in release_dir: builds image with tag ps-{app_id}:{deployment_id[:8]}.
        - If explicit image is specified: pulls image and verifies optional digest.
        """
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return False, "Docker executable not found on PATH"

        metadata_file = release_dir / "release_metadata.json"
        metadata: Dict[str, Any] = {}
        if metadata_file.exists():
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception:
                pass

        # Check for Dockerfile in release directory
        dockerfile_candidates = [
            release_dir / "Dockerfile",
            release_dir / "dockerfile",
        ]
        explicit_df = env_vars.get("DOCKERFILE") or metadata.get("dockerfile")
        if explicit_df:
            dockerfile_candidates.insert(0, (release_dir / explicit_df).resolve())

        dockerfile_path: Optional[Path] = None
        for candidate in dockerfile_candidates:
            if candidate.is_file():
                # Enforce sandbox containment: Dockerfile must be within release_dir
                try:
                    candidate.resolve().relative_to(release_dir.resolve())
                    dockerfile_path = candidate
                    break
                except ValueError:
                    return False, f"Security violation: Dockerfile '{candidate}' escapes release sandbox"

        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        build_tag = f"ps-{app_id}:{clean_dep_suffix}"

        # Resolve container port from Dockerfile or env
        target_container_port = 8000
        env_port = env_vars.get("CONTAINER_PORT") or env_vars.get("TARGET_PORT") or env_vars.get("PORT")
        if env_port and str(env_port).isdigit():
            target_container_port = int(env_port)

        if dockerfile_path:
            # Parse EXPOSE from Dockerfile if port not explicitly set
            try:
                df_text = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
                expose_match = re.search(r'^\s*EXPOSE\s+(\d+)', df_text, re.MULTILINE | re.IGNORECASE)
                if expose_match and not env_port:
                    target_container_port = int(expose_match.group(1))
            except Exception:
                pass

            # Execute hermetic image build
            build_cmd = [
                docker_cmd, "build",
                "-t", build_tag,
                "-f", str(dockerfile_path),
                str(release_dir)
            ]
            try:
                b_res = subprocess.run(
                    build_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec
                )
                if b_res.returncode != 0:
                    err_tail = (b_res.stderr or b_res.stdout or "Build failed").strip().splitlines()[-10:]
                    return False, f"Docker build failed (code {b_res.returncode}):\n" + "\n".join(err_tail)

                metadata["image"] = build_tag
                metadata["build_tag"] = build_tag
                metadata["container_port"] = target_container_port
                metadata["build_timestamp"] = get_current_iso_timestamp()
                with open(metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)

                return True, None
            except subprocess.TimeoutExpired:
                return False, f"Docker build timed out after {timeout_sec}s"
            except Exception as e:
                return False, f"Docker build invocation error: {e}"

        # Fallback to explicit image reference
        image_ref = env_vars.get("IMAGE") or env_vars.get("DOCKER_IMAGE") or metadata.get("image")
        if image_ref:
            # Validate image name syntax
            if not re.match(r'^[a-zA-Z0-9_\-./:]+$', str(image_ref)):
                return False, f"Invalid container image reference: '{image_ref}'"

            # Check if image exists locally first
            inspect_cmd = [docker_cmd, "image", "inspect", str(image_ref)]
            inspect_res = subprocess.run(inspect_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if inspect_res.returncode != 0:
                # Pull image with bounded timeout
                pull_cmd = [docker_cmd, "pull", str(image_ref)]
                try:
                    p_res = subprocess.run(
                        pull_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout_sec
                    )
                    if p_res.returncode != 0:
                        err_tail = (p_res.stderr or p_res.stdout or "Pull failed").strip().splitlines()[-5:]
                        return False, f"Docker image pull failed (code {p_res.returncode}): " + " ".join(err_tail)
                except subprocess.TimeoutExpired:
                    return False, f"Docker image pull timed out after {timeout_sec}s"
                except Exception as e:
                    return False, f"Docker pull invocation error: {e}"

            # Digest verification if declared
            expected_digest = env_vars.get("IMAGE_DIGEST") or metadata.get("image_digest")
            if expected_digest:
                digest_inspect = subprocess.run(
                    [docker_cmd, "image", "inspect", "--format", "{{index .RepoDigests 0}}", str(image_ref)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                actual_digest = digest_inspect.stdout.strip() if digest_inspect.returncode == 0 else ""
                if expected_digest.lower() not in actual_digest.lower():
                    return False, f"Image digest verification failed. Expected: '{expected_digest}', Found: '{actual_digest}'"

            metadata["image"] = str(image_ref)
            metadata["container_port"] = target_container_port
            with open(metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            return True, None

        return False, "Container deployment requires a Dockerfile or explicit image reference"

    def start_application(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        port: int,
        env_vars: Dict[str, str],
        start_cmd: Optional[List[str]] = None,
        log_file: Optional[Path] = None
    ) -> ProcessIdentityRecord:
        """
        Starts the container with unique deployment identity, strict loopback port binding,
        and bounded resource limits.
        """
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            raise RuntimeError("Docker executable not found on PATH")

        metadata_file = release_dir / "release_metadata.json"
        metadata: Dict[str, Any] = {}
        if metadata_file.exists():
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception:
                pass

        image = metadata.get("image") or env_vars.get("IMAGE") or env_vars.get("DOCKER_IMAGE")
        if not image:
            raise RuntimeError("No container image identified in release metadata")

        container_port = int(metadata.get("container_port") or 8000)
        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        container_name = f"ps-{app_id}-{clean_dep_suffix}"

        # Check if container is already running for this exact deployment (idempotent submission)
        check_res = subprocess.run(
            [docker_cmd, "inspect", "-f", "{{.State.Running}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        if check_res.returncode == 0 and check_res.stdout.strip().lower() == "true":
            # Container already running
            now_iso = get_current_iso_timestamp()
            return ProcessIdentityRecord(
                app_id=app_id,
                deployment_id=deployment_id,
                pid=0,
                port=port,
                command=[docker_cmd, "run", container_name],
                working_dir=str(release_dir),
                log_file=str(log_file) if log_file else "",
                created_at=now_iso
            )

        # Remove any stale/stopped container with the exact same deployment name
        subprocess.run([docker_cmd, "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Security: Strict loopback host bind (127.0.0.1) prevents untrusted external LAN exposure
        port_binding = f"127.0.0.1:{port}:{container_port}"

        # Bounded resource limits
        mem_limit = env_vars.get("MEMORY_LIMIT") or metadata.get("memory_limit") or "256m"
        cpu_limit = env_vars.get("CPU_LIMIT") or metadata.get("cpu_limit") or "0.5"

        run_args = [
            docker_cmd, "run", "-d",
            "--name", container_name,
            "-p", port_binding,
            "--memory", str(mem_limit),
            "--cpus", str(cpu_limit),
            "--restart", "unless-stopped"
        ]

        # Security: Filter out forbidden host and socket control environment variables
        forbidden_env = {"DOCKER_HOST", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"}
        for k, v in env_vars.items():
            if k in forbidden_env or "docker.sock" in str(v).lower():
                continue
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(k)):
                run_args.extend(["-e", f"{k}={v}"])

        run_args.extend(["-e", f"PORT={container_port}"])
        run_args.append(str(image))

        if start_cmd:
            run_args.extend(start_cmd)

        # Launch container
        run_res = subprocess.run(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if run_res.returncode != 0:
            err_msg = (run_res.stderr or run_res.stdout or "Failed to run container").strip()
            raise RuntimeError(f"Container launch failed (code {run_res.returncode}): {err_msg}")

        # Short pause to detect immediate startup crash
        time.sleep(0.5)

        # Inspect container status
        inspect_res = subprocess.run(
            [docker_cmd, "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}} {{.State.Pid}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        is_running = False
        exit_code = 0
        container_pid = 0
        if inspect_res.returncode == 0:
            parts = inspect_res.stdout.strip().split()
            if parts:
                is_running = parts[0].lower() == "true"
            if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                exit_code = int(parts[1])
            if len(parts) > 2 and parts[2].isdigit():
                container_pid = int(parts[2])

        if not is_running or exit_code != 0:
            # Capture crash logs
            log_res = subprocess.run(
                [docker_cmd, "logs", "--tail", "50", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            crash_logs = (log_res.stdout or log_res.stderr or "").strip()
            subprocess.run([docker_cmd, "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise RuntimeError(
                f"Container '{container_name}' terminated immediately with exit code {exit_code}.\n"
                f"--- Container Logs ---\n{crash_logs}"
            )

        # Write container ownership tracking record
        identity_file = release_dir / "container_identity.json"
        now_iso = get_current_iso_timestamp()
        identity_record = {
            "container_name": container_name,
            "image": str(image),
            "host_port": port,
            "container_port": container_port,
            "host_pid": container_pid,
            "started_at": now_iso
        }
        with open(identity_file, "w", encoding="utf-8") as f:
            json.dump(identity_record, f, indent=2)

        # Persistent log file header
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n=== PersonalServer Container Release: {container_name} ===\n")
                f.write(f"Timestamp: {now_iso}\n")
                f.write(f"Image: {image}\n")
                f.write(f"Port: 127.0.0.1:{port} -> {container_port}\n")
                f.write(f"Limits: Memory {mem_limit}, CPU {cpu_limit}\n\n")

        return ProcessIdentityRecord(
            app_id=app_id,
            deployment_id=deployment_id,
            pid=container_pid,
            port=port,
            command=run_args,
            working_dir=str(release_dir),
            log_file=str(log_file) if log_file else "",
            created_at=now_iso
        )

    def cleanup_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path
    ) -> None:
        """
        Safely stops and removes ONLY this deployment's container.
        Leaves other applications, other deployments, and unrelated containers completely untouched.
        """
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return

        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        container_name = f"ps-{app_id}-{clean_dep_suffix}"

        # Stop and remove this specific container
        subprocess.run([docker_cmd, "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def is_container_running(self, app_id: str, deployment_id: str) -> bool:
        """Checks if the container is currently running via docker inspect."""
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return False

        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        container_name = f"ps-{app_id}-{clean_dep_suffix}"
        res = subprocess.run(
            [docker_cmd, "inspect", "-f", "{{.State.Running}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        return res.returncode == 0 and res.stdout.strip().lower() == "true"

    def get_container_logs(self, app_id: str, deployment_id: str, tail: int = 50) -> str:
        """Retrieves bounded log output from the container."""
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return ""

        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        container_name = f"ps-{app_id}-{clean_dep_suffix}"
        res = subprocess.run(
            [docker_cmd, "logs", "--tail", str(tail), container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return (res.stdout or res.stderr or "").strip()

    def stop_container(self, app_id: str, deployment_id: str, timeout: int = 10) -> bool:
        """Gracefully stops the container."""
        docker_cmd = self._resolve_docker_bin()
        if not docker_cmd:
            return False

        clean_dep_suffix = deployment_id[:8] if deployment_id else "latest"
        container_name = f"ps-{app_id}-{clean_dep_suffix}"
        res = subprocess.run(
            [docker_cmd, "stop", "-t", str(timeout), container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return res.returncode == 0

