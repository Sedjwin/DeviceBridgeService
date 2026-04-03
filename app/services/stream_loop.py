"""
WebSocket audio stream loop — bidirectional real-time audio between a device and DBS.

Protocol (device connects to DBS):
  Device → DBS:
    {"type": "audio_chunk", "data": "<base64 PCM>", "sample_rate": 16000}
    {"type": "audio_end"}       — silence detected / utterance complete
    {"type": "ping"}

  DBS → Device:
    {"type": "audio_chunk", "data": "<base64 WAV/PCM>"}   — TTS output
    {"type": "audio_end"}                                   — TTS finished
    {"type": "expression", "expression": "thinking"}        — avatar update
    {"type": "display_text", "text": "..."}                 — overlay text
    {"type": "display_image", "image_b64": "..."}           — image push
    {"type": "settings_ack", "key": "silence_timeout_ms"}  — confirm settings applied
    {"type": "ping"}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Matches {emotion:happy} or {action:thumbs_up} tags in AgentManager responses
_EMOTION_TAG_RE = re.compile(r"\{emotion:(\w+)\}", re.IGNORECASE)
_ACTION_TAG_RE = re.compile(r"\{action:(\w+)\}", re.IGNORECASE)


# ── STT ───────────────────────────────────────────────────────────────────────

async def stt(wav_bytes: bytes) -> str:
    """Send WAV bytes to VoiceService STT. Returns transcript string."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{settings.voiceservice_url}/stt",
            content=wav_bytes,
            headers={"Content-Type": "audio/wav"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"VoiceService STT failed ({r.status_code}): {r.text[:200]}")
    return r.json().get("text", "")


# ── TTS ───────────────────────────────────────────────────────────────────────

async def tts(text: str, voice: str = "glados") -> tuple[bytes, int]:
    """
    Call VoiceService TTS. Returns (wav_bytes, sample_rate).
    Strips emotion/action tags from text before sending.
    """
    clean_text = _EMOTION_TAG_RE.sub("", _ACTION_TAG_RE.sub("", text)).strip()
    if not clean_text:
        return b"", 22050

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{settings.voiceservice_url}/tts",
            json={"text": clean_text, "voice": voice},
        )
    if r.status_code != 200:
        raise RuntimeError(f"VoiceService TTS failed ({r.status_code}): {r.text[:200]}")

    data = r.json()
    audio_b64 = data.get("audio", "")
    sample_rate = data.get("sample_rate", 22050)

    if not audio_b64:
        return b"", sample_rate

    return base64.b64decode(audio_b64), sample_rate


# ── AgentManager message ──────────────────────────────────────────────────────

async def send_to_agent(am_session_id: str, transcript: str) -> str:
    """
    Send a user transcript to AgentManager and return the full response text.
    Uses /sessions/{id}/message (non-streaming) for simplicity.
    For streaming, use send_to_agent_stream().
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{settings.agentmanager_url}/sessions/{am_session_id}/message",
            json={"role": "user", "content": transcript},
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"AgentManager message failed ({r.status_code}): {r.text[:200]}"
        )
    data = r.json()
    return data.get("content") or data.get("message") or data.get("response", "")


async def send_to_agent_stream(am_session_id: str, transcript: str) -> AsyncIterator[str]:
    """
    Stream AgentManager response text chunks for the given transcript.
    Yields text fragments as they arrive via SSE/chunked response from /sessions/{id}/stream.
    Falls back to non-streaming send_to_agent if streaming is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{settings.agentmanager_url}/sessions/{am_session_id}/stream",
                json={"role": "user", "content": transcript},
            ) as response:
                if response.status_code != 200:
                    # Fall back to non-streaming
                    full = await send_to_agent(am_session_id, transcript)
                    yield full
                    return

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                chunk_data = json.loads(payload)
                                text = (
                                    chunk_data.get("content")
                                    or chunk_data.get("delta", {}).get("content", "")
                                )
                                if text:
                                    yield text
                            except json.JSONDecodeError:
                                yield payload
    except Exception as exc:
        logger.warning("DBS: streaming AM request failed: %s — falling back to non-streaming", exc)
        full = await send_to_agent(am_session_id, transcript)
        yield full


# ── Emotion extraction ────────────────────────────────────────────────────────

def extract_emotions(text: str) -> list[str]:
    """Return all emotion names found in {emotion:X} tags."""
    return _EMOTION_TAG_RE.findall(text)


def strip_tags(text: str) -> str:
    """Remove {emotion:X} and {action:X} tags from agent response text."""
    import re as _re
    cleaned = _ACTION_TAG_RE.sub("", _EMOTION_TAG_RE.sub("", text))
    # Collapse any multiple spaces left behind by removed tags
    return _re.sub(r"  +", " ", cleaned).strip()


