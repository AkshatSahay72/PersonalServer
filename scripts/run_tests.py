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
    "tests/test_common_utils.py",
    "tests/test_persistence_stores.py",
    "tests/test_platform_config.py",
    "tests/test_portable_config.py",
    "tests/test_reproducibility.py",
    "tests/test_security_audit.py",
    "tests/test_logging_diagnostics.py",
    "tests/test_node_telemetry.py",
    "tests/test_cross_platform_reliability.py",
]

# Integration test suites: Protocol boundaries, multi-node simulator, job lifecycle, routing, HA
INTEGRATION_TEST_FILES = [
    "tests/test_reliable_job_lifecycle.py",
    "tests/test_multi_node_simulation.py",
    "tests/test_image_build_deploy.py",
    "tests/test_local_worker.py",
    "tests/test_ha_failover.py",
    "tests/test_ha_global_header.py",
    "tests/test_ha_leader_identity_audit.py",
    "tests/test_jobs_cancellation_and_pagination.py",
    "tests/test_nodes_diagnostics_and_failover.py",
    "tests/test_shell_layout_stability.py",
    "tests/test_dashboard_js_syntax.py",
    "tests/test_phase15a_image_deployment.py",
    "tests/test_phase14c_deboarding.py",
    "tests/test_phase14b_telemetry.py",
    "tests/test_phase12c_public_routing.py",
    "tests/test_phase11b_docker.py",
    "tests/test_phase11c_storage_modals_and_app_files.py",
    "tests/test_phase11d_docker_runtime.py",
    "tests/test_phase12_application_routing.py",
    "tests/test_phase13_source_deployment.py",
    "tests/test_storage_explorer_ui.py",
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
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "backups", "scratch", "storage", "__pycache__"}]
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
    group.add_argument("--integration", action="store_true", help="Run integration tests (simulated workers, lifecycle, routing)")
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
