#!/usr/bin/env python3
"""
PersonalServer Central Platform Configuration Loader
=====================================================
Single source of truth for platform domain, hostnames, and public URLs.
Loads and validates config/platform.yaml without storing or exposing secrets.
"""

import os
import re
import contextlib
from pathlib import Path
from typing import Optional, Dict, Any

# Root directory resolution
CONFIG_DIR = Path(__file__).resolve().parent
BASE_DIR = CONFIG_DIR.parent
PLATFORM_YAML = CONFIG_DIR / "platform.yaml"


class PlatformConfigError(ValueError):
    """Raised when platform.yaml is missing, unreadable, or malformed."""
    pass


def _simple_yaml_parse(text: str) -> Dict[str, Any]:
    """
    Lightweight, dependency-free YAML parser for platform.yaml key-value hierarchy.
    Ensures operation even in stripped Termux/container environments without PyYAML.
    """
    data: Dict[str, Any] = {}
    stack = [data]
    indent_levels = [-1]

    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line or not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        if ":" not in content:
            continue

        key, val = [p.strip() for p in content.split(":", 1)]

        while indent_levels and indent <= indent_levels[-1]:
            indent_levels.pop()
            stack.pop()

        current_dict = stack[-1]

        if not val:
            # New dictionary level
            new_dict: Dict[str, Any] = {}
            current_dict[key] = new_dict
            stack.append(new_dict)
            indent_levels.append(indent)
        else:
            # Scalar value
            if val.lower() in ("true", "yes"):
                parsed_val = True
            elif val.lower() in ("false", "no"):
                parsed_val = False
            elif val.isdigit():
                parsed_val = int(val)
            else:
                parsed_val = val.strip("'\"")
            current_dict[key] = parsed_val

    return data


