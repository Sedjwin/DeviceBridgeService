from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class SessionRuntime:
    def __init__(self) -> None:
        self._debug_queues: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)
        self._mic_queues: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)

    async def publish_debug(self, session_id: str, event: dict[str, Any]) -> None:
        await self._debug_queues[session_id].put(event)

    async def publish_mic(self, session_id: str, payload: dict[str, Any]) -> None:
        await self._mic_queues[session_id].put(payload)

    async def get_next_debug(self, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._debug_queues[session_id].get(), timeout=timeout)

    async def get_next_mic(self, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._mic_queues[session_id].get(), timeout=timeout)


runtime = SessionRuntime()
