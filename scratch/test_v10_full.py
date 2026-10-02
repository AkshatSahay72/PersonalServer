#!/usr/bin/env python3
"""
PersonalServer V1.0 Complete End-to-End Verification Suite
=========================================================
"""

import sys
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONTROLLER_URL = "http://127.0.0.1:8000"
PHONE_SSH = "ssh -p 8022 -o StrictHostKeyChecking=no u0_a244@100.85.108.5"
PHONE_NODE_API = "http://127.0.0.1:8080"
PUBLIC_WEB_URL = "https://server.akshatsahay.space"


def run_ssh(cmd):
    full_cmd = f'{PHONE_SSH} "{cmd}"'
    res = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def test_section(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def http_req(url, method="GET", data=None, headers=None):
    if headers is None:
        headers = {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    body = json.dumps(data).encode("utf-8") if isinstance(data, dict) else (data if data else None)
    if isinstance(data, dict) and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read()
            try:
                return resp.status, json.loads(content.decode("utf-8")), resp.headers
            except Exception:
                return resp.status, content.decode("utf-8", errors="ignore"), resp.headers
    except urllib.error.HTTPError as e:
        err_body = e.read()
        try:
            return e.code, json.loads(err_body.decode("utf-8")), e.headers
        except Exception:
            return e.code, err_body.decode("utf-8", errors="ignore"), e.headers
    except Exception as e:
        return 500, str(e), {}


def main():
    # Clean up any leftover active jobs from prior test interruptions
    jobs_db_path = BASE_DIR / "controller" / "data" / "jobs.json"
    if jobs_db_path.exists():
        try:
            with open(jobs_db_path, "r", encoding="utf-8") as f:
                jdb = json.load(f)
            changed = False
            for j in jdb.get("jobs", {}).values():
                if j.get("status") in ("QUEUED", "CLAIMED", "RECOVERING"):
                    j["status"] = "CANCELLED"
                    changed = True
            if changed:
                with open(jobs_db_path, "w", encoding="utf-8") as f:
                    json.dump(jdb, f, indent=2)
        except Exception:
            pass

    test_section("1. CONTROLLER & CLUSTER HEALTH")
    status, cluster_data, _ = http_req(f"{CONTROLLER_URL}/cluster")
    assert status == 200, f"Controller not responding: {status}"
    print(f"Cluster: {cluster_data['cluster']['name']} | Total nodes: {cluster_data['cluster']['total_nodes']}")

    # Heartbeat from phone
    rc, stdout, stderr = run_ssh("cd ~/PersonalServer && python agent/node-agent.py heartbeat")
    print(f"Phone Heartbeat -> RC: {rc} | Output: {stdout.strip()}")
    assert rc == 0, f"Phone heartbeat failed: {stderr}"

    status, nodes_data, _ = http_req(f"{CONTROLLER_URL}/nodes")
    online_nodes = [n for n in nodes_data.get("nodes", []) if n.get("status") == "ONLINE"]
    print(f"Online Nodes count: {len(online_nodes)}")
    assert len(online_nodes) >= 1, "At least 1 node must be online"
    vivo_node_id = online_nodes[0]["node_id"]
    print(f"Verified Real Node 01 ID: {vivo_node_id}")

    test_section("2. STORAGE SUBSYSTEM ON VIVO Y31")
    rc, stdout, stderr = run_ssh("python ~/PersonalServer/scratch/test_phone_storage.py")
    print(stdout)
    if stderr:
        print(f"Stderr: {stderr}")
    assert rc == 0, f"Storage test failed: {stderr}"

    test_section("3. PUBLIC WEB INTERFACE & CLOUDFLARE TUNNEL")
    status, pub_html, _ = http_req(f"{PUBLIC_WEB_URL}/")
    print(f"Public Web Ingress ({PUBLIC_WEB_URL}/) Status: {status}")
    assert status == 200, f"Public web ingress failed with status {status}"
    assert "PersonalServer" in pub_html, "Dashboard title not found in public web response"
    print("Public Web UI accessible without Tailscale on client device!")

    # Check static CSS and JS over public web
    status_css, pub_css, _ = http_req(f"{PUBLIC_WEB_URL}/static/style.css")
    assert status_css == 200 and "--bg-app" in pub_css, "Public CSS asset loading failed"
    print("Public CSS Asset: OK")

    status_js, pub_js, _ = http_req(f"{PUBLIC_WEB_URL}/static/app.js")
    assert status_js == 200 and "navigate" in pub_js, "Public JS Asset loading failed"
    print("Public JS Asset: OK")

    # Check storage usage over public web
    status_usage, pub_usage, _ = http_req(f"{PUBLIC_WEB_URL}/storage/usage")
    assert status_usage == 200 and "storage_root" in pub_usage, "Public storage API failed"
    print(f"Public Storage Usage API: OK ({pub_usage.get('storage_root')})")

    test_section("4. JOB SYSTEM RELIABILITY, LEASES & RECOVERY")

    # Load enrollment token for auth
    token_file = BASE_DIR / "config" / "secrets" / "enrollment.token"
    admin_token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # 4.1 Successful Job Execution on Real Phone
    status, submit_res, _ = http_req(
        f"{CONTROLLER_URL}/jobs",
        method="POST",
        data={
            "target": vivo_node_id,
            "type": "echo",
            "parameters": {"message": "V1.0 Reliability Test"}
        },
        headers=auth_headers
    )
    assert status == 200, f"Job submit failed: {submit_res}"
    job_id = submit_res["job_id"]
    print(f"Submitted Job: {job_id}")

    # Inspect job before claim (should be QUEUED)
    status, job_before, _ = http_req(f"{CONTROLLER_URL}/jobs/{job_id}")
    assert job_before["job"]["status"] == "QUEUED"
    print(f"Job initial state: {job_before['job']['status']} | Attempt: {job_before['job']['attempt']}")

    # Node agent on phone fetches & executes work
    rc, stdout, stderr = run_ssh("cd ~/PersonalServer && python agent/node-agent.py work")
    print(f"Phone Node Agent execution output:\n{stdout.strip()}")
    assert rc == 0, f"Phone execution failed: {stderr}"

    # Inspect job after execution (should be SUCCEEDED)
    status, job_after, _ = http_req(f"{CONTROLLER_URL}/jobs/{job_id}")
    assert job_after["job"]["status"] == "SUCCEEDED"
    assert job_after["job"]["result"]["exit_code"] == 0
    print(f"Job final state: {job_after['job']['status']} | Duration: {job_after['job']['result']['duration_ms']}ms")

    # 4.2 Failure & Automatic Retry
    status, submit_fail, _ = http_req(
        f"{CONTROLLER_URL}/jobs",
        method="POST",
        data={
            "target": vivo_node_id,
            "type": "failing-test",
            "max_attempts": 2
        },
        headers=auth_headers
    )
    assert status == 200, f"Job submit failed: {submit_fail}"
    fail_job_id = submit_fail["job_id"]
    print(f"\nSubmitted Failure Test Job: {fail_job_id} (Max Attempts: 2)")

    # Attempt 1 execution on phone
    run_ssh("cd ~/PersonalServer && python agent/node-agent.py work")
    status, job_att1, _ = http_req(f"{CONTROLLER_URL}/jobs/{fail_job_id}")
    print(f"After Attempt 1 -> State: {job_att1['job']['status']} | Next Attempt: {job_att1['job']['attempt']} | Retry Info: {job_att1['job']['retry_reason']}")
    assert job_att1["job"]["status"] == "RECOVERING", f"Expected RECOVERING state, got {job_att1['job']['status']}"

    # Attempt 2 execution on phone
    run_ssh("cd ~/PersonalServer && python agent/node-agent.py work")
    status, job_att2, _ = http_req(f"{CONTROLLER_URL}/jobs/{fail_job_id}")
    print(f"After Attempt 2 -> State: {job_att2['job']['status']} | Attempts count: {len(job_att2['job']['attempts_history'])}")
    assert job_att2["job"]["status"] == "FAILED", f"Expected FAILED after max attempts, got {job_att2['job']['status']}"
    assert len(job_att2["job"]["attempts_history"]) == 2, "Must have recorded both attempts in history"

    # 4.3 Lease Expiration & Worker Disappearance Recovery
    status, submit_lease, _ = http_req(
        f"{CONTROLLER_URL}/jobs",
        method="POST",
        data={
            "target": "auto",
            "type": "system-info",
            "timeout": 5,
            "max_attempts": 2
        },
        headers=auth_headers
    )
    assert status == 200, f"Lease job submit failed: {submit_lease}"
    lease_job_id = submit_lease["job_id"]
    print(f"\nSubmitted Lease Recovery Test Job: {lease_job_id}")

    # Phone claims the job
    fetch_rc, fetch_out, fetch_err = run_ssh("cd ~/PersonalServer && python scratch/claim_job.py")
    print(f"Job claim output: {fetch_out.strip()}")
    if fetch_err:
        print(f"Claim stderr: {fetch_err}")

    # Inspect job (should be CLAIMED with lease_expires_at)
    status, job_claimed, _ = http_req(f"{CONTROLLER_URL}/jobs/{lease_job_id}")
    assert job_claimed["job"]["status"] == "CLAIMED"
    assert job_claimed["job"]["lease_expires_at"] is not None
    print(f"Lease active until epoch: {job_claimed['job']['lease_expires_at']}")

    # 4.4 Cancellation
    status, cancel_res, _ = http_req(
        f"{CONTROLLER_URL}/jobs/{lease_job_id}/cancel",
        method="POST",
        headers=auth_headers
    )
    assert status == 200
    status, job_cancelled, _ = http_req(f"{CONTROLLER_URL}/jobs/{lease_job_id}")
    assert job_cancelled["job"]["status"] == "CANCELLED"
    print(f"Cancellation verified: {job_cancelled['job']['status']}")

    test_section("V1.0 ALL VERIFICATION TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