def load_raw_platform_yaml(file_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and parse platform.yaml using PyYAML if present, or built-in parser fallback."""
    path = file_path or PLATFORM_YAML
    if not path.exists():
        raise PlatformConfigError(f"Platform configuration file not found at {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        raise PlatformConfigError(f"Failed to read platform configuration file {path}: {e}")

    try:
        import yaml
        parsed = yaml.safe_load(content)
        if isinstance(parsed, dict):
            return parsed
    except ImportError:
        pass
    except Exception as e:
        raise PlatformConfigError(f"YAML parsing failed for {path}: {e}")

    return _simple_yaml_parse(content)


class PlatformConfig:
    """Immutable representation of validated platform domain and URL settings."""

    def __init__(self, raw_data: Dict[str, Any]):
        platform_section = raw_data.get("platform")
        if not isinstance(platform_section, dict):
            raise PlatformConfigError("Missing or invalid 'platform' root mapping in platform configuration")

        domain = str(platform_section.get("domain", "")).strip().lower()
        if not domain:
            raise PlatformConfigError("Required field 'platform.domain' is missing or empty")

        if "/" in domain or ":" in domain or " " in domain:
            raise PlatformConfigError(f"Invalid 'platform.domain' format: '{domain}' must be a valid domain/hostname")

        self.primary_domain: str = domain

        # Hosts
        hosts = platform_section.get("hosts", {})
        if not isinstance(hosts, dict):
            hosts = {}

        self.admin_host: str = str(hosts.get("admin") or f"server.{self.primary_domain}").strip().lower()
        self.api_host: str = str(hosts.get("api") or f"api.{self.primary_domain}").strip().lower()
        self.apps_host: str = str(hosts.get("apps") or f"apps.{self.primary_domain}").strip().lower()

        # Application routing
        app_sec = platform_section.get("application", {})
        if not isinstance(app_sec, dict):
            app_sec = {}

        scheme = str(app_sec.get("scheme", "https")).strip().lower()
        if scheme not in ("http", "https"):
            raise PlatformConfigError(f"Invalid application scheme '{scheme}': must be 'http' or 'https'")
        self.app_scheme: str = scheme
        self.is_path_based: bool = bool(app_sec.get("path_based", True))

        # Normalized URLs
        self.app_base_url: str = f"{self.app_scheme}://{self.primary_domain}"
        self.admin_url: str = f"{self.app_scheme}://{self.admin_host}"
        self.api_url: str = f"{self.app_scheme}://{self.api_host}"
        self.apps_url: str = f"{self.app_scheme}://{self.apps_host}"

    def get_app_public_url(self, route_path: str) -> str:
        """
        Dynamically construct the full public URL for an application route.
        Example: '/recallflow' -> 'https://akshatsahay.space/recallflow/'
        """
        if not route_path or str(route_path).strip() in ("", "-", "None"):
            return "-"

        clean_path = "/" + str(route_path).strip("/ ")
        return f"{self.app_base_url}{clean_path}/"

    def to_dict(self) -> Dict[str, Any]:
        """Sanitized JSON-serializable dictionary without internal secrets."""
        return {
            "domain": self.primary_domain,
            "hosts": {
                "admin": self.admin_host,
                "api": self.api_host,
                "apps": self.apps_host
            },
            "application": {
                "scheme": self.app_scheme,
                "path_based": self.is_path_based,
                "base_url": self.app_base_url,
                "apps_url": self.apps_url
            },
            "urls": {
                "primary": self.app_base_url,
                "admin": self.admin_url,
                "api": self.api_url,
                "apps": self.apps_url
            }
        }

    def __repr__(self) -> str:
        return f"<PlatformConfig domain={self.primary_domain} admin={self.admin_host} apps={self.apps_host}>"


# Global cache
_CACHED_CONFIG: Optional[PlatformConfig] = None


def get_platform_config(force_reload: bool = False) -> PlatformConfig:
    """Retrieve the singleton validated platform configuration."""
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None or force_reload:
        raw = load_raw_platform_yaml()
        _CACHED_CONFIG = PlatformConfig(raw)
    return _CACHED_CONFIG


def reload_platform_config() -> PlatformConfig:
    """Force reloads platform configuration from disk."""
    return get_platform_config(force_reload=True)


@contextlib.contextmanager
def override_platform_config(domain: Optional[str] = None, custom_config: Optional[Dict[str, Any]] = None):
    """
    Context manager for unit testing and validation without modifying platform.yaml on disk.
    Allows testing dynamic domain replacement (e.g. 'example.test').
    """
    global _CACHED_CONFIG
    previous = _CACHED_CONFIG
    try:
        if custom_config:
            _CACHED_CONFIG = PlatformConfig(custom_config)
        elif domain:
            test_raw = {
                "platform": {
                    "domain": domain,
                    "hosts": {
                        "admin": f"server.{domain}",
                        "api": f"api.{domain}",
                        "apps": f"apps.{domain}"
                    },
                    "application": {
                        "scheme": "https",
                        "path_based": True
                    }
                }
            }
            _CACHED_CONFIG = PlatformConfig(test_raw)
        yield _CACHED_CONFIG
    finally:
        _CACHED_CONFIG = previous


# ------------------------------------------------------------------------------
# Portable Controller Paths & Runtime Configuration Subsystem
# ------------------------------------------------------------------------------

def normalize_path(path_input: Any) -> Optional[Path]:
    """
    Normalizes a path string or Path object across Windows backslashes and Linux forward slashes.
    Returns None if path_input is None or empty.
    """
    if path_input is None:
        return None
    path_str = str(path_input).strip()
    if not path_str:
        return None
    return Path(path_str).resolve()


class ControllerPaths:
    """
    Centralized, cross-platform path resolver for controller data, secrets,
    logs, and runtime state. Fully configurable via environment variables
    for Windows dev, Raspberry Pi OS FHS, or test harnesses.
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        config_dir: Optional[Path] = None,
        secrets_dir: Optional[Path] = None,
        runtime_dir: Optional[Path] = None,
        logs_dir: Optional[Path] = None,
        storage_dir: Optional[Path] = None
    ):
        raw_base = os.environ.get("PERSONALSERVER_BASE_DIR")
        self.base_dir = normalize_path(raw_base) if raw_base else (base_dir or BASE_DIR).resolve()

        raw_config = os.environ.get("PERSONALSERVER_CONFIG_DIR")
        self.config_dir = normalize_path(raw_config) if raw_config else (config_dir or (self.base_dir / "config")).resolve()

        raw_data = os.environ.get("PERSONALSERVER_DATA_DIR")
        self.data_dir = normalize_path(raw_data) if raw_data else (data_dir or (self.base_dir / "controller" / "data")).resolve()

        raw_secrets = os.environ.get("PERSONALSERVER_SECRETS_DIR")
        self.secrets_dir = normalize_path(raw_secrets) if raw_secrets else (secrets_dir or (self.config_dir / "secrets")).resolve()

        raw_runtime = os.environ.get("PERSONALSERVER_RUNTIME_DIR")
        self.runtime_dir = normalize_path(raw_runtime) if raw_runtime else (runtime_dir or (self.base_dir / "runtime")).resolve()

        raw_logs = os.environ.get("PERSONALSERVER_LOGS_DIR")
        self.logs_dir = normalize_path(raw_logs) if raw_logs else (logs_dir or (self.base_dir / "logs")).resolve()

        raw_storage = os.environ.get("PERSONALSERVER_STORAGE_DIR")
        self.storage_dir = normalize_path(raw_storage) if raw_storage else (storage_dir or (self.base_dir / "storage")).resolve()

        # Database and token files
        self.nodes_file = self.data_dir / "nodes.json"
        self.backup_nodes_file = self.data_dir / "nodes.json.bak"
        self.jobs_file = self.data_dir / "jobs.json"
        self.backup_jobs_file = self.data_dir / "jobs.json.bak"
        self.apps_file = self.data_dir / "apps.json"
        self.backup_apps_file = self.data_dir / "apps.json.bak"
        self.onboarding_codes_file = self.data_dir / "onboarding_codes.json"
        self.backup_onboarding_codes_file = self.data_dir / "onboarding_codes.json.bak"
        self.enrollment_token_file = self.secrets_dir / "enrollment.token"
        self.leader_lease_file = self.data_dir / "leader_lease.json"
        self.ha_state_file = self.data_dir / "ha_state.json"

    def ensure_directories(self):
        """Creates all needed directory structures safely across Windows and Linux."""
        for d in (self.data_dir, self.secrets_dir, self.runtime_dir, self.logs_dir, self.storage_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate(self):
        """Validates that configured paths are usable and writable, raising PlatformConfigError on failure."""
        try:
            self.ensure_directories()
        except OSError as e:
            raise PlatformConfigError(f"Failed to create controller directories: {e}")

        # Check write permissions
        for name, d in [("data_dir", self.data_dir), ("secrets_dir", self.secrets_dir)]:
            test_file = d / f".write_test_{os.getpid()}"
            try:
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink()
            except OSError as e:
                raise PlatformConfigError(f"Directory '{name}' ({d}) is not writable: {e}")

    def to_dict(self) -> Dict[str, str]:
        """Returns string representation of paths for logging/diagnostics without secrets."""
        return {
            "base_dir": str(self.base_dir),
            "config_dir": str(self.config_dir),
            "data_dir": str(self.data_dir),
            "secrets_dir": str(self.secrets_dir),
            "runtime_dir": str(self.runtime_dir),
            "logs_dir": str(self.logs_dir),
            "storage_dir": str(self.storage_dir),
            "nodes_file": str(self.nodes_file),
            "jobs_file": str(self.jobs_file),
            "apps_file": str(self.apps_file),
            "onboarding_codes_file": str(self.onboarding_codes_file),
            "enrollment_token_file": str(self.enrollment_token_file),
            "leader_lease_file": str(self.leader_lease_file),
            "ha_state_file": str(self.ha_state_file),
        }


class ControllerRuntimeConfig:
    """Manages controller runtime network settings, timeouts, and defaults."""

    def __init__(self, paths: Optional[ControllerPaths] = None):
        self.paths = paths or ControllerPaths()
        self.host = os.environ.get("PERSONALSERVER_CONTROLLER_HOST", "0.0.0.0")

        port_raw = os.environ.get("PERSONALSERVER_CONTROLLER_PORT", "8000")
        try:
            self.port = int(port_raw)
            if not (1 <= self.port <= 65535):
                raise ValueError()
        except ValueError:
            raise PlatformConfigError(f"Invalid PERSONALSERVER_CONTROLLER_PORT '{port_raw}': must be an integer between 1 and 65535")

        hb_raw = os.environ.get("PERSONALSERVER_HEARTBEAT_TIMEOUT", "60")
        try:
            self.heartbeat_timeout = int(hb_raw)
            if self.heartbeat_timeout < 1:
                raise ValueError()
        except ValueError:
            raise PlatformConfigError(f"Invalid PERSONALSERVER_HEARTBEAT_TIMEOUT '{hb_raw}': must be a positive integer")

        grace_raw = os.environ.get("PERSONALSERVER_LEASE_GRACE_SEC", "20")
        try:
            self.lease_grace_sec = int(grace_raw)
            if self.lease_grace_sec < 0:
                raise ValueError()
        except ValueError:
            raise PlatformConfigError(f"Invalid PERSONALSERVER_LEASE_GRACE_SEC '{grace_raw}': must be a non-negative integer")

        ttl_raw = os.environ.get("PERSONALSERVER_ONBOARDING_TTL_SEC", "900")
        try:
            self.onboarding_ttl_sec = int(ttl_raw)
            if self.onboarding_ttl_sec < 1:
                raise ValueError()
        except ValueError:
            raise PlatformConfigError(f"Invalid PERSONALSERVER_ONBOARDING_TTL_SEC '{ttl_raw}': must be a positive integer")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "heartbeat_timeout": self.heartbeat_timeout,
            "lease_grace_sec": self.lease_grace_sec,
            "onboarding_ttl_sec": self.onboarding_ttl_sec,
            "paths": self.paths.to_dict(),
        }


