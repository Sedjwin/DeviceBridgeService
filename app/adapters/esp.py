"""
ESP adapter — ESP32-S3 devices (POD and variants).

Two protocols:
  esp_http  — HTTP REST transport (synchronous, for devices without persistent WS)
  esp_ws    — WebSocket streaming transport (POD primary protocol)

Device firmware spec: Device_Firmware/POD/firmware/
Hardware: Waveshare 1.32" AMOLED ESP32-S3

Expected device HTTP endpoints (served by firmware on its local HTTP server):
    GET  /health             → {"status":"ok","uptime_s":N,"free_heap":N,"version":"1.0.0"}
    GET  /manifest           → full capability manifest JSON
    POST /execute/{cap}      → run a direct capability; body: {payload}
    POST /audio/play         → play TTS audio; body: WAV bytes; returns {"ok":true}
    POST /settings           → push runtime settings; body: {"silence_timeout_ms":N,...}
    POST /expression         → set avatar expression; body: {"expression":"happy"}

WebSocket streaming:
    The device CONNECTS TO DBS (not the other way around).
    See /api/embodiment/sessions/{id}/stream endpoint in embodiment.py.
    DBS is the WS server; firmware is the WS client.
    esp_ws adapter handles HTTP calls (play/expression/settings) as push commands
    to the device's local HTTP server.

Audio push (for non-WS TTS, e.g. embody.speak from an agent):
    POST /audio/play with WAV bytes → device plays directly.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.adapters.base import DeviceAdapter

logger = logging.getLogger(__name__)


class ESPHTTPAdapter(DeviceAdapter):
    """
    ESP32 device using HTTP REST for all commands.
    Audio input is via HTTP audio_upload endpoint on DBS.
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        port = connection.get("http_port", 80)
        self._base = f"http://{host}:{port}"
        self._timeout = 10.0

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    async def ping(self) -> tuple[bool, float | None, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(self._url("/health"))
            if r.status_code == 200:
                data = r.json()
                return True, None, data
            return False, None, {}
        except Exception as exc:
            logger.debug("ESP ping failed: %s", exc)
            return False, None, {}

    async def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(self._url(f"/execute/{capability}"), json=payload)
        r.raise_for_status()
        return r.json()

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(self._url("/manifest"))
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            logger.warning("ESP fetch_live_manifest failed: %s", exc)
        return None

    async def setup_embodiment_session(
        self,
        session_id: str,
        manifest: dict[str, Any],
        character_vars: dict[str, Any] | None,
    ) -> None:
        """Push initial character vars / expression to device on session start."""
        try:
            expr = "neutral"
            if character_vars:
                expr = character_vars.get("default_expression", "neutral")
            await self.push_expression(expr, character_vars)
        except Exception as exc:
            logger.warning("ESP setup_embodiment_session: %s", exc)

    async def teardown_embodiment_session(self, session_id: str) -> None:
        """Return device to idle expression on session end."""
        try:
            await self.push_expression("neutral")
        except Exception as exc:
            logger.debug("ESP teardown_embodiment_session: %s", exc)

    async def push_device_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """POST /settings with settings dict."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(self._url("/settings"), json=settings)
        r.raise_for_status()
        return r.json()

    async def push_expression(
        self,
        expression: str,
        character_vars: dict[str, Any] | None = None,
    ) -> None:
        """POST /expression with {expression, character_vars}."""
        body: dict[str, Any] = {"expression": expression}
        if character_vars:
            body["character_vars"] = character_vars
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                await c.post(self._url("/expression"), json=body)
        except Exception as exc:
            logger.debug("ESP push_expression failed: %s", exc)

    async def stream_audio_to_device(self, wav_bytes: bytes, sample_rate: int) -> None:
        """POST WAV bytes to /audio/play."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    self._url("/audio/play"),
                    content=wav_bytes,
                    headers={"Content-Type": "audio/wav"},
                )
            if r.status_code != 200:
                logger.warning("ESP audio play returned HTTP %d", r.status_code)
        except Exception as exc:
            logger.warning("ESP stream_audio_to_device failed: %s", exc)


class ESPWSAdapter(ESPHTTPAdapter):
    """
    ESP32 device using WebSocket streaming (primary transport for POD).

    In the WS model the device connects TO DBS as a WS client —
    DBS is the WS server.  This adapter therefore has no persistent
    outbound WS connection.  Audio streaming is handled by the DBS
    WS stream loop in services/stream_loop.py.

    This adapter handles the out-of-band HTTP push calls:
      - push_expression   → POST /expression on device local HTTP server
      - stream_audio_to_device → POST /audio/play (for agent-initiated TTS
                                 outside the streaming session)
      - push_device_settings   → POST /settings

    For in-session TTS (during a WS stream), DBS sends audio_chunk
    messages over the open WebSocket directly — no HTTP needed.
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        # WS port is informational — firmware connects to DBS, not vice versa
        self._ws_port = connection.get("ws_port", connection.get("http_port", 80))
        logger.debug(
            "ESPWSAdapter init: device=%s http_port=%s ws_port=%s",
            host,
            connection.get("http_port", 80),
            self._ws_port,
        )

    # All methods inherited from ESPHTTPAdapter.
    # stream_audio_to_device pushes via HTTP /audio/play for agent-initiated
    # embody.speak calls that occur outside an active WS session.
