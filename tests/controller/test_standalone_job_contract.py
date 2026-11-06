#!/usr/bin/env python3
"""
PersonalServer V2.0-A — Standalone Python Job Artifact Contract Test Suite
========================================================================
Validates the normalized standalone job submission contract:
1. Top-level 'code', 'script', and 'args' normalized into parameters.
2. Output declarations normalized into canonical format recognized by JobExecutor.
3. Valid input artifact references resolved with expected filename and SHA-256.
4. Missing or invalid artifact references rejected with HTTP 400.
5. Backward compatibility with legacy parameters shapes and DAG workflows.
6. End-to-end Python execution consuming input artifact and producing output artifact.
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths
from controller.controller import ControllerHandler
from controller.stores import (
    load_jobs_db,
    save_jobs_db,
    load_nodes_db,
    save_nodes_db,
    load_artifacts_db,
    save_artifacts_db
)
from common.artifact_manager import (
    store_artifact_bytes,
    generate_artifact_id,
    compute_file_sha256
)
from agent.job_executor import (
    JobExecutor,
    JOB_STATE_QUEUED,
    JOB_STATE_CLAIMED,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED
)


class TestStandaloneJobContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="ps_standalone_contract_test_"))
        cls.data_dir = cls.test_dir / "data"
        cls.config_dir = cls.test_dir / "config"
        cls.secrets_dir = cls.config_dir / "secrets"
        cls.runtime_dir = cls.test_dir / "runtime"
        cls.logs_dir = cls.test_dir / "logs"
        cls.storage_dir = cls.test_dir / "storage"
        cls.artifacts_dir = cls.storage_dir / "artifacts"

        for d in (cls.data_dir, cls.secrets_dir, cls.runtime_dir, cls.logs_dir, cls.storage_dir, cls.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

        cls.enrollment_token = "standalone-test-token-secret-12345"
        (cls.secrets_dir / "enrollment.token").write_text(cls.enrollment_token + "\n", encoding="utf-8")

        cls.paths_ctx = override_controller_paths(
            data_dir=cls.data_dir,
            config_dir=cls.config_dir,
            secrets_dir=cls.secrets_dir,
            runtime_dir=cls.runtime_dir,
            logs_dir=cls.logs_dir,
            storage_dir=cls.storage_dir
        )
        cls.paths = cls.paths_ctx.__enter__()

        # Ensure test server runs as authorized leader in isolated test sandbox
        from controller.ha_helper import get_ha_manager
        get_ha_manager(paths=cls.paths, role="leader", force_reload=True)

        # Register a test compute node
        cls.node_id = "test-node-compute-01"
        cls.node_token = "node-token-compute-54321"
        nodes_db = {
            "nodes": {
                cls.node_id: {
                    "node_id": cls.node_id,
                    "name": "Test Compute Node",
                    "role": "compute",
                    "status": "ONLINE",
                    "auth_token": cls.node_token,
                    "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "verified_capabilities": {
                        "compute": True,
                        "python3": True,
                        "python": True,
                        "storage": True
                    },
                    "resources": {
                        "cpu_cores": 4,
                        "ram_mb": 4096,
                        "storage_gb": 50
                    }
                }
            }
        }
        save_nodes_db(nodes_db)

        ControllerHandler.heartbeat_timeout = 60
        ControllerHandler.default_backoff_sec = 0.0
        cls.httpd = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        cls.server_port = cls.httpd.server_address[1]
        cls.controller_url = f"http://127.0.0.1:{cls.server_port}"

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    def setUp(self):
        # Reset jobs_db for each test to keep test queue deterministic
        save_jobs_db({"jobs": {}})

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "httpd"):
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if hasattr(cls, "paths_ctx"):
            cls.paths_ctx.__exit__(None, None, None)
        try:
            from controller.ha_helper import get_ha_manager
            get_ha_manager(force_reload=True)
        except Exception:
            pass
        if hasattr(cls, "test_dir") and cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _api_get(self, path, token=None):
        url = f"{self.controller_url}{path}"
        headers = {"User-Agent": "PersonalServer-Test/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return e.code, data

    def _api_post(self, path, payload, token=None):
        url = f"{self.controller_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "PersonalServer-Test/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return e.code, data

    def test_01_top_level_code_and_args_normalized(self):
        """Top-level 'code' and 'args' are normalized into parameters without overwriting explicit parameters."""
        payload = {
            "type": "python-script",
            "name": "Top-Level Script Test",
            "target": self.node_id,
            "code": "print('hello top level')",
            "args": ["foo", "bar"],
            "timeout": 30
        }
        status, resp = self._api_post("/jobs", payload, token=self.enrollment_token)
        self.assertEqual(status, 200)
        job_id = resp["job_id"]

        jobs_db = load_jobs_db()
        job = jobs_db["jobs"][job_id]
        self.assertEqual(job["parameters"]["code"], "print('hello top level')")
        self.assertEqual(job["parameters"]["args"], ["foo", "bar"])

        # Test that explicit parameters['code'] takes precedence
        payload_explicit = {
            "type": "python-script",
            "target": self.node_id,
            "code": "print('ignored')",
            "parameters": {
                "code": "print('explicit takes precedence')"
            }
        }
        status_exp, resp_exp = self._api_post("/jobs", payload_explicit, token=self.enrollment_token)
        self.assertEqual(status_exp, 200)
        jobs_db = load_jobs_db()
        job_exp = jobs_db["jobs"][resp_exp["job_id"]]
        self.assertEqual(job_exp["parameters"]["code"], "print('explicit takes precedence')")

    def test_02_output_declarations_normalized_and_recognized(self):
        """Output declarations are normalized into canonical list of dicts recognized by JobExecutor."""
        payload = {
            "type": "python-script",
            "target": self.node_id,
            "code": "pass",
            "outputs": [
                "metrics.csv",
                {"name": "summary.json", "path": "summary.json"}
            ]
        }
        status, resp = self._api_post("/jobs", payload, token=self.enrollment_token)
        self.assertEqual(status, 200)
        job_id = resp["job_id"]

        jobs_db = load_jobs_db()
        job = jobs_db["jobs"][job_id]
        self.assertIn("outputs", job)
        self.assertIn("expected_outputs", job)
        self.assertEqual(len(job["outputs"]), 2)
        self.assertEqual(job["outputs"][0]["name"], "metrics.csv")
        self.assertEqual(job["outputs"][0]["filename"], "metrics.csv")
        self.assertEqual(job["outputs"][1]["name"], "summary.json")

    def test_03_valid_input_artifacts_resolved_with_checksum(self):
        """Valid input artifact references are resolved with expected filename, checksum, and size."""
        # 1. Create a seed artifact in artifacts_db
        content = b"header1,header2\nval1,val2\n"
        sha = hashlib.sha256(content).hexdigest()
        art_id = generate_artifact_id()

        art_rec = store_artifact_bytes(
            storage_root=self.artifacts_dir,
            artifact_id=art_id,
            filename="data_feed.csv",
            content_bytes=content,
            expected_sha256=sha,
            job_id="job-seed-producer"
        )
        art_rec["name"] = "data_feed.csv"

        art_db = load_artifacts_db()
        art_db["artifacts"][art_id] = art_rec
        save_artifacts_db(art_db)

        # 2. Submit standalone job referencing this artifact
        payload = {
            "type": "python-script",
            "target": self.node_id,
            "code": "print('processing feed')",
            "inputs": [
                {"artifact_id": art_id, "name": "custom_feed.csv"}
            ]
        }
        status, resp = self._api_post("/jobs", payload, token=self.enrollment_token)
        self.assertEqual(status, 200)

        jobs_db = load_jobs_db()
        job = jobs_db["jobs"][resp["job_id"]]
        self.assertIn("input_artifacts", job)
        self.assertEqual(len(job["input_artifacts"]), 1)
        in_art = job["input_artifacts"][0]
        self.assertEqual(in_art["artifact_id"], art_id)
        self.assertEqual(in_art["name"], "custom_feed.csv")
        self.assertEqual(in_art["filename"], "data_feed.csv")
        self.assertEqual(in_art["sha256"], sha)
        self.assertEqual(in_art["size_bytes"], len(content))

    def test_04_missing_or_invalid_artifact_references_rejected(self):
        """Missing or non-existent artifact references are rejected with HTTP 400 Bad Request."""
        payload_missing = {
            "type": "python-script",
            "target": self.node_id,
            "code": "pass",
            "inputs": [
                {"artifact_id": "art-nonexistent-99999"}
            ]
        }
        status, resp = self._api_post("/jobs", payload_missing, token=self.enrollment_token)
        self.assertEqual(status, 400)
        self.assertIn("not found in cluster artifact database", resp.get("error", ""))

        payload_malformed = {
            "type": "python-script",
            "target": self.node_id,
            "code": "pass",
            "inputs": [
                {"invalid_key": "some_value"}
            ]
        }
        status2, resp2 = self._api_post("/jobs", payload_malformed, token=self.enrollment_token)
        self.assertEqual(status2, 400)
        self.assertIn("missing required 'artifact_id'", resp2.get("error", ""))

    def test_05_legacy_parameters_and_dag_compatibility(self):
        """Legacy payload shapes with nested parameters remain 100% compatible."""
        legacy_payload = {
            "type": "python-script",
            "target": self.node_id,
            "parameters": {
                "code": "print('legacy works')",
                "args": ["leg1"],
                "outputs": ["out_leg.txt"]
            }
        }
        status, resp = self._api_post("/jobs", legacy_payload, token=self.enrollment_token)
        self.assertEqual(status, 200)

        jobs_db = load_jobs_db()
        job = jobs_db["jobs"][resp["job_id"]]
        self.assertEqual(job["parameters"]["code"], "print('legacy works')")
        self.assertEqual(job["parameters"]["args"], ["leg1"])
        self.assertEqual(len(job["outputs"]), 1)
        self.assertEqual(job["outputs"][0]["name"], "out_leg.txt")

    def test_06_execution_with_input_and_output_artifacts(self):
        """Python execution reads input artifact from sandbox and produces verified output artifact."""
        # 1. Create input artifact
        input_data = b"alpha,100\nbeta,200\n"
        input_sha = hashlib.sha256(input_data).hexdigest()
        art_id = generate_artifact_id()

        art_rec = store_artifact_bytes(
            storage_root=self.artifacts_dir,
            artifact_id=art_id,
            filename="input_numbers.csv",
            content_bytes=input_data,
            expected_sha256=input_sha,
            job_id="job-producer-mock"
        )
        art_db = load_artifacts_db()
        art_db["artifacts"][art_id] = art_rec
        save_artifacts_db(art_db)

        # 2. Submit standalone job
        script = """