_CACHED_CONTROLLER_PATHS: Optional[ControllerPaths] = None
_CACHED_RUNTIME_CONFIG: Optional[ControllerRuntimeConfig] = None


def get_controller_paths(force_reload: bool = False) -> ControllerPaths:
    """Retrieve the singleton ControllerPaths instance."""
    global _CACHED_CONTROLLER_PATHS
    if _CACHED_CONTROLLER_PATHS is None or force_reload:
        _CACHED_CONTROLLER_PATHS = ControllerPaths()
    return _CACHED_CONTROLLER_PATHS


def get_controller_runtime_config(force_reload: bool = False) -> ControllerRuntimeConfig:
    """Retrieve the singleton ControllerRuntimeConfig instance."""
    global _CACHED_RUNTIME_CONFIG
    if _CACHED_RUNTIME_CONFIG is None or force_reload:
        paths = get_controller_paths(force_reload=force_reload)
        _CACHED_RUNTIME_CONFIG = ControllerRuntimeConfig(paths=paths)
    return _CACHED_RUNTIME_CONFIG


@contextlib.contextmanager
def override_controller_paths(
    data_dir: Optional[Path] = None,
    config_dir: Optional[Path] = None,
    secrets_dir: Optional[Path] = None,
    runtime_dir: Optional[Path] = None,
    logs_dir: Optional[Path] = None,
    storage_dir: Optional[Path] = None
):
    """
    Context manager for isolated unit tests, temporary storage redirects,
    or test fixtures without modifying global workspace paths.
    """
    global _CACHED_CONTROLLER_PATHS, _CACHED_RUNTIME_CONFIG
    prev_paths = _CACHED_CONTROLLER_PATHS
    prev_runtime = _CACHED_RUNTIME_CONFIG

    new_paths = ControllerPaths(
        data_dir=data_dir,
        config_dir=config_dir,
        secrets_dir=secrets_dir,
        runtime_dir=runtime_dir,
        logs_dir=logs_dir,
        storage_dir=storage_dir
    )
    _CACHED_CONTROLLER_PATHS = new_paths
    _CACHED_RUNTIME_CONFIG = ControllerRuntimeConfig(paths=new_paths)
    try:
        yield new_paths
    finally:
        _CACHED_CONTROLLER_PATHS = prev_paths
        _CACHED_RUNTIME_CONFIG = prev_runtime


SENSITIVE_KEY_PATTERNS = {"token", "secret", "password", "key", "authorization", "cookie", "auth"}


def redact_sensitive_dict(data: Any, max_depth: int = 10) -> Any:
    """
    Recursively redacts sensitive values (tokens, passwords, secrets) from dictionaries
    for safe logging, API responses, and debugging.
    """
    if max_depth <= 0:
        return data
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(pat in k_lower for pat in SENSITIVE_KEY_PATTERNS):
                if v is None:
                    redacted[k] = None
                elif isinstance(v, str) and len(v) > 8:
                    redacted[k] = f"{v[:3]}...[REDACTED]...{v[-3:]}"
                else:
                    redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_dict(v, max_depth - 1)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_dict(item, max_depth - 1) for item in data]
    return data
