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
                "api": self.api_host
            },
            "application": {
                "scheme": self.app_scheme,
                "path_based": self.is_path_based,
                "base_url": self.app_base_url
            },
            "urls": {
                "primary": self.app_base_url,
                "admin": self.admin_url,
                "api": self.api_url
            }
        }

    def __repr__(self) -> str:
        return f"<PlatformConfig domain={self.primary_domain} admin={self.admin_host} api={self.api_host}>"


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
                        "api": f"api.{domain}"
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
