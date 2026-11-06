#!/usr/bin/env python3
"""
PersonalServer Runtime Adapter Interface
=========================================
Defines the pluggable runtime execution adapter contract for application release workloads.
Designed to support PythonVirtualenvAdapter, and extensible to NodeProcessAdapter,
StaticWebAdapter, and DockerAdapter without changing the scheduler contract.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from common.process_manager import ProcessIdentityRecord


class RuntimeAdapter(ABC):
    """
    Abstract contract for environment preparation, application lifecycle management,
    and process execution.
    """

    @property
    @abstractmethod
    def runtime_name(self) -> str:
        """The identifier of this runtime (e.g. 'python', 'nodejs', 'static', 'docker')."""
        pass

    @abstractmethod
    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        """
        Validates that the host execution node possesses the necessary binaries
        and tools to run this workload (e.g. Python interpreter >= 3.8).
        Returns (is_valid, error_reason).
        """
        pass

    @abstractmethod
    def prepare_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        env_vars: Dict[str, str],
        timeout_sec: float = 120.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Prepares an isolated runtime environment for the release (e.g. virtual environment,
        dependency installation). Must not install dependencies globally.
        Returns (success, error_reason).
        """
        pass

    @abstractmethod
    def start_application(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        port: int,
        env_vars: Dict[str, str],
        start_cmd: Optional[List[str]] = None,
        log_file: Optional[Path] = None
    ) -> ProcessIdentityRecord:
        """
        Starts the application process with isolated environment variables and logs.
        Returns the ProcessIdentityRecord for the running process.
        """
        pass

    @abstractmethod
    def cleanup_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path
    ) -> None:
        """
        Safely tears down or cleans up failed release environments or processes.
        """
        pass