with open("input_numbers.csv") as f:
    lines = [line.strip() for line in f if line.strip()]
total = sum(int(l.split(",")[1]) for l in lines)
with open("result_total.txt", "w") as f:
    f.write(f"TOTAL={total}\\n")
print(f"Processed {len(lines)} lines, sum={total}")
"""
        payload = {
            "type": "python-script",
            "target": self.node_id,
            "code": script,
            "inputs": [{"artifact_id": art_id, "name": "input_numbers.csv"}],
            "outputs": [{"name": "result_total.txt", "path": "result_total.txt"}]
        }
        status, submit_resp = self._api_post("/jobs", payload, token=self.enrollment_token)
        self.assertEqual(status, 200)
        job_id = submit_resp["job_id"]

        # 3. Worker claims job via /nodes/<id>/jobs/next
        claim_status, claim_resp = self._api_get(f"/nodes/{self.node_id}/jobs/next", token=self.node_token)
        self.assertEqual(claim_status, 200)
        claimed_job = claim_resp["job"]
        self.assertEqual(claimed_job["job_id"], job_id)
        self.assertEqual(len(claimed_job["input_artifacts"]), 1)

        # 4. Simulate worker sandbox setup (copying input artifact into sandbox as _download_input_artifacts would)
        worker_sandbox = self.runtime_dir / "jobs" / job_id
        worker_sandbox.mkdir(parents=True, exist_ok=True)
        shutil.copy2(art_rec["path"], str(worker_sandbox / "input_numbers.csv"))

        # 5. Execute via JobExecutor
        exec_res = JobExecutor.execute(claimed_job, sandbox_dir=worker_sandbox)
        self.assertEqual(exec_res["status"], JOB_STATE_SUCCEEDED)
        self.assertEqual(exec_res["exit_code"], 0)
        self.assertIn("sum=300", exec_res["stdout"])

        # 6. Verify declared outputs
        self.assertEqual(len(exec_res["declared_outputs"]), 1)
        out_art = exec_res["declared_outputs"][0]
        self.assertEqual(out_art["name"], "result_total.txt")
        self.assertTrue(Path(out_art["path"]).exists())
        self.assertEqual(Path(out_art["path"]).read_text().strip(), "TOTAL=300")

    def test_07_cli_submit_with_code_args_and_outputs(self):
        """CLI submit_job function properly normalizes code, args, inputs, and outputs."""
        from controller.cli import submit_job

        # Create an input artifact in storage/artifacts
        data = b"cli input data\n"
        art_id = generate_artifact_id()
        art_rec = store_artifact_bytes(
            storage_root=self.artifacts_dir,
            artifact_id=art_id,
            filename="cli_in.txt",
            content_bytes=data,
            expected_sha256=hashlib.sha256(data).hexdigest(),
            job_id="job-cli-seed"
        )
        art_db = load_artifacts_db()
        art_db["artifacts"][art_id] = art_rec
        save_artifacts_db(art_db)

        exit_code = submit_job(
            target_or_auto=self.node_id,
            job_type="python-script",
            name="CLI Test Job",
            timeout=45,
            code="import sys; print('cli code'); sys.exit(0)",
            args=["arg1", "arg2"],
            inputs=art_id,
            outputs="cli_out.json"
        )
        self.assertEqual(exit_code, 0)

        jobs_db = load_jobs_db()
        job = list(jobs_db["jobs"].values())[-1]
        self.assertEqual(job["name"], "CLI Test Job")
        self.assertEqual(job["parameters"]["code"], "import sys; print('cli code'); sys.exit(0)")
        self.assertEqual(job["parameters"]["args"], ["arg1", "arg2"])
        self.assertEqual(len(job["outputs"]), 1)
        self.assertEqual(job["outputs"][0]["name"], "cli_out.json")
        self.assertEqual(len(job["input_artifacts"]), 1)
        self.assertEqual(job["input_artifacts"][0]["artifact_id"], art_id)


if __name__ == "__main__":
    unittest.main()
