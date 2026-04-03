"""
ESP adapter stub — deferred until hardware spec is confirmed.

When ready, implement esp_http (HTTP polling) and esp_ws (WebSocket streaming).
The ESP declares its manifest at registration — DBS does not auto-detect for ESP;
the firmware provides the manifest via GET /manifest.

Protocols to register in registry.py when implemented:
    "esp_http" → ESPHTTPAdapter
    "esp_ws"   → ESPWSAdapter (shares most logic with ESPHTTPAdapter)
"""
from __future__ import annotations

import logging
from typing import Any

from app.adapters.base import DeviceAdapter

logger = logging.getLogger(__name__)

_NOT_IMPLEMENTED_MSG = (
    "ESP adapter is a stub — hardware spec not yet confirmed. "
    "Implement adapters/esp.py when firmware spec is finalised."
)


class ESPHTTPAdapter(DeviceAdapter):
    """
    Stub for ESP32 devices using HTTP REST transport.
    Expected device endpoints (to implement):
        GET  /manifest          → capability manifest JSON
        GET  /health            → {status, uptime_s, free_heap}
        POST /execute/{cap}     → run a capability
        POST /audio/play        → send TTS audio (WAV bytes)
        POST /settings          → push device settings
        POST /expression        → set avatar expression
        GET  /audio/stream      → chunked mic audio (SSE or chunked transfer)
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        port = connection.get("http_port", 80)
        self._base_url = f"http://{host}:{port}"
        logger.warning("ESPHTTPAdapter is a stub — %s", _NOT_IMPLEMENTED_MSG)

    async def ping(self) -> tuple[bool, float | None, dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        """
        ESP devices provide their manifest at GET /manifest.
        Implement when firmware spec is finalised.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def setup_embodiment_session(
        self,
        session_id: str,
        manifest: dict[str, Any],
        character_vars: dict[str, Any] | None,
    ) -> None:
        """
        Send character vars to device if avatar.type == "variable_render".
        Will POST character_vars to device endpoint (TBD in firmware spec).
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def teardown_embodiment_session(self, session_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def push_device_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """POST /settings with settings dict."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def push_expression(
        self,
        expression: str,
        character_vars: dict[str, Any] | None = None,
    ) -> None:
        """POST /expression with {expression, character_vars}."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def stream_audio_to_device(self, wav_bytes: bytes, sample_rate: int) -> None:
        """POST /audio/play with WAV bytes."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)


class ESPWSAdapter(ESPHTTPAdapter):
    """
    Stub for ESP32 devices using WebSocket streaming transport.
    Shares HTTP logic with ESPHTTPAdapter for non-streaming calls.
    Audio I/O is via a persistent WebSocket connection.
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        ws_port = connection.get("ws_port", 81)
        self._ws_url = f"ws://{host}:{ws_port}/stream"
        logger.warning("ESPWSAdapter is a stub — %s", _NOT_IMPLEMENTED_MSG)
