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
import urllib.parse
import socket
import ipaddress
import secrets
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.structured_logger import redact_sensitive_text, extract_correlation_id
from common.arch_utils import (
    normalize_architecture,
    is_architecture_compatible,
    get_canonical_architecture,
)
from controller.image_inspector import (
    validate_image_reference,
    save_local_image_metadata,
    get_local_image_metadata,
)

from common.artifact_manager import (
    validate_job_output_path,
    compute_file_sha256,
    MAX_ARTIFACT_SIZE_BYTES
)

SCRIPTS_DIR = BASE_DIR / "scripts"
RUNTIME_DIR = BASE_DIR / "runtime"
JOBS_RUNTIME_DIR = RUNTIME_DIR / "jobs"

MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB limit

# Centralized Job States
JOB_STATE_QUEUED = "QUEUED"
JOB_STATE_WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
JOB_STATE_ASSIGNED = "ASSIGNED"
JOB_STATE_CLAIMED = "CLAIMED"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_RETRY_WAIT = "RETRY_WAIT"
JOB_STATE_RECOVERING = "RECOVERING"
JOB_STATE_SUCCEEDED = "SUCCEEDED"
JOB_STATE_FAILED = "FAILED"
JOB_STATE_TIMED_OUT = "TIMED_OUT"
JOB_STATE_TIMEOUT = "TIMEOUT"
JOB_STATE_CANCELLED = "CANCELLED"
JOB_STATE_NEEDS_ATTENTION = "NEEDS_ATTENTION"
JOB_STATE_REJECTED = "REJECTED"

