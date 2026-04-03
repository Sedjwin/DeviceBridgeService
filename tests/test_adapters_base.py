"""Tests for the adapter base class new embodiment methods."""
from __future__ import annotations

import pytest

from app.adapters.base import DeviceAdapter


class _ConcreteAdapter(DeviceAdapter):
    """Minimal concrete adapter for testing default/base implementations."""

    async def ping(self):
        return (True, 1.0, {})

    async def execute(self, capability, payload):
        return {"ok": True, "capability": capability}


class TestAdapterBaseDefaults:
    """Verify default no-op and NotImplementedError behaviours on the base class."""

    @pytest.fixture
    def adapter(self) -> _ConcreteAdapter:
        return _ConcreteAdapter(host="127.0.0.1", connection={})

    @pytest.mark.asyncio
    async def test_ping_implemented(self, adapter):
        online, latency, info = await adapter.ping()
        assert online is True

    @pytest.mark.asyncio
    async def test_execute_implemented(self, adapter):
        result = await adapter.execute("display_text", {"text": "hi"})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_fetch_live_manifest_returns_none(self, adapter):
        result = await adapter.fetch_live_manifest()
        assert result is None

    @pytest.mark.asyncio
    async def test_stream_audio_to_device_raises(self, adapter):
        with pytest.raises(NotImplementedError):
            await adapter.stream_audio_to_device(b"wav", 22050)

    @pytest.mark.asyncio
    async def test_stream_audio_from_device_raises(self, adapter):
        with pytest.raises(NotImplementedError):
            async for _ in adapter.stream_audio_from_device():
                pass

    @pytest.mark.asyncio
    async def test_setup_embodiment_session_is_noop(self, adapter):
        """setup_embodiment_session should not raise — default is a no-op."""
        await adapter.setup_embodiment_session("session-1", {}, None)

    @pytest.mark.asyncio
    async def test_teardown_embodiment_session_is_noop(self, adapter):
        """teardown_embodiment_session should not raise."""
        await adapter.teardown_embodiment_session("session-1")

    @pytest.mark.asyncio
    async def test_push_device_settings_raises(self, adapter):
        """push_device_settings raises NotImplementedError by default."""
        with pytest.raises(NotImplementedError, match="does not support settings push"):
            await adapter.push_device_settings({"silence_timeout_ms": 3000})

    @pytest.mark.asyncio
    async def test_push_expression_is_noop(self, adapter):
        """push_expression is a no-op by default (not all devices support avatar)."""
        await adapter.push_expression("happy")
        await adapter.push_expression("neutral", character_vars={"appearance": {}})


class TestAdapterOverrides:
    """Verify that subclasses can properly override the new methods."""

    @pytest.mark.asyncio
    async def test_subclass_can_override_setup(self):
        calls = []

        class MyAdapter(_ConcreteAdapter):
            async def setup_embodiment_session(self, session_id, manifest, character_vars):
                calls.append(("setup", session_id, character_vars))

        adapter = MyAdapter(host="127.0.0.1", connection={})
        await adapter.setup_embodiment_session("sess-99", {"avatar": {}}, {"eye_count": 2})
        assert calls == [("setup", "sess-99", {"eye_count": 2})]

    @pytest.mark.asyncio
    async def test_subclass_can_override_teardown(self):
        calls = []

        class MyAdapter(_ConcreteAdapter):
            async def teardown_embodiment_session(self, session_id):
                calls.append(session_id)

        adapter = MyAdapter(host="127.0.0.1", connection={})
        await adapter.teardown_embodiment_session("sess-99")
        assert calls == ["sess-99"]

    @pytest.mark.asyncio
    async def test_subclass_can_override_push_settings(self):
        class MyAdapter(_ConcreteAdapter):
            async def push_device_settings(self, settings):
                return {"applied": list(settings.keys())}

        adapter = MyAdapter(host="127.0.0.1", connection={})
        result = await adapter.push_device_settings({"wake_word": "chef", "silence_timeout_ms": 2000})
        assert "wake_word" in result["applied"]

    @pytest.mark.asyncio
    async def test_subclass_can_override_push_expression(self):
        expressions = []

        class MyAdapter(_ConcreteAdapter):
            async def push_expression(self, expression, character_vars=None):
                expressions.append(expression)

        adapter = MyAdapter(host="127.0.0.1", connection={})
        await adapter.push_expression("happy")
        await adapter.push_expression("thinking")
        assert expressions == ["happy", "thinking"]
