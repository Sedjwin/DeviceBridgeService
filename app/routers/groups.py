"""Device group registry — CRUD for groups and group membership."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_admin_principal
from app.database import get_db
from app.models import Device, DeviceGroup, DeviceGroupMember
from app.schemas import (
    GroupCreate, GroupListItem, GroupMemberAdd, GroupMemberOut, GroupOut, GroupUpdate,
)

router = APIRouter(prefix="/api/groups", tags=["groups"])
logger = logging.getLogger(__name__)

VALID_MEMBER_ROLES = {"primary", "aux_speaker", "aux_display", "sensor", "input_terminal"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _member_out(m: DeviceGroupMember) -> GroupMemberOut:
    device = m.device
    return GroupMemberOut(
        id=m.id,
        group_id=m.group_id,
        device_id=m.device_id,
        device_name=device.name if device else "",
        device_slug=device.slug if device else "",
        role=m.role,
    )


def _group_out(g: DeviceGroup) -> GroupOut:
    return GroupOut(
        group_id=g.group_id,
        name=g.name,
        slug=g.slug,
        default_agent_id=g.default_agent_id,
        notes=g.notes,
        enabled=g.enabled,
        created_at=g.created_at,
        updated_at=g.updated_at,
        members=[_member_out(m) for m in (g.memberships or [])],
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[GroupListItem])
async def list_groups(db: AsyncSession = Depends(get_db)):
    """List all device groups."""
    result = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.memberships))
        .order_by(DeviceGroup.created_at.desc())
    )
    groups = result.scalars().all()
    return [
        GroupListItem(
            group_id=g.group_id,
            name=g.name,
            slug=g.slug,
            default_agent_id=g.default_agent_id,
            enabled=g.enabled,
            member_count=len(g.memberships or []),
            created_at=g.created_at,
        )
        for g in groups
    ]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupCreate,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Create a new device group."""
    existing = await db.execute(select(DeviceGroup).where(DeviceGroup.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"A group with slug '{body.slug}' already exists")

    group = DeviceGroup(
        name=body.name,
        slug=body.slug,
        default_agent_id=body.default_agent_id,
        notes=body.notes,
        enabled=True,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)

    result = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMember.device))
        .where(DeviceGroup.group_id == group.group_id)
    )
    group = result.scalar_one()
    logger.info("DBS: created group '%s' (slug=%s)", group.name, group.slug)
    return _group_out(group)


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(group_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single group including all member devices."""
    result = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMember.device))
        .where(DeviceGroup.group_id == group_id)
    )
    g = result.scalar_one_or_none()
    if not g:
        raise HTTPException(404, "Group not found")
    return _group_out(g)


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: str,
    body: GroupUpdate,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Update group name, slug, default_agent_id, notes, or enabled state."""
    result = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMember.device))
        .where(DeviceGroup.group_id == group_id)
    )
    g = result.scalar_one_or_none()
    if not g:
        raise HTTPException(404, "Group not found")

    if body.name is not None:
        g.name = body.name
    if body.slug is not None:
        # Check new slug uniqueness
        clash = await db.execute(
            select(DeviceGroup).where(DeviceGroup.slug == body.slug, DeviceGroup.group_id != group_id)
        )
        if clash.scalar_one_or_none():
            raise HTTPException(409, f"A group with slug '{body.slug}' already exists")
        g.slug = body.slug
    if body.default_agent_id is not None:
        g.default_agent_id = body.default_agent_id
    if body.notes is not None:
        g.notes = body.notes
    if body.enabled is not None:
        g.enabled = body.enabled

    await db.commit()

    result2 = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMember.device))
        .where(DeviceGroup.group_id == group_id)
    )
    return _group_out(result2.scalar_one())


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Delete a group (cascade removes memberships; does NOT delete devices)."""
    g = await db.get(DeviceGroup, group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    await db.delete(g)
    await db.commit()
    logger.info("DBS: deleted group '%s' (%s)", g.name, g.slug)


# ── Membership ─────────────────────────────────────────────────────────────────

@router.post("/{group_id}/devices", response_model=GroupMemberOut, status_code=201)
async def add_device_to_group(
    group_id: str,
    body: GroupMemberAdd,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Add a device to a group with a role."""
    if body.role not in VALID_MEMBER_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(VALID_MEMBER_ROLES)}")

    g = await db.get(DeviceGroup, group_id)
    if not g:
        raise HTTPException(404, "Group not found")

    device = await db.get(Device, body.device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    # Check for duplicate membership in same role
    existing = await db.execute(
        select(DeviceGroupMember).where(
            DeviceGroupMember.group_id == group_id,
            DeviceGroupMember.device_id == body.device_id,
            DeviceGroupMember.role == body.role,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Device is already in this group with role '{body.role}'")

    member = DeviceGroupMember(
        group_id=group_id,
        device_id=body.device_id,
        role=body.role,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)

    # Eager-load device for response
    result = await db.execute(
        select(DeviceGroupMember)
        .options(selectinload(DeviceGroupMember.device))
        .where(DeviceGroupMember.id == member.id)
    )
    member = result.scalar_one()
    logger.info(
        "DBS: added device '%s' to group '%s' as %s", device.slug, g.slug, body.role
    )
    return _member_out(member)


@router.delete("/{group_id}/devices/{device_id}", status_code=204)
async def remove_device_from_group(
    group_id: str,
    device_id: str,
    principal: dict = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
):
    """Remove a device from a group (all roles)."""
    result = await db.execute(
        select(DeviceGroupMember).where(
            DeviceGroupMember.group_id == group_id,
            DeviceGroupMember.device_id == device_id,
        )
    )
    members = result.scalars().all()
    if not members:
        raise HTTPException(404, "Device is not a member of this group")

    for m in members:
        await db.delete(m)
    await db.commit()
    logger.info("DBS: removed device %s from group %s", device_id, group_id)
