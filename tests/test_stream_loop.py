"""Tests for stream_loop.py — pure logic tests (no live services)."""
from __future__ import annotations

import base64
import json
import struct

import pytest
import respx
import httpx

from app.config import settings
from app.services.stream_loop import (
    _build_wav,
    extract_emotions,
    strip_tags,
    stt,
    tts,
    process_utterance,
)


# ── WAV builder ────────────────────────────────────────────────────────────────

class TestBuildWav:
    def test_wav_header_starts_with_riff(self):
        pcm = b"\x00" * 1000
        wav = _build_wav(pcm, sample_rate=16000)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"

    def test_wav_correct_data_length(self):
        pcm = b"\x00" * 200
        wav = _build_wav(pcm, sample_rate=16000)
        # WAV header is 44 bytes; total should be 44 + len(pcm)
        assert len(wav) == 44 + 200

    def test_wav_sample_rate_encoded(self):
        pcm = b"\x00" * 100
        wav = _build_wav(pcm, sample_rate=22050)
        # Bytes 24–27 are the sample rate in little-endian
        sample_rate_in_header = struct.unpack_from("<I", wav, 24)[0]
        assert sample_rate_in_header == 22050

    def test_wav_different_sample_rates(self):
        for sr in (8000, 16000, 22050, 44100):
            wav = _build_wav(b"\x00" * 100, sample_rate=sr)
            sr_decoded = struct.unpack_from("<I", wav, 24)[0]
            assert sr_decoded == sr


# ── Emotion extraction ────────────────────────────────────────────────────────

class TestExtractEmotions:
    def test_single_emotion(self):
        text = "Hello! {emotion:happy} How are you?"
        assert extract_emotions(text) == ["happy"]

    def test_multiple_emotions(self):
        text = "{emotion:thinking} Let me check... {emotion:happy} Done!"
        assert extract_emotions(text) == ["thinking", "happy"]

    def test_no_emotions(self):
        text = "Just a plain response with no tags."
        assert extract_emotions(text) == []

    def test_case_insensitive(self):
        text = "{EMOTION:HAPPY}"
        assert extract_emotions(text) == ["HAPPY"]

    def test_mixed_with_action_tags(self):
        text = "{emotion:curious} {action:thumbs_up} Interesting!"
        emotions = extract_emotions(text)
        assert emotions == ["curious"]  # action tags not captured


class TestStripTags:
    def test_strips_emotion_tags(self):
        text = "{emotion:happy} Hello there!"
        assert strip_tags(text) == "Hello there!"

    def test_strips_action_tags(self):
        text = "Yes! {action:thumbs_up}"
        assert strip_tags(text) == "Yes!"

    def test_strips_both_tag_types(self):
        text = "{emotion:thinking} Let me see... {action:nod} Yes."
        assert strip_tags(text) == "Let me see... Yes."

    def test_no_tags_unchanged(self):
        text = "Plain text with no tags."
        assert strip_tags(text) == "Plain text with no tags."

    def test_only_tags_returns_empty(self):
        text = "{emotion:happy}{action:wave}"
        assert strip_tags(text) == ""


# ── STT ───────────────────────────────────────────────────────────────────────

class TestSTT:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stt_returns_transcript(self):
        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(200, json={"text": "hello world"})
        )
        result = await stt(b"fake-wav-bytes")
        assert result == "hello world"

    @pytest.mark.asyncio
    @respx.mock
    async def test_stt_raises_on_error(self):
        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(RuntimeError, match="STT failed"):
            await stt(b"fake-wav-bytes")

    @pytest.mark.asyncio
    @respx.mock
    async def test_stt_returns_empty_string_when_no_text(self):
        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(200, json={"text": ""})
        )
        result = await stt(b"silence-wav")
        assert result == ""


# ── TTS ───────────────────────────────────────────────────────────────────────

