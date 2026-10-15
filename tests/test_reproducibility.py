#!/usr/bin/env python3
"""
Test Suite for Reproducible Development, Cross-Platform Paths, and Backup/Restore
=================================================================================
Validates:
1. Windows path behavior, normalization, and Linux compatibility.
2. Clean environment bootstrap and environment variable overrides.
3. Separation of runtime state, logs, and test artifacts from persistent databases.
4. Safe sample configurations (zero credentials, valid JSON/YAML structure).
5. Backup archive generation, exclusions (runtime/logs excluded), and SHA256 verification.
6. Restoration round-trip and path traversal defense in backup archives.
7. Dependency manifest consistency (pyproject.toml, requirements.txt, requirements-dev.txt).
"""

import os
import sys
import json
import shutil
import tempfile
import tarfile
import hashlib
import unittest
from pathlib import Path, PureWindowsPath, PurePosixPath

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.platform_config import (
    normalize_path,
    ControllerPaths,
    override_controller_paths,
    load_raw_platform_yaml,
    PlatformConfig,
    PlatformConfigError
)
from scripts.backup_restore import create_backup, restore_backup, verify_backup_archive


class TestPathNormalizationAndCrossPlatform(unittest.TestCase):
    """Verify that path resolution behaves consistently across Windows and Linux."""

    def test_01_normalize_path_handles_windows_and_posix_separators(self):
        """Path normalization converts mixed slashes and resolves accurately."""
        # Windows-style backslashes
        p_win = normalize_path(r"C:\PersonalServer\controller\data")
        self.assertIsNotNone(p_win)
        self.assertIn("PersonalServer", str(p_win))

        # POSIX-style forward slashes
        p_posix = normalize_path("/var/lib/personalserver/data")
        self.assertIsNotNone(p_posix)
        self.assertIn("personalserver", str(p_posix))

        # None and empty strings return None
        self.assertIsNone(normalize_path(None))
        self.assertIsNone(normalize_path(""))
        self.assertIsNone(normalize_path("   "))

    def test_02_controller_paths_environment_overrides(self):
        """ControllerPaths correctly honours all PERSONALSERVER_* environment variables."""
        with tempfile.TemporaryDirectory(prefix="ps_repro_test_") as tmp:
            tmp_path = Path(tmp).resolve()
            custom_data = tmp_path / "custom_data"
            custom_secrets = tmp_path / "custom_secrets"
            custom_runtime = tmp_path / "custom_runtime"
            custom_logs = tmp_path / "custom_logs"
            custom_storage = tmp_path / "custom_storage"

            env_patch = {
                "PERSONALSERVER_BASE_DIR": str(tmp_path),
                "PERSONALSERVER_DATA_DIR": str(custom_data),
                "PERSONALSERVER_SECRETS_DIR": str(custom_secrets),
                "PERSONALSERVER_RUNTIME_DIR": str(custom_runtime),
                "PERSONALSERVER_LOGS_DIR": str(custom_logs),
                "PERSONALSERVER_STORAGE_DIR": str(custom_storage),
            }

            orig_env = {k: os.environ.get(k) for k in env_patch}
            try:
                os.environ.update(env_patch)
                paths = ControllerPaths()
                self.assertEqual(paths.base_dir, tmp_path)
                self.assertEqual(paths.data_dir, custom_data)
                self.assertEqual(paths.secrets_dir, custom_secrets)
                self.assertEqual(paths.runtime_dir, custom_runtime)
                self.assertEqual(paths.logs_dir, custom_logs)
                self.assertEqual(paths.storage_dir, custom_storage)

                # Check database subfile paths
                self.assertEqual(paths.nodes_file, custom_data / "nodes.json")
                self.assertEqual(paths.jobs_file, custom_data / "jobs.json")
                self.assertEqual(paths.apps_file, custom_data / "apps.json")
                self.assertEqual(paths.enrollment_token_file, custom_secrets / "enrollment.token")

                # Test directory creation
                paths.ensure_directories()
                for d in (custom_data, custom_secrets, custom_runtime, custom_logs, custom_storage):
                    self.assertTrue(d.is_dir(), f"Expected directory {d} to exist")

                # Validate permissions
                paths.validate()

            finally:
                for k, v in orig_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_03_override_controller_paths_context_manager(self):
        """override_controller_paths properly isolates environment and restores on exit."""
        with tempfile.TemporaryDirectory(prefix="ps_ctx_test_") as tmp:
            tmp_path = Path(tmp).resolve()
            test_data = tmp_path / "isolated_data"
            test_data.mkdir()

            from config.platform_config import get_controller_paths
            prev_paths = get_controller_paths()
            with override_controller_paths(data_dir=test_data) as paths:
                self.assertEqual(paths.data_dir, test_data)
                self.assertEqual(get_controller_paths().data_dir, test_data)

            self.assertEqual(get_controller_paths(), prev_paths)


