"""
PersonalServer E07 — Unified On-Demand Camera, Audio and Live Streaming Integration Test Suite.

Covers:
1. LiveCallProvider lifecycle (IDLE -> REQUESTED -> ACTIVE -> STOPPED)
2. Duplicate session prevention (SESSION_ALREADY_ACTIVE)
3. Simulated frame generation & JPEG verification
4. Camera facing switch & microphone mute update
5. Two-way audio / Push-to-Talk speaker transmission
6. Watchdog timeout & automatic resource release
7. HardwareCapabilityManager integration
8. Controller HTTP routing (Auth, Offline, Local & Remote Dispatch)
"""

import base64
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from agent.hardware.live_call import LiveCallProvider, LiveCallSession
from agent.hardware.manager import HardwareCapabilityManager


class TestLiveCallSessionAndProvider(unittest.TestCase):
    def setUp(self):
        self.provider = LiveCallProvider(bridge_ipc=None)

    def tearDown(self):
        if self.provider.is_active():
            self.provider.stop_session()

    def test_initial_state_and_capabilities(self):
        """Initial state should be IDLE with rich camera and audio capabilities."""
        status = self.provider.get_status()
        self.assertFalse(status["session_active"])
        self.assertIsNone(status["session_id"])
        self.assertEqual(status["camera_facing"], "none")

        caps = self.provider.get_capabilities()
        self.assertIn("cameras", caps)
        self.assertIn("front", caps["cameras"])
        self.assertIn("back", caps["cameras"])
        self.assertIn("480p", caps["supported_resolutions"])
        self.assertIn(15, caps["supported_fps"])
        self.assertTrue(caps["audio_supported"])
        self.assertTrue(caps["speaker_supported"])

    def test_session_start_and_frame_delivery(self):
        """Starting a live call should transition to ACTIVE and produce JPEG frames."""
        res = self.provider.start_session(camera="front", quality="480p", fps=15, with_mic=True, with_speaker=True)
        self.assertTrue(res["success"])
        self.assertTrue(self.provider.is_active())
        self.assertIsNotNone(res["session_id"])

        status = self.provider.get_status()
        self.assertTrue(status["session_active"])
        self.assertEqual(status["camera_facing"], "front")
        self.assertTrue(status["mic_active"])
        self.assertTrue(status["speaker_active"])

        # Allow simulated generator to generate at least one frame
        time.sleep(0.15)
        frame = self.provider.get_latest_frame()
        self.assertIsNotNone(frame)
        self.assertTrue(isinstance(frame, bytes))
        self.assertGreater(len(frame), 10)
        # Check standard JPEG SOI (0xFFD8) and EOI (0xFFD9) markers
        self.assertEqual(frame[:2], b"\xff\xd8")
        self.assertEqual(frame[-2:], b"\xff\xd9")

    def test_duplicate_session_prevention(self):
        """Starting a session while another is active should fail with duplicate prevention."""
        res1 = self.provider.start_session(camera="front")
        self.assertTrue(res1["success"])

        res2 = self.provider.start_session(camera="back")
        self.assertFalse(res2["success"])
        self.assertEqual(res2.get("error"), "SESSION_ALREADY_ACTIVE")

    def test_session_update_camera_and_mic(self):
        """Updating camera facing and mic status should update the active session."""
        self.provider.start_session(camera="front", with_mic=True)
        self.assertEqual(self.provider.get_status()["camera_facing"], "front")

        # Switch camera to back
        res_update1 = self.provider.update_session(camera="back")
        self.assertTrue(res_update1["success"])
        self.assertEqual(self.provider.get_status()["camera_facing"], "back")

        # Mute microphone
        res_update2 = self.provider.update_session(with_mic=False)
        self.assertTrue(res_update2["success"])
        self.assertFalse(self.provider.get_status()["mic_active"])

    def test_speaker_audio_transmission(self):
        """Push-to-talk base64 PCM data should be accepted and accounted."""
        self.provider.start_session()
        pcm_raw = b"\x00\x01\x00\x02\x00\x03\x00\x04" * 128  # 1024 bytes PCM
        b64_audio = base64.b64encode(pcm_raw).decode("ascii")

        res = self.provider.transmit_speaker_audio(pcm_base64=b64_audio, sample_rate=16000)
        self.assertTrue(res["success"])
        self.assertEqual(res["bytes_received"], len(pcm_raw))
        self.assertEqual(self.provider.current_session.speaker_bytes_sent, len(pcm_raw))

        # Transmitting audio when session is idle should fail gracefully
        self.provider.stop_session()
        res_idle = self.provider.transmit_speaker_audio(pcm_base64=b64_audio)
        self.assertFalse(res_idle["success"])

    def test_session_clean_stop(self):
        """Stopping an active session should release hardware state back to IDLE."""
        self.provider.start_session()
        self.assertTrue(self.provider.is_active())

        stop_res = self.provider.stop_session()
        self.assertTrue(stop_res["success"])
        self.assertFalse(self.provider.is_active())
        self.assertIsNone(self.provider.get_latest_frame())
        self.assertEqual(self.provider.get_status()["camera_facing"], "none")

        # Stopping already idle session should be safe and idempotent
        stop_idle = self.provider.stop_session()
        self.assertTrue(stop_idle["success"])

    def test_session_watchdog_auto_expiration(self):
        """Inactive session beyond timeout should be automatically expired and cleaned up."""
        self.provider.start_session()
        session = self.provider.current_session
        self.assertIsNotNone(session)

        # Force session last_activity back by 400 seconds (watchdog default max duration is 300s)
        session.started_at = time.time() - 400
        session.last_activity = time.time() - 400

        # Check watchdog
        is_expired = session.check_watchdog_expiration()
        self.assertTrue(is_expired)

        # Verify provider auto-cleanup
        self.provider._check_session_watchdog()
        self.assertFalse(self.provider.is_active())


