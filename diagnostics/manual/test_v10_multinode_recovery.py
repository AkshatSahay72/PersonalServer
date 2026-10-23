#!/usr/bin/env python3
"""
PersonalServer V1.0 Multi-Node Recovery & Protection Test
=========================================================
"""

import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONTROLLER_URL = "http://127.0.0.1:8000"
TOKEN_FILE = BASE_DIR / "config" / "secrets" / "enrollment.token"
admin_token = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""


def http_post(endpoint, payload, auth=admin_token):
    req = urllib.request.Request(
        f"{CONTROLLER_URL}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": e.reason}


def http_get(endpoint, auth=admin_token):
    req = urllib.request.Request(
        f"{CONTROLLER_URL}{endpoint}",
        headers={"Authorization": f"Bearer {auth}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": e.reason}


def main():
    print("==========================================")
    print(" V1.0 MULTI-NODE & LEASE RECOVERY TEST")
    print("==========================================")

    db_file = BASE_DIR / "controller" / "data" / "jobs.json"
    if db_file.exists():
        with open(db_file, "r") as f:
            jdb = json.load(f)
        for j in jdb.get("jobs", {}).values():
            if j.get("status") in ("QUEUED", "CLAIMED", "RECOVERING"):
                j["status"] = "CANCELLED"
        with open(db_file, "w") as f:
            json.dump(jdb, f, indent=2)

    # 1. Register Mock Node 02
    print("\n[Step 1] Registering Mock Node 02 (worker-node-02)...")
    status, reg_res = http_post("/register", {
        "node_id": "worker-node-02",
        "name": "worker-laptop-compute",
        "role": "compute",
        "platform": "linux",
        "os": "Ubuntu 22.04",
        "architecture": "x86_64",
        "cpu_cores": 8,
        "ram_mb": 8192,
        "storage_gb": 256,
        "capabilities": {"compute": True, "storage": True, "network": True}
    })
    assert status == 200, f"Registration failed: {reg_res}"
    node2_auth = reg_res["auth_token"]
    print(f"Mock Node 02 registered! Auth token generated.")

    # Send heartbeat for Mock Node 02
    status, hb_res = http_post("/heartbeat", {
        "node_id": "worker-node-02",
        "status": "online",
        "services": {"node-api": "running"},
        "system": {
            "cpu_cores": 8,
            "load_average": [0.10, 0.15, 0.12],
            "memory": "2.0Gi/8.0Gi",
            "storage": "50Gi/256Gi",
            "uptime": "12:00:00"
        }
    }, auth=node2_auth)
    assert status == 200
    print("Mock Node 02 heartbeat sent -> ONLINE")

    # 2. Check cluster membership
    status, cluster = http_get("/cluster")
    print(f"Cluster Online Nodes: {cluster['cluster']['online']} / {cluster['cluster']['total_nodes']}")
    assert cluster['cluster']['online'] >= 2, "Both Node 01 and Node 02 must be online"

    # 3. Duplicate Claim Prevention Test
    print("\n[Step 2] Testing Duplicate Claim Protection...")
    status, job_res = http_post("/jobs", {
        "target": "worker-node-02",
        "type": "system-info"
    })
    job_id = job_res["job_id"]

    # Claim 1 by Node 02
    status, claim1 = http_get(f"/nodes/worker-node-02/jobs/next", auth=node2_auth)
    assert status == 200 and claim1.get("job") is not None
    assert claim1["job"]["job_id"] == job_id
    assert claim1["job"]["status"] == "CLAIMED"
    print(f"Claim 1 SUCCESS: Job {job_id} status={claim1['job']['status']}")

    # Claim 2 by Node 02 (immediate duplicate attempt)
    status, claim2 = http_get(f"/nodes/worker-node-02/jobs/next", auth=node2_auth)
    assert status == 200 and claim2.get("job") is None
    print(f"Claim 2 BLOCKED: Duplicate claim prevented! Result: {claim2['message']}")

    # 4. Auto-Target Lease Expiry & Rescheduling Test
    print("\n[Step 3] Testing Auto-Target Lease Expiration & Rescheduling...")
    # Submit job with short timeout
    status, auto_job = http_post("/jobs", {
        "target": "auto",
        "type": "echo",
        "timeout": 5,
        "max_attempts": 3,
        "parameters": {"msg": "lease test"}
    })
    auto_job_id = auto_job["job_id"]
    assigned_node = auto_job["job"]["target_node"]
    print(f"Auto-scheduled job {auto_job_id} -> Initially assigned to '{assigned_node}'")

    # Assigned node claims the job
    claim_auth = node2_auth if assigned_node == "worker-node-02" else admin_token
    status, auto_claim = http_get(f"/nodes/{assigned_node}/jobs/next", auth=claim_auth)
    assert status == 200 and auto_claim.get("job") is not None
    print(f"Job claimed with lease by {assigned_node}. Lease active.")

    # Simulate lease expiry by modifying lease_expires_at to the past in database
    db_file = BASE_DIR / "controller" / "data" / "jobs.json"
    with open(db_file, "r") as f:
        jdb = json.load(f)
    jdb["jobs"][auto_job_id]["lease_expires_at"] = time.time() - 10  # 10s in past
    with open(db_file, "w") as f:
        json.dump(jdb, f, indent=2)

    # Controller sweep (via GET /jobs or background sweeper)
    time.sleep(1)
    status, job_recovered = http_get(f"/jobs/{auto_job_id}")
    print(f"After Lease Expiry -> Status: {job_recovered['job']['status']} | Attempt: {job_recovered['job']['attempt']} | Retry Reason: {job_recovered['job']['retry_reason']}")
    assert job_recovered["job"]["status"] == "RECOVERING"
    assert job_recovered["job"]["attempt"] == 2

    # 5. Explicit-Target Recovery Test (No silent migration)
    print("\n[Step 4] Testing Explicit-Target Recovery (Preserving Target Node)...")
    # Drain any remaining active jobs first
    with open(db_file, "r") as f:
        jdb = json.load(f)
    for j in jdb.get("jobs", {}).values():
        if j.get("status") in ("QUEUED", "CLAIMED", "RECOVERING"):
            j["status"] = "CANCELLED"
    with open(db_file, "w") as f:
        json.dump(jdb, f, indent=2)

    status, exp_job = http_post("/jobs", {
        "target": "worker-node-02",
        "type": "health-check",
        "timeout": 5,
        "max_attempts": 2
    })
    exp_job_id = exp_job["job_id"]

    # Claim job
    status, claim_res = http_get(f"/nodes/worker-node-02/jobs/next", auth=node2_auth)
    assert claim_res.get("job") is not None and claim_res["job"]["job_id"] == exp_job_id

    # Expire lease in DB
    with open(db_file, "r") as f:
        jdb = json.load(f)
    jdb["jobs"][exp_job_id]["lease_expires_at"] = time.time() - 10
    with open(db_file, "w") as f:
        json.dump(jdb, f, indent=2)

    time.sleep(1)
    status, exp_recovered = http_get(f"/jobs/{exp_job_id}")
    print(f"Explicit target recovered -> Status: {exp_recovered['job']['status']} | Target Node: {exp_recovered['job']['target_node']} (Not migrated)")
    assert exp_recovered["job"]["status"] == "RECOVERING"
    assert exp_recovered["job"]["target_node"] == "worker-node-02"

    # 6. Node Removal & Old Token Protection
    print("\n[Step 5] Testing Node Removal & Token Invalidation...")
    status, rem_res = http_post("/nodes/worker-node-02/remove", {})
    assert status == 200
    print(f"Node 'worker-node-02' removed from cluster: {rem_res['status']}")

    # Attempt heartbeat with old token
    status, old_hb = http_post("/heartbeat", {
        "node_id": "worker-node-02",
        "status": "online"
    }, auth=node2_auth)
    print(f"Heartbeat with invalidated token -> HTTP {status} (Expected 403 Forbidden)")
    assert status == 403, f"Expected 403 Forbidden, got {status}"

    print("\n==========================================")
    print(" MULTI-NODE & LEASE TESTS 100% PASSED!")
    print("==========================================")


if __name__ == "__main__":
    main()
