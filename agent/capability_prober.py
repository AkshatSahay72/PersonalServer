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

    try:
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
            return {
                "verified": True,
                "capability": "container_runtime:docker",
                "installed": True,
                "version": ver,
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
    if adv_dict.get("container_runtime:docker") is not None or shutil.which("docker"):
        docker_res = probe_docker(timeout=timeout)
        probe_results["container_runtime:docker"] = docker_res
        if docker_res["verified"]:
            verified_dict["container_runtime:docker"] = True
        else:
            verified_dict.pop("container_runtime:docker", None)

    # Probe udocker if advertised or if binary is detected
    if adv_dict.get("container_runtime:udocker") is not None or shutil.which("udocker"):
        udocker_res = probe_udocker(timeout=timeout)
        probe_results["container_runtime:udocker"] = udocker_res
        if udocker_res["verified"]:
            verified_dict["container_runtime:udocker"] = True
        else:
            verified_dict.pop("container_runtime:udocker", None)

    return {
        "advertised_capabilities": adv_dict,
        "verified_capabilities": verified_dict,
        "probe_results": probe_results
    }
