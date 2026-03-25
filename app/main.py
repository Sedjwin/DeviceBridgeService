from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
import uuid
import wave
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.config import settings
from app.db import get_session_ctx, init_db
from app.models import BridgeSession, Device
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

DATA_ROOT = Path("data/devices")


def _safe_decode_b64(raw: str) -> bytes:
    try:
        return base64.b64decode(raw)
    except Exception:
        return b""


def _pcm_to_wav(pcm_chunks: list[bytes], sample_rate: int) -> bytes:
    merged = b"".join(pcm_chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(merged)
    return buf.getvalue()


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


async def _ensure_agentmanager_session(device_id: str, agent_id: str) -> tuple[str, str]:
    state = await runtime.get_device_state(device_id)
    if state.bridge_session_id and state.upstream_session_id and state.agent_id == agent_id:
        return state.bridge_session_id, state.upstream_session_id

    async with get_session_ctx() as db:
        row = await store.create_bridge_session(
            db,
            agent_id=agent_id,
            device_id=device_id,
            upstream_session_id="",
        )
        await store.add_session_event(
            db,
            session_id=row.session_id,
            event_type="ptt.bridge_session_created",
            payload={"agent_id": agent_id, "device_id": device_id},
        )
        bridge_session_id = row.session_id

    agent_session_url = f"{settings.agentmanager_url.rstrip('/')}/agents/{agent_id}/session"
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(agent_session_url, json={"device_id": device_id, "capabilities": {"audio": True}})
        res.raise_for_status()
        upstream_session_id = str(res.json().get("session_id", ""))

    async with get_session_ctx() as db:
        row = await db.get(BridgeSession, bridge_session_id)
        if row is not None:
            row.upstream_session_id = upstream_session_id
            await db.commit()

    state.agent_id = agent_id
    state.bridge_session_id = bridge_session_id
    state.upstream_session_id = upstream_session_id
    return bridge_session_id, upstream_session_id


async def _process_ptt_stop(device_id: str) -> None:
    state = await runtime.get_device_state(device_id)
    if not state.bridge_session_id or not state.upstream_session_id or not state.mic_chunks:
        return

    wav_bytes = _pcm_to_wav(state.mic_chunks, state.sample_rate)
    turn_id = uuid.uuid4().hex[:10]

    audio_in_path = DATA_ROOT / device_id / "sessions" / state.bridge_session_id / "audio_in" / f"{turn_id}.wav"
    _write_file(audio_in_path, wav_bytes)

    files = {"audio": ("input.wav", wav_bytes, "audio/wav")}
    am_audio_url = f"{settings.agentmanager_url.rstrip('/')}/sessions/{state.upstream_session_id}/audio"
    async with httpx.AsyncClient(timeout=120.0) as client:
        am_res = await client.post(am_audio_url, files=files)
        am_res.raise_for_status()
        agent_resp = am_res.json()

    async with get_session_ctx() as db:
        await store.add_session_event(
            db,
            session_id=state.bridge_session_id,
            event_type="ptt.agent_response",
            payload={"text": agent_resp.get("text", ""), "has_audio": bool(agent_resp.get("audio"))},
        )

    # Feed back into existing DBS dispatch pipeline.
    payload = {
        "text": agent_resp.get("text", ""),
        "audio_base64": agent_resp.get("audio"),
        "timeline": agent_resp.get("timeline", []) or [],
        "profile": None,
        "voice_config": None,
    }
    dbs_agent_output_url = f"http://127.0.0.1:8011/api/sessions/{state.bridge_session_id}/agent-output"
    async with httpx.AsyncClient(timeout=120.0) as client:
        await client.post(dbs_agent_output_url, json=payload)

    if agent_resp.get("audio"):
        audio_out_path = DATA_ROOT / device_id / "sessions" / state.bridge_session_id / "audio_out" / f"{turn_id}.wav.b64"
        _write_file(audio_out_path, str(agent_resp.get("audio")).encode("utf-8"))

    state.mic_chunks.clear()


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
        logger.info("device hello accepted: %s", device_id)

        while True:
            msg = await hub.receive_json(device_id)
            mtype = msg.get("type")
            logger.info("device message device=%s type=%s", device_id, mtype)
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
                state = await runtime.get_device_state(device_id)
                sample_rate = int(msg.get("sample_rate", state.sample_rate or 16000))
                state.sample_rate = sample_rate
                audio_base64 = str(msg.get("audio_base64", ""))
                raw_chunk = _safe_decode_b64(audio_base64)
                if raw_chunk:
                    state.mic_chunks.append(raw_chunk)

                session_id = str(msg.get("session_id", "")) or state.bridge_session_id
                if session_id:
                    await runtime.publish_mic(session_id, msg)
                    async with get_session_ctx() as db:
                        await store.add_session_event(db, session_id=session_id, event_type="mic.chunk", payload=msg)
                continue

            if mtype == "ptt.start":
                state = await runtime.get_device_state(device_id)
                caps = {}
                async with get_session_ctx() as db:
                    row = await db.get(Device, device_id)
                    if row is not None:
                        caps = store.device_capabilities_dict(row)
                agent_id = str(msg.get("agent_id", "")).strip() or str(caps.get("default_agent_id", "")).strip()
                if not agent_id:
                    await websocket.send_json({"type": "error", "detail": "ptt.start missing agent_id/default_agent_id"})
                    continue

                bridge_sid, upstream_sid = await _ensure_agentmanager_session(device_id, agent_id)
                state.listening = True
                state.mic_chunks.clear()
                state.bridge_session_id = bridge_sid
                state.upstream_session_id = upstream_sid
                await websocket.send_json({"type": "ptt.ready", "session_id": bridge_sid, "upstream_session_id": upstream_sid})
                async with get_session_ctx() as db:
                    await store.add_session_event(
                        db,
                        session_id=bridge_sid,
                        event_type="ptt.start",
                        payload={"agent_id": agent_id, "upstream_session_id": upstream_sid},
                    )
                continue

            if mtype == "ptt.stop":
                state = await runtime.get_device_state(device_id)
                state.listening = False
                await _process_ptt_stop(device_id)
                if state.bridge_session_id:
                    async with get_session_ctx() as db:
                        await store.add_session_event(db, session_id=state.bridge_session_id, event_type="ptt.stop", payload={})
                continue

            await websocket.send_json({"type": "error", "detail": f"unsupported message type: {mtype}"})
    except WebSocketDisconnect:
        logger.info("device disconnected: %s", device_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("websocket error device=%s err=%s", device_id, exc)
    finally:
        await hub.disconnect(device_id)
        await runtime.clear_device_state(device_id)
        async with get_session_ctx() as db:
            row = await db.get(Device, device_id)
            if row is not None:
                row.online = False
                await db.commit()
