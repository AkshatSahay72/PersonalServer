#!/usr/bin/env python3
"""
Live Cluster Test for Phase 14C:
Tests the complete lifecycle (REGISTER -> ONLINE -> DRAINING -> DEBOARDING -> REMOVED -> REJOIN_PREVENTED)
using an isolated disposable test node on the live cluster.
NEVER touches Node 01, Node 02, or Node 03.
"""

import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

CONTROLLER_URL = "http://100.85.108.5:8000"
ENROLLMENT_TOKEN = Path("config/secrets/enrollment.token").read_text(encoding="utf-8").strip()

def http_req(path, method="GET", data=None, token=None):
    url = f"{CONTROLLER_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        return e.code, json.loads(content) if content else {}

def main():
    test_node_id = "test-disposable-node-14c"
    print(f"=== Starting Live Disposable Node Lifecycle Test ({test_node_id}) ===")

    # 1. Register disposable node
    reg_data = {
        "node_id": test_node_id,
        "name": "Disposable Test Worker",
        "role": "compute",
        "cpu_cores": 2,
        "ram_mb": 2048,
        "storage_gb": 10
    }
    status, res = http_req("/register", method="POST", data=reg_data, token=ENROLLMENT_TOKEN)
    assert status == 200, f"Registration failed: {status} {res}"
    node_auth_token = res["auth_token"]
    print(f"[1/6] Registered test node: status={status}, auth_token acquired")

    # 2. Verify node is ONLINE
    status, res = http_req(f"/nodes/{test_node_id}", token=ENROLLMENT_TOKEN)
    assert status == 200 and res["node"]["status"] == "ONLINE", f"Expected ONLINE, got {res}"
    print(f"[2/6] Verified node is ONLINE")

    # 3. Drain node
    status, res = http_req(f"/nodes/{test_node_id}/drain", method="POST", token=ENROLLMENT_TOKEN)
    assert status == 200 and res["status"] == "DRAINING", f"Drain failed: {status} {res}"
    print(f"[3/6] Node transitioned to DRAINING")

    # 4. Send heartbeat while DRAINING -> should preserve DRAINING status
    hb_data = {"node_id": test_node_id, "services": {"node_api": "running"}}
    status, res = http_req("/heartbeat", method="POST", data=hb_data, token=node_auth_token)
    assert status == 200, f"Heartbeat failed: {status} {res}"
    status, node_info = http_req(f"/nodes/{test_node_id}", token=ENROLLMENT_TOKEN)
    assert node_info["node"]["status"] == "DRAINING", f"Heartbeat broke DRAINING: {node_info}"
    print(f"[4/6] Heartbeat preserved DRAINING state (did not revert to ONLINE)")

    # 5. Deboard node
    status, res = http_req(f"/nodes/{test_node_id}/deboard", method="POST", token=ENROLLMENT_TOKEN)
    assert status == 200 and res["status"] == "DEBOARDING", f"Deboard failed: {status} {res}"
    assert res["can_remove"] is True, f"Expected can_remove=True, got {res}"
    print(f"[5/6] Node transitioned to DEBOARDING, safety checks passed (can_remove=True)")

    # 6. Final safe removal
    status, res = http_req(f"/nodes/{test_node_id}/remove", method="POST", token=ENROLLMENT_TOKEN)
    assert status == 200 and res["status"] == "REMOVED", f"Remove failed: {status} {res}"
    print(f"[6/6] Node safely removed (status=REMOVED)")

    # 7. Rejoin Prevention check: Attempt heartbeat with old credentials
    status, res = http_req("/heartbeat", method="POST", data=hb_data, token=node_auth_token)
    assert status == 403, f"Expected 403 for old credentials on removed node, got {status} {res}"
    print(f"[REJOIN VERIFICATION] Old credentials rejected with HTTP {status}: {res.get('error')}")

    # 8. Verify all 3 real nodes remain ONLINE
    status, cluster = http_req("/cluster")
    assert status == 200
    online_names = [n["name"] for n in cluster["nodes"] if n["status"] == "ONLINE"]
    print(f"\nCluster status: online={cluster['cluster']['online']}, nodes={online_names}")
    assert "vivo-y31" in online_names, "Node 01 must be ONLINE"
    assert "node-02" in online_names, "Node 02 must be ONLINE"
    assert "Aki" in online_names, "Node 03 must be ONLINE"
    print("ALL THREE REAL NODES REMAIN ONLINE AND HEALTHY!")
    print("=== LIVE DISPOSABLE NODE TEST COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    main()
