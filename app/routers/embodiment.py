"""
Embodiment sessions router.

Endpoints:
  GET    /api/embodiment/sessions                  — list active sessions
  POST   /api/embodiment/sessions                  — create/claim session
  GET    /api/embodiment/sessions/{session_id}     — get session detail
  DELETE /api/embodiment/sessions/{session_id}     — release session
  POST   /api/embodiment/sessions/{session_id}/re_embody     — move primary device
  POST   /api/embodiment/sessions/{session_id}/aux_connect   — add aux device
  DELETE /api/embodiment/sessions/{session_id}/aux/{device_id} — remove aux device
  POST   /api/embodiment/sessions/{session_id}/configure     — push device settings
  POST   /api/embodiment/sessions/{session_id}/speak         — canonical TTS output
  POST   /api/embodiment/sessions/{session_id}/show_avatar   — update avatar/expression
  POST   /api/embodiment/sessions/{session_id}/show_image    — display image on primary
  POST   /api/embodiment/sessions/{session_id}/show_text     — display text on primary
  WS     /api/embodiment/sessions/{session_id}/stream        — bidirectional audio
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_principal
from app.config import settings
from app.database import get_db
from app.models import Device, EmbodimentSession, EmbodimentSessionDevice
from app.schemas import (
    AuxConnectRequest,
    ConfigureRequest,
    EmbodimentSessionCreate,
    EmbodimentSessionDeviceOut,
    EmbodimentSessionOut,
    ReEmbodyRequest,
    ShowAvatarRequest,
    ShowImageRequest,
    ShowTextRequest,
    SpeakEmbodimentRequest,
)
from app.services import embodiment_manager as em
from app.services.audio_router import speak_on_device
from app.services.stream_loop import run_ws_stream_loop

router = APIRouter(prefix="/api/embodiment", tags=["embodiment"])
logger = logging.getLogger(__name__)

VALID_AUX_ROLES = {"aux_speaker", "aux_display", "sensor_feed", "input_terminal"}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_session_or_404(
    session_id: str, db: AsyncSession, load_devices: bool = True
) -> EmbodimentSession:
    q = select(EmbodimentSession)
    if load_devices:
        q = q.options(selectinload(EmbodimentSession.devices).selectinload(EmbodimentSessionDevice.device))
    result = await db.execute(q.where(EmbodimentSession.session_id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Embodiment session not found")
    return session


def _session_device_out(sd: EmbodimentSessionDevice) -> EmbodimentSessionDeviceOut:
    device = sd.device
    return EmbodimentSessionDeviceOut(
        id=sd.id,
        session_id=sd.session_id,
        device_id=sd.device_id,
        device_name=device.name if device else "",
        device_slug=device.slug if device else "",
        role=sd.role,
        connected_at=sd.connected_at,
        disconnected_at=sd.disconnected_at,
        is_active=sd.is_active,
    )


def _session_out(session: EmbodimentSession) -> EmbodimentSessionOut:
    primary = session.primary_device
    return EmbodimentSessionOut(
        session_id=session.session_id,
        agent_id=session.agent_id,
        am_session_id=session.am_session_id,
        primary_device_id=session.primary_device_id,
        primary_device_name=primary.name if primary else "",
        primary_device_slug=primary.slug if primary else "",
        z_index=session.z_index,
        permission_plan=session.permission_plan,
        expires_at=session.expires_at,
        state=session.state,
        group_id=session.group_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        released_at=session.released_at,
        devices=[_session_device_out(sd) for sd in (session.devices or [])],
    )


async def _load_session_full(session_id: str, db: AsyncSession) -> EmbodimentSession:
    """Load session with all eager-loaded relationships."""
    result = await db.execute(
        select(EmbodimentSession)
        .options(
            selectinload(EmbodimentSession.devices).selectinload(EmbodimentSessionDevice.device),
            selectinload(EmbodimentSession.primary_device),
        )
        .where(EmbodimentSession.session_id == session_id)
    )
    return result.scalar_one_or_none()


# ── Session CRUD ──────────────────────────────────────────────────────────────

@router.get("/sessions", response_model=list[EmbodimentSessionOut])
async def list_sessions(
    state: str | None = None,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """List embodiment sessions. Filter by state with ?state=streaming|ambient|released."""
    q = (
        select(EmbodimentSession)
        .options(
            selectinload(EmbodimentSession.devices).selectinload(EmbodimentSessionDevice.device),
            selectinload(EmbodimentSession.primary_device),
        )
        .order_by(EmbodimentSession.created_at.desc())
    )
    if state:
        q = q.where(EmbodimentSession.state == state)

    result = await db.execute(q)
    sessions = result.scalars().all()
    return [_session_out(s) for s in sessions]


@router.post("/sessions", response_model=EmbodimentSessionOut, status_code=201)
async def create_session(
    body: EmbodimentSessionCreate,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Claim embodiment on a device or group.

    Preemption: if the target device is held by a session with z_index > body.z_index,
    returns 409 with details of the holding session.
    """
    try:
        session = await em.create_session(
            db=db,
            agent_id=body.agent_id,
            am_session_id=body.am_session_id,
            device_id=body.device_id,
            group_id=body.group_id,
            z_index=body.z_index,
            permission_plan=body.permission_plan,
            timeout_seconds=body.timeout_seconds,
        )
    except em.OccupiedError as e:
        raise HTTPException(
            409,
            detail={
                "error": "device_occupied",
                "device_id": e.device_id,
                "holder_agent_id": e.holder_agent_id,
                "holder_session_id": e.holder_session_id,
                "holder_z": e.holder_z,
            },
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    full = await _load_session_full(session.session_id, db)
    return _session_out(full)


@router.get("/sessions/{session_id}", response_model=EmbodimentSessionOut)
async def get_session(
    session_id: str,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Get session detail including all participating devices."""
    full = await _load_session_full(session_id, db)
    if not full:
        raise HTTPException(404, "Embodiment session not found")
    return _session_out(full)


@router.delete("/sessions/{session_id}", status_code=204)
async def release_session(
    session_id: str,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Release an embodiment session (agent or admin)."""
    session = await _get_session_or_404(session_id, db)
    if session.state == "released":
        return  # idempotent — already released
    await em.release_session(db, session, source="explicit")


# ── Session device management ─────────────────────────────────────────────────

@router.post("/sessions/{session_id}/re_embody", response_model=EmbodimentSessionOut)
async def re_embody(
    session_id: str,
    body: ReEmbodyRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Move the session's primary embodiment to a new device.
    The conversation (am_session_id) is preserved — call is uninterrupted.
    If release_previous=False, the old primary becomes an aux_display.
    """
    session = await _get_session_or_404(session_id, db)
    if session.state == "released":
        raise HTTPException(409, "Cannot re-embody a released session")

    try:
        await em.re_embody(db, session, body.device_id, body.release_previous)
    except em.OccupiedError as e:
        raise HTTPException(
            409,
            detail={
                "error": "device_occupied",
                "device_id": e.device_id,
                "holder_agent_id": e.holder_agent_id,
                "holder_session_id": e.holder_session_id,
                "holder_z": e.holder_z,
            },
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    # Expire identity map so selectinload re-fetches the updated devices collection
    db.expire_all()
    full = await _load_session_full(session_id, db)
    return _session_out(full)


@router.post("/sessions/{session_id}/aux_connect", response_model=EmbodimentSessionDeviceOut, status_code=201)
async def aux_connect(
    session_id: str,
    body: AuxConnectRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Add an auxiliary device (speaker, display, sensor) to the session."""
    if body.role not in VALID_AUX_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(VALID_AUX_ROLES)}")

    session = await _get_session_or_404(session_id, db, load_devices=False)
    if session.state == "released":
        raise HTTPException(409, "Cannot add devices to a released session")

    try:
        sd = await em.aux_connect(db, session, body.device_id, body.role)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    # Eager-load device for response
    result = await db.execute(
        select(EmbodimentSessionDevice)
        .options(selectinload(EmbodimentSessionDevice.device))
        .where(EmbodimentSessionDevice.id == sd.id)
    )
    sd = result.scalar_one()
    return _session_device_out(sd)


@router.delete("/sessions/{session_id}/aux/{device_id}", status_code=204)
async def aux_disconnect(
    session_id: str,
    device_id: str,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Remove an auxiliary device from the session."""
    session = await _get_session_or_404(session_id, db, load_devices=True)
    try:
        await em.aux_disconnect(db, session, device_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ── Canonical action endpoints ────────────────────────────────────────────────

@router.post("/sessions/{session_id}/configure")
async def configure(
    session_id: str,
    body: ConfigureRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Push settings changes to the primary device.
    DBS validates each key against manifest.settings_writable before sending.
    """
    session = await _get_session_or_404(session_id, db, load_devices=False)
    if session.state == "released":
        raise HTTPException(409, "Session is released")

    if not session.primary_device_id:
        raise HTTPException(409, "Session has no primary device")

    device = await db.get(Device, session.primary_device_id)
    if not device:
        raise HTTPException(404, "Primary device not found")

    # Validate keys against settings_writable in embodiment manifest
    em_raw = device.embodiment_manifest_json
    em_dict = json.loads(em_raw) if em_raw else {}
    writable = set(em_dict.get("settings_writable", []))

    if writable:
        rejected = [k for k in body.settings if k not in writable]
        if rejected:
            raise HTTPException(
                422,
                f"The following settings keys are not writable on this device: {rejected}. "
                f"Writable keys: {sorted(writable)}",
            )

    # Push settings to device via adapter
    from app.adapters.registry import get_adapter
    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    try:
        result = await adapter.push_device_settings(body.settings)
    except NotImplementedError:
        raise HTTPException(501, f"Device protocol '{device.protocol}' does not support settings push")
    except Exception as exc:
        raise HTTPException(502, f"Device rejected settings: {exc}")

    return {"ok": True, "applied": body.settings, "device_response": result}


@router.post("/sessions/{session_id}/speak")
async def speak(
    session_id: str,
    body: SpeakEmbodimentRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Synthesise speech and route to the primary device speaker.
    Optionally update avatar expression and/or dispatch to aux speakers simultaneously.
    """
    session = await _get_session_or_404(session_id, db, load_devices=True)
    if session.state == "released":
        raise HTTPException(409, "Session is released")

    if not session.primary_device_id:
        raise HTTPException(409, "Session has no primary device")

    primary_device = await db.get(Device, session.primary_device_id)
    if not primary_device:
        raise HTTPException(404, "Primary device not found")

    from app.adapters.registry import get_adapter

    connection = json.loads(primary_device.connection_json or "{}")
    primary_adapter = get_adapter(primary_device.protocol, primary_device.host, connection)

    results: dict[str, Any] = {}

    # Primary device TTS
    try:
        result = await speak_on_device(
            primary_adapter,
            body.text,
            body.voice,
            session_id=session_id,
        )
        results["primary"] = result
    except NotImplementedError:
        raise HTTPException(501, f"Primary device does not support audio output")
    except Exception as exc:
        raise HTTPException(502, f"TTS to primary device failed: {exc}")

    # Expression update (if requested and device supports it)
    if body.expression:
        em_raw = primary_device.embodiment_manifest_json
        em_dict = json.loads(em_raw) if em_raw else {}
        avatar_cfg = em_dict.get("avatar", {})
        avatar_type = avatar_cfg.get("type", "none")
        if avatar_type != "none":
            try:
                await primary_adapter.push_expression(body.expression)
            except NotImplementedError:
                pass
            except Exception as exc:
                logger.warning("DBS: expression push failed on %s: %s", primary_device.slug, exc)

    # Aux speakers (fire-and-forget, parallel)
    if body.aux_device_ids:
        async def _speak_aux(dev_id: str) -> None:
            aux_dev = await db.get(Device, dev_id)
            if not aux_dev:
                return
            aux_conn = json.loads(aux_dev.connection_json or "{}")
            aux_adapter = get_adapter(aux_dev.protocol, aux_dev.host, aux_conn)
            try:
                await speak_on_device(aux_adapter, body.text, body.voice, session_id=session_id)
            except Exception as exc:
                logger.warning("DBS: aux speak failed on %s: %s", aux_dev.slug, exc)

        await asyncio.gather(*[_speak_aux(dev_id) for dev_id in body.aux_device_ids])
        results["aux_dispatched"] = len(body.aux_device_ids)

    return {"ok": True, "session_id": session_id, **results}


@router.post("/sessions/{session_id}/show_avatar")
async def show_avatar(
    session_id: str,
    body: ShowAvatarRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Update the avatar expression on the primary device.
    Validates the expression against the device's manifest.avatar.expression_states.
    """
    session = await _get_session_or_404(session_id, db, load_devices=False)
    if session.state == "released":
        raise HTTPException(409, "Session is released")

    if not session.primary_device_id:
        raise HTTPException(409, "Session has no primary device")

    device = await db.get(Device, session.primary_device_id)
    if not device:
        raise HTTPException(404, "Primary device not found")

    # Validate expression against manifest
    em_raw = device.embodiment_manifest_json
    em_dict = json.loads(em_raw) if em_raw else {}
    avatar_cfg = em_dict.get("avatar", {})
    avatar_type = avatar_cfg.get("type", "none")
    valid_expressions = avatar_cfg.get("expression_states", [])

    if avatar_type == "none":
        raise HTTPException(501, "Primary device does not support avatar expressions")
    if valid_expressions and body.expression not in valid_expressions:
        raise HTTPException(
            422,
            f"Expression '{body.expression}' not in device's expression_states: {valid_expressions}",
        )

    from app.adapters.registry import get_adapter
    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    try:
        await adapter.push_expression(body.expression)
    except NotImplementedError:
        raise HTTPException(501, f"Device protocol '{device.protocol}' does not support push_expression")
    except Exception as exc:
        raise HTTPException(502, f"Expression push failed: {exc}")

    return {"ok": True, "expression": body.expression, "device": device.slug}


@router.post("/sessions/{session_id}/show_image")
async def show_image(
    session_id: str,
    body: ShowImageRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Display a base64-encoded image on the primary device screen."""
    session = await _get_session_or_404(session_id, db, load_devices=False)
    if session.state == "released":
        raise HTTPException(409, "Session is released")

    if not session.primary_device_id:
        raise HTTPException(409, "Session has no primary device")

    device = await db.get(Device, session.primary_device_id)
    if not device:
        raise HTTPException(404, "Primary device not found")

    from app.adapters.registry import get_adapter
    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    payload: dict[str, Any] = {"image": body.image_b64}
    if body.caption:
        payload["caption"] = body.caption

    try:
        result = await adapter.execute("display_image", payload)
    except Exception as exc:
        raise HTTPException(502, f"show_image failed on device '{device.slug}': {exc}")

    return {"ok": True, "device": device.slug, "result": result}


@router.post("/sessions/{session_id}/show_text")
async def show_text(
    session_id: str,
    body: ShowTextRequest,
    principal: dict = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Display text on the primary device screen."""
    session = await _get_session_or_404(session_id, db, load_devices=False)
    if session.state == "released":
        raise HTTPException(409, "Session is released")

    if not session.primary_device_id:
        raise HTTPException(409, "Session has no primary device")

    device = await db.get(Device, session.primary_device_id)
    if not device:
        raise HTTPException(404, "Primary device not found")

    from app.adapters.registry import get_adapter
    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    payload: dict[str, Any] = {"text": body.text, "color": body.color, "scroll": body.scroll}

    try:
        result = await adapter.execute("display_text", payload)
    except Exception as exc:
        raise HTTPException(502, f"show_text failed on device '{device.slug}': {exc}")

    return {"ok": True, "device": device.slug, "result": result}


# ── WebSocket audio stream ────────────────────────────────────────────────────

@router.websocket("/sessions/{session_id}/stream")
async def ws_audio_stream(
    session_id: str,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    Bidirectional audio WebSocket stream.
    The device connects here and sends audio_chunk / audio_end messages.
    DBS runs STT → AgentManager → TTS and streams audio back.

    Auth: The session_id implicitly identifies the session; validate it exists
    and is not released before accepting. For production, add token query param.
    """
    # Validate session without accepting WebSocket yet
    result = await db.execute(
        select(EmbodimentSession).where(EmbodimentSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    if session.state == "released":
        await websocket.close(code=4009, reason="Session is released")
        return

    if not session.am_session_id:
        await websocket.close(code=4009, reason="Session has no AgentManager session")
        return

    # Determine voice from primary device's manifest or fall back to default
    voice = "glados"
    if session.primary_device_id:
        primary_device = await db.get(Device, session.primary_device_id)
        if primary_device and primary_device.embodiment_manifest_json:
            em_dict = json.loads(primary_device.embodiment_manifest_json)
            voice = em_dict.get("voice", voice)

    # Build expression pusher coroutine factory for push_expression calls
    async def _push_expression_to_device(expression: str) -> None:
        if not session.primary_device_id:
            return
        dev = await db.get(Device, session.primary_device_id)
        if not dev:
            return
        from app.adapters.registry import get_adapter
        conn = json.loads(dev.connection_json or "{}")
        adapter = get_adapter(dev.protocol, dev.host, conn)
        try:
            await adapter.push_expression(expression)
        except Exception:
            pass

    # Transition session to streaming if it was ambient
    if session.state == "ambient":
        await em.set_session_state(db, session, "streaming")

    await run_ws_stream_loop(
        websocket=websocket,
        am_session_id=session.am_session_id,
        voice=voice,
        on_expression=_push_expression_to_device,
    )
