from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.db import get_session_ctx, init_db
from app.models import Device
from app.routers import admin, devices, health, sessions
from app.schemas import DeviceAck, DeviceHello, DeviceStatus
from app.services import store
from app.services.device_hub import hub
from app.services.runtime import runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("devicebridge")

@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="DeviceBridgeService", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(devices.router)
app.include_router(sessions.router)


@app.websocket("/ws/device/{device_id}")
async def ws_device(device_id: str, websocket: WebSocket) -> None:
    await hub.connect(device_id, websocket)
    logger.info("device connected: %s", device_id)

    try:
        hello_raw = await hub.receive_json(device_id)
        hello = DeviceHello.model_validate(hello_raw)

        async with get_session_ctx() as db:
            await store.upsert_device(
                db,
                device_id=device_id,
                name=hello.name,
                model=hello.model,
                firmware_version=hello.firmware_version,
                api_key=hello.api_key,
                capabilities=hello.capabilities,
            )
            device_row = await db.get(Device, device_id)
            if device_row is not None:
                device_row.online = True
                await db.commit()

        await websocket.send_json({"type": "hello.ack", "device_id": device_id})

        while True:
            msg = await hub.receive_json(device_id)
            mtype = msg.get("type")
            if mtype in {"ack", "nack"}:
                ack = DeviceAck.model_validate(msg)
                await hub.resolve_ack(device_id, ack.command_id, ack.ok, ack.error)
                continue

            if mtype == "device.status":
                status = DeviceStatus.model_validate(msg)
                async with get_session_ctx() as db:
                    await store.add_telemetry(db, device_id=device_id, payload=status.model_dump())
                continue

            if mtype == "mic.chunk":
                session_id = str(msg.get("session_id", ""))
                if session_id:
                    await runtime.publish_mic(session_id, msg)
                    async with get_session_ctx() as db:
                        await store.add_session_event(db, session_id=session_id, event_type="mic.chunk", payload=msg)
                continue

            await websocket.send_json({"type": "error", "detail": f"unsupported message type: {mtype}"})
    except WebSocketDisconnect:
        logger.info("device disconnected: %s", device_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("websocket error device=%s err=%s", device_id, exc)
    finally:
        await hub.disconnect(device_id)
        async with get_session_ctx() as db:
            row = await db.get(Device, device_id)
            if row is not None:
                row.online = False
                await db.commit()
