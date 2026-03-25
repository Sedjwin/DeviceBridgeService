from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.config import settings


@dataclass
class DeviceConnection:
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    pending_acks: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)


class DeviceHub:
    def __init__(self) -> None:
        self._connections: dict[str, DeviceConnection] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[device_id] = DeviceConnection(websocket=websocket)

    async def disconnect(self, device_id: str) -> None:
        async with self._lock:
            conn = self._connections.pop(device_id, None)
        if not conn:
            return
        for future in conn.pending_acks.values():
            if not future.done():
                future.set_exception(TimeoutError("Device disconnected before ACK"))
        await conn.websocket.close(code=1001)

    def is_online(self, device_id: str) -> bool:
        return device_id in self._connections

    async def receive_json(self, device_id: str) -> dict[str, Any]:
        conn = self._connections[device_id]
        data = await conn.websocket.receive_text()
        return json.loads(data)

    async def dispatch_command(
        self,
        device_id: str,
        command_type: str,
        payload: dict[str, Any],
        require_ack: bool = True,
    ) -> str:
        conn = self._connections.get(device_id)
        if not conn:
            raise RuntimeError(f"Device {device_id} is not connected")

        command_id = str(uuid.uuid4())
        msg = {"command_id": command_id, "type": command_type, "payload": payload}

        ack_future: asyncio.Future[dict[str, Any]] | None = None
        if require_ack:
            ack_future = asyncio.get_running_loop().create_future()
            conn.pending_acks[command_id] = ack_future

        await conn.websocket.send_json(msg)

        if require_ack and ack_future is not None:
            try:
                await asyncio.wait_for(ack_future, timeout=settings.command_ack_timeout_s)
            finally:
                conn.pending_acks.pop(command_id, None)

        return command_id

    async def resolve_ack(self, device_id: str, command_id: str, ok: bool, error: str | None = None) -> None:
        conn = self._connections.get(device_id)
        if not conn:
            return
        fut = conn.pending_acks.get(command_id)
        if fut and not fut.done():
            if ok:
                fut.set_result({"ok": True})
            else:
                fut.set_exception(RuntimeError(error or "Device NACK"))

    async def force_disconnect(self, device_id: str) -> bool:
        if device_id not in self._connections:
            return False
        await self.disconnect(device_id)
        return True


hub = DeviceHub()
