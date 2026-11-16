"""
SpeakerProvider implementation using Termux:API.
Supports local Text-to-Speech (TTS) via Android's built-in engine and audio file playback via termux-media-player.
"""

import os
import sys
import time
import json
import uuid
import shutil
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from agent.hardware.base import SessionState, HardwareSession, find_termux_tool

logger = logging.getLogger("agent.hardware.speaker")


class SpeakerProvider:
    """
    On-demand speaker provider supporting:
    1. Local offline Text-to-Speech (TTS) via termux-tts-speak
    2. Local audio playback via termux-media-player
    """
    def __init__(self):
        self.current_session: Optional[HardwareSession] = None
        self._lock = threading.RLock()

    def is_available(self) -> Tuple[bool, Optional[str]]:
        tts_bin = find_termux_tool("termux-tts-speak")
        player_bin = find_termux_tool("termux-media-player")
        if not tts_bin and not player_bin:
            return False, "Neither termux-tts-speak nor termux-media-player found (termux-api required)"
        return True, None

    def speak_text(
        self,
        text: str,
        engine: Optional[str] = None,
        language: Optional[str] = None,
        pitch: float = 1.0,
        rate: float = 1.0,
        stream: str = "NOTIFICATION",
        timeout_sec: float = 60.0
    ) -> Tuple[bool, Dict[str, Any]]:
        """Speaks text using local Android TTS engine."""
        with self._lock:
            if not text or not str(text).strip():
                return False, {"error": "Text to speak cannot be empty"}

            tts_bin = find_termux_tool("termux-tts-speak")
            if not tts_bin:
                return False, {"error": "termux-tts-speak command not available on this node"}

            # Stop existing playback/tts session if active
            if self.current_session and self.current_session.state == SessionState.ACTIVE:
                self.stop_session("Starting new speech request")

            session_id = f"spk-tts-{uuid.uuid4().hex[:8]}"
            session = HardwareSession(
                session_id=session_id,
                capability="speaker_tts",
                parameters={"text": text, "stream": stream},
                timeout_sec=timeout_sec
            )
            session.state = SessionState.REQUESTED
            self.current_session = session

            cmd = [tts_bin]
            if engine:
                cmd.extend(["-e", str(engine)])
            if language:
                cmd.extend(["-l", str(language)])
            cmd.extend(["-p", str(float(pitch))])
            cmd.extend(["-r", str(float(rate))])
            cmd.extend(["-s", str(stream)])

            try:
                # Launch asynchronously or run with bounded timeout
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                session.process = proc
                session.state = SessionState.ACTIVE
                session.started_at = time.time()
                session.extra_data = {
                    "mode": "tts",
                    "text_length": len(text),
                    "stream": stream
                }

                # Write text to stdin
                proc.stdin.write(text)
                proc.stdin.close()

                return True, {"status": "SPEAKING", "session": session.to_dict()}
            except Exception as e:
                session.state = SessionState.IDLE
                session.error = str(e)
                return False, {"error": str(e), "session_id": session_id}

    def play_audio(
        self,
        audio_file_path: str,
        timeout_sec: float = 300.0
    ) -> Tuple[bool, Dict[str, Any]]:
        """Plays a local audio file via termux-media-player."""
        with self._lock:
            player_bin = find_termux_tool("termux-media-player")
            if not player_bin:
                return False, {"error": "termux-media-player command not available on this node"}

            real_path = os.path.realpath(audio_file_path)
            if not os.path.exists(real_path) or not os.path.isfile(real_path):
                return False, {"error": f"Audio file not found: {audio_file_path}"}

            # Stop prior playback
            if self.current_session and self.current_session.state == SessionState.ACTIVE:
                self.stop_session("Starting new audio playback")

            session_id = f"spk-play-{uuid.uuid4().hex[:8]}"
            session = HardwareSession(
                session_id=session_id,
                capability="speaker_playback",
                parameters={"file": real_path},
                timeout_sec=timeout_sec
            )
            session.state = SessionState.REQUESTED
            self.current_session = session

            try:
                cmd = [player_bin, "play", real_path]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                if res.returncode != 0:
                    err = res.stderr.strip() or f"Player returned code {res.returncode}"
                    session.state = SessionState.IDLE
                    session.error = err
                    return False, {"error": err, "session_id": session_id}

                session.state = SessionState.ACTIVE
                session.started_at = time.time()
                session.resource_paths.append(real_path)
                session.extra_data = {
                    "mode": "audio_playback",
                    "file": real_path,
                    "file_size": os.path.getsize(real_path)
                }
                return True, {"status": "PLAYING", "session": session.to_dict()}
            except Exception as e:
                session.state = SessionState.IDLE
                session.error = str(e)
                return False, {"error": str(e), "session_id": session_id}

    def stop_session(self, reason: str = "Requested by controller") -> Dict[str, Any]:
        """Stops active TTS or media playback."""
        with self._lock:
            player_bin = find_termux_tool("termux-media-player") or "termux-media-player"
            # Send stop command to media player unconditionally
            try:
                subprocess.run([player_bin, "stop"], capture_output=True, timeout=5)
            except Exception:
                pass

            if not self.current_session:
                return {"status": "IDLE", "message": "No active speaker session"}

            session = self.current_session
            session.state = SessionState.STOPPING

            if session.process and session.process.poll() is None:
                try:
                    session.process.terminate()
                    session.process.wait(timeout=2)
                except Exception:
                    session.process.kill()

            session.state = SessionState.IDLE
            session.stopped_at = time.time()
            data = session.to_dict()
            data["stop_reason"] = reason
            self.current_session = None
            return {"status": "STOPPED", "session": data}

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            available, reason = self.is_available()
            if self.current_session and self.current_session.is_expired():
                self.stop_session("Session timeout reached")

            player_info = None
            player_bin = find_termux_tool("termux-media-player")
            if player_bin:
                try:
                    res = subprocess.run([player_bin, "info"], capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        player_info = res.stdout.strip()
                except Exception:
                    pass

            return {
                "available": available,
                "reason": reason,
                "session": self.current_session.to_dict() if self.current_session else None,
                "player_info": player_info
            }