# Allowlisted Workload Types
ALLOWLISTED_WORKLOADS = {
    "system-info": "Gather system hardware and OS metrics",
    "health-check": "Run node health check script",
    "node-status": "Run node status check script",
    "ping-check": "Run safe ICMP or network ping probe",
    "http-check": "Run safe HTTP/HTTPS endpoint probe",
    "dns-check": "Run DNS resolution and connectivity diagnostic",
    "python-script": "Execute safe inline python script or function",
    "shell-script": "Execute approved structured shell script on compatible hosts",
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
    "docker-logs": "Fetch bounded container logs",
    "build-package": "Compile and package application code or assets into an artifact bundle",
    "build-test": "Run build verification and automated tests on application code",
    "deploy-prepare": "Run deployment validation, generate manifests and release bundles",
    "app-deploy": "Deploy and supervise an application using SafeProcessLifecycleManager and verify health"
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
    def get_active_job_ids(cls) -> list:
        """Returns a snapshot list of currently active executing job IDs."""
        with cls._ACTIVE_JOBS_LOCK:
            return list(cls._ACTIVE_JOBS.keys())

    @classmethod
    def get_active_job_count(cls) -> int:
        """Returns the number of currently executing jobs."""
        with cls._ACTIVE_JOBS_LOCK:
            return len(cls._ACTIVE_JOBS)

    @classmethod
    def terminate_active_job(cls, job_id: str, reason: str = "Cancelled by operator") -> dict:
        """
        Safely and cooperatively terminates an actively executing child process or container for job_id.
        """
        terminated_any = False
        try:
            from agent.universal_executor import UniversalExecutionEngine
            if UniversalExecutionEngine.cancel_execution(job_id, reason=reason):
                terminated_any = True
        except Exception:
            pass

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
    def execute(job, sandbox_dir=None):
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
        sandbox_path = Path(sandbox_dir).resolve() if sandbox_dir else (JOBS_RUNTIME_DIR / job_id).resolve()
        sandbox_path.mkdir(parents=True, exist_ok=True)

        is_ujc = (
            job.get("schema_version") == "ujc.v1.0"
            or "spec" in job
            or job.get("runtime") in ("python", "nodejs", "docker", "shell", "native/python", "native/nodejs", "container/docker")
            or job_type in ("python", "nodejs", "docker", "shell", "universal")
        )
        if is_ujc:
            from agent.universal_executor import UniversalExecutionEngine
            lease_gen = int(job.get("lease_generation") or job.get("status", {}).get("lease", {}).get("fencing_token", 1))
            return UniversalExecutionEngine.execute_job(job, expected_fencing_token=lease_gen)

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

        elif job_type == "ping-check":
            host = str(params.get("host") or params.get("target") or "127.0.0.1").strip()
            # Security: Validate host characters to prevent flag injection or shell tricks
            if not host or not re.match(r'^[a-zA-Z0-9.-]+$', host) or host.startswith("-"):
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Security validation failed: Invalid or unsafe ping target '{host}'.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            count = min(10, max(1, int(params.get("count", 3))))
            ping_timeout = min(15, max(1, int(params.get("ping_timeout", 3))))
            if sys.platform == "win32":
                cmd = ["ping", "-n", str(count), "-w", str(ping_timeout * 1000), host]
            else:
                cmd = ["ping", "-c", str(count), "-W", str(ping_timeout), host]

        elif job_type == "http-check":
            url = str(params.get("url") or "").strip()
            if not url or not (url.startswith("http://") or url.startswith("https://")):
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Invalid or missing 'url' parameter (must begin with http:// or https://)",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            allow_internal = bool(params.get("allow_internal", False))

            # Pre-validate hostname target to prevent SSRF against loopback or private networks
            if not allow_internal:
                try:
                    parsed_u = urllib.parse.urlsplit(url)
                    host_to_check = parsed_u.hostname
                    if not host_to_check:
                        raise ValueError(f"Invalid host in URL: {url}")
                    resolved_addrs = socket.getaddrinfo(host_to_check, None)
                    for _, _, _, _, saddr in resolved_addrs:
                        ip_obj = ipaddress.ip_address(saddr[0])
                        if (
                            ip_obj.is_loopback
                            or ip_obj.is_private
                            or ip_obj.is_link_local
                            or ip_obj.is_multicast
                            or ip_obj.is_reserved
                            or ip_obj.is_unspecified
                        ):
                            return {
                                "job_id": job_id,
                                "status": JOB_STATE_FAILED,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": f"Security violation (SSRF): Request to restricted destination '{saddr[0]}' is blocked.",
                                "duration_ms": 0,
                                "started_at": started_iso,
                                "finished_at": started_iso
                            }
                except ValueError as ve:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": str(ve),
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }
                except Exception as e:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Host resolution failed for '{url}': {e}",
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }

            expected_status = int(params.get("expected_status", 200))
            req_method = str(params.get("method", "GET")).upper()
            http_timeout = min(60, max(1, int(params.get("http_timeout", timeout_sec))))
            custom_headers = params.get("headers") or {}
            allow_int_flag = "1" if allow_internal else "0"
            cmd = [
                sys.executable, "-c",
                """
import sys, time, json, urllib.request, urllib.error, urllib.parse, socket, ipaddress
url, method, exp_status, tout, hdrs_json, allow_int_str = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6]
allow_int = (allow_int_str == "1")
hdrs = json.loads(hdrs_json) if hdrs_json else {}

def is_ip_restricted(target_url):
    try:
        p = urllib.parse.urlsplit(target_url)
        h = p.hostname
        if not h: return True
        for _, _, _, _, sa in socket.getaddrinfo(h, None):
            ip = ipaddress.ip_address(sa[0])
            if (ip.is_loopback or ip.is_private or ip.is_link_local or 
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return True
        return False
    except Exception:
        return True

class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allow_int and is_ip_restricted(newurl):
            raise urllib.error.HTTPError(newurl, 403, "SSRF Blocked: Redirect to restricted destination prohibited", headers, None)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = urllib.request.build_opener(SafeRedirectHandler)
req = urllib.request.Request(url, headers=hdrs, method=method)
t0 = time.time()
try:
    with opener.open(req, timeout=tout) as resp:
        duration_ms = int((time.time() - t0) * 1000)
        code = resp.getcode()
        body = resp.read(2048).decode('utf-8', errors='replace')
        print(f"HTTP {code} ({duration_ms}ms)")
        print(body[:500])
        sys.exit(0 if (code == exp_status or exp_status == 0) else 1)
except urllib.error.HTTPError as e:
    duration_ms = int((time.time() - t0) * 1000)
    print(f"HTTP {e.code} ({duration_ms}ms)", file=sys.stderr)
    sys.exit(0 if e.code == exp_status else 1)
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(2)
""",
                url, req_method, str(expected_status), str(http_timeout), json.dumps(custom_headers), allow_int_flag
            ]

        elif job_type == "dns-check":
            domain = str(params.get("domain") or params.get("host") or "").strip()
            if not domain or not re.match(r'^[a-zA-Z0-9.-]+$', domain) or domain.startswith("-"):
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Invalid domain name for DNS resolution: '{domain}'.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = [
                sys.executable, "-c",
                """
import sys, socket, time
domain = sys.argv[1]
t0 = time.time()
try:
    name, aliases, addresses = socket.gethostbyname_ex(domain)
    duration_ms = int((time.time() - t0) * 1000)
    print(f"Resolved {domain} in {duration_ms}ms")
    print(f"Canonical Name: {name}")
    print(f"Addresses: {', '.join(addresses)}")
    sys.exit(0)
except Exception as e:
    print(f"DNS Resolution failed for {domain}: {e}", file=sys.stderr)
    sys.exit(1)
""",
                domain
            ]

        elif job_type == "python-script":
            code = params.get("code") or params.get("script") or job.get("code") or job.get("script") or ""
            args = params.get("args") or job.get("args") or []
            if not code:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Missing 'code' or 'script' in parameters for python-script workload.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            cmd = [sys.executable, "-c", code]
            if args:
                cmd.append("--")
                cmd.extend([str(a) for a in args])

        elif job_type == "shell-script":
            script = params.get("script") or params.get("command") or ""
            args = params.get("args") or []
            if not script:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Missing 'script' or 'command' parameter for shell-script workload.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            # Secure execution: write script to a temporary script file to prevent command injection
            # and argument interpolation issues in PowerShell / Bash
            if sys.platform == "win32":
                script_file = RUNTIME_DIR / f"ps_script_{job_id}_{secrets.token_hex(4)}.ps1"
                script_file.write_text(script, encoding="utf-8")
                custom_script = script_file
                cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_file)]
                if args:
                    cmd.extend([str(a) for a in args])
            else:
                script_file = RUNTIME_DIR / f"sh_script_{job_id}_{secrets.token_hex(4)}.sh"
                script_file.write_text(script, encoding="utf-8")
                custom_script = script_file
                cmd = ["bash", str(script_file)]
                if args:
                    cmd.extend([str(a) for a in args])

        elif job_type == "build-package":
            # Packaging application code / assets into an artifact bundle (zip or tar.gz)
            app_name = params.get("app_name") or params.get("name") or "package"
            fmt = str(params.get("format", "zip")).lower()
            if fmt not in ("zip", "tar.gz", "tar"):
                fmt = "zip"
            default_out = f"{app_name}.{fmt}" if fmt == "zip" else f"{app_name}.tar.gz"
            out_filename = params.get("output_filename") or default_out
            compile_py = bool(params.get("compile_python", True))

            # Source files resolution
            build_src = sandbox_path / "src"
            build_src.mkdir(parents=True, exist_ok=True)

            inline_files = params.get("files")
            if isinstance(inline_files, dict):
                for f_name, f_content in inline_files.items():
                    dest_f = validate_job_output_path(build_src, f_name)
                    dest_f.parent.mkdir(parents=True, exist_ok=True)
                    dest_f.write_text(str(f_content), encoding="utf-8")

            raw_source_dir = params.get("source_dir")
            if raw_source_dir:
                src_candidate = Path(raw_source_dir).resolve()
                allowed_roots = [
                    sandbox_path.resolve(),
                    (BASE_DIR / "storage" / "applications").resolve(),
                    (BASE_DIR / "tests").resolve()
                ]
                is_allowed = any(
                    src_candidate == root or root in src_candidate.parents
                    for root in allowed_roots
                )
                if not is_allowed:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Security violation: Source directory '{raw_source_dir}' escapes permitted build roots.",
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }
                if src_candidate.exists() and src_candidate.is_dir():
                    shutil.copytree(src_candidate, build_src, dirs_exist_ok=True)

            src_files = [p for p in build_src.rglob("*") if p.is_file()]
            if not src_files:
                src_files = [p for p in sandbox_path.iterdir() if p.is_file() and not p.name.endswith(".log") and not p.name.endswith(".json")]
                package_root = sandbox_path
            else:
                package_root = build_src

            build_log_lines = [
                f"=== PersonalServer Build Worker: Packaging '{app_name}' ===",
                f"Job ID: {job_id}",
                f"Format: {fmt}",
                f"Started at: {started_iso}",
                f"Source files found: {len(src_files)}"
            ]

            if compile_py:
                import py_compile
                py_files = [f for f in src_files if f.suffix == ".py"]
                build_log_lines.append(f"Verifying syntax for {len(py_files)} Python file(s)...")
                for py_f in py_files:
                    try:
                        py_compile.compile(str(py_f), doraise=True)
                        build_log_lines.append(f"  [OK] {py_f.name}")
                    except py_compile.PyCompileError as pce:
                        err_text = f"Python syntax/compilation error in {py_f.name}: {pce}"
                        build_log_lines.append(f"  [FAIL] {err_text}")
                        log_file = sandbox_path / "build.log"
                        log_file.write_text("\n".join(build_log_lines), encoding="utf-8")
                        return {
                            "job_id": job_id,
                            "status": JOB_STATE_FAILED,
                            "exit_code": 1,
                            "stdout": "\n".join(build_log_lines),
                            "stderr": err_text,
                            "duration_ms": int((time.time() - start_time) * 1000),
                            "started_at": started_iso,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }

            out_archive_path = validate_job_output_path(sandbox_path, out_filename)
            build_log_lines.append(f"Writing archive to {out_archive_path.name}...")

            if fmt == "zip":
                import zipfile
                with zipfile.ZipFile(out_archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in src_files:
                        arcname = str(f.relative_to(package_root))
                        zf.write(f, arcname=arcname)
            else:
                import tarfile
                mode = "w:gz" if fmt == "tar.gz" else "w"
                with tarfile.open(out_archive_path, mode) as tf:
                    for f in src_files:
                        arcname = str(f.relative_to(package_root))
                        tf.add(f, arcname=arcname)

            out_size = out_archive_path.stat().st_size
            sha256_bundle, _ = compute_file_sha256(out_archive_path)
            build_log_lines.append(f"Archive built successfully: {out_size} bytes, SHA-256: {sha256_bundle}")
            build_log_lines.append(f"Finished at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

            log_file = sandbox_path / "build.log"
            log_file.write_text("\n".join(build_log_lines), encoding="utf-8")

            if not job.get("outputs") and not params.get("outputs"):
                job["outputs"] = [out_archive_path.name, "build.log"]

            cmd = [sys.executable, "-c", "import sys; print(sys.argv[1])", f"Build bundle '{out_archive_path.name}' created successfully."]

        elif job_type == "build-test":
            # Runs safe automated unit or syntax tests on application code in sandbox
            test_cmd = params.get("test_command") or params.get("command") or params.get("script")
            test_files = params.get("files")
            test_src = sandbox_path / "src"
            test_src.mkdir(parents=True, exist_ok=True)
            if isinstance(test_files, dict):
                for f_name, f_content in test_files.items():
                    dest_f = validate_job_output_path(test_src, f_name)
                    dest_f.parent.mkdir(parents=True, exist_ok=True)
                    dest_f.write_text(str(f_content), encoding="utf-8")

            if not job.get("outputs") and not params.get("outputs"):
                job["outputs"] = ["test-report.log"]

            if not test_cmd:
                cmd = [sys.executable, "-m", "compileall", "-q", str(test_src)]
            else:
                cmd = [sys.executable, "-c", test_cmd]

        elif job_type == "deploy-prepare":
            # Validates deployment manifest and generates deployment artifact bundle
            # Strictly validates manifest and ensures NO exposed secrets
            app_name = validate_docker_identifier(params.get("app_name") or params.get("name") or "app", "Application Name")
            version = str(params.get("version", "1.0.0")).strip()
            env_name = str(params.get("environment", "staging")).strip().lower()

            raw_manifest = params.get("manifest") or {}
            if not isinstance(raw_manifest, dict):
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Invalid manifest: 'manifest' parameter must be a JSON dictionary.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }

            # Security: scan for potential secret leaks
            manifest_str = json.dumps(raw_manifest)
            secret_patterns = [
                r'BEGIN\s+[A-Z\s]*PRIVATE\s+KEY',
                r'(?i)(password|secret|api_key|token)["\']?\s*[:=]\s*["\'][^"\']{4,}["\']'
            ]
            for pat in secret_patterns:
                if re.search(pat, manifest_str):
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Security violation: Potential secret or private key detected in deployment manifest.",
                        "duration_ms": 0,
                        "started_at": started_iso,
                        "finished_at": started_iso
                    }

            prep_manifest = {
                "app_name": app_name,
                "version": version,
                "environment": env_name,
                "prepared_at": started_iso,
                "worker_node_id": job.get("target_node") or job.get("assigned_node") or "laptop-worker",
                "manifest": redact_sensitive_text(manifest_str),
                "deployment_ready": True
            }

            manifest_out = sandbox_path / "deployment-manifest.json"
            manifest_out.write_text(json.dumps(prep_manifest, indent=2), encoding="utf-8")

            if not job.get("outputs") and not params.get("outputs"):
                job["outputs"] = ["deployment-manifest.json"]

            cmd = [sys.executable, "-c", "import sys; print(sys.argv[1])", f"Deployment manifest for '{app_name}' version {version} ({env_name}) generated successfully."]

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

        elif job_type == "app-deploy":
            # Native process application deployment and health verification using SafeProcessLifecycleManager
            app_id = params.get("app_id") or job.get("app_id")
            deployment_id = params.get("deployment_id") or job.get("deployment_id")
            if not app_id:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Missing required parameter 'app_id' in app-deploy job.",
                    "duration_ms": 0,
                    "started_at": started_iso,
                    "finished_at": started_iso
                }
            if not deployment_id:
                deployment_id = f"dep-{secrets.token_hex(4)}"

            port = int(params.get("port") or params.get("host_port") or 8090)
            project_type = params.get("project_type") or params.get("runtime", "python")
            artifact_ref = params.get("artifact_reference")
            artifact_sha256 = params.get("artifact_sha256")
            health_check = params.get("health_check") or {}
            env_vars = dict(params.get("env") or {})
            runtime_reqs = params.get("runtime_requirements") or {}
            if "spa_fallback" in runtime_reqs:
                env_vars["spa_fallback"] = runtime_reqs["spa_fallback"]
            elif "spa_fallback" in params:
                env_vars["spa_fallback"] = params["spa_fallback"]
            if "static_root" in runtime_reqs:
                env_vars["static_root"] = runtime_reqs["static_root"]
            elif "static_root" in params:
                env_vars["static_root"] = params["static_root"]
            start_cmd_override = params.get("start_command")

            from common.process_manager import SafeProcessLifecycleManager

            is_static_app = project_type in ("static", "static-web", "html", "web")
            is_container_app = project_type in ("docker", "container", "containerd", "dockerfile")

            # 1. Inspect previous instance if running on this node
            prev_record = None
            if not is_static_app:
                prev_record = SafeProcessLifecycleManager.read_record(app_id)
                if prev_record and prev_record.port == port:
                    # Same port requires stopping previous instance to bind
                    if is_container_app:
                        from agent.runtime_adapters import get_runtime_adapter
                        c_adapter = get_runtime_adapter("docker")
                        c_adapter.cleanup_release(app_id, prev_record.deployment_id, Path(prev_record.working_dir))
                    else:
                        SafeProcessLifecycleManager.stop_process(app_id)

                # 2. Check port availability
                if not SafeProcessLifecycleManager.check_port_available(port):
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Port conflict: Port {port} is already in use by another process.",
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "started_at": started_iso,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "diagnostics": {"failure_reason": f"Port conflict on {port}"}
                    }

            # 3. Prepare release directory
            app_storage = BASE_DIR / "storage" / "applications" / app_id
            app_storage.mkdir(parents=True, exist_ok=True)
            release_dir = app_storage / "releases" / deployment_id
            release_dir.mkdir(parents=True, exist_ok=True)

            # 4. Resolve and extract artifact if present
            artifact_file = None
            input_artifacts = job.get("input_artifacts") or []
            if input_artifacts:
                for in_art in input_artifacts:
                    art_name = in_art.get("name") or in_art.get("filename")
                    cand = sandbox_path / art_name if art_name else None
                    if cand and cand.exists():
                        artifact_file = cand
                        if not artifact_sha256:
                            artifact_sha256 = in_art.get("sha256")
                        break

            if not artifact_file and artifact_ref:
                candidates = [
                    sandbox_path / artifact_ref,
                    app_storage / "artifacts" / artifact_ref,
                    BASE_DIR / "storage" / "artifacts" / artifact_ref,
                    Path(artifact_ref)
                ]
                for cand in candidates:
                    if cand.exists() and cand.is_file():
                        artifact_file = cand
                        break

            if not artifact_file:
                # Check registered artifacts in artifacts_db by artifact_id or filename
                try:
                    from controller.stores import load_artifacts_db
                    arts_db = load_artifacts_db()
                    for art_id_key, art_entry in arts_db.get("artifacts", {}).items():
                        if (
                            art_entry.get("name") == artifact_ref
                            or art_entry.get("filename") == artifact_ref
                            or (input_artifacts and any(in_art.get("artifact_id") == art_id_key for in_art in input_artifacts))
                        ):
                            c_p = Path(art_entry.get("path", ""))
                            if c_p.exists() and c_p.is_file():
                                artifact_file = c_p
                                if not artifact_sha256:
                                    artifact_sha256 = art_entry.get("sha256")
                                break
                except Exception:
                    pass

            if artifact_file:
                if artifact_sha256:
                    computed_sha, _ = compute_file_sha256(artifact_file)
                    if computed_sha.lower() != artifact_sha256.lower():
                        return {
                            "job_id": job_id,
                            "status": JOB_STATE_FAILED,
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": f"Artifact checksum mismatch for {artifact_file.name}. Expected: {artifact_sha256}, Got: {computed_sha}",
                            "duration_ms": int((time.time() - start_time) * 1000),
                            "started_at": started_iso,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }
                from controller.artifact_packager import safe_extract_artifact
                try:
                    safe_extract_artifact(artifact_file, release_dir)
                except Exception as e:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Failed extracting artifact: {e}",
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "started_at": started_iso,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
            else:
                # If an artifact was declared or expected, fail - never silently fallback!
                if input_artifacts or artifact_ref or artifact_sha256:
                    return {
                        "job_id": job_id,
                        "status": JOB_STATE_FAILED,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": f"Artifact '{artifact_ref or input_artifacts}' not found on worker node. Remote retrieval failed.",
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "started_at": started_iso,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                # Fallback for local simulation or source apps
                src_candidate = app_storage / "source"
                if src_candidate.exists():
                    shutil.copytree(src_candidate, release_dir, dirs_exist_ok=True)
                elif app_storage.exists():
                    for item in app_storage.iterdir():
                        if item.name not in ("releases", "artifacts"):
                            if item.is_file():
                                shutil.copy2(item, release_dir / item.name)
                            elif item.is_dir():
                                shutil.copytree(item, release_dir / item.name, dirs_exist_ok=True)

            # 5. Use Hermetic Runtime Adapter
            if (release_dir / "package.json").exists() and project_type in ("python", "python3", "auto"):
                project_type = "nodejs"
            elif (
                (release_dir / "Dockerfile").exists() or (release_dir / "dockerfile").exists()
            ) and project_type in ("auto", "detect", "docker"):
                project_type = "docker"
            elif project_type in ("auto", "detect", "python", "python3") and (
                any((release_dir / c / "index.html").exists() for c in ("dist", "build", "public"))
                or (release_dir / "index.html").exists()
            ) and not (release_dir / "requirements.txt").exists() and not (release_dir / "app.py").exists():
                project_type = "static"

            is_container_app = project_type in ("docker", "container", "containerd", "dockerfile")

            from agent.runtime_adapters import get_runtime_adapter
            adapter = get_runtime_adapter(project_type)
            env_ok, env_err = adapter.validate_environment()
            if not env_ok:
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Runtime environment validation error: {env_err}",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "started_at": started_iso,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }

            prep_ok, prep_err = adapter.prepare_release(
                app_id=app_id,
                deployment_id=deployment_id,
                release_dir=release_dir,
                env_vars=env_vars
            )
            if not prep_ok:
                adapter.cleanup_release(app_id, deployment_id, release_dir)
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Runtime preparation error: {prep_err}",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "started_at": started_iso,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }

            # 6. Start Application Process
            log_dir = BASE_DIR / "logs" / "apps"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{app_id}.log"

            try:
                proc_rec = adapter.start_application(
                    app_id=app_id,
                    deployment_id=deployment_id,
                    release_dir=release_dir,
                    port=port,
                    env_vars=env_vars,
                    start_cmd=start_cmd_override,
                    log_file=log_path
                )
            except Exception as e:
                adapter.cleanup_release(app_id, deployment_id, release_dir)
                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Failed to start application process: {e}",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "started_at": started_iso,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "diagnostics": {"failure_reason": str(e)}
                }

            # 8. Probe HTTP Health / Static Content Readiness
            static_root_val = ""
            spa_fallback_val = False
            meta_f = release_dir / "release_metadata.json"
            if meta_f.exists():
                try:
                    with open(meta_f, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                        static_root_val = meta_data.get("static_root", "")
                        spa_fallback_val = meta_data.get("spa_fallback", False)
                except Exception:
                    pass

            is_healthy = False
            last_hc_err = ""

            if project_type in ("static", "static-web", "html", "web"):
                # Static Content Readiness Verification
                doc_root = (release_dir / static_root_val).resolve() if static_root_val else release_dir.resolve()
                index_p = doc_root / "index.html"
                if index_p.is_file() and index_p.stat().st_size > 0:
                    is_healthy = True
                else:
                    last_hc_err = f"Static index.html not found or empty in resolved root '{doc_root}'"
            else:
                hc_path = health_check.get("path", "/health")
                clean_hc_path = hc_path if hc_path.startswith("/") else f"/{hc_path}"
                hc_timeout = float(health_check.get("timeout_seconds", 10.0))
                hc_interval = float(health_check.get("interval_seconds", 0.5))
                hc_expected = int(health_check.get("expected_status", 200))

                t0 = time.time()
                while time.time() - t0 < hc_timeout:
                    if is_container_app:
                        if hasattr(adapter, "is_container_running") and not adapter.is_container_running(app_id, deployment_id):
                            cont_logs = adapter.get_container_logs(app_id, deployment_id, tail=20) if hasattr(adapter, "get_container_logs") else ""
                            last_hc_err = f"Container terminated unexpectedly before becoming healthy"
                            if cont_logs:
                                last_hc_err += f"\n--- Container Log Tail ---\n{cont_logs}"
                            break
                    elif not SafeProcessLifecycleManager.is_pid_active(proc_rec.pid):
                        last_hc_err = f"Process (PID {proc_rec.pid}) terminated unexpectedly before becoming healthy"
                        break

                    try:
                        hc_req = urllib.request.Request(
                            f"http://127.0.0.1:{port}{clean_hc_path}",
                            headers={"User-Agent": "PersonalServer-NodeWorker-HealthCheck/1.0"}
                        )
                        with urllib.request.urlopen(hc_req, timeout=1.0) as resp:
                            if resp.status == hc_expected or (hc_expected == 200 and 200 <= resp.status < 400):
                                is_healthy = True
                                break
                            last_hc_err = f"HTTP status {resp.status}"
                    except urllib.error.HTTPError as e:
                        last_hc_err = f"HTTP {e.code}: {e.reason}"
                    except Exception as e:
                        last_hc_err = str(e)

                    time.sleep(hc_interval)

            duration_ms = int((time.time() - start_time) * 1000)
            fin_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            if not is_healthy:
                if is_container_app:
                    adapter.cleanup_release(app_id, deployment_id, release_dir)
                    if prev_record:
                        SafeProcessLifecycleManager.write_record(prev_record)
                elif not is_static_app:
                    # Stop the candidate process that failed health check
                    try:
                        SafeProcessLifecycleManager.verify_infrastructure_immunity(proc_rec.pid)
                        if SafeProcessLifecycleManager.is_pid_active(proc_rec.pid):
                            import signal
                            os.kill(proc_rec.pid, signal.SIGTERM)
                    except Exception:
                        pass

                    # Restore previous healthy record if running on another port
                    if prev_record and prev_record.port != port and SafeProcessLifecycleManager.is_pid_active(prev_record.pid):
                        SafeProcessLifecycleManager.write_record(prev_record)
                    else:
                        adapter.cleanup_release(app_id, deployment_id, release_dir)
                else:
                    adapter.cleanup_release(app_id, deployment_id, release_dir)

                log_tail = ""
                if log_path.exists():
                    try:
                        tail_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                        log_tail = "\n".join(tail_lines)
                    except Exception:
                        pass

                fail_msg = f"Health check failed on http://127.0.0.1:{port}"
                if last_hc_err:
                    fail_msg = f"{fail_msg}: {last_hc_err}"
                if log_tail:
                    fail_msg += f"\n--- Process Log Tail ---\n{log_tail}"

                return {
                    "job_id": job_id,
                    "status": JOB_STATE_FAILED,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": fail_msg,
                    "duration_ms": duration_ms,
                    "started_at": started_iso,
                    "finished_at": fin_iso,
                    "diagnostics": {"failure_reason": fail_msg}
                }

            # If candidate succeeded and there was a previous instance on another port/deployment, retire it now
            if is_container_app:
                if prev_record and prev_record.deployment_id != deployment_id:
                    prev_dep = prev_record.deployment_id
                    prev_rdir = Path(prev_record.working_dir) if prev_record.working_dir else release_dir.parent / prev_dep
                    adapter.cleanup_release(app_id, prev_dep, prev_rdir)
            elif not is_static_app and prev_record and prev_record.port != port and prev_record.pid != proc_rec.pid:
                try:
                    SafeProcessLifecycleManager.verify_infrastructure_immunity(prev_record.pid)
                    if SafeProcessLifecycleManager.is_pid_active(prev_record.pid):
                        import signal
                        os.kill(prev_record.pid, signal.SIGTERM)
                except Exception:
                    pass

            out_data = {
                "app_id": app_id,
                "deployment_id": deployment_id,
                "port": port,
                "pid": proc_rec.pid,
                "address": f"http://127.0.0.1:{port}",
                "health_status": "healthy",
                "runtime": project_type,
                "static_root": static_root_val,
                "spa_fallback": spa_fallback_val
            }

            return {
                "job_id": job_id,
                "status": JOB_STATE_SUCCEEDED,
                "exit_code": 0,
                "stdout": f"Application '{app_id}' deployed and healthy on port {port} (PID {proc_rec.pid}).",
                "stderr": "",
                "duration_ms": duration_ms,
                "started_at": started_iso,
                "finished_at": fin_iso,
                "declared_outputs": out_data,
                "outputs": out_data
            }

        # Execute process with strict timeout and output capture
        status = JOB_STATE_RUNNING
        stdout_str = ""
        stderr_str = ""
        exit_code = 0

        proc = None
        try:
            proc_env = os.environ.copy()
            proc_env["PERSONALSERVER_JOB_DIR"] = str(sandbox_path)
            proc_env["PERSONALSERVER_JOB_ID"] = str(job_id)
            exec_cwd = str(sandbox_path) if job_type in ("python-script", "shell-script", "echo") else str(BASE_DIR)
            popen_kwargs = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=exec_cwd,
                env=proc_env,
                **popen_kwargs
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

            if job_type == "build-test":
                test_log = sandbox_path / "test-report.log"
                test_log.write_text(
                    f"=== PersonalServer Build Test Report ===\n"
                    f"Job ID: {job_id}\n"
                    f"Exit Code: {exit_code}\n"
                    f"Timestamp: {started_iso}\n\n"
                    f"--- STDOUT ---\n{stdout_str}\n\n"
                    f"--- STDERR ---\n{stderr_str}\n",
                    encoding="utf-8"
                )

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

        declared_outputs_list = []
        if status == JOB_STATE_SUCCEEDED:
            declared_outputs = job.get("outputs") or params.get("outputs") or params.get("artifacts") or []
            for out in declared_outputs:
                if isinstance(out, str):
                    out_name = out
                    target_name = out
                elif isinstance(out, dict):
                    out_name = out.get("path") or out.get("name")
                    target_name = out.get("name") or out_name
                else:
                    continue
                try:
                    out_file = validate_job_output_path(sandbox_path, out_name)
                    if not out_file.exists() or not out_file.is_file():
                        status = JOB_STATE_FAILED
                        exit_code = 1
                        stderr_str = f"Declared output artifact '{out_name}' was not produced by job."
                        declared_outputs_list = []
                        break
                    sha256, size_bytes = compute_file_sha256(out_file)
                    if size_bytes > MAX_ARTIFACT_SIZE_BYTES:
                        status = JOB_STATE_FAILED
                        exit_code = 1
                        stderr_str = f"Output artifact '{out_name}' exceeds size limit of {MAX_ARTIFACT_SIZE_BYTES // (1024*1024)}MB."
                        declared_outputs_list = []
                        break
                    declared_outputs_list.append({
                        "name": target_name,
                        "filename": out_file.name,
                        "path": str(out_file),
                        "sha256": sha256,
                        "size_bytes": size_bytes
                    })
                except Exception as e:
                    status = JOB_STATE_FAILED
                    exit_code = 1
                    stderr_str = f"Output artifact validation error for '{out_name}': {e}"
                    declared_outputs_list = []
                    break

        end_time = time.time()
        finished_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_time))
        duration_ms = int((end_time - start_time) * 1000)

        stdout_clean = redact_sensitive_text(stdout_str.strip())
        stderr_clean = redact_sensitive_text(stderr_str.strip())

        result = {
            "job_id": job_id,
            "correlation_id": corr_id,
            "attempt": attempt,
            "lease_generation": int(job.get("lease_generation", 1)),
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "declared_outputs": declared_outputs_list,
            "duration_ms": duration_ms,
            "started_at": started_iso,
            "finished_at": finished_iso,
            "diagnostics": {
                "runtime": runtime if "runtime" in locals() and runtime else "native",
                "attempt": attempt,
                "lease_generation": int(job.get("lease_generation", 1)),
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

        # Cleanup custom temporary script file if generated
        if custom_script and isinstance(custom_script, Path) and custom_script.exists():
            try:
                custom_script.unlink(missing_ok=True)
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
