from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceConversationState:
    device_id: str
    agent_id: str = ""
    bridge_session_id: str = ""
    upstream_session_id: str = ""
    listening: bool = False
    sample_rate: int = 16000
    mic_chunks: list[bytes] = field(default_factory=list)
    mic_activity_token: int = 0


class SessionRuntime:
    def __init__(self) -> None:
        self._debug_queues: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)
        self._mic_queues: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)
        self._device_states: dict[str, DeviceConversationState] = {}
        self._state_lock = asyncio.Lock()

    async def publish_debug(self, session_id: str, event: dict[str, Any]) -> None:
        await self._debug_queues[session_id].put(event)

    async def publish_mic(self, session_id: str, payload: dict[str, Any]) -> None:
        await self._mic_queues[session_id].put(payload)

    async def get_next_debug(self, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._debug_queues[session_id].get(), timeout=timeout)

    async def get_next_mic(self, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._mic_queues[session_id].get(), timeout=timeout)

    async def get_device_state(self, device_id: str) -> DeviceConversationState:
        async with self._state_lock:
            state = self._device_states.get(device_id)
            if state is None:
                state = DeviceConversationState(device_id=device_id)
                self._device_states[device_id] = state
            return state

    async def clear_device_state(self, device_id: str) -> None:
        async with self._state_lock:
            self._device_states.pop(device_id, None)


runtime = SessionRuntime()
