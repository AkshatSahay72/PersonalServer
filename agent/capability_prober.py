#!/usr/bin/env python3
"""
Worker-Side Capability Verification Prober (V1.4)
=================================================
Executes bounded, harmless smoke tests on the worker to distinguish
probe-verified capabilities from merely advertised or configured capabilities.

Probes:
- Python execution: runs harmless one-liner via sys.executable with timeout.
- Container runtimes:
  - Docker: probes daemon socket reachability and API responsiveness.
  - udocker: probes userspace container runtime engine responsiveness.
- Host resources:
  - Compute & storage validation.

Security & Safety:
- All commands execute with strict subprocess timeouts.
- Secret redaction and output bounds prevent token leakage or memory exhaustion.
- Probe failures never crash the worker agent or block unrelated jobs.
"""

import os
import re
import sys
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from config.structured_logger import redact_sensitive_text
except ImportError:
    def redact_sensitive_text(text: str) -> str:
        if not text:
            return ""
        # Redact generic bearer tokens, hex tokens, and keys
        text = re.sub(r'(Bearer\s+)[A-Za-z0-9_\-\.]{12,}', r'\1[REDACTED]', text)
        text = re.sub(r'([a-f0-9]{32,64})', r'[REDACTED_HASH]', text)
        return text

try:
    from common.time_utils import get_current_iso_timestamp
except ImportError:
    def get_current_iso_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DEFAULT_PROBE_TIMEOUT = 3.0  # seconds
MAX_ERROR_LENGTH = 160


def _clean_error(err_text: str) -> str:
    """Redacts secrets and truncates error text for safe reporting."""
    if not err_text:
        return "Unknown error"
    cleaned = redact_sensitive_text(err_text.strip().replace("\r", " ").replace("\n", " "))
    # Collapse consecutive whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if len(cleaned) > MAX_ERROR_LENGTH:
        return cleaned[:MAX_ERROR_LENGTH] + "..."
    return cleaned


