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

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        """
        Optionally fetch a live capability manifest from the device itself.
        Return None if the device doesn't support self-description.
        """
        return None

    async def stream_audio_to_device(self, wav_bytes: bytes, sample_rate: int) -> None:
        """Send audio to the device (devices with speaker support)."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support audio output")

    async def stream_audio_from_device(self) -> AsyncIterator[bytes]:
        """Receive audio from the device (devices with mic support)."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support audio input")
        yield  # make it a generator
