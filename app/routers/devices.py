"""Device registry — CRUD, ping, and ToolGateway sync."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_admin_principal
from app.database import get_db
from app.models import Device, DeviceCapability
from app.adapters.registry import get_adapter
from app.schemas import (
    DeviceListItem, DeviceOut, DeviceRegister, DeviceUpdate,
    CapabilityOut, PingResult,
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
        notes=d.notes,
        enabled=d.enabled,
        last_seen=d.last_seen,
        created_at=d.created_at,
        updated_at=d.updated_at,
        capabilities=[_cap_out(c) for c in (d.capabilities or [])],
    )


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
    result = await db.execute(select(Device).order_by(Device.created_at.desc()))
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
    await db.refresh(device)

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
    await db.refresh(device)
    logger.info("DBS: registered device '%s' slug=%s synced=%d failed=%d", name, slug, synced, failed)
    return _device_out(device)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
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
    d = await db.get(Device, device_id)
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

    await db.commit()
    await db.refresh(d)
    return _device_out(d)


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Device, device_id)
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
    d = await db.get(Device, device_id)
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
    d = await db.get(Device, device_id)
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
    d = await db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")

    connection = json.loads(d.connection_json or "{}")
    adapter    = get_adapter(d.protocol, d.host, connection)

    t0 = _time.monotonic()
    try:
        result = await adapter.execute(capability_name, body)
        duration_ms = int((_time.monotonic() - t0) * 1000)
        return {"status": "ok", "capability": capability_name, "device": d.slug,
                "data": result, "duration_ms": duration_ms}
    except Exception as exc:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        raise HTTPException(500, f"Execution failed: {exc}")
