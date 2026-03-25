from __future__ import annotations

import json
import time
import asyncio
import base64
import io
import struct
import wave
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


def _choose_sample_rate(caps: dict, source_rate: int | None) -> int | None:
    supported = [int(x) for x in caps.get("sample_rates", []) if int(x) > 0]
    preferred = caps.get("preferred_sample_rate")
    if preferred is not None:
        try:
            preferred_i = int(preferred)
            if preferred_i > 0 and (not supported or preferred_i in supported):
                return preferred_i
        except (TypeError, ValueError):
            pass
    if source_rate and source_rate > 0:
        if not supported:
            return int(source_rate)
        return min(supported, key=lambda rate: abs(rate - int(source_rate)))
    if supported:
        return supported[0]
    return source_rate


def _wav_resample(audio_bytes: bytes, target_rate: int | None) -> tuple[bytes, int | None]:
    if not target_rate or target_rate <= 0:
        return audio_bytes, None
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            source_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if source_rate == target_rate:
            return audio_bytes, source_rate
        if channels != 1 or sampwidth != 2:
            return audio_bytes, source_rate
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        if not samples:
            return audio_bytes, source_rate
        out_count = max(1, int(round(len(samples) * target_rate / source_rate)))
        converted_samples: list[int] = []
        if len(samples) == 1:
            converted_samples = [samples[0]] * out_count
        else:
            scale = (len(samples) - 1) / max(1, out_count - 1)
            for idx in range(out_count):
                src_pos = idx * scale
                left = int(src_pos)
                right = min(left + 1, len(samples) - 1)
                frac = src_pos - left
                value = int(round(samples[left] * (1.0 - frac) + samples[right] * frac))
                converted_samples.append(max(-32768, min(32767, value)))
        converted = struct.pack(f"<{len(converted_samples)}h", *converted_samples)
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(target_rate)
            wf.writeframes(converted)
        return out.getvalue(), target_rate
    except Exception:
        return audio_bytes, None


def _wav_to_pcm(audio_bytes: bytes) -> tuple[bytes, int | None]:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            source_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if channels != 1 or sampwidth != 2:
            return b"", source_rate
        return frames, source_rate
    except Exception:
        return b"", None


def _prepare_device_audio(device: Device, session_id: str, audio_base64: str, sample_rate: int | None) -> dict:
    caps = store.device_capabilities_dict(device)
    audio_bytes = base64.b64decode(audio_base64)
    target_rate = _choose_sample_rate(caps, sample_rate)
    adapted_bytes, adapted_rate = _wav_resample(audio_bytes, target_rate)
    adapted_b64 = base64.b64encode(adapted_bytes).decode("ascii")
    audio_path, audio_size = _materialize_audio_output(device.device_id, session_id, adapted_b64)
    pcm_bytes, pcm_rate = _wav_to_pcm(adapted_bytes)

    methods = [str(x).strip().lower() for x in caps.get("audio_methods", []) if str(x).strip()]
    if not methods:
        methods = ["inline", "url", "ws_stream"]
    preferred_method = str(caps.get("preferred_audio_method", "") or "").strip().lower()
    max_inline_audio_bytes = caps.get("max_inline_audio_bytes")
    try:
        inline_limit = int(max_inline_audio_bytes) if max_inline_audio_bytes is not None else settings.device_inline_audio_max_bytes
    except (TypeError, ValueError):
        inline_limit = settings.device_inline_audio_max_bytes

    if "ws_stream" in methods and pcm_bytes and preferred_method in {"ws_stream", "stream", ""}:
        return {
            "command_type": "audio.stream",
            "sample_rate": pcm_rate or adapted_rate or sample_rate,
            "audio_size": len(pcm_bytes),
            "audio_path": audio_path,
            "pcm_bytes": pcm_bytes,
            "prebuffer_ms": int(caps.get("stream_prebuffer_ms") or 250),
            "chunk_bytes": 4096,
        }

    use_inline = "inline" in methods and audio_size <= inline_limit and (
        preferred_method == "inline" or "url" not in methods
    )
    if use_inline:
        return {
            "command_type": "audio.play",
            "command_payload": {
                "session_id": session_id,
                "audio_base64": adapted_b64,
                "sample_rate": adapted_rate or sample_rate,
            },
            "audio_size": audio_size,
            "audio_path": audio_path,
        }

    relative_path = f"/api/device/sessions/{session_id}/audio/{audio_path.name}"
    return {
        "command_type": "audio.play_url",
        "command_payload": {
            "session_id": session_id,
            "url": f"{settings.public_base_url.rstrip('/')}{relative_path}",
            "path": relative_path,
            "sample_rate": adapted_rate or sample_rate,
            "content_type": "audio/wav",
            "bytes": audio_size,
            "prebuffer_ms": caps.get("stream_prebuffer_ms"),
        },
        "audio_size": audio_size,
        "audio_path": audio_path,
    }


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
    device = await db.get(Device, row.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    await _emit_debug(session_id, "audio_start", {"bytes_b64": len(payload.audio_base64)})
    prepared = _prepare_device_audio(device, session_id, payload.audio_base64, payload.sample_rate)
    command_type = str(prepared["command_type"])
    audio_size = int(prepared["audio_size"])
    audio_path = prepared["audio_path"]
    visemes = _compress_visemes(payload.visemes)

    if command_type == "audio.stream":
        pcm_bytes = prepared["pcm_bytes"]
        chunk_bytes = int(prepared.get("chunk_bytes") or 4096)
        sample_rate_out = int(prepared.get("sample_rate") or payload.sample_rate or 16000)
        prebuffer_ms = int(prepared.get("prebuffer_ms") or 250)
        total_chunks = max(1, (len(pcm_bytes) + chunk_bytes - 1) // chunk_bytes)
        stream_id = await hub.dispatch_command(
            row.device_id,
            "audio.stream.start",
            {
                "session_id": session_id,
                "sample_rate": sample_rate_out,
                "channels": 1,
                "format": "pcm_s16le",
                "bytes": len(pcm_bytes),
                "chunk_bytes": chunk_bytes,
                "prebuffer_ms": prebuffer_ms,
                "visemes": visemes,
                "total_chunks": total_chunks,
            },
            require_ack=False,
        )
        for seq, offset in enumerate(range(0, len(pcm_bytes), chunk_bytes)):
            chunk = pcm_bytes[offset : offset + chunk_bytes]
            await hub.dispatch_command(
                row.device_id,
                "audio.stream.chunk",
                {
                    "session_id": session_id,
                    "seq": seq,
                    "audio_base64": base64.b64encode(chunk).decode("ascii"),
                },
                require_ack=False,
            )
            if seq % 4 == 3:
                await asyncio.sleep(0.005)
        await hub.dispatch_command(
            row.device_id,
            "audio.stream.end",
            {"session_id": session_id, "total_chunks": total_chunks},
            require_ack=False,
        )
        command_id = stream_id
    else:
        command_payload = dict(prepared["command_payload"])
        command_payload["visemes"] = visemes
        command_id = await hub.dispatch_command(
            row.device_id,
            command_type,
            command_payload,
            require_ack=False,
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
        command_id = await hub.dispatch_command(row.device_id, command["type"], command["payload"], require_ack=False)
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
