#!/usr/bin/env python3
"""
PersonalServer Unified Test Runner & Static Check CLI
=====================================================
Provides single, deterministic commands for unit tests, integration tests,
and code quality checks across Windows, Linux, and CI environments.

Usage:
  python scripts/run_tests.py --unit         # Run fast isolated unit tests (< 2s)
  python scripts/run_tests.py --integration  # Run integration & simulation test suite
  python scripts/run_tests.py --all          # Run complete test suite
  python scripts/run_tests.py --lint         # Run static syntax and lint checks
"""

import os
import sys
import time
import argparse
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Unit test suites: Pure Python, in-memory, no sockets, no background threads
UNIT_TEST_FILES = [
    "tests/unit/test_common_utils.py",
    "tests/unit/test_persistence_stores.py",
    "tests/unit/test_platform_config.py",
    "tests/unit/test_portable_config.py",
    "tests/unit/test_reproducibility.py",
    "tests/unit/test_logging_diagnostics.py",
    "tests/unit/test_node_telemetry_metrics.py",
    "tests/unit/test_cross_platform_reliability.py",
    "tests/unit/test_doctor.py",
    "tests/unit/test_worker_lifecycle_safety.py",
]

# Integration test suites: Protocol boundaries, multi-node simulator, job lifecycle, routing, HA
INTEGRATION_TEST_FILES = [
    # Controller domain
    "tests/controller/test_job_lifecycle.py",
    "tests/controller/test_job_cancellation_pagination.py",
    "tests/controller/test_job_deletion.py",
    "tests/controller/test_node_deboarding.py",
    "tests/controller/test_node_diagnostics.py",
    "tests/controller/test_node_telemetry.py",
    "tests/controller/test_local_worker.py",
    "tests/controller/test_multi_node_simulation.py",
    "tests/controller/test_notifications.py",
    "tests/controller/test_distributed_scheduler.py",
    "tests/controller/test_cross_worker_artifact_exchange.py",
    "tests/canary/test_staging_canary.py",
    # Storage domain
    "tests/storage/test_storage_explorer_api.py",
    "tests/storage/test_storage_folder_features.py",
    "tests/storage/test_storage_sharing.py",
    "tests/storage/test_app_storage_and_modals.py",
    # Routing domain
    "tests/routing/test_app_routing.py",
    "tests/routing/test_public_ingress_routing.py",
    # Deployment domain
    "tests/deployment/test_docker_capabilities.py",
    "tests/deployment/test_docker_runtime.py",
    "tests/deployment/test_image_build.py",
    "tests/deployment/test_image_deployment.py",
    "tests/deployment/test_source_deployment.py",
    # Security domain
    "tests/security/test_security_audit.py",
    # High Availability domain
    "tests/ha/test_ha_failover.py",
    "tests/ha/test_ha_global_header.py",
    "tests/ha/test_ha_leader_identity_audit.py",
    # Web UI domain
    "tests/ui/test_dashboard_js_syntax.py",
    "tests/ui/test_shell_layout_stability.py",
]


def check_pytest_installed() -> bool:
    try:
        import pytest
        return True
    except ImportError:
        return False


def run_tests_with_pytest(test_files: list, verbose: bool = False) -> int:
    import pytest
    args = ["-ra", "--strict-markers"]
    if verbose:
        args.append("-v")
    else:
        args.append("-q")
    args.extend(test_files)
    print(f"[>] Running {len(test_files)} test suite(s) with pytest:")
    for f in test_files:
        print(f"    - {f}")
    print("-" * 70)
    return pytest.main(args)


