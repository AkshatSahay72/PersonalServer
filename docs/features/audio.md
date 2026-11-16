# Feature: Audio Subsystem (Microphone & Speaker)

**Subsystem:** AudioRecord & AudioTrack Hardware Bridge  
**Implementation:** `MicrophoneBridgeManager.java`, `agent/hardware/microphone.py`, `agent/hardware/speaker.py`  

---

## 1. Architecture & Decoupling

In PersonalServer, audio is strictly **decoupled from video**:
- **Independent Lifecycle:** A user can stream video without enabling the microphone.
- **Graceful Permission Degradation:** If `android.permission.RECORD_AUDIO` is missing or denied by the user, the Native Bridge logs an informational warning and starts the CCTV video stream without failing.

---

## 2. Technical Specifications

- **Microphone Capture Format:** 16,000 Hz (16 kHz), 16-bit PCM, Mono channel.
- **Buffer Size:** 1,280 bytes (40 ms chunks) to guarantee low latency.
- **Speaker Playback Format:** 16,000 Hz, 16-bit PCM Mono output via `AudioTrack`.
- **Operating Modes:**
  - **Listen-In (Planned M1):** One-way microphone audio streamed alongside the live video feed.
  - **Full-Duplex Intercom (Future):** Bidirectional live audio calling with acoustic echo cancellation.
