#!/usr/bin/env python3
"""
Comprehensive Test Suite for Phase 10B: Easy Node Onboarding
"""

import sys
import os
import json
import time
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

import controller.controller as ctrl
import importlib.util
spec = importlib.util.spec_from_file_location("node_agent", str(PROJECT_ROOT / "agent" / "node-agent.py"))
agent = importlib.util.module_from_spec(spec)
sys.modules["node_agent"] = agent
spec.loader.exec_module(agent)

def run_tests():
    print("==================================================")
    print(" PersonalServer Phase 10B Test Suite")
    print("==================================================")
    
    passed = 0
    failed = 0

    def assert_test(cond, name):
        nonlocal passed, failed
        if cond:
            print(f"[PASS] {name}")
            passed += 1
        else:
            print(f"[FAIL] {name}")
            failed += 1

    # 1. Hardware Discovery Tests
    print("\n--- Testing Hardware Discovery ---")
    hw = agent.discover_hardware(PROJECT_ROOT)
    assert_test(hw.get("cpu_cores") and hw["cpu_cores"] > 0, "Hardware discovery detects CPU cores")
    assert_test(hw.get("ram_mb") and hw["ram_mb"] > 0, "Hardware discovery detects RAM")
    assert_test(hw.get("storage_gb") and hw["storage_gb"] > 0, "Hardware discovery detects Storage")
    assert_test(bool(hw.get("os")), f"Hardware discovery detects OS ({hw.get('os')})")
    assert_test(bool(hw.get("architecture")), f"Hardware discovery detects Arch ({hw.get('architecture')})")
    assert_test(bool(hw.get("name")), f"Hardware discovery detects Hostname ({hw.get('name')})")

    # 2. Node ID Generation & Persistence
    print("\n--- Testing Node ID Generation & Persistence ---")
    nid1 = agent.generate_node_id()
    nid2 = agent.generate_node_id()
    assert_test(nid1.startswith("server-") and len(nid1) == 23, "Generated Node ID matches format server-<16 hex>")
    assert_test(nid1 != nid2, "Generated Node IDs are distinct & random")

    # 3. Onboarding Code Generation & Hashing
    print("\n--- Testing Onboarding Code Generation & Storage ---")
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        old_data_dir = ctrl.DATA_DIR
        old_codes_file = ctrl.ONBOARDING_CODES_FILE
        old_nodes_file = ctrl.NODES_FILE
        old_jobs_file = ctrl.JOBS_FILE
        old_secrets_dir = ctrl.SECRETS_DIR
        old_enroll_file = ctrl.ENROLLMENT_TOKEN_FILE

        try:
            ctrl.DATA_DIR = temp_path / "data"
            ctrl.SECRETS_DIR = temp_path / "secrets"
            ctrl.ONBOARDING_CODES_FILE = ctrl.DATA_DIR / "onboarding_codes.json"
            ctrl.BACKUP_ONBOARDING_CODES_FILE = ctrl.DATA_DIR / "onboarding_codes.json.bak"
            ctrl.NODES_FILE = ctrl.DATA_DIR / "nodes.json"
            ctrl.BACKUP_NODES_FILE = ctrl.DATA_DIR / "nodes.json.bak"
            ctrl.JOBS_FILE = ctrl.DATA_DIR / "jobs.json"
            ctrl.BACKUP_JOBS_FILE = ctrl.DATA_DIR / "jobs.json.bak"
            ctrl.ENROLLMENT_TOKEN_FILE = ctrl.SECRETS_DIR / "enrollment.token"

            # Generate code with 10s TTL
            code1, ttl1 = ctrl.generate_onboarding_code(10)
            assert_test(code1.startswith("PS-") and len(code1) == 12, f"Generated code format PS-XXXX-XXXX ({code1})")
            
            # Verify code is hashed in storage (NOT plaintext)
            codes_db = ctrl.load_onboarding_codes_db()
            raw_content = ctrl.ONBOARDING_CODES_FILE.read_text(encoding="utf-8")
            assert_test(code1 not in raw_content, "Onboarding code is NOT stored in plaintext")
            
            code1_hash = ctrl.hash_onboarding_code(code1)
            assert_test(code1_hash in codes_db["codes"], "SHA-256 hash of onboarding code is present in store")

            # 4. Code Validation & Consumption
            print("\n--- Testing Code Validation & Single-Use Consumption ---")
            ok, err = ctrl.validate_and_consume_onboarding_code("PS-INVALID-CODE", "test-node-1")
            assert_test(not ok and "Invalid" in err, "Invalid code is rejected")

            ok, err = ctrl.validate_and_consume_onboarding_code(code1, "test-node-1")
            assert_test(ok and err == "", "Valid code is successfully validated and consumed")

            # Reusing the consumed code must fail
            ok, err = ctrl.validate_and_consume_onboarding_code(code1, "test-node-2")
            assert_test(not ok and "already been used" in err, "Reusing consumed code is immediately rejected")

            # 5. Expiration Test
            print("\n--- Testing Code Expiration ---")
            code_exp, _ = ctrl.generate_onboarding_code(1)  # 1 second TTL
            time.sleep(1.2)
            ok, err = ctrl.validate_and_consume_onboarding_code(code_exp, "test-node-3")
            assert_test(not ok and "expired" in err, "Expired code is rejected")

            # 6. Concurrency / Race Condition Test
            print("\n--- Testing Concurrent Code Consumption (Race Safety) ---")
            code_race, _ = ctrl.generate_onboarding_code(60)
            results = []

            def try_consume(node_id):
                res, _ = ctrl.validate_and_consume_onboarding_code(code_race, node_id)
                results.append(res)

            threads = [threading.Thread(target=try_consume, args=(f"race-node-{i}",)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            success_count = sum(1 for r in results if r is True)
            assert_test(success_count == 1, f"Exactly one concurrent thread consumes the code (got {success_count}/10)")

            # 7. Controller HTTP Integration Tests
            print("\n--- Testing Controller HTTP Registration Endpoint ---")
            # Start in-process test server
            test_port = 8991
            server = ctrl.HTTPServer(("127.0.0.1", test_port), ctrl.ControllerHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            time.sleep(0.5)

            controller_base = f"http://127.0.0.1:{test_port}"

            # Test A: Registration with Onboarding Code via X-Onboarding-Code
            code_http, _ = ctrl.generate_onboarding_code(60)
            node_payload = {
                "node_id": "test-auto-node-01",
                "name": "worker-alpha",
                "role": "compute",
                "platform": "linux",
                "os": "Linux",
                "architecture": "x86_64",
                "cpu_cores": 4,
                "ram_mb": 4096,
                "storage_gb": 64
            }

            req = urllib.request.Request(
                f"{controller_base}/register",
                data=json.dumps(node_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Onboarding-Code": code_http
                },
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=5) as resp:
                reg_res = json.loads(resp.read().decode("utf-8"))
                assert_test(reg_res.get("status") == "registered", "HTTP registration with X-Onboarding-Code returns status 'registered'")
                auth_tok = reg_res.get("auth_token")
                assert_test(bool(auth_tok) and len(auth_tok) == 40, "HTTP registration returns 40-char per-node auth_token")

            # Test B: Code is now consumed - re-registering with same code should fail HTTP 401
            try:
                req_reuse = urllib.request.Request(
                    f"{controller_base}/register",
                    data=json.dumps(node_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Onboarding-Code": code_http
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req_reuse, timeout=5) as resp:
                    assert_test(False, "Reused onboarding code should return 401")
            except urllib.error.HTTPError as e:
                assert_test(e.code == 401, f"Reused onboarding code returns HTTP 401 (got {e.code})")

            # Test C: Static Enrollment Token Backward Compatibility
            static_token = ctrl.get_or_create_enrollment_token()
            node_static_payload = {
                "node_id": "test-static-node-02",
                "name": "worker-beta",
                "role": "compute",
                "platform": "linux",
                "os": "Linux",
                "architecture": "x86_64",
                "cpu_cores": 8,
                "ram_mb": 8192,
                "storage_gb": 128
            }

            req_static = urllib.request.Request(
                f"{controller_base}/register",
                data=json.dumps(node_static_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {static_token}"
                },
                method="POST"
            )

            with urllib.request.urlopen(req_static, timeout=5) as resp:
                static_res = json.loads(resp.read().decode("utf-8"))
                assert_test(static_res.get("status") == "registered", "Static enrollment token registration still works seamlessly")

            # Test D: Node Heartbeat with received auth_token
            hb_payload = {
                "node_id": "test-auto-node-01",
                "status": "online",
                "timestamp": ctrl.get_current_iso_timestamp(),
                "services": {"node_api": "running"},
                "system": {"cpu_cores": 4, "memory": "1G/4G"}
            }

            req_hb = urllib.request.Request(
                f"{controller_base}/heartbeat",
                data=json.dumps(hb_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {auth_tok}"
                },
                method="POST"
            )

            with urllib.request.urlopen(req_hb, timeout=5) as resp:
                hb_res = json.loads(resp.read().decode("utf-8"))
                assert_test(hb_res.get("status") == "ok" and hb_res.get("ack") is True, "Node heartbeat authenticated with issued auth_token succeeds")

            # Test E: Agent Onboard End-to-End Simulation
            print("\n--- Testing Agent Onboarding End-to-End Workflow ---")
            code_agent_onboard, _ = ctrl.generate_onboarding_code(60)
            
            # Use a mock args object
            class MockArgs:
                def __init__(self, controller, code):
                    self.controller = controller
                    self.code = code

            old_node_json = agent.NODE_JSON
            old_controller_json = agent.CONTROLLER_JSON
            old_reg_file = agent.REGISTRATION_FILE
            old_start_script = agent.START_SCRIPT

            agent_temp = temp_path / "agent_test"
            agent_temp.mkdir(parents=True, exist_ok=True)
            agent_cfg = agent_temp / "config"
            agent_runtime = agent_temp / "runtime"
            agent_cfg.mkdir(parents=True, exist_ok=True)
            agent_runtime.mkdir(parents=True, exist_ok=True)

            agent.CONFIG_DIR = agent_cfg
            agent.RUNTIME_DIR = agent_runtime
            agent.NODE_JSON = agent_cfg / "node.json"
            agent.CONTROLLER_JSON = agent_cfg / "controller.json"
            agent.REGISTRATION_FILE = agent_runtime / "registration.json"
            agent.START_SCRIPT = agent_temp / "dummy_start.sh" # Non-existent or dummy
            
            try:
                # 1. Onboard should create node.json & controller.json & registration.json
                res_code = agent.cmd_onboard(MockArgs(controller_base, code_agent_onboard))
                assert_test(res_code == 0, "agent.cmd_onboard executes successfully (returncode 0)")
                assert_test(agent.NODE_JSON.exists(), "agent.cmd_onboard generated config/node.json")
                assert_test(agent.CONTROLLER_JSON.exists(), "agent.cmd_onboard generated config/controller.json")
                assert_test(agent.REGISTRATION_FILE.exists(), "agent.cmd_onboard saved runtime/registration.json")

                # Verify node identity in node.json
                with open(agent.NODE_JSON, "r", encoding="utf-8") as f:
                    created_node_cfg = json.load(f)
                orig_nid = created_node_cfg.get("node_id")
                assert_test(bool(orig_nid) and orig_nid.startswith("server-"), "Created node.json contains valid server-<id>")

                # 2. Existing node.json should NOT be overwritten on subsequent run
                code_agent_onboard_2, _ = ctrl.generate_onboarding_code(60)
                # Modify custom field in node.json
                created_node_cfg["custom_flag"] = "do_not_overwrite"
                with open(agent.NODE_JSON, "w", encoding="utf-8") as f:
                    json.dump(created_node_cfg, f, indent=2)

                res_code_2 = agent.cmd_onboard(MockArgs(controller_base, code_agent_onboard_2))
                assert_test(res_code_2 == 0, "Second onboard run succeeds")
                with open(agent.NODE_JSON, "r", encoding="utf-8") as f:
                    reloaded_cfg = json.load(f)
                assert_test(reloaded_cfg.get("custom_flag") == "do_not_overwrite", "Existing config/node.json was NOT overwritten")
                assert_test(reloaded_cfg.get("node_id") == orig_nid, "Node ID persisted across onboarding runs")

            finally:
                agent.CONFIG_DIR = PROJECT_ROOT / "config"
                agent.RUNTIME_DIR = PROJECT_ROOT / "runtime"
                agent.NODE_JSON = old_node_json
                agent.CONTROLLER_JSON = old_controller_json
                agent.REGISTRATION_FILE = old_reg_file
                agent.START_SCRIPT = old_start_script

            server.shutdown()
            server.server_close()

        finally:
            ctrl.DATA_DIR = old_data_dir
            ctrl.SECRETS_DIR = old_secrets_dir
            ctrl.ONBOARDING_CODES_FILE = old_codes_file
            ctrl.BACKUP_ONBOARDING_CODES_FILE = old_data_dir / "onboarding_codes.json.bak"
            ctrl.NODES_FILE = old_nodes_file
            ctrl.BACKUP_NODES_FILE = old_data_dir / "nodes.json.bak"
            ctrl.JOBS_FILE = old_jobs_file
            ctrl.BACKUP_JOBS_FILE = old_data_dir / "jobs.json.bak"
            ctrl.ENROLLMENT_TOKEN_FILE = old_enroll_file

    print("\n==================================================")
    print(f" Test Results: {passed} PASSED, {failed} FAILED")
    print("==================================================")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(run_tests())
