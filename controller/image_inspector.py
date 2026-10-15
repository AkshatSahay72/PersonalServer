#!/usr/bin/env python3
"""
PersonalServer Image Inspector & Architecture Compatibility Subsystem
======================================================================
Validates container image references, determines target architecture
compatibility, inspects exposed application ports, and manages caching.
"""

import re
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

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

# Image reference regex matching standard OCI/Docker image format:
# [registry/][user/]image[:tag]
# Disallows shell metacharacters, spaces, control characters, leading flags
IMAGE_REF_REGEX = re.compile(
    r'^(?:(?=[^:\/]{1,253})(?!-)[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*(?::[0-9]+)?\/)?'
    r'(?:[a-z0-9_-]+\/)*[a-z0-9_-]+(?::[a-zA-Z0-9_.-]+)?$',
    re.IGNORECASE
)

LOCAL_IMAGE_STORE_DIR = Path(__file__).resolve().parent.parent / "storage" / "images"


def normalize_architecture(arch: Optional[str]) -> str:
    """Normalizes arbitrary architecture strings (e.g. 'aarch64', 'linux/arm64') to canonical form."""
    if not arch or not isinstance(arch, str):
        return ""
    clean = arch.strip().lower()
    return ARCH_MAP.get(clean, clean.split("/")[-1])


def validate_image_reference(image_str: str) -> Dict[str, Any]:
    """
    Validates container image reference syntax, supporting tags and immutable sha256 digests.
    Returns parsed dictionary:
    {
      "raw": "username/app:tag[@sha256:...]",
      "registry": "docker.io" or custom,
      "repository": "username/app",
      "image_name": "app",
      "tag": "tag" (defaults to "latest"),
      "digest": "sha256:..." or None,
      "immutable": bool,
      "canonical_ref": "..."
    }
    Raises ValueError on invalid or malicious image references.
    """
    if not image_str or not isinstance(image_str, str):
        raise ValueError("Image reference must be a non-empty string.")

    cleaned = image_str.strip()
    if len(cleaned) > 256:
        raise ValueError("Image reference exceeds maximum allowed length (256 characters).")

    # Reject shell injection tokens or options
    if any(c in cleaned for c in (";", "&", "|", "`", "$", "(", ")", "{", "}", "<", ">", "\\", " ", "\t", "\n")):
        raise ValueError("Image reference contains illegal characters.")

    if cleaned.startswith("-"):
        raise ValueError("Image reference cannot start with a hyphen.")

    digest_part = None
    if "@" in cleaned:
        base_part, digest_part = cleaned.rsplit("@", 1)
        if not re.match(r'^sha256:[a-fA-F0-9]{64}$', digest_part):
            raise ValueError(f"Invalid image digest format: '{digest_part}'. Expected sha256:<64-hex-digits>")
        ref_to_validate = base_part
    else:
        ref_to_validate = cleaned

    if not IMAGE_REF_REGEX.match(ref_to_validate):
        raise ValueError(f"Invalid container image format: '{cleaned}'. Expected format: [user/]repository[:tag][@sha256:digest]")

    # Extract tag (only from the final segment after the last '/')
    if "/" in ref_to_validate:
        path_prefix, last_segment = ref_to_validate.rsplit("/", 1)
        if ":" in last_segment:
            repo_tail, tag_part = last_segment.rsplit(":", 1)
            repo_part = f"{path_prefix}/{repo_tail}"
        else:
            repo_part = ref_to_validate
            tag_part = "latest"
    else:
        if ":" in ref_to_validate:
            repo_part, tag_part = ref_to_validate.rsplit(":", 1)
        else:
            repo_part = ref_to_validate
            tag_part = "latest"

    # Extract registry and repository
    segments = repo_part.split("/")
    if len(segments) == 1:
        registry = "docker.io"
        repo = f"library/{segments[0]}"
        image_name = segments[0]
    elif len(segments) == 2:
        registry = "docker.io"
        repo = repo_part
        image_name = segments[1]
    else:
        registry = segments[0]
        repo = "/".join(segments[1:])
        image_name = segments[-1]

    raw_val = f"{repo_part}:{tag_part}@{digest_part}" if digest_part else f"{repo_part}:{tag_part}"
    canon_ref = f"{registry}/{repo}:{tag_part}@{digest_part}" if digest_part else f"{registry}/{repo}:{tag_part}"

    return {
        "raw": raw_val,
        "registry": registry,
        "repository": repo,
        "image_name": image_name,
        "tag": tag_part,
        "digest": digest_part,
        "immutable": bool(digest_part),
        "canonical_ref": canon_ref
    }


