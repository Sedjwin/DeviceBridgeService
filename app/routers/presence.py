"""Presence management — assigns agent sessions to devices."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_admin_principal
from app.database import get_db
from app.models import Device, DevicePresence
from app.schemas import PresenceCreate, PresenceOut, PresenceTransfer
from app.services.presence_manager import presence_manager

router = APIRouter(prefix="/api/presence", tags=["presence"])
logger = logging.getLogger(__name__)


def _presence_out(p: DevicePresence, device_name: str = "") -> PresenceOut:
    return PresenceOut(
        id=p.id,
        session_id=p.session_id,
        device_id=p.device_id,
        agent_id=p.agent_id,
        is_active=p.is_active,
        acquired_at=p.acquired_at,
        released_at=p.released_at,
        device_name=device_name,
    )


@router.get("", response_model=list[PresenceOut])
async def list_presences(db: AsyncSession = Depends(get_db)):
    """List all currently active presences."""
    result = await db.execute(
        select(DevicePresence, Device.name)
        .join(Device, DevicePresence.device_id == Device.device_id)
        .where(DevicePresence.is_active == True)  # noqa: E712
        .order_by(DevicePresence.acquired_at.desc())
    )
    return [_presence_out(p, name) for p, name in result.all()]


@router.post("", response_model=PresenceOut, status_code=201)
async def acquire_presence(
    body: PresenceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Assign an agent session to a device."""
    device = await db.get(Device, body.device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    # Release any existing presence for this session
    old_device_id = presence_manager.release(body.session_id)
    if old_device_id:
        old_result = await db.execute(
            select(DevicePresence).where(
                DevicePresence.session_id == body.session_id,
                DevicePresence.is_active == True,  # noqa: E712
            )
        )
        old_p = old_result.scalar_one_or_none()
        if old_p:
            old_p.is_active   = False
            old_p.released_at = datetime.now(timezone.utc).replace(tzinfo=None)

    presence_manager.acquire(body.session_id, body.device_id, body.agent_id)

    p = DevicePresence(
        session_id=body.session_id,
        device_id=body.device_id,
        agent_id=body.agent_id,
        is_active=True,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.info("DBS: session %s → device %s (%s)", body.session_id, device.name, device.slug)
    return _presence_out(p, device.name)


@router.get("/{session_id}", response_model=PresenceOut)
async def get_presence(session_id: str, db: AsyncSession = Depends(get_db)):
    entry = presence_manager.get(session_id)
    if not entry:
        raise HTTPException(404, "No active presence for this session")
    result = await db.execute(
        select(DevicePresence, Device.name)
        .join(Device, DevicePresence.device_id == Device.device_id)
        .where(DevicePresence.session_id == session_id, DevicePresence.is_active == True)  # noqa: E712
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "Presence record not found")
    return _presence_out(row[0], row[1])


@router.delete("/{session_id}", status_code=204)
async def release_presence(session_id: str, db: AsyncSession = Depends(get_db)):
    presence_manager.release(session_id)
    result = await db.execute(
        select(DevicePresence).where(
            DevicePresence.session_id == session_id,
            DevicePresence.is_active == True,  # noqa: E712
        )
    )
    p = result.scalar_one_or_none()
    if p:
        p.is_active   = False
        p.released_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()


@router.post("/{session_id}/transfer", response_model=PresenceOut)
async def transfer_presence(
    session_id: str,
    body: PresenceTransfer,
    db: AsyncSession = Depends(get_db),
):
    """Move a session's presence to a different device."""
    new_device = await db.get(Device, body.new_device_id)
    if not new_device:
        raise HTTPException(404, "Target device not found")

    presence_manager.transfer(session_id, body.new_device_id)

    # Close old DB record
    old_result = await db.execute(
        select(DevicePresence).where(
            DevicePresence.session_id == session_id,
            DevicePresence.is_active == True,  # noqa: E712
        )
    )
    old_p = old_result.scalar_one_or_none()
    agent_id = old_p.agent_id if old_p else ""
    if old_p:
        old_p.is_active   = False
        old_p.released_at = datetime.now(timezone.utc).replace(tzinfo=None)

    new_p = DevicePresence(
        session_id=session_id,
        device_id=body.new_device_id,
        agent_id=agent_id,
        is_active=True,
    )
    db.add(new_p)
    await db.commit()
    await db.refresh(new_p)
    logger.info("DBS: transferred session %s → device %s", session_id, new_device.name)
    return _presence_out(new_p, new_device.name)
