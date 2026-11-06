#!/usr/bin/env python3
"""
PersonalServer V1.0 DAG Workflow Demonstration
=============================================
Demonstrates an end-to-end multi-step DAG workflow with:
1. Two-node simulated cluster (producer and consumer).
2. Explicit job dependency enforcement (stage1 -> stage2).
3. Dependency-aware scheduling (consumer cannot claim before producer succeeds).
4. Authenticated artifact transfer with SHA-256 cryptographic verification.
5. Inspectable state transitions and workflow completion observability.
"""

import base64
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agent.simulated_worker import SimulatedWorker, WorkerBehavior
from controller.onboarding import get_or_create_enrollment_token

CONTROLLER_URL = "http://127.0.0.1:8000"


def http_get(path: str, token: str) -> dict:
    url = f"{CONTROLLER_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(path: str, payload: dict, token: str) -> dict:
    url = f"{CONTROLLER_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 80)
    print(" PersonalServer V1.0 — DAG-Based Job Workflows Local Demonstration")
    print("=" * 80)

    # 1. Load enrollment token
    admin_token = get_or_create_enrollment_token()
    print(f"\n[1] Admin credentials loaded (Enrollment token: {admin_token[:8]}...)")

    # 2. Register two simulated workers: Producer and Consumer
    print("\n[2] Registering Simulated Worker Nodes...")
    producer_worker = SimulatedWorker(
        node_id="sim-node-producer",
        name="Telemetry Producer Node",
        controller_url=CONTROLLER_URL,
        enrollment_token=admin_token,
        role="compute",
        resources={"cpu_cores": 4, "ram_mb": 8192, "storage_gb": 100},
        capabilities={"compute": True, "storage": True, "network": True}
    )
    producer_worker.register()
    producer_worker.send_heartbeat()
    print(f"  • Producer registered: node_id='{producer_worker.node_id}' (token: {producer_worker.auth_token[:8]}...)")

    consumer_worker = SimulatedWorker(
        node_id="sim-node-consumer",
        name="Data Analytics Consumer Node",
        controller_url=CONTROLLER_URL,
        enrollment_token=admin_token,
        role="compute",
        resources={"cpu_cores": 4, "ram_mb": 8192, "storage_gb": 100},
        capabilities={"compute": True, "storage": True, "network": True}
    )
    consumer_worker.register()
    consumer_worker.send_heartbeat()
    print(f"  • Consumer registered: node_id='{consumer_worker.node_id}' (token: {consumer_worker.auth_token[:8]}...)")

    # 3. Define and submit DAG workflow specification
    print("\n[3] Submitting 2-Job DAG Workflow Specification via POST /workflows...")
    workflow_spec = {
        "name": "Sensor Data Processing Pipeline",
        "description": "Multi-stage pipeline demonstrating dependency unblocking and SHA-256 artifact exchange",
        "failure_policy": "fail_fast",
        "steps": {
            "extract_telemetry": {
                "name": "Stage 1: Extract Sensor Telemetry",
                "type": "echo",
                "target": producer_worker.node_id,
                "parameters": {"message": "Extracting sensor telemetry dataset..."},
                "outputs": [{"name": "telemetry.csv", "filename": "telemetry.csv"}]
            },
            "aggregate_analytics": {
                "name": "Stage 2: Aggregate Analytics",
                "type": "echo",
                "target": consumer_worker.node_id,
                "depends_on": ["extract_telemetry"],
                "parameters": {"message": "Aggregating telemetry analytics..."},
                "inputs": [{"step": "extract_telemetry", "name": "telemetry.csv"}]
            }
        }
    }

    create_resp = http_post("/workflows", workflow_spec, admin_token)
    wf = create_resp["workflow"]
    wf_id = wf["workflow_id"]
    job1_id = wf["steps"]["extract_telemetry"]["job_id"]
    job2_id = wf["steps"]["aggregate_analytics"]["job_id"]

    print(f"  • Workflow Created: {wf['name']} (ID: {wf_id})")
    print(f"  • Stage 1 Job ID:    {job1_id} [extract_telemetry] -> target: {producer_worker.node_id}")
    print(f"  • Stage 2 Job ID:    {job2_id} [aggregate_analytics] -> target: {consumer_worker.node_id} (depends on: extract_telemetry)")

    # 4. Verify initial state: Stage 1 is QUEUED, Stage 2 is WAITING_FOR_DEPENDENCY
    print("\n[4] Querying Initial Workflow & Job States via GET /workflows/{wf_id}...")
    wf_state = http_get(f"/workflows/{wf_id}", admin_token)
    jobs_map = wf_state.get("jobs", {})
    j1_init = jobs_map.get("extract_telemetry", {})
    j2_init = jobs_map.get("aggregate_analytics", {})

    print(f"  • Workflow Status:               {wf_state['workflow']['status']}")
    print(f"  • Stage 1 Status:                {j1_init.get('status')} (waiting_reason: {j1_init.get('waiting_reason')})")
    print(f"  • Stage 2 Status:                {j2_init.get('status')} (waiting_reason: {j2_init.get('waiting_reason')})")

    assert j1_init.get("status") == "QUEUED", f"Expected QUEUED, got {j1_init.get('status')}"
    assert j2_init.get("status") == "WAITING_FOR_DEPENDENCY", f"Expected WAITING_FOR_DEPENDENCY, got {j2_init.get('status')}"

    # 5. Dependency check: Consumer polls for jobs before producer completes
    print("\n[5] Dependency Protection Check: Consumer attempts to poll for jobs...")
    consumer_early_job = consumer_worker.poll_and_claim_job()
    print(f"  • Consumer poll result: {consumer_early_job} (Verified: Dependent job cannot be claimed!)")
    assert consumer_early_job is None, "ERROR: Consumer claimed dependent job before producer completed!"

    # 6. Stage 1 Execution by Producer
    print("\n[6] Stage 1 Execution (Producer Node):")
    claimed_j1 = producer_worker.poll_and_claim_job()
    print(f"  • Producer polled and CLAIMED Job: {claimed_j1['job_id']}")
    print(f"  • Status transition: QUEUED -> {claimed_j1['status']} (Lease generation: {claimed_j1['lease_generation']})")
    assert claimed_j1["job_id"] == job1_id

    # Producer generates dataset artifact
    dataset_content = (
        b"device_id,timestamp_utc,temperature_c,pressure_hpa,voltage_v\n"
        b"dev-001,2026-10-24T23:55:01Z,24.15,1013.25,3.85\n"
        b"dev-002,2026-10-24T23:55:02Z,23.80,1013.20,3.82\n"
        b"dev-003,2026-10-24T23:55:03Z,24.50,1013.30,3.90\n"
    )
    expected_sha256 = hashlib.sha256(dataset_content).hexdigest()
    print(f"  • Generated Dataset: size={len(dataset_content)} bytes, SHA-256={expected_sha256}")

    # Producer uploads artifact
    uploaded_artifact = producer_worker.upload_artifact(
        job_id=job1_id,
        filename="telemetry.csv",
        content_bytes=dataset_content,
        name="telemetry.csv",
        workflow_id=wf_id,
        step_id="extract_telemetry"
    )
    art_id = uploaded_artifact["artifact_id"]
    print(f"  • Artifact Uploaded: artifact_id='{art_id}', producing_node='{uploaded_artifact.get('producing_node')}'")

    # Producer completes job
    j1_result = producer_worker.submit_job_result(
        job_id=job1_id,
        status="SUCCEEDED",
        exit_code=0,
        stdout="Telemetry extraction completed successfully. 3 records produced.",
        artifacts=[uploaded_artifact]
    )
    print(f"  • Producer reported result: status=SUCCEEDED, exit_code=0 (ack={j1_result.get('ack')})")

    # 7. Check Dependency Unblocking
    print("\n[7] Querying Workflow State After Stage 1 Completion:")
    wf_state_mid = http_get(f"/workflows/{wf_id}", admin_token)
    j1_mid = wf_state_mid.get("jobs", {}).get("extract_telemetry", {})
    j2_mid = wf_state_mid.get("jobs", {}).get("aggregate_analytics", {})

    print(f"  • Workflow Status:               {wf_state_mid['workflow']['status']} (progress: {wf_state_mid['workflow']['progress_percent']}%)")
    print(f"  • Stage 1 Status:                {j1_mid.get('status')}")
    print(f"  • Stage 2 Status:                {j2_mid.get('status')}")
    print(f"  • Stage 2 Input Artifacts:       {len(j2_mid.get('input_artifacts', []))} artifact(s) resolved")

    assert j1_mid.get("status") == "SUCCEEDED"
    assert j2_mid.get("status") == "QUEUED", f"Expected Stage 2 to be QUEUED, got {j2_mid.get('status')}"
    assert len(j2_mid.get("input_artifacts", [])) == 1
    resolved_art = j2_mid["input_artifacts"][0]
    print(f"    - Name:      {resolved_art['name']}")
    print(f"    - ID:        {resolved_art['artifact_id']}")
    print(f"    - SHA-256:   {resolved_art['sha256']}")
    assert resolved_art["artifact_id"] == art_id
    assert resolved_art["sha256"] == expected_sha256

    # 8. Stage 2 Execution by Consumer
    print("\n[8] Stage 2 Execution (Consumer Node):")
    claimed_j2 = consumer_worker.poll_and_claim_job()
    print(f"  • Consumer polled and CLAIMED Job: {claimed_j2['job_id']}")
    print(f"  • Status transition: QUEUED -> {claimed_j2['status']} (Lease generation: {claimed_j2['lease_generation']})")
    assert claimed_j2["job_id"] == job2_id

    # Consumer downloads artifact and cryptographically verifies SHA-256
    print("  • Consumer downloading input artifact from controller...")
    downloaded_bytes = consumer_worker.download_and_verify_artifact(
        artifact_id=art_id,
        expected_sha256=expected_sha256
    )
    actual_sha256 = hashlib.sha256(downloaded_bytes).hexdigest()
    print(f"  • Downloaded {len(downloaded_bytes)} bytes")
    print(f"  • SHA-256 Verification: {actual_sha256} == {expected_sha256} [MATCH CONFIRMED]")
    assert downloaded_bytes == dataset_content

    # Consumer submits job completion
    j2_result = consumer_worker.submit_job_result(
        job_id=job2_id,
        status="SUCCEEDED",
        exit_code=0,
        stdout=f"Analytics aggregated over {len(downloaded_bytes)} bytes. Verified checksum {actual_sha256[:12]}."
    )
    print(f"  • Consumer reported result: status=SUCCEEDED, exit_code=0 (ack={j2_result.get('ack')})")

    # 9. Final Workflow Completion & Observability
    print("\n[9] Final Workflow State & Verification:")
    wf_final = http_get(f"/workflows/{wf_id}", admin_token)
    wf_meta = wf_final["workflow"]
    all_jobs_list = wf_final.get("jobs_list", [])

    print("=" * 80)
    print(f" Workflow ID:        {wf_meta['workflow_id']}")
    print(f" Name:               {wf_meta['name']}")
    print(f" Final Status:       {wf_meta['status']}")
    print(f" Progress:           {wf_meta['progress_percent']}% ({wf_meta['completed_steps']}/{wf_meta['total_steps']} steps completed)")
    print(f" Started At:         {wf_meta.get('started_at')}")
    print(f" Finished At:        {wf_meta.get('finished_at')}")
    print("=" * 80)
    print(" Job Execution Summary:")
    for j in all_jobs_list:
        print(f"  • Job: {j['job_id']} [{j['workflow_step_id']}]")
        print(f"      Node:        {j.get('assigned_node')}")
        print(f"      Status:      {j.get('status')}")
        print(f"      Started At:  {j.get('started_at')}")
        print(f"      Finished At: {j.get('finished_at')}")
        if j.get("artifacts"):
            print(f"      Output Arts: {[a['artifact_id'] for a in j['artifacts']]}")
        if j.get("input_artifacts"):
            print(f"      Input Arts:  {[a['artifact_id'] for a in j['input_artifacts']]}")
    print("=" * 80)

    assert wf_meta["status"] == "SUCCEEDED", f"Expected workflow SUCCEEDED, got {wf_meta['status']}"
    assert wf_meta["progress_percent"] == 100.0
    print("\n>>> DEMONSTRATION COMPLETE: ALL ASSERTIONS PASSED SUCCESSFULLY <<<")


if __name__ == "__main__":
    main()
