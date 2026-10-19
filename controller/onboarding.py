#!/usr/bin/env python3
"""
Controller Onboarding & Enrollment Subsystem
===========================================
Single-use cryptographic onboarding tokens and node enrollment code lifecycle management.
"""

import os
import hmac
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Tuple

from config.platform_config import get_controller_paths, get_controller_runtime_config
from controller.stores import (
    load_onboarding_codes_db,
    save_onboarding_codes_db,
    ONBOARDING_LOCK,
)

RUNTIME_CFG = get_controller_runtime_config()
DEFAULT_ONBOARDING_TTL_SEC = RUNTIME_CFG.onboarding_ttl_sec  # default: 15 minutes (900s)


def hash_onboarding_code(code: str) -> str:
    """Computes a SHA-256 hash of the normalized onboarding code for secure storage."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def get_or_create_enrollment_token() -> str:
    """
    Retrieves or generates the persistent cluster enrollment token stored at
    config/secrets/enrollment_token.secret with 0600 permissions.
    """
    paths = get_controller_paths()
    paths.ensure_directories()
    token_file = paths.enrollment_token_file
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_hex(24)
    token_file.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except Exception:
        pass
    return token


def generate_onboarding_code(ttl_seconds: int = DEFAULT_ONBOARDING_TTL_SEC) -> Tuple[str, int]:
    """
    Generates a cryptographically secure, single-use onboarding code.
    Format: PS-XXXX-XXXX
    Stores SHA-256 hash in onboarding_codes.json.
    Returns (code_str, ttl_seconds).
    """
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    code = f"PS-{part1}-{part2}"

    code_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        # Clean up stale codes older than 24 hours
        now_ts = now_dt.timestamp()
        filtered = {}
        for h, info in db.get("codes", {}).items():
            try:
                exp_ts = datetime.fromisoformat(info["expires_at"]).timestamp()
                if now_ts - exp_ts < 86400:
                    filtered[h] = info
            except Exception:
                pass
        db["codes"] = filtered

        db.setdefault("codes", {})[code_hash] = {
            "created_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "used": False,
            "used_at": None,
            "used_by_node": None
        }
        save_onboarding_codes_db(db)

    return code, ttl_seconds


def validate_and_consume_onboarding_code(code: str, node_id: str) -> Tuple[bool, str]:
    """
    Thread-safe validation and single-use consumption of an onboarding code.
    Returns (is_valid: bool, error_reason: str).
    """
    if not code or not code.strip():
        return False, "Missing onboarding code"

    input_hash = hash_onboarding_code(code)
    now_dt = datetime.now(timezone.utc)

    with ONBOARDING_LOCK:
        db = load_onboarding_codes_db()
        codes = db.get("codes", {})

        matched_entry = None
        for h, entry in codes.items():
            if hmac.compare_digest(h, input_hash):
                matched_entry = entry
                break

        if not matched_entry:
            return False, "Invalid onboarding code"

        if matched_entry.get("used"):
            return False, "Onboarding code has already been used"

        try:
            expires_at = datetime.fromisoformat(matched_entry["expires_at"])
            if now_dt > expires_at:
                return False, "Onboarding code has expired"
        except Exception:
            return False, "Corrupt expiration timestamp on onboarding code"

        # Atomically consume the code
        matched_entry["used"] = True
        matched_entry["used_at"] = now_dt.isoformat()
        matched_entry["used_by_node"] = node_id
        save_onboarding_codes_db(db)

        return True, ""


__all__ = [
    "DEFAULT_ONBOARDING_TTL_SEC",
    "hash_onboarding_code",
    "get_or_create_enrollment_token",
    "generate_onboarding_code",
    "validate_and_consume_onboarding_code",
]