def save_local_image_metadata(
    image_ref: str,
    digest: str,
    architecture: str,
    metadata: Optional[Dict[str, Any]] = None,
    store_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Persists immutable image metadata into local image store.
    Enables offline tracking, version pinning, and reproducible local builds.
    """
    store = (store_dir or LOCAL_IMAGE_STORE_DIR).resolve()
    store.mkdir(parents=True, exist_ok=True)

    parsed = validate_image_reference(image_ref)
    clean_key = re.sub(r'[^a-zA-Z0-9_.-]', '_', f"{parsed['repository']}_{parsed['tag']}")
    meta_path = store / f"{clean_key}.json"

    record = {
        "image_ref": image_ref,
        "repository": parsed["repository"],
        "tag": parsed["tag"],
        "digest": digest,
        "architecture": normalize_architecture(architecture),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metadata": metadata or {}
    }
    meta_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def get_local_image_metadata(
    image_ref: str,
    store_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Retrieves cached metadata from the local image store if available."""
    try:
        parsed = validate_image_reference(image_ref)
        store = (store_dir or LOCAL_IMAGE_STORE_DIR).resolve()
        clean_key = re.sub(r'[^a-zA-Z0-9_.-]', '_', f"{parsed['repository']}_{parsed['tag']}")
        meta_path = store / f"{clean_key}.json"
        if meta_path.exists() and meta_path.is_file():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def is_architecture_compatible(node_arch: str, supported_architectures: Any) -> bool:
    """Checks whether a node's architecture is satisfied by the image's supported architectures."""
    if not supported_architectures:
        return True  # If unknown, allow scheduling and let node verify

    if isinstance(supported_architectures, str):
        supported_architectures = [supported_architectures]

    norm_node = normalize_architecture(node_arch)
    norm_supported = {normalize_architecture(a) for a in supported_architectures if a}
    return norm_node in norm_supported


def detect_image_architectures(image_ref: str, timeout: int = 5, store_dir: Optional[Path] = None) -> List[str]:
    """
    Discovers supported architectures for an image reference.
    Checks local image store first, then queries public Docker Hub if online.
    Returns list of normalized architectures (e.g. ['amd64', 'arm64']).
    Falls back to universal ['amd64', 'arm64'] if unreachable or offline.
    """
    try:
        parsed = validate_image_reference(image_ref)
    except ValueError:
        return []

    # 1. Check local image store
    local_meta = get_local_image_metadata(image_ref, store_dir)
    if local_meta and local_meta.get("architecture"):
        return [local_meta["architecture"]]

    # 2. If Docker Hub registry
    if parsed["registry"] == "docker.io":
        repo = parsed["repository"]
        tag = parsed["tag"]
        url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}/"
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-ImageInspector/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                images = data.get("images", [])
                archs = set()
                for img in images:
                    arch = img.get("architecture")
                    if arch:
                        norm = normalize_architecture(arch)
                        if norm:
                            archs.add(norm)
                if archs:
                    return sorted(list(archs))
        except (urllib.error.HTTPError, urllib.error.URLError, Exception):
            pass

    # Safe default: permissive multi-arch list
    return ["amd64", "arm64"]


def detect_application_port(
    image_ref: Optional[str] = None,
    dockerfile_content: Optional[str] = None,
    explicit_port: Optional[int] = None,
    blueprint_port: Optional[int] = None,
    app_name: Optional[str] = None
) -> int:
    """
    Determines application container listening port by priority:
    1. Explicit port override (from request/UI)
    2. Blueprint / personalserver.yaml port
    3. Dockerfile EXPOSE / ENV PORT inspection
    4. Image name & app name heuristic (ExamBuddy -> 5000)
    5. Safe default 8000
    """
    if explicit_port and isinstance(explicit_port, (int, str)):
        try:
            p = int(explicit_port)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass

    if blueprint_port and isinstance(blueprint_port, (int, str)):
        try:
            p = int(blueprint_port)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass

    # Inspect Dockerfile content if provided
    if dockerfile_content and isinstance(dockerfile_content, str):
        # Look for EXPOSE <port>
        expose_match = re.search(r'^\s*EXPOSE\s+(\d+)', dockerfile_content, re.MULTILINE | re.IGNORECASE)
        if expose_match:
            try:
                p = int(expose_match.group(1))
                if 1 <= p <= 65535:
                    return p
            except ValueError:
                pass

        # Look for ENV PORT=<port> or PORT <port>
        env_port_match = re.search(r'^\s*ENV\s+PORT[=\s]+(\d+)', dockerfile_content, re.MULTILINE | re.IGNORECASE)
        if env_port_match:
            try:
                p = int(env_port_match.group(1))
                if 1 <= p <= 65535:
                    return p
            except ValueError:
                pass

    # Image & App name heuristics
    combined_name = f"{image_ref or ''} {app_name or ''}".lower()
    if "exambuddy" in combined_name:
        return 5000
    if "nginx" in combined_name:
        return 80
    if "httpd" in combined_name or "apache" in combined_name:
        return 80
    if "redis" in combined_name:
        return 6379
    if "postgres" in combined_name:
        return 5432
    if "mongo" in combined_name:
        return 27017

    return 8000
