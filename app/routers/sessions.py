from __future__ import annotations

import json
import time
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models import Device
from app.schemas import AgentAudioIn, AgentOutputIn, AgentTimelineIn, SessionOut, SessionStartIn, SessionStopOut
from app.services.device_hub import hub
from app.services.mapping import MappingContext, MappingEngine
from app.services.runtime import runtime
from app.services import store

router = APIRouter(prefix="/api", tags=["sessions"])
mapper = MappingEngine()


async def _emit_debug(session_id: str, event: str, payload: dict) -> None:
    data = {"event": event, **payload, "ts": int(time.time() * 1000)}
    await runtime.publish_debug(session_id, data)


@router.post("/sessions/start", response_model=SessionOut)
async def start_session(payload: SessionStartIn, db: AsyncSession = Depends(get_db)) -> SessionOut:
    device = await db.get(Device, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    if not hub.is_online(payload.device_id):
        raise HTTPException(status_code=409, detail="device is offline")

    row = await store.create_bridge_session(
        db,
        agent_id=payload.agent_id,
        device_id=payload.device_id,
        upstream_session_id=payload.upstream_session_id,
    )
    await store.add_session_event(
        db,
        session_id=row.session_id,
        event_type="session.start",
        payload={"agent_id": payload.agent_id, "device_id": payload.device_id},
    )
    await _emit_debug(row.session_id, "start", {"agent_id": row.agent_id, "device_id": row.device_id})
    return SessionOut(
        session_id=row.session_id,
        agent_id=row.agent_id,
        device_id=row.device_id,
        upstream_session_id=row.upstream_session_id,
        active=row.active,
    )


@router.post("/sessions/{session_id}/agent-audio")
async def post_agent_audio(session_id: str, payload: AgentAudioIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = await store.get_bridge_session(db, session_id=session_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="session not found")

    await _emit_debug(session_id, "audio_start", {"bytes_b64": len(payload.audio_base64)})
    command_id = await hub.dispatch_command(
        row.device_id,
        "audio.play",
        {"session_id": session_id, "audio_base64": payload.audio_base64, "sample_rate": payload.sample_rate},
        require_ack=True,
    )
    await store.add_session_event(db, session_id=session_id, event_type="audio.play", payload={"command_id": command_id})
    await _emit_debug(session_id, "audio_done", {"command_id": command_id})
    return {"status": "ok", "command_id": command_id}


@router.post("/sessions/{session_id}/agent-timeline")
async def post_agent_timeline(session_id: str, payload: AgentTimelineIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = await store.get_bridge_session(db, session_id=session_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="session not found")

    device = await db.get(Device, row.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    mapping_row = await store.get_or_create_mapping(db, agent_id=row.agent_id, device_id=row.device_id)
    emotion_map, action_map = store.parse_mapping(mapping_row)

    ctx = MappingContext(
        device_capabilities=store.device_capabilities_dict(device),
        preferred_render_mode=mapping_row.preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )

    commands = mapper.timeline_to_commands(payload.timeline, ctx)
    await _emit_debug(session_id, "timeline_parse", {"event_count": len(payload.timeline), "command_count": len(commands)})

    dispatched: list[str] = []
    for command in commands:
        command_id = await hub.dispatch_command(row.device_id, command["type"], command["payload"], require_ack=True)
        dispatched.append(command_id)

    await store.add_session_event(
        db,
        session_id=session_id,
        event_type="timeline.dispatch",
        payload={"commands": commands, "command_ids": dispatched},
    )
    await _emit_debug(session_id, "timeline_done", {"command_count": len(dispatched)})
    return {"status": "ok", "commands": len(dispatched), "command_ids": dispatched}


@router.post("/sessions/{session_id}/agent-output")
async def post_agent_output(session_id: str, payload: AgentOutputIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = await store.get_bridge_session(db, session_id=session_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="session not found")

    await _emit_debug(session_id, "agent_output_start", {"has_audio": payload.audio_base64 is not None})

    timeline_result = await post_agent_timeline(
        session_id,
        AgentTimelineIn(timeline=payload.timeline, profile=payload.profile),
        db,
    )

    audio_result: dict | None = None
    if payload.audio_base64:
        audio_result = await post_agent_audio(
            session_id,
            AgentAudioIn(audio_base64=payload.audio_base64),
            db,
        )

    await store.add_session_event(
        db,
        session_id=session_id,
        event_type="agent.output",
        payload={"text": payload.text, "timeline_count": len(payload.timeline), "has_audio": payload.audio_base64 is not None},
    )

    await _emit_debug(session_id, "done", {"text": payload.text, "timeline": timeline_result, "audio": audio_result})
    return {"status": "ok", "timeline": timeline_result, "audio": audio_result}


@router.post("/sessions/{session_id}/stop", response_model=SessionStopOut)
async def stop_session(session_id: str, db: AsyncSession = Depends(get_db)) -> SessionStopOut:
    row = await store.end_bridge_session(db, session_id=session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    await store.add_session_event(db, session_id=session_id, event_type="session.stop", payload={})
    await _emit_debug(session_id, "stop", {})
    return SessionStopOut(session_id=row.session_id, active=row.active)


@router.get("/sessions/{session_id}/debug")
async def get_debug_stream(session_id: str) -> StreamingResponse:
    async def generator():
        while True:
            try:
                event = await runtime.get_next_debug(session_id, timeout=20.0)
                yield f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
            except asyncio.TimeoutError:
                yield "data: {\"event\":\"heartbeat\"}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/mic")
async def get_next_mic_chunk(session_id: str) -> dict:
    try:
        chunk = await runtime.get_next_mic(session_id, timeout=5.0)
        return {"status": "ok", "chunk": chunk}
    except asyncio.TimeoutError:
        return {"status": "empty"}
