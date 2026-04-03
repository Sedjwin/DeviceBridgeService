"""Tests for DeviceManifest schema validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    AudioInputManifest,
    AudioOutputManifest,
    AvatarManifest,
    CameraManifest,
    DeviceManifest,
    DisplayManifest,
)


class TestAudioInputManifest:
    def test_valid_websocket_transport(self):
        m = AudioInputManifest(transport="websocket_stream", sample_rate=16000)
        assert m.transport == "websocket_stream"
        assert m.sample_rate == 16000

    def test_valid_null_transport(self):
        m = AudioInputManifest(transport=None)
        assert m.transport is None

    def test_all_valid_transports(self):
        for t in ("websocket_stream", "http_upload", "wake_word", "push_to_talk", "button", None):
            m = AudioInputManifest(transport=t)
            assert m.transport == t

    def test_invalid_transport_raises(self):
        with pytest.raises(ValidationError, match="transport"):
            AudioInputManifest(transport="bluetooth")

    def test_defaults(self):
        m = AudioInputManifest()
        assert m.silence_timeout_ms == 2000
        assert m.format == "pcm_s16le"
        assert m.configurable == []

    def test_configurable_field(self):
        m = AudioInputManifest(configurable=["silence_timeout_ms", "wake_word"])
        assert "silence_timeout_ms" in m.configurable


class TestAudioOutputManifest:
    def test_valid_transport(self):
        m = AudioOutputManifest(transport="http_push", sample_rate=22050)
        assert m.transport == "http_push"

    def test_invalid_transport(self):
        with pytest.raises(ValidationError, match="transport"):
            AudioOutputManifest(transport="magic")

    def test_null_transport(self):
        m = AudioOutputManifest(transport=None)
        assert m.transport is None

    def test_default_sample_rate(self):
        m = AudioOutputManifest()
        assert m.sample_rate == 22050


class TestAvatarManifest:
    def test_valid_types(self):
        for t in ("variable_render", "simple_sprite", "agent_controlled", "none"):
            m = AvatarManifest(type=t)
            assert m.type == t

    def test_invalid_type(self):
        with pytest.raises(ValidationError, match="avatar.type"):
            AvatarManifest(type="hologram")

    def test_expression_states(self):
        m = AvatarManifest(type="simple_sprite", expression_states=["happy", "sad", "thinking"])
        assert "thinking" in m.expression_states

    def test_default_none(self):
        m = AvatarManifest()
        assert m.type == "none"
        assert m.expression_states == []


class TestDisplayManifest:
    def test_valid(self):
        m = DisplayManifest(width=320, height=240, type="tft")
        assert m.width == 320

    def test_invalid_type(self):
        with pytest.raises(ValidationError, match="display.type"):
            DisplayManifest(type="plasma")

    def test_all_valid_types(self):
        for t in ("tft", "oled", "epaper", "rgb_matrix", "none"):
            m = DisplayManifest(type=t)
            assert m.type == t


class TestDeviceManifest:
    def test_all_fields(self):
        m = DeviceManifest(
            audio_input={"transport": "websocket_stream", "sample_rate": 16000},
            audio_output={"transport": "http_push"},
            avatar={"type": "variable_render", "expression_states": ["happy", "neutral"]},
            display={"width": 128, "height": 64, "type": "oled"},
            camera={"supported": False},
            settings_writable=["silence_timeout_ms", "wake_word"],
        )
        assert m.audio_input.transport == "websocket_stream"
        assert m.avatar.type == "variable_render"
        assert m.display.type == "oled"
        assert "wake_word" in m.settings_writable

    def test_empty_manifest_is_valid(self):
        """All fields optional — missing means unsupported."""
        m = DeviceManifest()
        assert m.audio_input is None
        assert m.avatar is None
        assert m.settings_writable == []

    def test_partial_manifest(self):
        m = DeviceManifest(audio_output={"transport": "url_pull"})
        assert m.audio_input is None
        assert m.audio_output.transport == "url_pull"

    def test_nested_validation_error_propagates(self):
        with pytest.raises(ValidationError):
            DeviceManifest(audio_input={"transport": "invalid_transport"})

    def test_serialise_roundtrip(self):
        m = DeviceManifest(
            audio_input={"transport": "wake_word", "wake_word": "chef"},
            settings_writable=["silence_timeout_ms"],
        )
        json_str = m.model_dump_json(exclude_none=True)
        m2 = DeviceManifest.model_validate_json(json_str)
        assert m2.audio_input.wake_word == "chef"
        assert m2.settings_writable == ["silence_timeout_ms"]