class TestSafeConfigurationAndSamples(unittest.TestCase):
    """Verify that configuration templates and sample files contain zero real secrets."""

    def test_01_sample_secrets_contain_no_real_credentials(self):
        """Ensure sample enrollment token and registry credentials contain only dummy placeholders."""
        token_example = BASE_DIR / "config" / "secrets" / "enrollment.token.example"
        self.assertTrue(token_example.exists(), "enrollment.token.example must exist")
        token_content = token_example.read_text(encoding="utf-8")
        # Ensure it contains placeholder zeroes and comments
        self.assertIn("0000000000000000000000000000000000000000000000000000000000000000", token_content)

        registry_example = BASE_DIR / "config" / "secrets" / "registry_credentials.example.json"
        self.assertTrue(registry_example.exists(), "registry_credentials.example.json must exist")
        data = json.loads(registry_example.read_text(encoding="utf-8"))
        self.assertIn("registries", data)
        for reg_name, creds in data["registries"].items():
            self.assertIn("example", creds.get("username", "").lower())
            self.assertIn("example", creds.get("password_or_token", "").lower())

    def test_02_node_example_json_is_safe(self):
        """node.example.json must contain placeholder identifiers and non-conflicting values."""
        node_example = BASE_DIR / "config" / "node.example.json"
        self.assertTrue(node_example.exists(), "node.example.json must exist")
        data = json.loads(node_example.read_text(encoding="utf-8"))
        self.assertIn("node_id", data)
        self.assertTrue(data["node_id"].startswith("server-xx") or "example" in data["node_id"])

    def test_03_env_example_has_valid_syntax(self):
        """.env.example must be parseable with valid key-value pairs and comments."""
        env_example = BASE_DIR / ".env.example"
        self.assertTrue(env_example.exists(), ".env.example must exist")
        lines = env_example.read_text(encoding="utf-8").splitlines()
        keys_found = set()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                keys_found.add(k.strip())
        self.assertIn("PERSONALSERVER_CONTROLLER_HOST", keys_found)
        self.assertIn("PERSONALSERVER_CONTROLLER_PORT", keys_found)
        self.assertIn("PERSONALSERVER_HEARTBEAT_TIMEOUT", keys_found)


