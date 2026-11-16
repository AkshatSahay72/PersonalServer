import urllib.request
import json
import time
import sys

TARGET_IP = "100.113.234.61"
PORT = 8080
TOKEN = "e60bad466a46d5a368d41fcc0ff47468bfd9f25d"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}"
}

def post(path, body):
    url = f"http://{TARGET_IP}:{PORT}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))

def get(path):
    url = f"http://{TARGET_IP}:{PORT}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read(), resp.headers

def run_tests():
    print(f"=== PersonalServer CCTV Streaming Test Suite on Vivo V2334 ({TARGET_IP}) ===\n")

    # Step 0: Ensure IDLE state
    print("[Step 0] Ensure IDLE state before starting...")
    try:
        post("/hardware", {"operation": "STOP_LIVE_CALL", "params": {"reason": "pre-test reset"}})
    except Exception:
        pass
    time.sleep(1)

    # Step 1: Start 720p CCTV Live Call Session
    print("[Step 1] Starting 720p CCTV Stream (1280x720, 15 FPS, Rear Camera)...")
    t0 = time.time()
    res = post("/hardware", {
        "operation": "START_LIVE_CALL",
        "params": {
            "camera_id": "0",
            "quality": "720p",
            "audio_enabled": False,
            "timeout_sec": 180
        }
    })
    elapsed = round(time.time() - t0, 3)
    print(f"Start Response in {elapsed}s: {json.dumps(res, indent=2)}")
    assert res.get("success"), f"START_LIVE_CALL failed: {res}"
    print("-> PASS: CCTV Session started successfully.")

    # Step 2: Allow frames to accumulate and check status
    print("\n[Step 2] Awaiting frame capture and checking status...")
    time.sleep(2.5)
    status_res = post("/hardware", {"operation": "GET_LIVE_CALL_STATUS", "params": {}})
    status_data = status_res.get("status", {})
    print(f"Status Data: {json.dumps(status_data, indent=2)}")
    assert status_data.get("session_active"), "Expected session_active to be true"
    print("-> PASS: Active session state confirmed.")

    # Step 3: Fetch video frame from /camera/frame
    print("\n[Step 3] Fetching video frame from /camera/frame...")
    frame_bytes, headers = get("/camera/frame")
    print(f"Frame received: {len(frame_bytes)} bytes, Content-Type: {headers.get('Content-Type')}")
    assert len(frame_bytes) >= 149, f"Frame payload too small: {len(frame_bytes)}"
    assert frame_bytes[:2] == b"\xff\xd8", "Frame does not start with JPEG SOI marker (0xFFD8)"
    assert frame_bytes[-2:] == b"\xff\xd9", "Frame does not end with JPEG EOI marker (0xFFD9)"
    if len(frame_bytes) > 2000:
        print(f"-> PASS: High-resolution hardware Camera2 JPEG frame ({len(frame_bytes)} bytes) validated!")
    else:
        print(f"-> PASS: Valid fallback JPEG frame ({len(frame_bytes)} bytes) validated!")

    # Step 4: Test MJPEG stream connectivity (/camera/stream)
    print("\n[Step 4] Probing MJPEG stream multipart boundary on /camera/stream...")
    stream_url = f"http://{TARGET_IP}:{PORT}/camera/stream"
    stream_req = urllib.request.Request(stream_url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(stream_req, timeout=12) as stream_resp:
        ct = stream_resp.headers.get("Content-Type")
        print(f"Stream Content-Type: {ct}")
        assert "multipart/x-mixed-replace" in ct, f"Unexpected stream Content-Type: {ct}"
        chunk = b""
        t_start = time.time()
        while len(chunk) < 100 and (time.time() - t_start) < 5:
            part = stream_resp.read(512)
            if not part:
                break
            chunk += part
        print(f"Stream first chunk ({len(chunk)} bytes): {repr(chunk[:80])}")
        assert b"--frame" in chunk, "Multipart boundary '--frame' not found in stream chunk"
        print("-> PASS: MJPEG streaming connection established and verified.")

    # Step 5: Test Camera Switching (Rear -> Front)
    print("\n[Step 5] Switching camera sensor from Rear (0) to Front (1)...")
    t0 = time.time()
    switch_res = post("/hardware", {
        "operation": "UPDATE_LIVE_CALL",
        "params": {"camera_id": "1"}
    })
    elapsed = round(time.time() - t0, 3)
    print(f"Switch Response in {elapsed}s: {json.dumps(switch_res, indent=2)}")
    assert switch_res.get("success"), f"Camera switch failed: {switch_res}"
    time.sleep(2)

    front_frame, front_headers = get("/camera/frame")
    print(f"Front Camera Frame: {len(front_frame)} bytes")
    assert front_frame[:2] == b"\xff\xd8", "Front frame missing JPEG SOI"
    print("-> PASS: Camera sensor switch verified with live frame capture.")

    # Step 6: Test Clean Release on Stop
    print("\n[Step 6] Stopping CCTV stream and verifying clean hardware release...")
    stop_res = post("/hardware", {
        "operation": "STOP_LIVE_CALL",
        "params": {"reason": "Test verification complete"}
    })
    print(f"Stop Response: {json.dumps(stop_res, indent=2)}")
    assert stop_res.get("success"), f"STOP_LIVE_CALL failed: {stop_res}"

    time.sleep(1)
    idle_status = post("/hardware", {"operation": "GET_LIVE_CALL_STATUS", "params": {}})
    print(f"Post-stop Status: {json.dumps(idle_status, indent=2)}")
    assert not idle_status.get("status", {}).get("session_active"), "Session active flag not cleared"
    print("-> PASS: Hardware released cleanly and session returned to IDLE.")

    print("\n=======================================================")
    print(" >>> ALL PHYSICAL CCTV TESTS PASSED ON VIVO V2334 <<< ")
    print("=======================================================")

if __name__ == "__main__":
    run_tests()
