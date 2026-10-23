#!/usr/bin/env python3
"""
PersonalServer HA Leader Identity and Cluster State Audit Test Suite
====================================================================
Tests:
1. Vivo Y31 as active leader while the Windows PC (Aki) is offline.
2. Stale persisted leader identity pointing to node-pc.
3. Unknown or unregistered leader ID.
4. A standby controller being online without being the leader.
5. A successful leader change updating every relevant state and component.
6. An unavailable HA endpoint producing an explicit unknown/unreachable state.
7. Ensuring the UI does not independently invent leadership based on node status.
8. Authoritative controller node ID resolution precedence.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import override_controller_paths, ControllerPaths
from config.ha_manager import (
    HAManager,
    HAState,
    LeaderLease,
    resolve_controller_node_id
)


class TestHALeaderIdentityAudit(unittest.TestCase):

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="ps_ha_audit_"))
        self.data_dir = self.test_root / "data"
        self.config_dir = self.test_root / "config"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.paths_ctx = override_controller_paths(
            data_dir=self.data_dir,
            config_dir=self.config_dir
        )
        self.paths = self.paths_ctx.__enter__()

        # Seed realistic cluster nodes.json
        self.nodes_data = {
            "cluster_name": "PersonalServer",
            "nodes": {
                "server-5387a86bf36116b1": {
                    "node_id": "server-5387a86bf36116b1",
                    "name": "vivo-y31",
                    "role": "compute",
                    "platform": "termux",
                    "status": "ONLINE",
                    "tailscale_ip": "100.85.108.5"
                },
                "server-f8b1485ecf458f74": {
                    "node_id": "server-f8b1485ecf458f74",
                    "name": "Aki",
                    "role": "compute",
                    "platform": "win32",
                    "status": "OFFLINE",
                    "tailscale_ip": "100.120.251.42"
                },
                "server-95bad5ff01424d4c8d184330d6d2e394": {
                    "node_id": "server-95bad5ff01424d4c8d184330d6d2e394",
                    "name": "node-02",
                    "role": "compute",
                    "platform": "termux",
                    "status": "ONLINE",
                    "tailscale_ip": "100.90.10.20"
                }
            }
        }
        (self.data_dir / "nodes.json").write_text(json.dumps(self.nodes_data, indent=2), encoding="utf-8")

    def tearDown(self):
        self.paths_ctx.__exit__(None, None, None)
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_01_vivo_y31_active_leader_while_windows_pc_offline(self):
        """Vivo Y31 is the active leader while Windows PC Aki is offline."""
        ha = HAManager(
            node_id="server-5387a86bf36116b1",
            role="leader",
            paths=self.paths
        )
        ha.initialize()

        status = ha.get_status_dict()
        self.assertEqual(status["role"], "leader")
        self.assertEqual(status["node_id"], "server-5387a86bf36116b1")
        self.assertEqual(status["leader_id"], "server-5387a86bf36116b1")
        self.assertEqual(status["leader_name"], "vivo-y31")
        self.assertTrue(status["leader_online"], "Vivo Y31 should be verified online")
        self.assertTrue(status["leader_registered"], "Vivo Y31 should be registered in nodes.json")
        self.assertTrue(status["lease_valid"])

    def test_02_stale_persisted_leader_identity_pointing_to_node_pc(self):
        """Stale persisted lease pointing to node-pc is flagged as unregistered and unverified."""
        lease_file = self.data_dir / "leader_lease.json"
        stale_lease = LeaderLease(
            term=1,
            leader_id="node-pc",
            leader_url="http://192.168.1.50:8000",
            lease_expires_at=time.time() + 60,
            state_version=1
        )
        stale_lease.save_to_file(lease_file)

        # Standby controller loads disk lease
        ha = HAManager(
            node_id="server-95bad5ff01424d4c8d184330d6d2e394",
            role="standby",
            paths=self.paths
        )
        ha.initialize()

        name, is_online, is_registered = ha.resolve_leader_name("node-pc")
        self.assertEqual(name, "node-pc")
        self.assertFalse(is_online, "node-pc is not an online node")
        self.assertFalse(is_registered, "node-pc is not registered in nodes.json")

        status = ha.get_status_dict()
        self.assertEqual(status["leader_id"], "node-pc")
        self.assertFalse(status["leader_registered"], "node-pc must not be reported as a registered leader")
        self.assertFalse(status["leader_online"], "node-pc must not be reported as online")

    def test_03_unknown_unregistered_leader_id(self):
        """Any random or unregistered leader ID is marked unregistered and unverified."""
        ha = HAManager(
            node_id="server-5387a86bf36116b1",
            role="auto",
            paths=self.paths
        )
        ha.initialize()

        name, is_online, is_registered = ha.resolve_leader_name("unknown-phantom-master-999")
        self.assertEqual(name, "unknown-phantom-master-999")
        self.assertFalse(is_registered)
        self.assertFalse(is_online)

    def test_04_standby_controller_online_without_being_leader(self):
        """Standby controller correctly identifies that it is NOT the leader while Vivo Y31 is leader."""
        # Active leader Vivo Y31 holds lease
        vivo_lease = LeaderLease(
            term=2,
            leader_id="server-5387a86bf36116b1",
            leader_url="http://100.85.108.5:8000",
            lease_expires_at=time.time() + 30,
            state_version=5
        )
        vivo_lease.save_to_file(self.data_dir / "leader_lease.json")

        # Standby controller on Node 02
        standby_ha = HAManager(
            node_id="server-95bad5ff01424d4c8d184330d6d2e394",
            role="standby",
            paths=self.paths
        )
        standby_ha.initialize()

        self.assertFalse(standby_ha.is_leader)
        self.assertTrue(standby_ha.is_standby)
        self.assertEqual(standby_ha.current_leader_id, "server-5387a86bf36116b1")

        status = standby_ha.get_status_dict()
        self.assertEqual(status["role"], "standby")
        self.assertEqual(status["node_id"], "server-95bad5ff01424d4c8d184330d6d2e394")
        self.assertEqual(status["leader_id"], "server-5387a86bf36116b1")
        self.assertEqual(status["leader_name"], "vivo-y31")
        self.assertTrue(status["leader_online"])
        self.assertTrue(status["leader_registered"])

    def test_05_successful_leader_change_preserves_term_and_updates_identity(self):
        """Successful election increments term and cleanly updates leader identity."""
        ha = HAManager(
            node_id="server-5387a86bf36116b1",
            role="leader",
            paths=self.paths
        )
        ha.initialize()
        initial_term = ha.current_term

        # Promote self for next term
        ha.elect_self(reason="Scheduled failover test")
        self.assertEqual(ha.current_term, initial_term + 1)
        self.assertEqual(ha.current_leader_id, "server-5387a86bf36116b1")
        status = ha.get_status_dict()
        self.assertEqual(status["term"], initial_term + 1)
        self.assertEqual(status["leader_name"], "vivo-y31")

    def test_06_controller_node_id_resolution_precedence(self):
        """Validates resolve_controller_node_id precedence and automatic registration matching."""
        # 1. Explicit argument has highest priority
        self.assertEqual(
            resolve_controller_node_id(node_id="server-explicit-123", paths=self.paths),
            "server-explicit-123"
        )

        # 2. Environment variable PERSONALSERVER_NODE_ID
        os.environ["PERSONALSERVER_NODE_ID"] = "server-5387a86bf36116b1"
        try:
            self.assertEqual(
                resolve_controller_node_id(paths=self.paths),
                "server-5387a86bf36116b1"
            )
        finally:
            del os.environ["PERSONALSERVER_NODE_ID"]

        # 3. Local node.json config file
        (self.config_dir / "node.json").write_text(
            json.dumps({"node_id": "server-from-node-json", "name": "test-node"}),
            encoding="utf-8"
        )
        self.assertEqual(
            resolve_controller_node_id(paths=self.paths),
            "server-from-node-json"
        )
        (self.config_dir / "node.json").unlink()

    def test_07_no_node_pc_hardcoding_in_html_or_code(self):
        """Verifies that 'node-pc' does not appear anywhere in static HTML."""
        index_html = BASE_DIR / "services" / "node-api" / "static" / "index.html"
        self.assertTrue(index_html.exists())
        content = index_html.read_text(encoding="utf-8")
        self.assertNotIn("node-pc", content, "node-pc must not exist anywhere in index.html")

    def test_08_js_leader_resolution_all_scenarios(self):
        """Runs Node.js script evaluating resolveLeaderInfo from app.js across all 7 cluster scenarios."""
        node_bin = shutil.which("node")
        if not node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        js_script = r"""
        const fs = require('fs');
        const path = require('path');
        const code = fs.readFileSync(path.join('services', 'node-api', 'static', 'app.js'), 'utf-8');

        // Extract resolveLeaderInfo and resolveNodeDisplayName
        const extractFunc = (fnName) => {
            const start = code.indexOf(`function ${fnName}`);
            if (start === -1) throw new Error(`Function ${fnName} not found`);
            let depth = 0;
            let end = -1;
            for (let i = code.indexOf('{', start); i < code.length; i++) {
                if (code[i] === '{') depth++;
                else if (code[i] === '}') {
                    depth--;
                    if (depth === 0) { end = i + 1; break; }
                }
            }
            return code.substring(start, end);
        };

        const fnCode = extractFunc('resolveLeaderInfo') + '\n' + extractFunc('resolveNodeDisplayName');
        const vm = require('vm');
        const sandbox = {
            cachedNodes: [
                { node_id: "server-5387a86bf36116b1", name: "vivo-y31", status: "ONLINE" },
                { node_id: "server-f8b1485ecf458f74", name: "Aki", status: "OFFLINE" },
                { node_id: "server-95bad5ff01424d4c8d184330d6d2e394", name: "node-02", status: "ONLINE" }
            ],
            console: console
        };
        vm.createContext(sandbox);
        vm.runInContext(fnCode, sandbox);

        // Scenario 1: Vivo Y31 active leader
        const s1 = sandbox.resolveLeaderInfo({
            role: "leader",
            node_id: "server-5387a86bf36116b1",
            lease_valid: true,
            term: 2
        });
        if (s1.displayText !== "vivo-y31" || !s1.isVerified || s1.statusText !== "Active") {
            throw new Error(`Scenario 1 failed: ${JSON.stringify(s1)}`);
        }

        // Scenario 2: Stale leader identity pointing to node-pc
        const s2 = sandbox.resolveLeaderInfo({
            role: "standby",
            leader_id: "node-pc",
            lease_valid: true,
            term: 1
        });
        if (!s2.displayText.includes("Leader status unknown (node-pc)") || s2.isVerified) {
            throw new Error(`Scenario 2 failed: ${JSON.stringify(s2)}`);
        }

        // Scenario 3: Stale offline node Aki in leader state
        const s3 = sandbox.resolveLeaderInfo({
            role: "standby",
            leader_id: "server-f8b1485ecf458f74",
            lease_valid: true,
            term: 1
        });
        if (!s3.displayText.includes("Offline") || s3.isVerified) {
            throw new Error(`Scenario 3 failed: ${JSON.stringify(s3)}`);
        }

        // Scenario 4: Standby controller online replicating from Vivo Y31
        const s4 = sandbox.resolveLeaderInfo({
            role: "standby",
            node_id: "server-95bad5ff01424d4c8d184330d6d2e394",
            leader_id: "server-5387a86bf36116b1",
            lease_valid: true,
            term: 2
        });
        if (s4.displayText !== "vivo-y31" || !s4.isVerified || s4.statusText !== "Standby") {
            throw new Error(`Scenario 4 failed: ${JSON.stringify(s4)}`);
        }

        // Scenario 5: Election in progress or expired lease
        const s5 = sandbox.resolveLeaderInfo({
            role: "candidate",
            leader_id: "server-5387a86bf36116b1",
            lease_valid: false,
            term: 3
        });
        if (s5.displayText !== "Electing..." || s5.statusText !== "Election" || s5.isVerified) {
            throw new Error(`Scenario 5 failed: ${JSON.stringify(s5)}`);
        }

        // Scenario 6: HA endpoint unreachable (null or empty object)
        const s6 = sandbox.resolveLeaderInfo(null);
        if (!s6.displayText.includes("Leader status unknown") || s6.isVerified) {
            throw new Error(`Scenario 6 failed: ${JSON.stringify(s6)}`);
        }

        // Scenario 7: Fenced controller
        const s7 = sandbox.resolveLeaderInfo({
            role: "fenced",
            node_id: "server-5387a86bf36116b1",
            lease_valid: false,
            term: 2
        });
        if (s7.displayText !== "Fenced" || s7.statusText !== "Fenced" || s7.isVerified) {
            throw new Error(`Scenario 7 failed: ${JSON.stringify(s7)}`);
        }

        console.log("ALL_JS_HA_SCENARIOS_PASSED");
        """
        result = subprocess.run(
            [node_bin, "-e", js_script],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"JavaScript HA leader resolution scenarios failed:\n{result.stderr}\nOutput: {result.stdout}"
        )
        self.assertIn("ALL_JS_HA_SCENARIOS_PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