# ── Full pipeline (used by WS loop and HTTP upload) ───────────────────────────

async def process_utterance(
    am_session_id: str,
    wav_bytes: bytes,
    voice: str = "glados",
) -> dict[str, Any]:
    """
    Run the full STT → AgentManager → TTS pipeline for one utterance.
    Returns:
        {
            "transcript": str,
            "response_text": str,
            "audio_b64": str,
            "sample_rate": int,
            "expression": str | None,
        }
    Used by the HTTP upload endpoint for devices that cannot maintain a WebSocket.
    """
    # STT
    transcript = await stt(wav_bytes)
    if not transcript:
        return {
            "transcript": "",
            "response_text": "",
            "audio_b64": "",
            "sample_rate": 22050,
            "expression": None,
        }

    # AgentManager
    response_text = await send_to_agent(am_session_id, transcript)

    # Extract first emotion for expression update
    emotions = extract_emotions(response_text)
    expression = emotions[0] if emotions else None

    # TTS (on clean text)
    audio_bytes, sample_rate = await tts(strip_tags(response_text), voice=voice)
    audio_b64 = base64.b64encode(audio_bytes).decode() if audio_bytes else ""

    return {
        "transcript": transcript,
        "response_text": strip_tags(response_text),
        "audio_b64": audio_b64,
        "sample_rate": sample_rate,
        "expression": expression,
    }


# ── WebSocket stream handler ───────────────────────────────────────────────────

async def run_ws_stream_loop(
    websocket,  # starlette.websockets.WebSocket
    am_session_id: str,
    voice: str = "glados",
    on_expression: asyncio.Coroutine | None = None,
) -> None:
    """
    Main WebSocket audio loop.
    Called from the WS endpoint in embodiment.py.

    The device connects and sends audio_chunk messages until audio_end,
    then DBS runs STT → AM → TTS and streams audio back.

    on_expression: optional async callable(expression: str) to push expression
                   updates to the device alongside audio.
    """
    audio_chunks: list[bytes] = []
    sample_rate = 16000

    await websocket.accept()
    logger.info("DBS: WS stream opened for AM session %s", am_session_id)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                await websocket.send_text(json.dumps({"type": "ping"}))
                continue
            except Exception:
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "ping"}))

            elif msg_type == "audio_chunk":
                data_b64 = msg.get("data", "")
                if data_b64:
                    audio_chunks.append(base64.b64decode(data_b64))
                sample_rate = msg.get("sample_rate", sample_rate)

            elif msg_type == "audio_end":
                if not audio_chunks:
                    continue

                wav_bytes = _build_wav(b"".join(audio_chunks), sample_rate)
                audio_chunks = []

                try:
                    # STT
                    transcript = await stt(wav_bytes)
                    if not transcript:
                        continue

                    # Stream agent response
                    collected_text = ""
                    async for chunk in send_to_agent_stream(am_session_id, transcript):
                        collected_text += chunk

                        # Send expression updates as they arrive in the text
                        for emotion in extract_emotions(chunk):
                            await websocket.send_text(
                                json.dumps({"type": "expression", "expression": emotion})
                            )
                            if on_expression:
                                try:
                                    await on_expression(emotion)
                                except Exception:
                                    pass

                    # TTS on the full collected response
                    clean_text = strip_tags(collected_text)
                    if clean_text:
                        audio_out, sr_out = await tts(clean_text, voice=voice)
                        if audio_out:
                            # Stream audio in chunks (8 KB per message)
                            for i in range(0, len(audio_out), 8192):
                                chunk_b64 = base64.b64encode(audio_out[i:i + 8192]).decode()
                                await websocket.send_text(
                                    json.dumps({"type": "audio_chunk", "data": chunk_b64})
                                )
                            await websocket.send_text(json.dumps({"type": "audio_end"}))

                except Exception as exc:
                    logger.error("DBS: WS pipeline error: %s", exc)
                    # Don't crash the loop; device will retry utterance

    except Exception as exc:
        logger.info("DBS: WS stream closed for AM session %s: %s", am_session_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("DBS: WS stream handler exited for AM session %s", am_session_id)


# ── WAV builder ────────────────────────────────────────────────────────────────

def _build_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """
    Wrap raw PCM bytes in a minimal WAV header.
    Used when the device sends raw PCM chunks that need to be packaged for VoiceService STT.
    """
    import struct

    data_len = len(pcm_bytes)
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,           # PCM chunk size
        1,            # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_len,
    )
    return header + pcm_bytes
