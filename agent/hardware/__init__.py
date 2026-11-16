"""
PersonalServer Hardware Package
Exposes CameraProvider, MicrophoneProvider, SpeakerProvider, and HardwareCapabilityManager.
"""

from agent.hardware.base import SessionState, HardwareSession
from agent.hardware.camera import CameraProvider
from agent.hardware.microphone import MicrophoneProvider
from agent.hardware.speaker import SpeakerProvider
from agent.hardware.manager import HardwareCapabilityManager

__all__ = [
    "SessionState",
    "HardwareSession",
    "CameraProvider",
    "MicrophoneProvider",
    "SpeakerProvider",
    "HardwareCapabilityManager"
]

