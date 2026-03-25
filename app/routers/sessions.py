from __future__ import annotations

import json
import time
import asyncio
import base64
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.config import settings
from app.models import Device
from app.schemas import AgentAudioIn, AgentOutputIn, AgentTimelineIn, SessionOut, SessionStartIn, SessionStopOut
from app.services.device_hub import hub
from app.services.llm_mapper import suggest_rules_with_llm
from app.services.mapping import MappingContext, MappingEngine
from app.services.runtime import runtime
from app.services import store

router = APIRouter(prefix="/api", tags=["sessions"])
mapper = MappingEngine()
DATA_ROOT = Path("data/devices")


async def _emit_debug(session_id: str, event: str, payload: dict) -> None:
    data = {"event": event, **payload, "ts": int(time.time() * 1000)}
    await runtime.publish_debug(session_id, data)


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _materialize_audio_output(device_id: str, session_id: str, audio_base64: str) -> tuple[Path, int]:
    audio_bytes = base64.b64decode(audio_base64)
    audio_out_dir = DATA_ROOT / device_id / "sessions" / session_id / "audio_out"
    wav_path = audio_out_dir / "reply.wav"
    b64_path = audio_out_dir / "reply.wav.b64"
    _write_file(wav_path, audio_bytes)
    _write_file(b64_path, audio_base64.encode("utf-8"))
    return wav_path, len(audio_bytes)


def _compress_visemes(timeline: list) -> list[dict]:
    visemes = [event for event in timeline if getattr(event, "type", "") == "viseme"]
    if not visemes:
        return []

    out: list[dict] = []
    last_value: str | int | None = None
    last_t = -999999
    for event in visemes:
        value = event.value
        if value == last_value and (event.t - last_t) < 90:
            continue
        out.append({"t": int(event.t), "value": value})
        last_value = value
        last_t = int(event.t)
        if len(out) >= 160:
            break
    return out


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
    audio_path, audio_size = _materialize_audio_output(row.device_id, session_id, payload.audio_base64)

    if audio_size <= settings.device_inline_audio_max_bytes:
        command_type = "audio.play"
        command_payload = {
            "session_id": session_id,
            "audio_base64": payload.audio_base64,
            "sample_rate": payload.sample_rate,
            "visemes": _compress_visemes(payload.visemes),
        }
    else:
        relative_path = f"/api/device/sessions/{session_id}/audio/{audio_path.name}"
        command_type = "audio.play_url"
        command_payload = {
            "session_id": session_id,
            "url": f"{settings.public_base_url.rstrip('/')}{relative_path}",
            "path": relative_path,
            "sample_rate": payload.sample_rate,
            "content_type": "audio/wav",
            "bytes": audio_size,
            "visemes": _compress_visemes(payload.visemes),
        }

    command_id = await hub.dispatch_command(
        row.device_id,
        command_type,
        command_payload,
        require_ack=True,
    )
    await store.add_session_event(
        db,
        session_id=session_id,
        event_type=command_type,
        payload={"command_id": command_id, "bytes": audio_size, "path": str(audio_path)},
    )
    await _emit_debug(session_id, "audio_done", {"command_id": command_id})
    return {"status": "ok", "command_id": command_id, "command_type": command_type, "bytes": audio_size}


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
    caps = store.device_capabilities_dict(device)
    animations = [str(x) for x in caps.get("animations", ["neutral_blink"])]
    supported_modes = [str(x) for x in caps.get("render_modes", ["line"])]
    passthrough_model_tags = bool(caps.get("accepts_model_directives", False))

    unknown_emotions = sorted(
        {
            str(ev.value)
            for ev in payload.timeline
            if ev.type == "emotion" and str(ev.value) not in emotion_map
        }
    )
    unknown_actions = sorted(
        {
            str(ev.value)
            for ev in payload.timeline
            if ev.type == "action" and str(ev.value) not in action_map
        }
    )

    if settings.mapping_llm_on_miss and (unknown_emotions or unknown_actions):
        new_emotions = await suggest_rules_with_llm(
            source_type="emotion",
            labels=unknown_emotions,
            animations=animations,
            supported_modes=supported_modes,
            preferred_render_mode=mapping_row.preferred_render_mode,
            passthrough_model_tags=passthrough_model_tags,
        )
        new_actions = await suggest_rules_with_llm(
            source_type="action",
            labels=unknown_actions,
            animations=animations,
            supported_modes=supported_modes,
            preferred_render_mode=mapping_row.preferred_render_mode,
            passthrough_model_tags=passthrough_model_tags,
        )
        emotion_map.update(new_emotions)
        action_map.update(new_actions)
        mapping_row = await store.save_mapping(
            db,
            mapping=mapping_row,
            preferred_render_mode=mapping_row.preferred_render_mode,
            emotion_map=emotion_map,
            action_map=action_map,
        )

    ctx = MappingContext(
        device_capabilities=caps,
        preferred_render_mode=mapping_row.preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )

    commands = mapper.timeline_to_commands(payload.timeline, ctx)
    await store.add_session_event(
        db,
        session_id=session_id,
        event_type="agent.timeline",
        payload={"timeline": [event.model_dump() for event in payload.timeline]},
    )
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

    timeline_result: dict | None = None
    audio_result: dict | None = None
    errors: list[str] = []

    try:
        timeline_result = await post_agent_timeline(
            session_id,
            AgentTimelineIn(timeline=payload.timeline, profile=payload.profile),
            db,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"timeline dispatch failed: {exc}")
        await store.add_session_event(
            db,
            session_id=session_id,
            event_type="dispatch.error",
            payload={"stage": "timeline", "error": str(exc)},
        )

    if payload.audio_base64:
        try:
            audio_result = await post_agent_audio(
                session_id,
                AgentAudioIn(
                    audio_base64=payload.audio_base64,
                    sample_rate=payload.sample_rate or 22050,
                    visemes=payload.timeline,
                ),
                db,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"audio dispatch failed: {exc}")
            await store.add_session_event(
                db,
                session_id=session_id,
                event_type="dispatch.error",
                payload={"stage": "audio", "error": str(exc)},
            )

    await store.add_session_event(
        db,
        session_id=session_id,
        event_type="agent.output",
        payload={"text": payload.text, "timeline_count": len(payload.timeline), "has_audio": payload.audio_base64 is not None},
    )

    await _emit_debug(session_id, "done", {"text": payload.text, "timeline": timeline_result, "audio": audio_result, "errors": errors})
    return {"status": "ok" if not errors else "partial", "timeline": timeline_result, "audio": audio_result, "errors": errors}


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