class TestBackupAndRestoreRoundTrip(unittest.TestCase):
    """Verify that state backup and restoration is completely reproducible and safe."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_backup_test_"))
        self.source_data = self.test_dir / "source" / "data"
        self.source_secrets = self.test_dir / "source" / "secrets"
        self.source_storage = self.test_dir / "source" / "storage"
        self.source_runtime = self.test_dir / "source" / "runtime"
        self.source_logs = self.test_dir / "source" / "logs"

        for d in (self.source_data, self.source_secrets, self.source_storage, self.source_runtime, self.source_logs):
            d.mkdir(parents=True, exist_ok=True)

        # Seed realistic persistent state
        self.nodes_data = {
            "cluster_name": "TestCluster",
            "nodes": {
                "node-01": {"node_id": "node-01", "name": "worker-1", "status": "ONLINE"}
            }
        }
        (self.source_data / "nodes.json").write_text(json.dumps(self.nodes_data), encoding="utf-8")

        self.jobs_data = {
            "jobs": {
                "job-101": {"job_id": "job-101", "status": "SUCCEEDED", "type": "test"}
            }
        }
        (self.source_data / "jobs.json").write_text(json.dumps(self.jobs_data), encoding="utf-8")

        (self.source_secrets / "enrollment.token").write_text("secure-test-token-abcdef\n", encoding="utf-8")
        (self.source_storage / "sample_app_file.txt").write_text("Hello PersonalServer Storage!", encoding="utf-8")

        # Ephemeral runtime & logs that MUST NOT be included in backup
        (self.source_runtime / "test.pid").write_text("99999", encoding="utf-8")
        (self.source_logs / "controller.log").write_text("2026-10-15 [INFO] Controller started", encoding="utf-8")

        self.backup_output = self.test_dir / "backup.tar.gz"
        self.restore_target = self.test_dir / "restored"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_backup_creation_and_exclusions(self):
        """Backup creates valid tar.gz containing data, secrets, storage, but EXCLUDING runtime and logs."""
        meta = create_backup(
            output_file=self.backup_output,
            data_dir=self.source_data,
            secrets_dir=self.source_secrets,
            storage_dir=self.source_storage,
            include_storage=True
        )

        self.assertTrue(self.backup_output.exists(), "Backup archive must be created")
        self.assertIn("sha256", meta)
        self.assertGreater(meta["archive_size_bytes"], 0)

        # Inspect archive members
        with tarfile.open(self.backup_output, "r:gz") as tar:
            names = tar.getnames()
            # Must include persistent files
            self.assertTrue(any("nodes.json" in n for n in names))
            self.assertTrue(any("jobs.json" in n for n in names))
            self.assertTrue(any("enrollment.token" in n for n in names))
            self.assertTrue(any("sample_app_file.txt" in n for n in names))
            self.assertTrue(any("manifest.json" in n for n in names))

            # Must NOT include ephemeral runtime or logs
            self.assertFalse(any(".pid" in n for n in names))
            self.assertFalse(any(".log" in n for n in names))

    def test_02_verify_backup_integrity(self):
        """verify_backup_archive successfully validates SHA256 and internal manifest."""
        create_backup(
            output_file=self.backup_output,
            data_dir=self.source_data,
            secrets_dir=self.source_secrets,
            storage_dir=self.source_storage,
            include_storage=True
        )

        is_valid, report = verify_backup_archive(self.backup_output)
        self.assertTrue(is_valid, f"Verification failed: {report}")
        self.assertEqual(report.get("status"), "VALID")
        self.assertIn("nodes.json", report.get("files", []))

    def test_03_restore_backup_round_trip(self):
        """Restoring backup to clean directory restores identical files and data."""
        create_backup(
            output_file=self.backup_output,
            data_dir=self.source_data,
            secrets_dir=self.source_secrets,
            storage_dir=self.source_storage,
            include_storage=True
        )

        result = restore_backup(
            archive_path=self.backup_output,
            target_data_dir=self.restore_target / "data",
            target_secrets_dir=self.restore_target / "secrets",
            target_storage_dir=self.restore_target / "storage"
        )

        self.assertEqual(result.get("status"), "RESTORED")

        # Verify restored contents match original
        restored_nodes = json.loads((self.restore_target / "data" / "nodes.json").read_text(encoding="utf-8"))
        self.assertEqual(restored_nodes, self.nodes_data)

        restored_jobs = json.loads((self.restore_target / "data" / "jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(restored_jobs, self.jobs_data)

        restored_token = (self.restore_target / "secrets" / "enrollment.token").read_text(encoding="utf-8").strip()
        self.assertEqual(restored_token, "secure-test-token-abcdef")

        restored_file = (self.restore_target / "storage" / "sample_app_file.txt").read_text(encoding="utf-8")
        self.assertEqual(restored_file, "Hello PersonalServer Storage!")

    def test_04_restore_rejects_path_traversal_payloads(self):
        """Restore strictly prevents extraction of paths outside target directories."""
        malicious_tar_path = self.test_dir / "malicious.tar.gz"
        payload_data = b"malicious content"
        payload_hash = hashlib.sha256(payload_data).hexdigest()
        manifest_data = {
            "format_version": "1.0",
            "created_at": "2026-10-15T00:00:00Z",
            "files": {
                "data/../../etc/cron.d/malicious": {
                    "size_bytes": len(payload_data),
                    "sha256": payload_hash
                }
            }
        }
        manifest_bytes = json.dumps(manifest_data).encode("utf-8")

        with tarfile.open(malicious_tar_path, "w:gz") as tar:
            import io
            m_info = tarfile.TarInfo(name="manifest.json")
            m_info.size = len(manifest_bytes)
            tar.addfile(m_info, io.BytesIO(manifest_bytes))

            tarinfo = tarfile.TarInfo(name="data/../../etc/cron.d/malicious")
            tarinfo.size = len(payload_data)
            tar.addfile(tarinfo, io.BytesIO(payload_data))

        with self.assertRaises(ValueError) as ctx:
            restore_backup(
                archive_path=malicious_tar_path,
                target_data_dir=self.restore_target / "data",
                target_secrets_dir=self.restore_target / "secrets"
            )
        self.assertIn("traversal", str(ctx.exception).lower())


class TestDependencyManifests(unittest.TestCase):
    """Verify that dependency definitions across pyproject.toml and requirements are consistent."""

    def test_01_requirements_files_exist(self):
        """Both requirements.txt and requirements-dev.txt exist and are readable."""
        req_core = BASE_DIR / "requirements.txt"
        req_dev = BASE_DIR / "requirements-dev.txt"
        self.assertTrue(req_core.exists(), "requirements.txt must exist")
        self.assertTrue(req_dev.exists(), "requirements-dev.txt must exist")

        core_text = req_core.read_text(encoding="utf-8")
        dev_text = req_dev.read_text(encoding="utf-8")

        self.assertIn("pyyaml", core_text.lower())
        self.assertIn("pytest", dev_text.lower())
        self.assertIn("ruff", dev_text.lower())

    def test_02_pyproject_toml_configuration(self):
        """pyproject.toml contains valid pytest and ruff tool sections."""
        pyproject_file = BASE_DIR / "pyproject.toml"
        self.assertTrue(pyproject_file.exists(), "pyproject.toml must exist")
        content = pyproject_file.read_text(encoding="utf-8")

        self.assertIn("[tool.pytest.ini_options]", content)
        self.assertIn("testpaths", content)
        self.assertIn("norecursedirs", content)
        self.assertIn("[tool.ruff]", content)


class TestHardwareMigrationReadiness(unittest.TestCase):
    """Verify hardware migration data structures, Termux constraints, and architecture mappings."""

    def test_01_termux_capability_profile_and_scheduler_isolation(self):
        """Termux nodes are accepted for python/compute jobs and strictly rejected for docker."""
        from datetime import datetime, timezone
        from scheduler.scheduler import ResourceScheduler

        now_iso = datetime.now(timezone.utc).isoformat()
        cluster = {
            "nodes": {
                "server-termux-vivo": {
                    "node_id": "server-termux-vivo",
                    "name": "vivo-y31",
                    "platform": "termux",
                    "status": "ONLINE",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True, "python_runtime": True, "container_runtime:docker": False},
                    "resources": {"cpu_cores": 8, "ram_mb": 5697, "storage_gb": 106}
                },
                "server-rpi-arm64": {
                    "node_id": "server-rpi-arm64",
                    "name": "raspberry-pi-5",
                    "platform": "linux",
                    "status": "ONLINE",
                    "last_seen": now_iso,
                    "capabilities": {"compute": True, "storage": True, "container_runtime:docker": True},
                    "resources": {"cpu_cores": 4, "ram_mb": 8192, "storage_gb": 128}
                }
            }
        }

        # 1. Docker workload must select Raspberry Pi and reject Termux
        docker_reqs = {"capabilities": ["container_runtime:docker"], "min_cpu_cores": 1}
        decision_docker = ResourceScheduler.select_node(docker_reqs, cluster, timeout_seconds=60)
        self.assertEqual(decision_docker.get("selected_node"), "server-rpi-arm64")
        self.assertIn("server-termux-vivo", decision_docker.get("rejected", {}))
        self.assertIn("container_runtime:docker", decision_docker["rejected"]["server-termux-vivo"])

        # 2. General compute workload accepts Termux worker
        compute_reqs = {"capabilities": ["compute"], "min_cpu_cores": 2}
        decision_compute = ResourceScheduler.select_node(compute_reqs, cluster, timeout_seconds=60)
        self.assertIsNotNone(decision_compute.get("selected_node"))

    def test_02_raspberry_pi_arm64_multiarch_compatibility(self):
        """Architecture normalizer maps aarch64 and arm64 identically for multi-arch builds."""
        from controller.image_inspector import normalize_architecture, is_architecture_compatible

        self.assertEqual(normalize_architecture("aarch64"), "arm64")
        self.assertEqual(normalize_architecture("arm64"), "arm64")
        self.assertEqual(normalize_architecture("linux/arm64"), "arm64")
        self.assertEqual(normalize_architecture("x86_64"), "amd64")
        self.assertEqual(normalize_architecture("amd64"), "amd64")

        # Compatibility checks
        self.assertTrue(is_architecture_compatible("aarch64", "arm64"))
        self.assertTrue(is_architecture_compatible("arm64", "aarch64"))
        self.assertFalse(is_architecture_compatible("amd64", "arm64"))

    def test_03_systemd_fhs_path_mapping(self):
        """ControllerPaths correctly instantiates Linux FHS paths expected on Raspberry Pi OS."""
        paths = ControllerPaths(
            base_dir=Path("/opt/personalserver"),
            config_dir=Path("/etc/personalserver"),
            secrets_dir=Path("/etc/personalserver/secrets"),
            data_dir=Path("/var/lib/personalserver/data"),
            runtime_dir=Path("/run/personalserver"),
            logs_dir=Path("/var/log/personalserver"),
            storage_dir=Path("/var/lib/personalserver/storage")
        )
        self.assertTrue(paths.base_dir.as_posix().endswith("/opt/personalserver"))
        self.assertTrue(paths.config_dir.as_posix().endswith("/etc/personalserver"))
        self.assertTrue(paths.secrets_dir.as_posix().endswith("/etc/personalserver/secrets"))
        self.assertTrue(paths.data_dir.as_posix().endswith("/var/lib/personalserver/data"))
        self.assertTrue(paths.runtime_dir.as_posix().endswith("/run/personalserver"))
        self.assertTrue(paths.logs_dir.as_posix().endswith("/var/log/personalserver"))
        self.assertTrue(paths.storage_dir.as_posix().endswith("/var/lib/personalserver/storage"))

    def test_04_termux_install_script_syntax_and_structure(self):
        """install/termux.sh exists, contains non-root safeguards and valid bash structure."""
        termux_script = BASE_DIR / "install" / "termux.sh"
        self.assertTrue(termux_script.exists(), "install/termux.sh must exist")
        content = termux_script.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("#!/data/data/com.termux/files/usr/bin/bash") or content.startswith("#!/bin/bash") or content.startswith("#!/usr/bin/env bash"))
        self.assertIn("termux-wake-lock", content)
        self.assertIn("node.json", content)
        self.assertIn("container_runtime:docker", content)


if __name__ == "__main__":
    unittest.main()
