"""Device registry — CRUD, ping, ToolGateway sync, events, and audio upload."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_admin_principal, get_principal
from app.database import get_db
from app.models import Device, DeviceCapability, DeviceEvent
from app.adapters.registry import get_adapter
from app.schemas import (
    AudioUploadResult,
    DeviceEventCreate,
    DeviceEventOut,
    DeviceListItem,
    DeviceManifest,
    DeviceOut,
    DeviceRegister,
    DeviceUpdate,
    CapabilityOut,
    PingResult,
)
from app.services.tool_sync import sync_device_to_toolgateway, retire_device_tools

router = APIRouter(prefix="/api/devices", tags=["devices"])
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cap_out(c: DeviceCapability) -> CapabilityOut:
    return CapabilityOut(
        id=c.id,
        device_id=c.device_id,
        name=c.name,
        description=c.description,
        parameters=json.loads(c.parameters_json or "{}"),
        tg_tool_id=c.tg_tool_id,
        tg_tool_name=c.tg_tool_name,
        synced_at=c.synced_at,
    )


def _device_out(d: Device) -> DeviceOut:
    em_raw = d.embodiment_manifest_json
    return DeviceOut(
        device_id=d.device_id,
        name=d.name,
        slug=d.slug,
        type=d.type,
        protocol=d.protocol,
        host=d.host,
        connection=json.loads(d.connection_json or "{}"),
        status=d.status,
        display=json.loads(d.display_json) if d.display_json else None,
        audio=json.loads(d.audio_json) if d.audio_json else None,
        input=json.loads(d.input_json) if d.input_json else None,
        embodiment_manifest=json.loads(em_raw) if em_raw else None,
        notes=d.notes,
        enabled=d.enabled,
        last_seen=d.last_seen,
        created_at=d.created_at,
        updated_at=d.updated_at,
        capabilities=[_cap_out(c) for c in (d.capabilities or [])],
    )


def _validate_embodiment_manifest(raw: dict[str, Any]) -> str:
    """Validate and normalise an embodiment manifest dict. Returns JSON string."""
    try:
        validated = DeviceManifest.model_validate(raw)
        return validated.model_dump_json(exclude_none=True)
    except Exception as exc:
        raise HTTPException(422, f"Invalid embodiment_manifest: {exc}")


def _parse_manifest(body: DeviceRegister) -> tuple[str, str, str, str, str, dict, dict, dict | None, dict | None, dict | None, list[dict]]:
    """Extract device fields from manifest or individual fields."""
    m = body.manifest or {}
    name        = m.get("name")        or body.name        or ""
    slug        = m.get("slug")        or body.slug        or ""
    dev_type    = m.get("type")        or body.type        or "display"
    protocol    = m.get("protocol")    or body.protocol    or "wled"
    host        = m.get("connection", {}).get("host") or body.host or ""
    connection  = {**m.get("connection", {}), **body.connection}
    connection.pop("host", None)  # host stored separately
    display     = m.get("display")     or body.display
    audio       = m.get("audio")       or body.audio
    inp         = m.get("input")       or body.input
    caps        = m.get("capabilities") or body.capabilities or []

    if not name or not slug or not host:
        raise HTTPException(422, "name, slug, and host are required (or provide a full manifest)")

    return name, slug, dev_type, protocol, host, connection, {}, display, audio, inp, caps


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[DeviceListItem])
async def list_devices(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.capabilities)).order_by(Device.created_at.desc())
    )
    devices = result.scalars().all()
    items = []
    for d in devices:
        caps = d.capabilities or []
        items.append(DeviceListItem(
            device_id=d.device_id,
            name=d.name,
            slug=d.slug,
            type=d.type,
            protocol=d.protocol,
            host=d.host,
            status=d.status,
            enabled=d.enabled,
            capability_count=len(caps),
            synced_count=sum(1 for c in caps if c.tg_tool_id),
            last_seen=d.last_seen,
            created_at=d.created_at,
        ))
    return items


@router.post("", response_model=DeviceOut, status_code=201)
async def register_device(
    body: DeviceRegister,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    name, slug, dev_type, protocol, host, connection, _, display, audio, inp, caps = _parse_manifest(body)

    # Slug uniqueness check
    existing = await db.execute(select(Device).where(Device.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"A device with slug '{slug}' already exists")

    # Validate embodiment manifest if provided
    embodiment_manifest_json: str | None = None
    if body.embodiment_manifest:
        embodiment_manifest_json = _validate_embodiment_manifest(body.embodiment_manifest)

    # For WLED: auto-fetch manifest if no capabilities provided
    if not caps and protocol == "wled":
        try:
            adapter = get_adapter(protocol, host, {**connection, "host": host})
            live_manifest = await adapter.fetch_live_manifest()
            if live_manifest:
                caps    = live_manifest.get("capabilities", caps)
                display = display or live_manifest.get("display")
                audio   = audio   or live_manifest.get("audio")
                inp     = inp     or live_manifest.get("input")
                logger.info("DBS: auto-detected %d capabilities from WLED device %s", len(caps), host)
        except Exception as exc:
            logger.warning("DBS: WLED auto-detect failed for %s: %s", host, exc)

    # Build full manifest for storage
    manifest = {
        "name": name, "slug": slug, "type": dev_type, "protocol": protocol,
        "connection": {"host": host, **connection},
        "display": display, "audio": audio, "input": inp,
        "capabilities": caps,
    }

    device = Device(
        name=name, slug=slug, type=dev_type, protocol=protocol, host=host,
        connection_json=json.dumps(connection),
        manifest_json=json.dumps(manifest),
        embodiment_manifest_json=embodiment_manifest_json,
        display_json=json.dumps(display) if display else None,
        audio_json=json.dumps(audio) if audio else None,
        input_json=json.dumps(inp) if inp else None,
        notes=body.notes,
        status="unknown",
        enabled=True,
    )
    db.add(device)
    await db.flush()  # get device_id

    for cap_def in caps:
        cap = DeviceCapability(
            device_id=device.device_id,
            name=cap_def["name"],
            description=cap_def.get("description", ""),
            parameters_json=json.dumps(cap_def.get("parameters", {})),
        )
        db.add(cap)

    await db.commit()

    # Ping to set initial status
    try:
        adapter = get_adapter(protocol, host, {**connection})
        online, _, _ = await adapter.ping()
        device.status    = "online" if online else "offline"
        device.last_seen = datetime.now(timezone.utc).replace(tzinfo=None) if online else device.last_seen
        await db.commit()
        await db.refresh(device)
    except Exception:
        pass

    # Sync capabilities → ToolGateway
    synced, failed = await sync_device_to_toolgateway(db, device)
    logger.info("DBS: registered device '%s' slug=%s synced=%d failed=%d", name, slug, synced, failed)

    # Reload with relationships for response
    result2 = await db.execute(
        select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device.device_id)
    )
    device = result2.scalar_one()
    return _device_out(device)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device_id)
    )
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")
    return _device_out(d)


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: str,
    body: DeviceUpdate,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")

    if body.name    is not None: d.name    = body.name
    if body.host    is not None: d.host    = body.host
    if body.notes   is not None: d.notes   = body.notes
    if body.enabled is not None: d.enabled = body.enabled
    if body.connection is not None:
        d.connection_json = json.dumps(body.connection)
    if body.display is not None:
        d.display_json = json.dumps(body.display)
    if body.audio is not None:
        d.audio_json = json.dumps(body.audio)
    if body.input is not None:
        d.input_json = json.dumps(body.input)
    if body.embodiment_manifest is not None:
        d.embodiment_manifest_json = _validate_embodiment_manifest(body.embodiment_manifest)

    await db.commit()
    result3 = await db.execute(select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device_id))
    d = result3.scalar_one()
    return _device_out(d)


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")

    retired = await retire_device_tools(d)
    await db.delete(d)
    await db.commit()
    logger.info("DBS: deleted device %s (%s), retired %d TG tools", d.name, d.slug, retired)


@router.post("/{device_id}/ping", response_model=PingResult)
async def ping_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.device_id == device_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")

    connection = json.loads(d.connection_json or "{}")
    adapter    = get_adapter(d.protocol, d.host, connection)
    online, latency_ms, info = await adapter.ping()

    d.status = "online" if online else "offline"
    if online:
        d.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()

    return PingResult(online=online, latency_ms=latency_ms, info=info)


@router.post("/{device_id}/sync", response_model=dict)
async def sync_device(
    device_id: str,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Re-sync all capabilities to ToolGateway (idempotent)."""
    result = await db.execute(select(Device).options(selectinload(Device.capabilities)).where(Device.device_id == device_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")

    synced, failed = await sync_device_to_toolgateway(db, d)
    return {"synced": synced, "failed": failed}


@router.post("/{device_id}/test/{capability_name}", tags=["test"])
async def test_capability(
    device_id: str,
    capability_name: str,
    body: dict,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Admin test endpoint — execute a capability directly without going through ToolGateway."""
    import time as _time
    result = await db.execute(select(Device).where(Device.device_id == device_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Device not found")

    connection = json.loads(d.connection_json or "{}")
    adapter    = get_adapter(d.protocol, d.host, connection)

    t0 = _time.monotonic()
    try:
        exec_result = await adapter.execute(capability_name, body)
        duration_ms = int((_time.monotonic() - t0) * 1000)
        return {"status": "ok", "capability": capability_name, "device": d.slug,
                "data": exec_result, "duration_ms": duration_ms}
    except Exception as exc:
        raise HTTPException(500, f"Execution failed: {exc}")


# ── Device Events ──────────────────────────────────────────────────────────────

@router.post("/{slug}/events", response_model=DeviceEventOut, status_code=201, tags=["events"])
async def receive_device_event(
    slug: str,
    body: DeviceEventCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive an event from a physical device (wake_word, button_press, motion, custom).

    Wake-word handling:
    - Looks up device → group → default_agent_id.
    - If device has no active EmbodimentSession: auto-creates one.
    - If device has an active ambient session: transitions it to streaming.
    - Injects a system message into the AgentManager session.

    No auth required — devices post here directly. DBS is on the local network.
    """
    result = await db.execute(select(Device).where(Device.slug == slug))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device '{slug}' not found")

    # Log the event
    event = DeviceEvent(
        device_slug=slug,
        device_id=device.device_id,
        event_type=body.event_type,
        payload_json=json.dumps(body.payload),
    )
    db.add(event)
    await db.flush()

    resulting_session_id: str | None = None

    # Handle wake_word / button_press — auto-embody logic
    if body.event_type in ("wake_word", "button_press"):
        from app.models import EmbodimentSession, DeviceGroup, DeviceGroupMember
        from app.services import embodiment_manager as em_svc

        # Check for active session on this device
        active_session = await em_svc.get_active_session_on_device(db, device.device_id)

        if active_session and active_session.state == "ambient":
            # Resume ambient session → streaming
            await em_svc.set_session_state(db, active_session, "streaming")
            resulting_session_id = active_session.session_id

            if active_session.am_session_id:
                await em_svc._inject_am_system_message(
                    active_session.am_session_id,
                    f"User reactivated device '{device.name}' via {body.event_type}. Resuming.",
                )
            logger.info(
                "DBS: event '%s' on '%s' resumed ambient session %s",
                body.event_type, slug, active_session.session_id,
            )

        elif not active_session:
            # Find group and default_agent_id
            group_member_result = await db.execute(
                select(DeviceGroupMember)
                .options(selectinload(DeviceGroupMember.group))
                .where(
                    DeviceGroupMember.device_id == device.device_id,
                    DeviceGroupMember.role == "primary",
                )
                .limit(1)
            )
            member = group_member_result.scalar_one_or_none()
            default_agent_id = member.group.default_agent_id if (member and member.group) else None
            group_id = member.group_id if member else None

            if default_agent_id:
                try:
                    new_session = await em_svc.create_session(
                        db=db,
                        agent_id=default_agent_id,
                        am_session_id=None,  # DBS will create new AM session
                        device_id=device.device_id,
                        group_id=group_id,
                        z_index=0,
                        permission_plan="active",
                        timeout_seconds=None,
                    )
                    resulting_session_id = new_session.session_id

                    # Inject activation context
                    if new_session.am_session_id:
                        await em_svc._inject_am_system_message(
                            new_session.am_session_id,
                            f"User activated device '{device.name}' (slug: {slug}) "
                            f"via {body.event_type}. You are now embodied on this device.",
                        )

                    logger.info(
                        "DBS: event '%s' on '%s' created new session %s for agent %s",
                        body.event_type, slug, new_session.session_id, default_agent_id,
                    )
                except em_svc.OccupiedError as oe:
                    logger.info(
                        "DBS: event '%s' on '%s' — device occupied by agent %s (z=%d), skipping auto-embody",
                        body.event_type, slug, oe.holder_agent_id, oe.holder_z,
                    )
                except Exception as exc:
                    logger.warning(
                        "DBS: auto-embody failed for event '%s' on '%s': %s",
                        body.event_type, slug, exc,
                    )
            else:
                logger.info(
                    "DBS: event '%s' on '%s' — no group default_agent_id, ignoring",
                    body.event_type, slug,
                )

    # Store resulting session_id on event record
    event.session_id = resulting_session_id
    await db.commit()
    await db.refresh(event)

    logger.info("DBS: received event '%s' on device '%s'", body.event_type, slug)
    return DeviceEventOut(
        id=event.id,
        device_slug=event.device_slug,
        device_id=event.device_id,
        event_type=event.event_type,
        payload_json=event.payload_json,
        session_id=event.session_id,
        created_at=event.created_at,
    )


# ── HTTP Audio Upload ──────────────────────────────────────────────────────────

@router.post("/{slug}/audio_upload", response_model=AudioUploadResult, tags=["audio"])
async def audio_upload(
    slug: str,
    audio: UploadFile = File(..., description="WAV audio file from device microphone"),
    db: AsyncSession = Depends(get_db),
):
    """
    HTTP audio upload fallback for devices that cannot maintain a WebSocket.

    Device POSTs a WAV file; DBS runs STT → AgentManager → TTS and returns
    the response audio, expression, and text in a single HTTP response.

    The device must have an active EmbodimentSession. DBS looks up the session
    by device slug to find the am_session_id for the AgentManager call.
    """
    result = await db.execute(select(Device).where(Device.slug == slug))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device '{slug}' not found")

    # Find active session on device
    from app.services import embodiment_manager as em_svc
    active_session = await em_svc.get_active_session_on_device(db, device.device_id)
    if not active_session:
        raise HTTPException(
            409,
            f"No active embodiment session on device '{slug}'. "
            "Create an embodiment session before uploading audio.",
        )
    if not active_session.am_session_id:
        raise HTTPException(409, "Active session has no AgentManager session")

    # Read WAV bytes
    wav_bytes = await audio.read()
    if not wav_bytes:
        raise HTTPException(422, "Empty audio file")

    from app.services.stream_loop import process_utterance

    try:
        result_data = await process_utterance(
            am_session_id=active_session.am_session_id,
            wav_bytes=wav_bytes,
            voice="glados",
        )
    except Exception as exc:
        raise HTTPException(502, f"Pipeline error: {exc}")

    # If the response has an expression, push it to the device
    if result_data.get("expression") and device.embodiment_manifest_json:
        em_dict = json.loads(device.embodiment_manifest_json)
        avatar_type = em_dict.get("avatar", {}).get("type", "none")
        if avatar_type != "none":
            try:
                connection = json.loads(device.connection_json or "{}")
                adapter = get_adapter(device.protocol, device.host, connection)
                await adapter.push_expression(result_data["expression"])
            except Exception:
                pass

    return AudioUploadResult(
        transcript=result_data["transcript"],
        audio_b64=result_data["audio_b64"],
        expression=result_data.get("expression"),
        text=result_data.get("response_text"),
    )
