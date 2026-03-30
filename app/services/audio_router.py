"""Audio bridge — routes speech between VoiceService and a device."""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def speak_on_device(
    adapter,  # DeviceAdapter
    text: str,
    voice: str = "glados",
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Synthesise speech via VoiceService, then route audio to the device adapter.
    For devices without audio output this raises NotImplementedError.
    """
    # Step 1: TTS via VoiceService
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{settings.voiceservice_url}/tts",
            json={"text": text, "voice": voice},
        )
    if r.status_code != 200:
        raise RuntimeError(f"VoiceService TTS failed ({r.status_code}): {r.text[:200]}")

    data        = r.json()
    audio_b64   = data.get("audio", "")
    sample_rate = data.get("sample_rate", 22050)
    timeline    = data.get("timeline", [])

    if not audio_b64:
        raise RuntimeError("VoiceService returned no audio")

    wav_bytes = base64.b64decode(audio_b64)

    # Step 2: Send to device
    await adapter.stream_audio_to_device(wav_bytes, sample_rate)

    return {
        "ok":          True,
        "duration_ms": data.get("duration_ms"),
        "sample_rate": sample_rate,
        "timeline":    timeline,
        "session_id":  session_id,
    }


async def listen_on_device(
    adapter,  # DeviceAdapter
    duration_s: float = 5.0,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Capture audio from a device, then run STT via VoiceService.
    Returns {"transcript": "...", "duration_s": ...}.
    """
    # Step 1: Capture audio from device
    chunks = []
    async for chunk in adapter.stream_audio_from_device():
        chunks.append(chunk)

    raw_audio = b"".join(chunks)
    if not raw_audio:
        return {"transcript": "", "duration_s": duration_s}

    # Step 2: STT via VoiceService
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{settings.voiceservice_url}/stt",
            content=raw_audio,
            headers={"Content-Type": "audio/wav"},
        )

    if r.status_code != 200:
        raise RuntimeError(f"VoiceService STT failed ({r.status_code}): {r.text[:200]}")

    result = r.json()
    return {
        "transcript": result.get("text", ""),
        "duration_s": duration_s,
        "session_id": session_id,
    }
