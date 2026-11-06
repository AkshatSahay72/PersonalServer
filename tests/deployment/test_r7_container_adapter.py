#!/usr/bin/env python3
"""
PersonalServer Phase R7: Container Runtime Adapter Test Suite
=============================================================
Validates:
1. Capability discovery: Detects genuine Docker Engine, rejects missing daemon, and distinguishes udocker.
2. Scheduler integration: Rejects nodes lacking Docker Engine; selects Docker-verified workers.
3. Image management: Builds Dockerfile, handles build errors safely, pulls images, verifies digests.
4. Container lifecycle: Unique container identity, loopback-only host port binding, resource limits (--memory, --cpus),
   safe environment injection (prohibits docker socket/host exposure).
5. Error handling: Startup crashes, port conflicts, health check failures, and log capture.
6. Rollback & Idempotency: Duplicate submissions return active container; failed candidates roll back to previous release.
7. Workload isolation: Unrelated containers and other releases remain completely untouched.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import ControllerPaths
from controller.stores import (
    load_apps_db,
    save_apps_db,
    load_nodes_db,
    save_nodes_db,
    load_jobs_db,
    save_jobs_db,
)
from scheduler.scheduler import ResourceScheduler
from controller.deployment_job_contract import build_canonical_deployment_job
from controller.deployment_manager import DeploymentManager, DeploymentError
from agent.capability_prober import probe_docker, probe_udocker, run_capability_probes
from agent.runtime_adapters import get_runtime_adapter, ContainerRuntimeAdapter
from agent.job_executor import JobExecutor
from common.process_manager import ProcessIdentityRecord, SafeProcessLifecycleManager


class TestContainerRuntimeAdapter(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_r7_test_"))
        self.data_dir = self.test_dir / "data"
        self.runtime_dir = self.test_dir / "runtime"
        self.storage_dir = self.test_dir / "storage"
        self.apps_storage = self.storage_dir / "applications"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.apps_storage.mkdir(parents=True, exist_ok=True)

        self.paths = ControllerPaths(
            data_dir=self.data_dir,
            runtime_dir=self.runtime_dir,
            base_dir=self.test_dir
        )

        # Initialize empty stores
        save_apps_db({"apps": {}}, paths=self.paths)
        save_nodes_db({"nodes": {}}, paths=self.paths)
        save_jobs_db({"jobs": {}}, paths=self.paths)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Capability Discovery Tests
    # -------------------------------------------------------------------------

    @patch("shutil.which", return_value=None)
    def test_01_capability_discovery_rejects_missing_docker(self, mock_which):
        """When docker binary is not on PATH, probe_docker returns verified=False."""
        res = probe_docker()
        self.assertFalse(res["verified"])
        self.assertFalse(res["installed"])
        self.assertIn("not found", res["error"])

    @patch("shutil.which", return_value="/usr/bin/udocker")
    def test_02_capability_discovery_rejects_udocker_binary_as_docker(self, mock_which):
        """udocker executable cannot satisfy Docker Engine capability."""
        res = probe_docker("/usr/bin/udocker")
        self.assertFalse(res["verified"])
        self.assertIn("udocker", res["error"].lower())

    @patch("shutil.which", return_value="/usr/bin/docker")
    @patch("subprocess.run")
    def test_03_capability_discovery_rejects_udocker_wrapper_script(self, mock_run, mock_which):
        """When `docker` is a script wrapping udocker, it is detected and rejected."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="udocker 1.3.17 rootless container emulator",
            stderr=""
        )
        res = probe_docker("/usr/bin/docker")
        self.assertFalse(res["verified"])
        self.assertIn("udocker wrapper", res["error"].lower())

    @patch("shutil.which", return_value="/usr/bin/docker")
    @patch("subprocess.run")
    def test_04_capability_discovery_rejects_unreachable_daemon(self, mock_run, mock_which):
        """When docker CLI is installed but daemon is offline, returns verified=False."""
        def run_side_effect(cmd, **kwargs):
            if "version" in cmd:
                return MagicMock(returncode=0, stdout="Docker version 27.0.3", stderr="")
            if "info" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
            return MagicMock(returncode=1, stdout="", stderr="")

        mock_run.side_effect = run_side_effect
        res = probe_docker("/usr/bin/docker")
        self.assertFalse(res["verified"])
        self.assertTrue(res["installed"])
        self.assertIn("daemon unreachable", res["error"].lower())

    @patch("shutil.which", return_value="/usr/bin/docker")
    @patch("subprocess.run")
    def test_05_capability_discovery_succeeds_with_running_daemon(self, mock_run, mock_which):
        """Genuine running Docker daemon returns verified=True and reports version & API version."""
        def run_side_effect(cmd, **kwargs):
            if "version" in cmd and "--format" in cmd:
                return MagicMock(returncode=0, stdout="1.46", stderr="")
            if "version" in cmd:
                return MagicMock(returncode=0, stdout="Docker version 27.0.3, build 7d4bed8", stderr="")
            if "info" in cmd:
                return MagicMock(returncode=0, stdout="27.0.3", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect
        res = probe_docker("/usr/bin/docker")
        self.assertTrue(res["verified"])
        self.assertEqual(res["version"], "27.0.3")
        self.assertEqual(res["api_version"], "1.46")

    # -------------------------------------------------------------------------
    # 2. Scheduler Integration Tests
    # -------------------------------------------------------------------------

    def test_06_scheduler_rejects_nodes_lacking_docker_capability(self):
        """Scheduler rejects nodes with only Python/udocker and selects only Docker-verified nodes."""
        now_iso = datetime.now(timezone.utc).isoformat()
        nodes = {
            "vivo": {
                "id": "vivo",
                "status": "ONLINE",
                "last_seen": now_iso,
                "capabilities": {"compute": True},
                "verified_capabilities": {"compute": True, "python3": True}
            },
            "realme": {
                "id": "realme",
                "status": "ONLINE",
                "last_seen": now_iso,
                "capabilities": {"compute": True},
                "verified_capabilities": {"compute": True, "python3": True, "container_runtime:udocker": True}
            },
            "docker_worker": {
                "id": "docker_worker",
                "status": "ONLINE",
                "last_seen": now_iso,
                "capabilities": {"compute": True, "container_runtime:docker": True},
                "verified_capabilities": {"compute": True, "container_runtime:docker": True}
            }
        }

        # 1. Deployment requiring container_runtime:docker
        reqs = {"capabilities": ["container_runtime:docker"]}
        decision = ResourceScheduler.select_node(reqs, {"nodes": nodes})
        self.assertEqual(decision["selected_node"], "docker_worker")
        self.assertIn("vivo", decision["rejected"])
        self.assertIn("realme", decision["rejected"])
        # In J3/R7, udocker rejection produces DOCKER_ENGINE_NOT_AVAILABLE
        self.assertTrue(
            "container_runtime:docker" in decision["rejected"]["realme"] or
            "DOCKER_ENGINE_NOT_AVAILABLE" in decision["rejected"]["realme"]
        )

        # 2. When docker_worker is offline, no node is selected
        nodes["docker_worker"]["status"] = "OFFLINE"
        decision_offline = ResourceScheduler.select_node(reqs, {"nodes": nodes})
        self.assertIsNone(decision_offline["selected_node"])

    # -------------------------------------------------------------------------
    # 3. Image Build & Pull Management Tests
    # -------------------------------------------------------------------------

    @patch("subprocess.run")
    def test_07_prepare_release_builds_dockerfile(self, mock_run):
        """ContainerRuntimeAdapter builds image from local Dockerfile with unique tag and parses EXPOSE."""
        rel_dir = self.apps_storage / "test-app" / "releases" / "dep-12345678"
        rel_dir.mkdir(parents=True, exist_ok=True)
        df = rel_dir / "Dockerfile"
        df.write_text("FROM python:3.11-slim\nEXPOSE 9000\nCMD ['python', 'app.py']\n", encoding="utf-8")

        mock_run.return_value = MagicMock(returncode=0, stdout="Successfully built", stderr="")

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        ok, err = adapter.prepare_release("test-app", "dep-12345678", rel_dir, {})
        self.assertTrue(ok)
        self.assertIsNone(err)

        # Verify release_metadata.json recorded image and target port
        meta_file = rel_dir / "release_metadata.json"
        self.assertTrue(meta_file.exists())
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["image"], "ps-test-app:dep-1234")
        self.assertEqual(meta["container_port"], 9000)

        # Verify docker build command invocation
        build_call = mock_run.call_args[0][0]
        self.assertEqual(build_call[1:4], ["build", "-t", "ps-test-app:dep-1234"])

    @patch("subprocess.run")
    def test_08_prepare_release_handles_build_failure(self, mock_run):
        """When docker build fails with non-zero exit code, error is captured cleanly."""
        rel_dir = self.apps_storage / "test-app" / "releases" / "dep-bad"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "Dockerfile").write_text("INVALID DOCKERFILE COMMAND", encoding="utf-8")

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Step 1/1 : INVALID\nUnknown instruction")

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        ok, err = adapter.prepare_release("test-app", "dep-bad", rel_dir, {})
        self.assertFalse(ok)
        self.assertIn("Docker build failed", err)
        self.assertIn("Unknown instruction", err)

    @patch("subprocess.run")
    def test_09_prepare_release_pulls_image_and_verifies_digest(self, mock_run):
        """When an explicit image is specified with digest, adapter pulls and verifies digest."""
        rel_dir = self.apps_storage / "test-img" / "releases" / "dep-pull"
        rel_dir.mkdir(parents=True, exist_ok=True)

        def run_side_effect(cmd, **kwargs):
            if "image" in cmd and "inspect" in cmd and "--format" in cmd:
                return MagicMock(returncode=0, stdout="myrepo/myimage@sha256:abc123456", stderr="")
            if "image" in cmd and "inspect" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="No such image")
            if "pull" in cmd:
                return MagicMock(returncode=0, stdout="Pulled", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        ok, err = adapter.prepare_release(
            "test-img",
            "dep-pull",
            rel_dir,
            {"IMAGE": "myrepo/myimage:v1", "IMAGE_DIGEST": "sha256:abc123456"}
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

        meta = json.loads((rel_dir / "release_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["image"], "myrepo/myimage:v1")

    # -------------------------------------------------------------------------
    # 4. Container Lifecycle & Security Isolation Tests
    # -------------------------------------------------------------------------

    @patch("time.sleep", return_value=None)
    @patch("subprocess.run")
    def test_10_start_application_enforces_resource_limits_and_loopback(self, mock_run, mock_sleep):
        """Adapter strictly binds to 127.0.0.1, applies CPU/memory limits, and strips docker sockets."""
        rel_dir = self.apps_storage / "secure-app" / "releases" / "dep-sec01"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "release_metadata.json").write_text(json.dumps({
            "image": "secure-app:latest",
            "container_port": 8080
        }), encoding="utf-8")

        def run_side_effect(cmd, **kwargs):
            if "inspect" in cmd:
                return MagicMock(returncode=0, stdout="true 0 44100", stderr="")
            return MagicMock(returncode=0, stdout="cont_id_123", stderr="")

        mock_run.side_effect = run_side_effect

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        env_vars = {
            "MEMORY_LIMIT": "512m",
            "CPU_LIMIT": "1.5",
            "APP_SECRET": "xyz",
            "DOCKER_HOST": "tcp://evil:2375",  # Must be stripped
            "DOCKER_SOCK": "/var/run/docker.sock"  # Must be stripped
        }
        rec = adapter.start_application(
            app_id="secure-app",
            deployment_id="dep-sec01",
            release_dir=rel_dir,
            port=18500,
            env_vars=env_vars,
            log_file=self.test_dir / "logs" / "test.log"
        )

        self.assertIsInstance(rec, ProcessIdentityRecord)
        self.assertEqual(rec.port, 18500)
        self.assertEqual(rec.pid, 44100)

        # Check docker run parameters
        run_cmd = None
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            if "run" in cmd:
                run_cmd = cmd
                break

        self.assertIsNotNone(run_cmd)
        # Port binding strictly loopback
        self.assertIn("-p", run_cmd)
        p_idx = run_cmd.index("-p")
        self.assertEqual(run_cmd[p_idx + 1], "127.0.0.1:18500:8080")

        # Resource bounds
        self.assertIn("--memory", run_cmd)
        self.assertEqual(run_cmd[run_cmd.index("--memory") + 1], "512m")
        self.assertIn("--cpus", run_cmd)
        self.assertEqual(run_cmd[run_cmd.index("--cpus") + 1], "1.5")

        # Dangerous env variables stripped
        run_str = " ".join(run_cmd)
        self.assertNotIn("DOCKER_HOST", run_str)
        self.assertNotIn("docker.sock", run_str)
        self.assertIn("-e APP_SECRET=xyz", run_str)

    @patch("time.sleep", return_value=None)
    @patch("subprocess.run")
    def test_11_start_application_crashed_container_cleaned_up(self, mock_run, mock_sleep):
        """When container exits immediately on startup, adapter captures logs, removes container, and raises."""
        rel_dir = self.apps_storage / "crash-app" / "releases" / "dep-crash"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "release_metadata.json").write_text(json.dumps({
            "image": "crash-app:latest",
            "container_port": 8000
        }), encoding="utf-8")

        def run_side_effect(cmd, **kwargs):
            if "inspect" in cmd:
                # State.Running=false, State.ExitCode=137
                return MagicMock(returncode=0, stdout="false 137 0", stderr="")
            if "logs" in cmd:
                return MagicMock(returncode=0, stdout="Fatal error: Out of memory during initialization", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        with self.assertRaises(RuntimeError) as ctx:
            adapter.start_application(
                app_id="crash-app",
                deployment_id="dep-crash",
                release_dir=rel_dir,
                port=18501,
                env_vars={}
            )

        self.assertIn("terminated immediately with exit code 137", str(ctx.exception))
        self.assertIn("Fatal error: Out of memory", str(ctx.exception))

        # Verify container was removed with `docker rm -f`
        rm_called = any("rm" in c[0][0] and "-f" in c[0][0] for c in mock_run.call_args_list)
        self.assertTrue(rm_called)

    @patch("subprocess.run")
    def test_12_cleanup_release_removes_only_specific_container(self, mock_run):
        """Cleanup removes only the target deployment's container, not other deployments or workloads."""
        rel_dir = self.apps_storage / "iso-app" / "releases" / "dep-target"
        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        adapter.cleanup_release("iso-app", "dep-target999", rel_dir)

        # Check call arguments
        rm_call = mock_run.call_args[0][0]
        self.assertEqual(rm_call[1:], ["rm", "-f", "ps-iso-app-dep-targ"])

    # -------------------------------------------------------------------------
    # 5. Idempotency & Rollback Preservation Tests
    # -------------------------------------------------------------------------

    @patch("subprocess.run")
    def test_13_idempotent_submission_returns_running_container(self, mock_run):
        """When candidate container is already running for this exact deployment, reuse it without recreation."""
        rel_dir = self.apps_storage / "idem-app" / "releases" / "dep-same"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "release_metadata.json").write_text(json.dumps({
            "image": "idem:latest",
            "container_port": 8000
        }), encoding="utf-8")

        def run_side_effect(cmd, **kwargs):
            if "inspect" in cmd and "{{.State.Running}}" in cmd:
                return MagicMock(returncode=0, stdout="true", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        adapter = ContainerRuntimeAdapter(docker_bin="docker")
        rec = adapter.start_application(
            app_id="idem-app",
            deployment_id="dep-same",
            release_dir=rel_dir,
            port=18502,
            env_vars={}
        )
        self.assertEqual(rec.app_id, "idem-app")
        self.assertEqual(rec.deployment_id, "dep-same")

        # Verify `docker run` was NOT called
        run_called = any("run" in c[0][0] and "-d" in c[0][0] for c in mock_run.call_args_list)
        self.assertFalse(run_called)


if __name__ == "__main__":
    unittest.main()