def run_tests_with_unittest(test_files: list, verbose: bool = False) -> int:
    import unittest
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for f in test_files:
        p = (BASE_DIR / f).resolve()
        if p.exists():
            discovered = loader.discover(start_dir=str(p.parent), pattern=p.name)
            suite.addTests(discovered)

    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def run_lint_checks() -> int:
    """Runs ruff static checks if available, or falls back to standard ast.parse on all .py files."""
    print("[>] Running static code checks...")
    import importlib.util
    if importlib.util.find_spec("ruff"):
        res = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=str(BASE_DIR))
        return res.returncode

    # Built-in fallback: Check Python syntax on all tracked/core files using AST
    print("[*] 'ruff' not installed in current environment; falling back to Python AST syntax check.")
    import ast
    errors = 0
    checked = 0
    for root, dirs, files in os.walk(str(BASE_DIR)):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "backups", "diagnostics", "scratch", "storage", "__pycache__"}]
        for f in files:
            if f.endswith(".py"):
                checked += 1
                fpath = Path(root) / f
                try:
                    ast.parse(fpath.read_text(encoding="utf-8", errors="replace"), filename=str(fpath))
                except SyntaxError as e:
                    print(f"  [!] SyntaxError in {fpath.relative_to(BASE_DIR)}: {e}")
                    errors += 1

    if errors == 0:
        print(f"[OK] Syntax check passed across all {checked} Python source files.")
        return 0
    else:
        print(f"[!] Found {errors} syntax error(s).")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="PersonalServer Unified Test Runner & Quality Tool"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--unit", action="store_true", help="Run fast unit tests (in-memory, no sockets)")
    group.add_argument("--integration", action="store_true", help="Run all integration tests across domains")
    group.add_argument("--controller", action="store_true", help="Run controller domain tests (jobs, nodes, notifications)")
    group.add_argument("--storage", action="store_true", help="Run storage domain tests (explorer, sharing, folders)")
    group.add_argument("--routing", action="store_true", help="Run routing domain tests (app router, public ingress)")
    group.add_argument("--deployment", action="store_true", help="Run deployment domain tests (docker, image, git source)")
    group.add_argument("--security", action="store_true", help="Run security audit tests")
    group.add_argument("--ha", action="store_true", help="Run high availability & leader election tests")
    group.add_argument("--ui", action="store_true", help="Run web UI layout & JS syntax tests")
    group.add_argument("--all", action="store_true", help="Run all test suites")
    group.add_argument("--lint", action="store_true", help="Run formatting and static syntax checks")

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose test reporting")

    args = parser.parse_args()
    start_time = time.time()

    if args.lint:
        exit_code = run_lint_checks()
        sys.exit(exit_code)

    if args.unit:
        selected_files = UNIT_TEST_FILES
        category_name = "Unit Tests"
    elif args.integration:
        selected_files = INTEGRATION_TEST_FILES
        category_name = "Integration Tests"
    elif args.controller:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/controller/")]
        category_name = "Controller Domain Tests"
    elif args.storage:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/storage/")]
        category_name = "Storage Domain Tests"
    elif args.routing:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/routing/")]
        category_name = "Routing Domain Tests"
    elif args.deployment:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/deployment/")]
        category_name = "Deployment Domain Tests"
    elif args.security:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/security/")]
        category_name = "Security Domain Tests"
    elif args.ha:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/ha/")]
        category_name = "High Availability Domain Tests"
    elif args.ui:
        selected_files = [f for f in INTEGRATION_TEST_FILES if f.startswith("tests/ui/")]
        category_name = "UI Domain Tests"
    else:
        selected_files = UNIT_TEST_FILES + INTEGRATION_TEST_FILES
        category_name = "All Tests (Unit + Integration)"

    print("=" * 70)
    print(f"PersonalServer Test Suite: {category_name}")
    print("=" * 70)

    if check_pytest_installed():
        exit_code = run_tests_with_pytest(selected_files, verbose=args.verbose)
    else:
        print("[*] pytest not found in current environment. Using standard unittest runner.")
        exit_code = run_tests_with_unittest(selected_files, verbose=args.verbose)

    duration = time.time() - start_time
    print("=" * 70)
    status_str = "SUCCESS" if exit_code == 0 else "FAILED"
    print(f"Test run completed in {duration:.2f}s | Result: {status_str}")
    print("=" * 70)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
