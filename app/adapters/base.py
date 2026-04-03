"""Abstract base for all device protocol adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


class DeviceAdapter(ABC):
    """
    All protocol adapters implement this interface.
    Adapters are instantiated per-device using their connection config.
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        self.host = host
        self.connection = connection

    # ── Core (must implement) ──────────────────────────────────────────────────

    @abstractmethod
    async def ping(self) -> tuple[bool, float | None, dict[str, Any]]:
        """
        Test connectivity.
        Returns (online, latency_ms, info_dict).
        """

    @abstractmethod
    async def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a named capability with the given payload.
        Returns a result dict. Raises on failure.
        """

    # ── Optional discovery ─────────────────────────────────────────────────────

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        """
        Optionally fetch a live capability manifest from the device itself.
        Return None if the device doesn't support self-description.
        """
        return None

    # ── Audio (optional) ───────────────────────────────────────────────────────

    async def stream_audio_to_device(self, wav_bytes: bytes, sample_rate: int) -> None:
        """Send TTS audio to the device speaker."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support audio output")

    async def stream_audio_from_device(self) -> AsyncIterator[bytes]:
        """Receive audio from the device mic."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support audio input")
        yield  # make it an async generator

    # ── Embodiment lifecycle (optional) ───────────────────────────────────────

    async def setup_embodiment_session(
        self,
        session_id: str,
        manifest: dict[str, Any],
        character_vars: dict[str, Any] | None,
    ) -> None:
        """
        Called when an embodiment session starts on this device.
        Establish transport, send initial character vars if supported.
        Default: no-op.
        """

    async def teardown_embodiment_session(self, session_id: str) -> None:
        """
        Called when a session is released or transferred away from this device.
        Default: no-op.
        """

    async def push_device_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """
        Push runtime settings to the device (e.g. silence_timeout_ms, wake_word).
        Returns {"ok": True} on success. Raises on failure.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support settings push"
        )

    async def push_expression(
        self,
        expression: str,
        character_vars: dict[str, Any] | None = None,
    ) -> None:
        """
        Send an expression/emotion state to the device display.
        For variable_render devices, character_vars may carry per-emotion render params.
        Default: no-op (not all devices support avatar expressions).
        """
