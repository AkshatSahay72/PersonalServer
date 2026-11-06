#!/usr/bin/env python3
"""
Controller Artifact Route Handlers
==================================
HTTP request handling for artifact listing, metadata queries, file downloads,
atomic uploads, raw binary streaming, and SHA-256 integrity verification.
"""

import base64
import os
import sys
from typing import Any, Dict, Optional

from common.artifact_manager import (
    generate_artifact_id,
    store_artifact_bytes,
    store_artifact_stream,
    verify_artifact_integrity,
)
from common.crypto_utils import constant_time_compare
from config.platform_config import get_controller_paths
from controller.onboarding import get_or_create_enrollment_token
from controller.stores import (
    load_artifacts_db,
    load_jobs_db,
    save_artifacts_db,
    save_jobs_db,
)


def handle_artifact_get(handler: Any, path: str, query: Dict[str, list]) -> bool:
    """
    Handles GET requests for artifact listing, metadata, binary download,
    and job artifact associations.
    Returns True if handled, False otherwise.
    """
    # GET /artifacts - List cluster artifacts
    if path == "/artifacts":
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to list artifacts"})
            return True
        artifacts_db = load_artifacts_db()
        arts = list(artifacts_db.get("artifacts", {}).values())
        handler.send_json(200, {
            "artifacts": arts,
            "count": len(arts)
        })
        return True

    # GET /artifacts/<artifact_id>/metadata - Artifact metadata & checksum status
    if path.startswith("/artifacts/") and path.endswith("/metadata"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to access artifact metadata"})
            return True
        art_id = path[11:-9].strip()
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True

        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        if not is_admin:
            caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
            if not caller_node_id:
                handler.send_json(401, {"error": "Unauthorized: Node credentials invalid"})
                return True
            art_wf_id = art.get("workflow_id")
            authorized_nodes = list(art.get("authorized_nodes", []))
            if art.get("is_alias"):
                canon_id = art.get("canonical_artifact_id")
                if canon_id and canon_id in artifacts_db.get("artifacts", {}):
                    authorized_nodes = list(dict.fromkeys(authorized_nodes + (artifacts_db["artifacts"][canon_id].get("authorized_nodes") or [])))
            producing_node = art.get("producing_node")
            if not producing_node and art.get("job_id"):
                jobs_db = load_jobs_db()
                j = jobs_db.get("jobs", {}).get(art["job_id"], {})
                producing_node = j.get("assigned_node") or j.get("target_node")

            if art_wf_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == art_wf_id]
                node_in_wf = any(j.get("assigned_node") == caller_node_id or j.get("target_node") == caller_node_id for j in wf_jobs)
                if not node_in_wf and caller_node_id not in authorized_nodes and producing_node != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifacts for workflow '{art_wf_id}'"})
                    return True
            else:
                # Ad-hoc artifact without workflow: restrict to producing node or explicitly authorized nodes
                if caller_node_id != producing_node and caller_node_id not in authorized_nodes:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifact '{art_id}' (not producer or authorized node)"})
                    return True

        is_valid = verify_artifact_integrity(art)
        handler.send_json(200, {"artifact": art, "integrity_verified": is_valid})
        return True

    # GET /artifacts/<artifact_id>/download or GET /artifacts/<artifact_id>
    if path.startswith("/artifacts/"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to download artifacts"})
            return True
        art_id = path[11:].rstrip("/")
        if art_id.endswith("/download"):
            art_id = art_id[:-9]
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True

        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        if not is_admin:
            caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
            if not caller_node_id:
                handler.send_json(401, {"error": "Unauthorized: Node credentials invalid"})
                return True
            art_wf_id = art.get("workflow_id")
            authorized_nodes = list(art.get("authorized_nodes", []))
            if art.get("is_alias"):
                canon_id = art.get("canonical_artifact_id")
                if canon_id and canon_id in artifacts_db.get("artifacts", {}):
                    authorized_nodes = list(dict.fromkeys(authorized_nodes + (artifacts_db["artifacts"][canon_id].get("authorized_nodes") or [])))
            producing_node = art.get("producing_node")
            if not producing_node and art.get("job_id"):
                jobs_db = load_jobs_db()
                j = jobs_db.get("jobs", {}).get(art["job_id"], {})
                producing_node = j.get("assigned_node") or j.get("target_node")

            if art_wf_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == art_wf_id]
                node_in_wf = any(j.get("assigned_node") == caller_node_id or j.get("target_node") == caller_node_id for j in wf_jobs)
                if not node_in_wf and caller_node_id not in authorized_nodes and producing_node != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifacts for workflow '{art_wf_id}'"})
                    return True
            else:
                # Ad-hoc artifact without workflow: restrict to producing node, explicitly authorized nodes,
                # or any node assigned/targeted for a job consuming this artifact
                is_consumer_node = False
                jobs_db = load_jobs_db()
                for j in jobs_db.get("jobs", {}).values():
                    if j.get("assigned_node") == caller_node_id or j.get("target_node") == caller_node_id:
                        for in_art in j.get("input_artifacts", []):
                            if in_art.get("artifact_id") == art_id:
                                is_consumer_node = True
                                break
                        if is_consumer_node:
                            break

                if not is_consumer_node and caller_node_id != producing_node and caller_node_id not in authorized_nodes:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not authorized to access artifact '{art_id}' (not producer or authorized node)"})
                    return True

        file_path = art.get("path")
        if not file_path or not os.path.exists(file_path):
            handler.send_json(404, {"error": f"Artifact file on disk missing for '{art_id}'"})
            return True

        # Strict integrity check against registered SHA-256 before streaming
        if not verify_artifact_integrity(art):
            handler.send_json(500, {"error": f"Artifact integrity verification failed on disk for '{art_id}' (tampered or corrupted)"})
            return True

        file_size = os.path.getsize(file_path)
        handler.send_response(200)
        handler.send_header("Content-Type", art.get("content_type", "application/octet-stream"))
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("X-Artifact-Sha256", art.get("sha256", ""))
        handler.send_header("Content-Disposition", f'attachment; filename="{art.get("filename", art_id)}"')
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                handler.wfile.write(chunk)
        return True

    # GET /jobs/<job_id>/artifacts
    if path.startswith("/jobs/") and path.endswith("/artifacts"):
        token = handler.parse_auth_token()
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to access job artifacts"})
            return True
        job_id = path[6:-10].strip()
        artifacts_db = load_artifacts_db()
        matching = [
            a for a in artifacts_db.get("artifacts", {}).values()
            if a.get("job_id") == job_id
        ]
        handler.send_json(200, {
            "job_id": job_id,
            "artifacts": matching,
            "count": len(matching)
        })
        return True

    return False