class TestTTS:
    @pytest.mark.asyncio
    @respx.mock
    async def test_tts_returns_audio(self):
        fake_audio = base64.b64encode(b"fake-wav-data").decode()
        respx.post(f"{settings.voiceservice_url}/tts").mock(
            return_value=httpx.Response(200, json={"audio": fake_audio, "sample_rate": 22050})
        )
        audio_bytes, sample_rate = await tts("Hello world")
        assert audio_bytes == b"fake-wav-data"
        assert sample_rate == 22050

    @pytest.mark.asyncio
    @respx.mock
    async def test_tts_strips_emotion_tags(self):
        """Emotion tags should be stripped before sending to TTS."""
        fake_audio = base64.b64encode(b"wav").decode()
        captured_body = {}

        def handler(request):
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json={"audio": fake_audio, "sample_rate": 22050})

        respx.post(f"{settings.voiceservice_url}/tts").mock(side_effect=handler)
        await tts("{emotion:happy} Yes, certainly! {action:thumbs_up}")
        assert "{emotion" not in captured_body.get("text", "")
        assert "certainly" in captured_body.get("text", "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_tts_empty_after_stripping_returns_empty(self):
        """If text is only tags, return empty bytes without calling VoiceService."""
        respx.post(f"{settings.voiceservice_url}/tts").mock(
            return_value=httpx.Response(200, json={"audio": "", "sample_rate": 22050})
        )
        audio_bytes, _ = await tts("{emotion:happy}{action:wave}")
        assert audio_bytes == b""

    @pytest.mark.asyncio
    @respx.mock
    async def test_tts_raises_on_error(self):
        respx.post(f"{settings.voiceservice_url}/tts").mock(
            return_value=httpx.Response(503, text="Service unavailable")
        )
        with pytest.raises(RuntimeError, match="TTS failed"):
            await tts("hello")


# ── Full pipeline ─────────────────────────────────────────────────────────────

class TestProcessUtterance:
    @pytest.mark.asyncio
    @respx.mock
    async def test_full_pipeline(self):
        """STT → AM → TTS pipeline returns expected dict."""
        fake_audio = base64.b64encode(b"response-wav").decode()

        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(200, json={"text": "What time is it?"})
        )
        respx.post(f"{settings.agentmanager_url}/sessions/am-123/message").mock(
            return_value=httpx.Response(200, json={"content": "{emotion:happy} It's noon!"})
        )
        respx.post(f"{settings.voiceservice_url}/tts").mock(
            return_value=httpx.Response(200, json={"audio": fake_audio, "sample_rate": 22050})
        )

        result = await process_utterance(
            am_session_id="am-123",
            wav_bytes=b"user-audio",
            voice="glados",
        )

        assert result["transcript"] == "What time is it?"
        assert "noon" in result["response_text"]
        assert result["expression"] == "happy"
        assert result["audio_b64"] != ""
        assert result["sample_rate"] == 22050

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_transcript_returns_early(self):
        """If STT returns empty string, pipeline returns without calling AM."""
        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(200, json={"text": ""})
        )

        result = await process_utterance(
            am_session_id="am-123",
            wav_bytes=b"silence",
        )

        assert result["transcript"] == ""
        assert result["audio_b64"] == ""
        assert result["expression"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_without_emotion_has_no_expression(self):
        fake_audio = base64.b64encode(b"wav").decode()

        respx.post(f"{settings.voiceservice_url}/stt").mock(
            return_value=httpx.Response(200, json={"text": "Hi!"})
        )
        respx.post(f"{settings.agentmanager_url}/sessions/am-123/message").mock(
            return_value=httpx.Response(200, json={"content": "Hello there!"})
        )
        respx.post(f"{settings.voiceservice_url}/tts").mock(
            return_value=httpx.Response(200, json={"audio": fake_audio, "sample_rate": 22050})
        )

        result = await process_utterance(am_session_id="am-123", wav_bytes=b"hi")
        assert result["expression"] is None