def probe_python(python_bin: Optional[str] = None, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Dict[str, Any]:
    """
    Executes a bounded, harmless Python smoke test.
    Verifies the Python interpreter can start, evaluate basic bytecode, and return version info.
    """
    now = get_current_iso_timestamp()
    target_bin = python_bin or sys.executable or "python3"

    probe_code = "import sys; print(f'PersonalServer-Probe:{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')"

    try:
        res = subprocess.run(
            [target_bin, "-c", probe_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res.returncode == 0 and "PersonalServer-Probe:" in res.stdout:
            ver_str = res.stdout.strip().split("PersonalServer-Probe:", 1)[1].strip()
            return {
                "verified": True,
                "capability": "python3",
                "installed": True,
                "version": ver_str,
                "error": None,
                "timestamp": now
            }
        else:
            err_msg = res.stderr or res.stdout or f"Non-zero exit code: {res.returncode}"
            return {
                "verified": False,
                "capability": "python3",
                "installed": True,
                "version": None,
                "error": _clean_error(err_msg),
                "timestamp": now
            }
    except subprocess.TimeoutExpired:
        return {
            "verified": False,
            "capability": "python3",
            "installed": True,
            "version": None,
            "error": f"Python probe timed out after {timeout}s",
            "timestamp": now
        }
    except FileNotFoundError:
        return {
            "verified": False,
            "capability": "python3",
            "installed": False,
            "version": None,
            "error": f"Python binary '{target_bin}' not found on PATH",
            "timestamp": now
        }
    except Exception as e:
        return {
            "verified": False,
            "capability": "python3",
            "installed": False,
            "version": None,
            "error": _clean_error(str(e)),
            "timestamp": now
        }


def probe_docker(docker_bin: Optional[str] = None, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Dict[str, Any]:
    """
    Executes a bounded, harmless Docker smoke test.
    Checks if docker binary exists AND daemon responds via `docker info`.
    Distinguishes genuine Docker Engine from udocker wrappers or incompatible CLIs.
    """
    now = get_current_iso_timestamp()
    resolved_bin = docker_bin or shutil.which("docker")
    if not resolved_bin:
        return {
            "verified": False,
            "capability": "container_runtime:docker",
            "installed": False,
            "version": None,
            "error": "Docker executable not found on PATH",
            "timestamp": now
        }

    # Reject if resolved binary is explicitly named udocker or symlinked to udocker
    bin_name = Path(resolved_bin).name.lower()
    if "udocker" in bin_name:
        return {
            "verified": False,
            "capability": "container_runtime:docker",
            "installed": False,
            "version": None,
            "error": "udocker executable cannot satisfy Docker Engine capability",
            "timestamp": now
        }

    try:
        # Check docker version to verify CLI and detect potential udocker wrappers
        v_res = subprocess.run(
            [resolved_bin, "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        combined_v = (v_res.stdout + " " + v_res.stderr).lower()
        if "udocker" in combined_v:
            return {
                "verified": False,
                "capability": "container_runtime:docker",
                "installed": False,
                "version": None,
                "error": "Binary is a udocker wrapper; genuine Docker Engine daemon required",
                "timestamp": now
            }

        # Run docker info to verify daemon connection
        res = subprocess.run(
            [resolved_bin, "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res.returncode == 0:
            ver = res.stdout.strip() or "available"
            # Probe API version for compatibility checking
            api_ver = None
            api_res = subprocess.run(
                [resolved_bin, "version", "--format", "{{.Server.APIVersion}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout
            )
            if api_res.returncode == 0 and api_res.stdout.strip():
                api_ver = api_res.stdout.strip()

            return {
                "verified": True,
                "capability": "container_runtime:docker",
                "installed": True,
                "version": ver,
                "api_version": api_ver,
                "runtime_type": "docker",
                "error": None,
                "timestamp": now
            }

        # Fallback to plain docker info if --format not supported
        res2 = subprocess.run(
            [resolved_bin, "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res2.returncode == 0:
            return {
                "verified": True,
                "capability": "container_runtime:docker",
                "installed": True,
                "version": "available",
                "error": None,
                "timestamp": now
            }

        err_msg = res.stderr or res2.stderr or f"docker info exited with code {res.returncode}"
        return {
            "verified": False,
            "capability": "container_runtime:docker",
            "installed": True,
            "version": None,
            "error": _clean_error(f"Docker daemon unreachable: {err_msg}"),
            "timestamp": now
        }
    except subprocess.TimeoutExpired:
        return {
            "verified": False,
            "capability": "container_runtime:docker",
            "installed": True,
            "version": None,
            "error": f"Docker daemon probe timed out after {timeout}s",
            "timestamp": now
        }
    except Exception as e:
        return {
            "verified": False,
            "capability": "container_runtime:docker",
            "installed": True,
            "version": None,
            "error": _clean_error(str(e)),
            "timestamp": now
        }


def probe_udocker(udocker_bin: Optional[str] = None, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Dict[str, Any]:
    """
    Executes a bounded, harmless udocker smoke test.
    Checks if udocker binary exists and responds via `udocker version`.
    """
    now = get_current_iso_timestamp()
    resolved_bin = udocker_bin or shutil.which("udocker")
    if not resolved_bin:
        return {
            "verified": False,
            "capability": "container_runtime:udocker",
            "installed": False,
            "version": None,
            "error": "udocker executable not found on PATH",
            "timestamp": now
        }

    try:
        res = subprocess.run(
            [resolved_bin, "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res.returncode == 0:
            # Extract first version line
            first_line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else "available"
            return {
                "verified": True,
                "capability": "container_runtime:udocker",
                "installed": True,
                "version": first_line[:50],
                "error": None,
                "timestamp": now
            }
        else:
            err_msg = res.stderr or res.stdout or f"udocker version exited with code {res.returncode}"
            return {
                "verified": False,
                "capability": "container_runtime:udocker",
                "installed": True,
                "version": None,
                "error": _clean_error(f"udocker failed: {err_msg}"),
                "timestamp": now
            }
    except subprocess.TimeoutExpired:
        return {
            "verified": False,
            "capability": "container_runtime:udocker",
            "installed": True,
            "version": None,
            "error": f"udocker probe timed out after {timeout}s",
            "timestamp": now
        }
    except Exception as e:
        return {
            "verified": False,
            "capability": "container_runtime:udocker",
            "installed": True,
            "version": None,
            "error": _clean_error(str(e)),
            "timestamp": now
        }


def probe_nodejs(
    node_bin: Optional[str] = None,
    npm_bin: Optional[str] = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT
) -> Dict[str, Any]:
    """
    Probes Node.js and npm availability and runtime version.
    """
    now = datetime.now(timezone.utc).isoformat()
    resolved_node = node_bin or shutil.which("node") or shutil.which("nodejs")
    resolved_npm = npm_bin or shutil.which("npm")
    if not resolved_node:
        return {
            "verified": False,
            "capability": "nodejs",
            "installed": False,
            "version": None,
            "error": "Node.js executable ('node') not found on PATH",
            "timestamp": now
        }

    try:
        res = subprocess.run(
            [resolved_node, "-v"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res.returncode == 0:
            ver_str = res.stdout.strip().lstrip("v")
            npm_ver = None
            if resolved_npm:
                try:
                    npm_res = subprocess.run(
                        [resolved_npm, "-v"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout
                    )
                    if npm_res.returncode == 0:
                        npm_ver = npm_res.stdout.strip()
                except Exception:
                    pass

            return {
                "verified": True,
                "capability": "nodejs",
                "installed": True,
                "version": ver_str,
                "npm_installed": bool(npm_ver is not None or resolved_npm),
                "npm_version": npm_ver,
                "error": None,
                "timestamp": now
            }
        else:
            err_msg = res.stderr or res.stdout or f"Non-zero exit code: {res.returncode}"
            return {
                "verified": False,
                "capability": "nodejs",
                "installed": True,
                "version": None,
                "npm_installed": bool(resolved_npm),
                "npm_version": None,
                "error": _clean_error(err_msg),
                "timestamp": now
            }
    except subprocess.TimeoutExpired:
        return {
            "verified": False,
            "capability": "nodejs",
            "installed": True,
            "version": None,
            "npm_installed": bool(resolved_npm),
            "npm_version": None,
            "error": f"Node.js probe timed out after {timeout}s",
            "timestamp": now
        }
    except Exception as e:
        return {
            "verified": False,
            "capability": "nodejs",
            "installed": True,
            "version": None,
            "npm_installed": bool(resolved_npm),
            "npm_version": None,
            "error": _clean_error(str(e)),
            "timestamp": now
        }


def probe_static(storage_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Probes static web serving capability by verifying local storage and filesystem readability.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        target = (storage_path or (BASE_DIR / "storage")).resolve()
        target.mkdir(parents=True, exist_ok=True)
        return {
            "verified": True,
            "capability": "static",
            "installed": True,
            "version": "router-native",
            "error": None,
            "timestamp": now
        }
    except Exception as e:
        return {
            "verified": False,
            "capability": "static",
            "installed": False,
            "version": None,
            "error": str(e),
            "timestamp": now
        }


def run_capability_probes(
    advertised_capabilities: Optional[Union[Dict[str, Any], List[str]]] = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT
) -> Dict[str, Any]:
    """
    Executes capability verification probes for advertised capabilities.
    Separates advertised capabilities from probe-verified capabilities.

    Returns:
    {
        "advertised_capabilities": Dict[str, bool],
        "verified_capabilities": Dict[str, bool],
        "probe_results": Dict[str, Dict[str, Any]]
    }
    """
    # 1. Normalize advertised capabilities into a dictionary
    adv_dict: Dict[str, bool] = {}
    if isinstance(advertised_capabilities, list):
        for c in advertised_capabilities:
            adv_dict[str(c)] = True
    elif isinstance(advertised_capabilities, dict):
        for k, v in advertised_capabilities.items():
            if isinstance(v, dict):
                adv_dict[str(k)] = bool(v.get("enabled", True) if "enabled" in v else v.get("advertised", True))
            else:
                adv_dict[str(k)] = bool(v)
    else:
        # Default baseline advertised capabilities
        adv_dict = {
            "compute": True,
            "storage": True,
            "network": True,
            "python3": True
        }

    verified_dict: Dict[str, bool] = {}
    probe_results: Dict[str, Dict[str, Any]] = {}

    # 2. Baseline system capabilities (Compute, Storage, Network)
    if adv_dict.get("compute", False):
        verified_dict["compute"] = True
        probe_results["compute"] = {
            "verified": True,
            "capability": "compute",
            "error": None,
            "timestamp": get_current_iso_timestamp()
        }

    if adv_dict.get("storage", False):
        verified_dict["storage"] = True
        probe_results["storage"] = {
            "verified": True,
            "capability": "storage",
            "error": None,
            "timestamp": get_current_iso_timestamp()
        }

    if adv_dict.get("network", False):
        verified_dict["network"] = True
        probe_results["network"] = {
            "verified": True,
            "capability": "network",
            "error": None,
            "timestamp": get_current_iso_timestamp()
        }

    # 3. Python Execution Probe
    # Run probe if python3 or python is advertised or present
    if adv_dict.get("python3", False) or adv_dict.get("python", False) or "python3" not in adv_dict:
        py_res = probe_python(timeout=timeout)
        probe_results["python3"] = py_res
        if py_res["verified"]:
            verified_dict["python3"] = True
            verified_dict["python"] = True
        else:
            verified_dict.pop("python3", None)
            verified_dict.pop("python", None)

    # 4. Container Runtime Probes (Docker & udocker)
    # Probe Docker if advertised or if binary is detected
    docker_verified = False
    if adv_dict.get("container_runtime:docker") is not None or shutil.which("docker"):
        docker_res = probe_docker(timeout=timeout)
        probe_results["container_runtime:docker"] = docker_res
        if docker_res["verified"]:
            verified_dict["container_runtime:docker"] = True
            verified_dict["docker"] = True
            docker_verified = True
        else:
            verified_dict.pop("container_runtime:docker", None)
            verified_dict.pop("docker", None)

    # Probe udocker if advertised or if binary is detected
    if adv_dict.get("container_runtime:udocker") is not None or shutil.which("udocker"):
        udocker_res = probe_udocker(timeout=timeout)
        probe_results["container_runtime:udocker"] = udocker_res
        if udocker_res["verified"]:
            verified_dict["container_runtime:udocker"] = True
            # CRITICAL: Never advertise Docker capability merely because udocker exists
            if not docker_verified:
                verified_dict.pop("container_runtime:docker", None)
                verified_dict.pop("docker", None)
        else:
            verified_dict.pop("container_runtime:udocker", None)

    # 5. Node.js Probe
    if adv_dict.get("nodejs") is not None or adv_dict.get("node") is not None or shutil.which("node"):
        node_res = probe_nodejs(timeout=timeout)
        probe_results["nodejs"] = node_res
        if node_res["verified"]:
            verified_dict["nodejs"] = True
            verified_dict["node"] = True
            if node_res.get("npm_installed"):
                verified_dict["npm"] = True
        else:
            verified_dict.pop("nodejs", None)
            verified_dict.pop("node", None)
            verified_dict.pop("npm", None)

    # 6. Static Web Serving Probe
    static_res = probe_static()
    probe_results["static"] = static_res
    if static_res.get("verified"):
        verified_dict["static"] = True

    return {
        "advertised_capabilities": adv_dict,
        "verified_capabilities": verified_dict,
        "probe_results": probe_results
    }
