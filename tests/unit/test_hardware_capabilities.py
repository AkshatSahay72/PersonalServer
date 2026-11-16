"""
Unit tests for PersonalServer Hardware Capabilities:
Camera, Microphone, Speaker providers, HardwareCapabilityManager, and HTTP routes.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from agent.hardware.base import SessionState, HardwareSession, find_termux_tool
from agent.hardware.camera import CameraProvider
from agent.hardware.microphone import MicrophoneProvider
from agent.hardware.speaker import SpeakerProvider
from agent.hardware.manager import HardwareCapabilityManager
from controller.http.node_routes import handle_node_get, handle_node_post
from controller.stores import save_nodes_db


class TestHardwareSession(unittest.TestCase):
    def test_session_lifecycle_and_expiration(self):
        session = HardwareSession(
            session_id="test-123",
            capability="camera",
            parameters={"camera_id": "0"},
            timeout_sec=0.1
        )
        self.assertEqual(session.state, SessionState.REQUESTED)
        self.assertFalse(session.is_expired())

        session.state = SessionState.ACTIVE
        session.started_at = time.time() - 0.2
        self.assertTrue(session.is_expired())

        session_dict = session.to_dict()
        self.assertEqual(session_dict["session_id"], "test-123")
        self.assertEqual(session_dict["capability"], "camera")
        self.assertEqual(session_dict["state"], "ACTIVE")
        self.assertGreater(session_dict["duration_sec"], 0.1)


class TestCameraProvider(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.provider = CameraProvider(storage_dir=Path(self.temp_dir))

    def tearDown(self):
        self.provider.stop_session()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("agent.hardware.camera.find_termux_tool")
    def test_is_available(self, mock_find):
        mock_find.side_effect = lambda cmd: "/data/data/com.termux/files/usr/bin/" + cmd
        avail, reason = self.provider.is_available()
        self.assertTrue(avail)
        self.assertIsNone(reason)

        mock_find.side_effect = lambda cmd: None
        avail, reason = self.provider.is_available()
        self.assertFalse(avail)
        self.assertIn("not found", reason)

    @patch("subprocess.run")
    @patch("agent.hardware.camera.find_termux_tool", return_value="/bin/termux-camera-info")
    def test_get_info(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"id": "0", "facing": "back"}, {"id": "1", "facing": "front"}]),
            stderr=""
        )
        info = self.provider.get_info()
        self.assertEqual(len(info), 2)
        self.assertEqual(info[0]["id"], "0")

    @patch("subprocess.run")
    @patch("agent.hardware.camera.find_termux_tool", return_value="/bin/termux-camera-photo")
    def test_photo_capture_success(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ok, res = self.provider.start_session(parameters={"camera_id": "0", "mode": "photo"})
        self.assertTrue(ok)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("session", res)
        self.assertEqual(self.provider.current_session.state, SessionState.ACTIVE)

        # Duplicate session rejection
        dup_ok, dup_res = self.provider.start_session(parameters={"camera_id": "0"})
        self.assertFalse(dup_ok)
        self.assertIn("already active", dup_res["error"])

        # Explicit stop
        stop_res = self.provider.stop_session(reason="Operator stop")
        self.assertEqual(stop_res["status"], "STOPPED")
        self.assertIsNone(self.provider.current_session)

    @patch("subprocess.run")
    @patch("agent.hardware.camera.find_termux_tool", return_value="/bin/termux-camera-photo")
    def test_permission_denied_detection(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Permission denial: android.permission.CAMERA required"
        )
        ok, res = self.provider.start_session(parameters={"camera_id": "0", "mode": "photo"})
        self.assertFalse(ok)
        self.assertIn("ANDROID_PERMISSION_DENIED", res["error"])


class TestMicrophoneProvider(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.provider = MicrophoneProvider(storage_dir=Path(self.temp_dir))

    def tearDown(self):
        self.provider.stop_session()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("subprocess.run")
    @patch("agent.hardware.microphone.find_termux_tool", return_value="/bin/termux-microphone-record")
    def test_start_and_stop_session(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ok, res = self.provider.start_session(parameters={"duration_sec": 10, "encoder": "aac"})
        self.assertTrue(ok)
        self.assertEqual(res["status"], "ACTIVE")
        self.assertIsNotNone(self.provider.current_session)

        # Stop session
        stop_res = self.provider.stop_session(reason="Finished recording")
        self.assertEqual(stop_res["status"], "STOPPED")
        self.assertIsNone(self.provider.current_session)

    @patch("subprocess.run")
    @patch("agent.hardware.microphone.find_termux_tool", return_value="/bin/termux-microphone-record")
    def test_permission_denied(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="java.lang.SecurityException: Permission denied"
        )
        ok, res = self.provider.start_session()
        self.assertFalse(ok)
        self.assertIn("ANDROID_PERMISSION_DENIED", res["error"])


class TestSpeakerProvider(unittest.TestCase):
    def setUp(self):
        self.provider = SpeakerProvider()

    def tearDown(self):
        self.provider.stop_session()

    @patch("subprocess.Popen")
    @patch("agent.hardware.speaker.find_termux_tool", return_value="/bin/termux-tts-speak")
    def test_speak_text(self, mock_find, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        # Empty text rejection
        ok, res = self.provider.speak_text("")
        self.assertFalse(ok)
        self.assertIn("cannot be empty", res["error"])

        # Valid text
        ok, res = self.provider.speak_text("Hello PersonalServer", pitch=1.0, rate=1.0)
        self.assertTrue(ok)
        self.assertEqual(res["status"], "SPEAKING")
        self.assertIsNotNone(self.provider.current_session)

        self.provider.stop_session()
        self.assertIsNone(self.provider.current_session)

    @patch("subprocess.run")
    @patch("agent.hardware.speaker.find_termux_tool", return_value="/bin/termux-media-player")
    def test_play_audio(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Non-existent file
        ok, res = self.provider.play_audio("/non/existent/file.mp3")
        self.assertFalse(ok)
        self.assertIn("not found", res["error"])

        # Existing temporary file
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"dummy audio data")
            temp_path = f.name

        try:
            ok, res = self.provider.play_audio(temp_path)
            self.assertTrue(ok)
            self.assertEqual(res["status"], "PLAYING")
            self.assertEqual(self.provider.current_session.state, SessionState.ACTIVE)
        finally:
            self.provider.stop_session()
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestHardwareCapabilityManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mgr = HardwareCapabilityManager(storage_dir=Path(self.temp_dir))

    def tearDown(self):
        self.mgr.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_execute_operation_routing(self):
        # GET_HARDWARE_STATUS
        res = self.mgr.execute_operation("GET_HARDWARE_STATUS")
        self.assertTrue(res["success"])
        self.assertIn("camera", res["status"])
        self.assertIn("microphone", res["status"])
        self.assertIn("speaker", res["status"])

        # Unsupported operation
        bad_res = self.mgr.execute_operation("INVALID_OP")
        self.assertFalse(bad_res["success"])
        self.assertIn("Unsupported", bad_res["error"])

    @patch("agent.hardware.camera.find_termux_tool", return_value="/bin/termux-camera-photo")
    @patch("subprocess.run")
    def test_camera_operations(self, mock_run, mock_find):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        res = self.mgr.execute_operation("START_CAMERA", {"camera_id": "0", "mode": "photo"})
        self.assertTrue(res["success"])

        stop_res = self.mgr.execute_operation("STOP_CAMERA")
        self.assertTrue(stop_res["success"])
        self.assertEqual(stop_res["result"]["status"], "STOPPED")

    @patch("agent.hardware.microphone.find_termux_tool", return_value="/bin/termux-microphone-record")
    @patch("subprocess.run")
    def test_microphone_operations(self, mock_run, mock_find):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        res = self.mgr.execute_operation("START_MIC", {"duration_sec": 5})
        self.assertTrue(res["success"])

        stop_res = self.mgr.execute_operation("STOP_MIC")
        self.assertTrue(stop_res["success"])

    @patch("agent.hardware.speaker.find_termux_tool", return_value="/bin/termux-tts-speak")
    @patch("subprocess.Popen")
    def test_speaker_operations(self, mock_popen, mock_find):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        res = self.mgr.execute_operation("SPEAK_TEXT", {"text": "Test speech"})
        self.assertTrue(res["success"])

        stop_res = self.mgr.execute_operation("STOP_AUDIO")
        self.assertTrue(stop_res["success"])

    def test_watchdog_cleanup(self):
        # Create an expired session directly
        sess = HardwareSession("exp-cam", "camera", timeout_sec=0.01)
        sess.state = SessionState.ACTIVE
        sess.started_at = time.time() - 1.0
        self.mgr.camera.current_session = sess

        # Let the watchdog loop run
        time.sleep(2.5)
        self.assertIsNone(self.mgr.camera.current_session)


class TestControllerHardwareRoutes(unittest.TestCase):
    def setUp(self):
        from controller.onboarding import get_or_create_enrollment_token
        self.admin_token = get_or_create_enrollment_token()
        self.handler = MagicMock()
        self.handler.heartbeat_timeout = 90
        self.handler.is_authenticated_admin_or_node.return_value = True
        self.handler.parse_auth_token.return_value = self.admin_token

        # Mock database with local and worker nodes
        self.nodes_db = {
            "cluster_name": "test-cluster",
            "nodes": {
                "local": {
                    "node_id": "local",
                    "ip": "127.0.0.1",
                    "status": "ONLINE",
                    "verified_capabilities": {"camera": True, "microphone": True, "speaker": True}
                },
                "worker-offline": {
                    "node_id": "worker-offline",
                    "ip": "100.1.2.3",
                    "status": "OFFLINE",
                    "verified_capabilities": {"camera": True}
                }
            }
        }
        save_nodes_db(self.nodes_db)

    def test_get_hardware_local(self):
        sent = {}
        def send_json(status_code, payload):
            sent["status"] = status_code
            sent["payload"] = payload

        self.handler.send_json.side_effect = send_json
        handled = handle_node_get(self.handler, "/nodes/local/hardware", {})
        self.assertTrue(handled)
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["payload"]["node_id"], "local")
        self.assertIn("hardware", sent["payload"])

    def test_get_hardware_offline_node(self):
        sent = {}
        def send_json(status_code, payload):
            sent["status"] = status_code
            sent["payload"] = payload

        self.handler.send_json.side_effect = send_json
        handled = handle_node_get(self.handler, "/nodes/worker-offline/hardware", {})
        self.assertTrue(handled)
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["payload"]["status"], "offline")
        self.assertIn("OFFLINE", sent["payload"]["error"])

    def test_post_hardware_missing_operation(self):
        sent = {}
        def send_json(status_code, payload):
            sent["status"] = status_code
            sent["payload"] = payload

        self.handler.send_json.side_effect = send_json
        handled = handle_node_post(self.handler, "/nodes/local/hardware", {}, self.admin_token)
        self.assertTrue(handled)
        self.assertEqual(sent["status"], 400)
        self.assertIn("Missing required field 'operation'", sent["payload"]["error"])

    def test_post_hardware_offline_node(self):
        sent = {}
        def send_json(status_code, payload):
            sent["status"] = status_code
            sent["payload"] = payload

        self.handler.send_json.side_effect = send_json
        handled = handle_node_post(
            self.handler,
            "/nodes/worker-offline/hardware",
            {"operation": "START_CAMERA"},
            self.admin_token
        )
        self.assertTrue(handled)
        self.assertEqual(sent["status"], 503)
        self.assertIn("OFFLINE", sent["payload"]["error"])

    def test_post_hardware_local_execution(self):
        sent = {}
        def send_json(status_code, payload):
            sent["status"] = status_code
            sent["payload"] = payload

        self.handler.send_json.side_effect = send_json
        handled = handle_node_post(
            self.handler,
            "/nodes/local/hardware",
            {"operation": "GET_HARDWARE_STATUS"},
            self.admin_token
        )
        self.assertTrue(handled)
        self.assertEqual(sent["status"], 200)
        self.assertTrue(sent["payload"]["success"])
        self.assertIn("camera", sent["payload"]["status"])


if __name__ == "__main__":
    unittest.main()

