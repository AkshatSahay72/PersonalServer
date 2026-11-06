#!/usr/bin/env python3
"""
PersonalServer Runtime Adapters
===============================
Pluggable adapters for application runtimes.
"""

from typing import Optional
from agent.runtime_adapters.base import RuntimeAdapter
from agent.runtime_adapters.python_adapter import PythonVirtualenvAdapter
from agent.runtime_adapters.nodejs_adapter import NodejsProcessAdapter
from agent.runtime_adapters.static_adapter import StaticWebAdapter
from agent.runtime_adapters.container_adapter import ContainerRuntimeAdapter

__all__ = [
    "RuntimeAdapter",
    "PythonVirtualenvAdapter",
    "NodejsProcessAdapter",
    "StaticWebAdapter",
    "ContainerRuntimeAdapter",
    "get_runtime_adapter",
]


def get_runtime_adapter(runtime_name: str = "python") -> RuntimeAdapter:
    """
    Factory function returning the appropriate RuntimeAdapter for a project type.
    Supports native Python (virtualenv), native Node.js, static web workloads,
    and containerized workloads (Docker Engine).
    """
    norm = str(runtime_name or "python").strip().lower()
    if norm in ("static", "static-web", "html", "web"):
        return StaticWebAdapter()

    if norm in ("nodejs", "node", "javascript", "js", "npm", "express"):
        return NodejsProcessAdapter()

    if norm in ("docker", "container", "containerd", "dockerfile"):
        return ContainerRuntimeAdapter()

    if norm in ("python", "python3", "self-hosted", "fastapi", "flask", "django"):
        return PythonVirtualenvAdapter()

    # Defaults to PythonVirtualenvAdapter for backward compatibility with existing python services
    return PythonVirtualenvAdapter()
