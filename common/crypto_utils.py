#!/usr/bin/env python3
"""
Cryptographic Comparison Utilities
==================================
Provides timing-attack safe string/bytes comparison for tokens, secrets, and auth headers.
"""

import hmac
from typing import Any


def constant_time_compare(val1: Any, val2: Any) -> bool:
    """
    Constant-time comparison for secrets and tokens to prevent timing attacks.
    Returns False if either operand is empty, None, or not a string/bytes.
    """
    if not val1 or not val2:
        return False
    if not isinstance(val1, (str, bytes)) or not isinstance(val2, (str, bytes)):
        return False
    try:
        return hmac.compare_digest(val1, val2)
    except Exception:
        return False