class TestHardwareCapabilityManagerLiveCall(unittest.TestCase):
    def setUp(self):
        self.mgr = HardwareCapabilityManager.get_instance()
        if self.mgr.live_call.is_active():
            self.mgr.live_call.stop_session()

    def tearDown(self):
        if self.mgr.live_call.is_active():
            self.mgr.live_call.stop_session()

    def test_execute_operation_start_status_update_stop(self):
        """Manager execute_operation dispatches live call opcodes correctly."""
        # 1. Start
        start_res = self.mgr.execute_operation(
            "START_LIVE_CALL",
            parameters={"camera": "front", "quality": "480p", "with_mic": True, "with_speaker": True}
        )
        self.assertTrue(start_res.get("success"))

        # 2. Status
        status_res = self.mgr.execute_operation("GET_LIVE_CALL_STATUS")
        self.assertTrue(status_res.get("success"))
        self.assertTrue(status_res["status"]["session_active"])

        # 3. Update
        update_res = self.mgr.execute_operation("UPDATE_LIVE_CALL", parameters={"camera": "back"})
        self.assertTrue(update_res.get("success"))
        self.assertEqual(self.mgr.live_call.get_status()["camera_facing"], "back")

        # 4. Speak
        pcm_raw = b"\x00" * 256
        b64_audio = base64.b64encode(pcm_raw).decode("ascii")
        speak_res = self.mgr.execute_operation("LIVE_CALL_SPEAK", parameters={"pcm_base64": b64_audio})
        self.assertTrue(speak_res.get("success"))

        # 5. Stop
        stop_res = self.mgr.execute_operation("STOP_LIVE_CALL")
        self.assertTrue(stop_res.get("success"))
        self.assertFalse(self.mgr.live_call.is_active())


