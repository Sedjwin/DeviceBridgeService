"""Generic HTTP REST device adapter — for Pi bridges and other HTTP-capable devices."""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.adapters.base import DeviceAdapter


class HTTPDeviceAdapter(DeviceAdapter):
    """
    Adapter for devices that expose a simple HTTP REST API.

    The device is expected to implement:
      GET  /health            → {"status": "ok", ...}
      GET  /capabilities      → {"capabilities": [...]}  (optional)
      POST /execute/{cap}     → executes capability with JSON body
      POST /audio/speak       → {wav_b64, sample_rate} → plays audio
      POST /audio/listen      → {duration_s} → {transcript}
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        self.http_port = connection.get("http_port", 80)
        self._base_url = f"http://{host}:{self.http_port}"

    async def ping(self) -> tuple[bool, float | None, dict[str, Any]]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base_url}/health")
            latency_ms = (time.monotonic() - t0) * 1000
            if r.status_code == 200:
                return True, round(latency_ms, 1), r.json()
            return False, None, {}
        except Exception:
            return False, None, {}

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base_url}/capabilities")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    async def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{self._base_url}/execute/{capability}", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"Device returned HTTP {r.status_code}: {r.text[:200]}")
        content_type = r.headers.get("content-type", "")
        if "application/json" in content_type:
            return r.json()
        return {"text": r.text, "http_status": r.status_code}