def handle_artifact_post(handler: Any, path: str, body: Optional[Dict[str, Any]], token: Optional[str], query: Dict[str, list]) -> bool:
    """
    Handles POST requests for artifact upload, streaming upload, and verification.
    Returns True if handled, False otherwise.
    """
    # POST /artifacts/upload - Secure atomic artifact upload with SHA-256
    if path == "/artifacts/upload":
        if not body:
            handler.send_json(400, {"error": "Missing JSON request body"})
            return True
        enrollment_token = get_or_create_enrollment_token()
        if not handler.is_authenticated_admin_or_node() and not constant_time_compare(token, enrollment_token):
            handler.send_json(401, {"error": "Unauthorized: Authentication required to upload artifacts"})
            return True

        filename = body.get("filename") or body.get("name")
        content_b64 = body.get("content_base64")
        if not filename or content_b64 is None:
            handler.send_json(400, {"error": "Missing required 'filename' or 'content_base64'"})
            return True

        # Bound request payload memory size: 64 MB base64 limit
        if len(content_b64) > 64 * 1024 * 1024:
            handler.send_json(413, {"error": "Payload Too Large: Base64 artifact exceeds 64MB limit. Use streaming upload."})
            return True

        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception as e:
            handler.send_json(400, {"error": f"Invalid base64 encoding: {e}"})
            return True

        expected_sha256 = body.get("sha256")
        job_id = body.get("job_id")
        workflow_id = body.get("workflow_id")
        content_type = body.get("content_type", "application/octet-stream")
        metadata = body.get("metadata", {})

        # Fencing generation validation for job artifact uploads (J1/J3 mandatory HTTP 412)
        if job_id:
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if job:
                req_gen = body.get("lease_generation")
                if req_gen is None and body.get("fencing_token") is not None:
                    req_gen = body.get("fencing_token")
                curr_gen = int(job.get("lease_generation", 1))
                if req_gen is not None and int(req_gen) < curr_gen:
                    handler.send_json(412, {
                        "error": f"Precondition Failed: Stale worker fencing generation {req_gen} < current {curr_gen}",
                        "job_id": job_id,
                        "fencing_token": curr_gen,
                        "superseded": True
                    })
                    return True

        storage_root = get_controller_paths().artifacts_dir
        art_id = generate_artifact_id()

        try:
            art_record = store_artifact_bytes(
                storage_root=storage_root,
                artifact_id=art_id,
                filename=filename,
                content_bytes=content_bytes,
                expected_sha256=expected_sha256,
                job_id=job_id,
                workflow_id=workflow_id,
                content_type=content_type,
                metadata=metadata
            )
        except ValueError as e:
            handler.send_json(400, {"error": str(e)})
            return True
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to store artifact: {e}"})
            return True

        caller_node_id = handler.get_authenticated_node_id() if hasattr(handler, "get_authenticated_node_id") else None
        if isinstance(caller_node_id, str):
            art_record["producing_node"] = caller_node_id
        elif job_id:
            jobs_db = load_jobs_db()
            j = jobs_db.get("jobs", {}).get(job_id, {})
            if j.get("assigned_node"):
                art_record["producing_node"] = j.get("assigned_node")

        auth_nodes = body.get("authorized_nodes") or metadata.get("authorized_nodes")
        if auth_nodes and isinstance(auth_nodes, list):
            art_record["authorized_nodes"] = [str(n) for n in auth_nodes]

        artifacts_db = load_artifacts_db()
        artifacts_db.setdefault("artifacts", {})[art_id] = art_record
        save_artifacts_db(artifacts_db)

        if job_id:
            try:
                jobs_db = load_jobs_db()
                if job_id in jobs_db.get("jobs", {}):
                    job_arts = jobs_db["jobs"][job_id].setdefault("artifacts", [])
                    if not any(a.get("artifact_id") == art_id for a in job_arts):
                        job_arts.append(art_record)
                        save_jobs_db(jobs_db)
            except Exception as e:
                print(f"[CONTROLLER-ARTIFACTS] Error linking artifact to job {job_id}: {e}", file=sys.stderr)

        handler.send_json(201, {
            "status": "UPLOADED",
            "artifact": art_record
        })
        return True

    # POST /artifacts/upload/raw - Streaming raw binary artifact upload
    if path == "/artifacts/upload/raw":
        enrollment_token = get_or_create_enrollment_token()
        is_admin = False
        if hasattr(handler, "is_authenticated_admin"):
            admin_val = handler.is_authenticated_admin()
            if isinstance(admin_val, bool):
                is_admin = admin_val
        if not is_admin:
            is_admin = constant_time_compare(token, enrollment_token)

        caller_node_id = None
        if hasattr(handler, "get_authenticated_node_id"):
            raw_node_id = handler.get_authenticated_node_id()
            if isinstance(raw_node_id, str):
                caller_node_id = raw_node_id

        if not is_admin and not caller_node_id and not handler.is_authenticated_admin_or_node():
            handler.send_json(401, {"error": "Unauthorized: Authentication required to upload artifacts"})
            return True

        filename = query.get("filename", [None])[0] or handler.headers.get("X-Artifact-Filename")
        if not filename:
            handler.send_json(400, {"error": "Missing 'filename' query parameter or X-Artifact-Filename header"})
            return True

        try:
            content_length = int(handler.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0

        # Maximum artifact limit: 64 MB
        MAX_ARTIFACT_LIMIT = 64 * 1024 * 1024
        if content_length > MAX_ARTIFACT_LIMIT:
            handler.send_json(413, {"error": f"Payload Too Large: Artifact exceeds {MAX_ARTIFACT_LIMIT // (1024*1024)}MB limit."})
            return True

        expected_sha256 = query.get("sha256", [None])[0] or handler.headers.get("X-Artifact-Sha256")
        job_id = query.get("job_id", [None])[0] or handler.headers.get("X-Job-Id")
        workflow_id = query.get("workflow_id", [None])[0] or handler.headers.get("X-Workflow-Id")
        name = query.get("name", [None])[0] or handler.headers.get("X-Artifact-Name") or filename
        step_id = query.get("step_id", [None])[0] or handler.headers.get("X-Workflow-Step-Id")
        content_type = handler.headers.get("Content-Type", "application/octet-stream")

        # Worker assignment authorization & Fencing validation
        if job_id:
            jobs_db = load_jobs_db()
            job = jobs_db.get("jobs", {}).get(job_id)
            if job:
                req_gen = query.get("lease_generation", [None])[0] or query.get("fencing_token", [None])[0] or handler.headers.get("X-Lease-Generation") or handler.headers.get("X-Fencing-Token")
                curr_gen = int(job.get("lease_generation", 1))
                if req_gen is not None and int(req_gen) < curr_gen:
                    handler.send_json(412, {
                        "error": f"Precondition Failed: Stale worker fencing generation {req_gen} < current {curr_gen}",
                        "job_id": job_id,
                        "fencing_token": curr_gen,
                        "superseded": True
                    })
                    return True

                if not is_admin and caller_node_id and job.get("assigned_node") and job.get("assigned_node") != caller_node_id:
                    handler.send_json(403, {"error": f"Forbidden: Node '{caller_node_id}' is not assigned to job '{job_id}'"})
                    return True
                if not workflow_id and job.get("workflow_id"):
                    workflow_id = job.get("workflow_id")
                if not step_id and job.get("workflow_step_id"):
                    step_id = job.get("workflow_step_id")

        # Check for existing artifact on retry (idempotent duplicate avoidance)
        artifacts_db = load_artifacts_db()
        existing_art = next(
            (a for a in artifacts_db.get("artifacts", {}).values()
             if a.get("job_id") == job_id and (a.get("filename") == filename or a.get("name") == name)),
            None
        )
        if existing_art and expected_sha256 and existing_art.get("sha256", "").lower() == expected_sha256.lower():
            if content_length > 0:
                # Drain stream cleanly
                try:
                    _ = handler.rfile.read(content_length)
                except Exception:
                    pass
            if verify_artifact_integrity(existing_art):
                handler.send_json(200, {
                    "status": "EXISTS",
                    "artifact": existing_art,
                    "message": "Artifact already exists with identical checksum."
                })
                return True

        storage_root = get_controller_paths().artifacts_dir
        art_id = generate_artifact_id()

        try:
            art_record = store_artifact_stream(
                storage_root=storage_root,
                artifact_id=art_id,
                filename=filename,
                stream=handler.rfile,
                total_bytes_expected=content_length,
                expected_sha256=expected_sha256,
                job_id=job_id,
                workflow_id=workflow_id,
                content_type=content_type,
                max_bytes=MAX_ARTIFACT_LIMIT
            )
            art_record["name"] = name
            art_record["workflow_step_id"] = step_id
            art_record["producing_node"] = caller_node_id if isinstance(caller_node_id, str) else None
            if workflow_id:
                jobs_db = load_jobs_db()
                wf_jobs = [j for j in jobs_db.get("jobs", {}).values() if j.get("workflow_id") == workflow_id]
                auth_nodes = [j.get("assigned_node") for j in wf_jobs if isinstance(j.get("assigned_node"), str)]
                if isinstance(caller_node_id, str):
                    auth_nodes.append(caller_node_id)
                art_record["authorized_nodes"] = list(set(auth_nodes))
        except ValueError as e:
            handler.send_json(400, {"error": str(e)})
            return True
        except Exception as e:
            handler.send_json(500, {"error": f"Failed to store streaming artifact: {e}"})
            return True

        artifacts_db = load_artifacts_db()
        artifacts_db.setdefault("artifacts", {})[art_id] = art_record
        save_artifacts_db(artifacts_db)

        if job_id:
            try:
                jobs_db = load_jobs_db()
                if job_id in jobs_db.get("jobs", {}):
                    job_arts = jobs_db["jobs"][job_id].setdefault("artifacts", [])
                    if not any(a.get("artifact_id") == art_id for a in job_arts):
                        job_arts.append(art_record)
                        save_jobs_db(jobs_db)
            except Exception as e:
                print(f"[CONTROLLER-ARTIFACTS] Error linking streaming artifact to job {job_id}: {e}", file=sys.stderr)

        handler.send_json(201, {
            "status": "UPLOADED",
            "artifact": art_record
        })
        return True

    # POST /artifacts/<artifact_id>/verify - Verify integrity of artifact on disk
    if path.startswith("/artifacts/") and path.endswith("/verify"):
        art_id = path[11:-7].strip()
        artifacts_db = load_artifacts_db()
        art = artifacts_db.get("artifacts", {}).get(art_id)
        if not art:
            handler.send_json(404, {"error": f"Artifact '{art_id}' not found"})
            return True
        is_valid = verify_artifact_integrity(art)
        handler.send_json(200, {
            "artifact_id": art_id,
            "integrity_verified": is_valid,
            "artifact": art
        })
        return True

    return False


__all__ = [
    "handle_artifact_get",
    "handle_artifact_post",
]