class TestControllerLiveCallRoutes(unittest.TestCase):
    def setUp(self):
        self.mgr = HardwareCapabilityManager.get_instance()
        if self.mgr.live_call.is_active():
            self.mgr.live_call.stop_session()

    def tearDown(self):
        if self.mgr.live_call.is_active():
            self.mgr.live_call.stop_session()

    @patch("controller.http.node_routes.load_nodes_db")
    def test_unauthenticated_request_rejected(self, mock_load_nodes):
        from controller.http.node_routes import handle_node_post, handle_node_get

        handler = MagicMock()
        handler.parse_auth_token.return_value = None
        handler.is_authenticated_admin_or_node.return_value = False

        # POST /nodes/node-1/live-call/start
        handled = handle_node_post(handler, "/api/nodes/node-1/live-call/start", {}, None)
        self.assertTrue(handled)
        handler.send_json.assert_called_with(401, {"error": "Unauthorized: Authentication required for live call"})

        # GET /nodes/node-1/live-call/status
        handler.reset_mock()
        handled_get = handle_node_get(handler, "/api/nodes/node-1/live-call/status", {})
        self.assertTrue(handled_get)
        handler.send_json.assert_called_with(401, {"error": "Unauthorized: Authentication required for live call access"})

    @patch("controller.http.node_routes.load_nodes_db")
    def test_offline_node_rejected(self, mock_load_nodes):
        from controller.http.node_routes import handle_node_post

        mock_load_nodes.return_value = {
            "nodes": {
                "android-test-node": {
                    "node_id": "android-test-node",
                    "status": "OFFLINE",
                    "ip": "100.113.234.61"
                }
            }
        }

        handler = MagicMock()
        handler.parse_auth_token.return_value = "valid-token"
        handler.is_authenticated_admin_or_node.return_value = True

        handled = handle_node_post(handler, "/api/nodes/android-test-node/live-call/start", {}, "valid-token")
        self.assertTrue(handled)
        call_args = handler.send_json.call_args
        self.assertEqual(call_args[0][0], 503)
        self.assertIn("OFFLINE", call_args[0][1]["error"])

    @patch("controller.http.node_routes.load_nodes_db")
    def test_local_node_live_call_dispatch(self, mock_load_nodes):
        from controller.http.node_routes import handle_node_post, handle_node_get

        mock_load_nodes.return_value = {
            "nodes": {
                "local": {
                    "node_id": "local",
                    "status": "ONLINE",
                    "ip": "127.0.0.1"
                }
            }
        }

        handler = MagicMock()
        handler.parse_auth_token.return_value = "valid-token"
        handler.is_authenticated_admin_or_node.return_value = True

        # Start live call
        handled_start = handle_node_post(
            handler,
            "/api/nodes/local/live-call/start",
            {"camera": "front", "quality": "480p", "with_mic": True},
            "valid-token"
        )
        self.assertTrue(handled_start)
        handler.send_json.assert_called()
        self.assertEqual(handler.send_json.call_args[0][0], 200)
        self.assertTrue(handler.send_json.call_args[0][1]["success"])

        # Status check
        handler.reset_mock()
        handled_status = handle_node_get(handler, "/api/nodes/local/live-call/status", {})
        self.assertTrue(handled_status)
        self.assertEqual(handler.send_json.call_args[0][0], 200)
        self.assertTrue(handler.send_json.call_args[0][1]["status"]["session_active"])

        # Frame fetch
        handler.reset_mock()
        time.sleep(0.12)  # allow simulated generator frame
        handled_frame = handle_node_get(handler, "/api/nodes/local/live-call/frame", {})
        self.assertTrue(handled_frame)
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "image/jpeg")

        # Stop call
        handler.reset_mock()
        handled_stop = handle_node_post(handler, "/api/nodes/local/live-call/stop", {}, "valid-token")
        self.assertTrue(handled_stop)
        self.assertEqual(handler.send_json.call_args[0][0], 200)
        self.assertFalse(self.mgr.live_call.is_active())


if __name__ == "__main__":
    unittest.main()
