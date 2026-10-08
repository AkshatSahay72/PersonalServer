#!/usr/bin/env python3
"""
PersonalServer Environment Variables & .env Subsystem
======================================================
Secure server-side parsing, classification, validation,
and masking of application environment variables.
"""

import re
from typing import Dict, Any, Optional

SECRET_INDICATORS = (
    "key", "secret", "password", "token", "auth", "cred",
    "private", "cert", "database_url", "conn", "pwd", "hash", "jwt"
)

VALID_ENV_KEY_REGEX = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def is_secret_variable(key_name: str) -> bool:
    """Detects whether an environment variable name signifies a secret."""
    if not key_name or not isinstance(key_name, str):
        return False
    lower = key_name.lower()
    return any(indicator in lower for indicator in SECRET_INDICATORS)


def parse_env_file_content(content_str: str) -> Dict[str, Dict[str, Any]]:
    """
    Parses raw .env file text into structured variable definitions:
    {
      "DATABASE_URL": {"value": "...", "is_secret": True},
      "PORT": {"value": "5000", "is_secret": False}
    }
    Security:
    - Never logs or exposes raw values.
    - Strips shell escape syntax, quotes, and inline comments safely.
    - Rejects invalid variable identifiers.
    """
    if not content_str or not isinstance(content_str, str):
        return {}

    parsed: Dict[str, Dict[str, Any]] = {}

    for raw_line in content_str.splitlines():
        line = raw_line.strip()
        # Skip empty lines and full line comments
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        parts = line.split("=", 1)
        k = parts[0].strip()

        # Remove export keyword if present (e.g. "export PORT=5000")
        if k.startswith("export "):
            k = k[7:].strip()

        if not VALID_ENV_KEY_REGEX.match(k):
            continue

        v = parts[1].strip()

        # Handle quoted values ("..." or '...')
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        else:
            # Strip trailing comments from unquoted values (e.g. "PORT=5000 # comment")
            if " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            elif "\t#" in v:
                v = v.split("\t#", 1)[0].rstrip()

        is_secret = is_secret_variable(k)
        parsed[k] = {
            "value": v,
            "is_secret": is_secret
        }

    return parsed


def sanitize_env_vars_input(raw_input: Any) -> Dict[str, Dict[str, Any]]:
    """
    Normalizes arbitrary environment variable input (dict, list, or .env string)
    into standard canonical structure:
    {
      "KEY": {"value": "...", "is_secret": bool}
    }
    """
    if isinstance(raw_input, str):
        return parse_env_file_content(raw_input)

    normalized: Dict[str, Dict[str, Any]] = {}

    if isinstance(raw_input, dict):
        for k, v in raw_input.items():
            key_str = str(k).strip()
            if not VALID_ENV_KEY_REGEX.match(key_str):
                continue
            if isinstance(v, dict):
                val_str = str(v.get("value", ""))
                is_sec = bool(v.get("is_secret", is_secret_variable(key_str)))
            else:
                val_str = str(v)
                is_sec = is_secret_variable(key_str)
            normalized[key_str] = {"value": val_str, "is_secret": is_sec}

    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, dict):
                k = str(item.get("key") or item.get("name") or "").strip()
                if not VALID_ENV_KEY_REGEX.match(k):
                    continue
                val_str = str(item.get("value", ""))
                is_sec = bool(item.get("is_secret", is_secret_variable(k)))
                normalized[k] = {"value": val_str, "is_secret": is_sec}

    return normalized
