#!/usr/bin/env python3
"""
Architecture Utilities
======================
Standardized CPU and container architecture normalization, matching, and compatibility checks.
"""

import platform
from typing import Optional, Any, List

# Standard architecture aliases mapped to canonical identifiers
ARCH_MAP = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "arm64v8": "arm64",
    "linux/arm64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
    "x64": "amd64",
    "linux/amd64": "amd64",
    "armv7": "armv7",
    "armv7l": "armv7",
    "armhf": "armv7",
    "linux/arm/v7": "armv7",
    "i386": "386",
    "x86": "386",
    "386": "386",
    "linux/386": "386"
}


def normalize_architecture(arch: Optional[str]) -> str:
    """Normalizes arbitrary architecture strings (e.g. 'aarch64', 'linux/arm64') to canonical form."""
    if not arch or not isinstance(arch, str):
        return ""
    clean = arch.strip().lower()
    return ARCH_MAP.get(clean, clean.split("/")[-1])


def is_architecture_compatible(node_arch: str, supported_architectures: Any) -> bool:
    """Checks whether a node's architecture is satisfied by an image's supported architectures."""
    if not supported_architectures:
        return True  # If unknown, allow scheduling and let node verify

    if isinstance(supported_architectures, str):
        supported_architectures = [supported_architectures]

    norm_node = normalize_architecture(node_arch)
    norm_supported = {normalize_architecture(a) for a in supported_architectures if a}
    return norm_node in norm_supported


def get_canonical_architecture() -> str:
    """Returns normalized CPU architecture for the current machine."""
    machine = platform.machine().strip().lower()
    return normalize_architecture(machine)
