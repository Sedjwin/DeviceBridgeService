"""
Embodiment session lifecycle — creation, preemption, avatar delivery, release, re-embody.

This module is a stateless service layer (pure async functions).
All state is persisted in the database; no in-memory singletons.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    Device,
    DeviceGroup,
    DeviceGroupMember,
    EmbodimentSession,
    EmbodimentSessionDevice,
)

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fetch_agent_profile(agent_id: str) -> dict[str, Any] | None:
    """
    Fetch the agent profile from AgentManager.
    Returns the full agent dict, or None on failure.
    Expected shape: {profile: {appearance: {...}, emotions: {...}}, ...}
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.agentmanager_url}/agents/{agent_id}")
        if r.status_code == 200:
            return r.json()
        logger.warning(
            "DBS: could not fetch agent profile for %s — AM returned %d", agent_id, r.status_code
        )
    except Exception as exc:
        logger.warning("DBS: AgentManager unavailable when fetching agent %s: %s", agent_id, exc)
    return None


async def _create_am_session(agent_id: str) -> str | None:
    """
    Ask AgentManager to start a new conversation session for the agent.
    Returns the new am_session_id, or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{settings.agentmanager_url}/agents/{agent_id}/session",
                json={},
            )
        if r.status_code in (200, 201):
            data = r.json()
            return data.get("session_id") or data.get("id")
        logger.warning(
            "DBS: failed to create AM session for agent %s — AM returned %d: %s",
            agent_id, r.status_code, r.text[:200],
        )
    except Exception as exc:
        logger.warning("DBS: AgentManager unavailable when creating session for %s: %s", agent_id, exc)
    return None


async def _inject_am_system_message(am_session_id: str, text: str) -> None:
    """
    Inject a system-role message into an AgentManager conversation session.
    Used to inform the agent it has been embodied on a device.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{settings.agentmanager_url}/sessions/{am_session_id}/message",
                json={"role": "system", "content": text},
            )
    except Exception as exc:
        logger.warning("DBS: failed to inject system message into AM session %s: %s", am_session_id, exc)


async def _setup_avatar_on_device(
    device: Device,
    agent_profile: dict[str, Any] | None,
) -> None:
    """
    Dispatch character vars (appearance, emotions) to the device adapter
    based on the manifest avatar type.
    """
    if not agent_profile:
        return

    from app.adapters.registry import get_adapter

    embodiment_manifest_raw = device.embodiment_manifest_json
    if not embodiment_manifest_raw:
        return

    try:
        em = json.loads(embodiment_manifest_raw)
    except Exception:
        return

    avatar_cfg = em.get("avatar", {})
    avatar_type = avatar_cfg.get("type", "none")

    if avatar_type == "none":
        return

    if avatar_type == "agent_controlled":
        logger.warning(
            "DBS: device %s has avatar.type='agent_controlled' — not implemented, treating as none",
            device.slug,
        )
        return

    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    profile = agent_profile.get("profile", {})
    appearance = profile.get("appearance", {})
    emotions = profile.get("emotions", {})
    character_vars = {"appearance": appearance, "emotions": emotions} if profile else None

    try:
        if avatar_type == "variable_render":
            # Send full character vars — device renders agent's face
            await adapter.push_expression("neutral", character_vars=character_vars)
        elif avatar_type == "simple_sprite":
            # Just send expression string — device maps to its own artwork
            await adapter.push_expression("neutral")
    except NotImplementedError:
        logger.debug(
            "DBS: adapter for %s does not implement push_expression — skipping avatar setup",
            device.slug,
        )
    except Exception as exc:
        logger.warning("DBS: avatar setup failed for device %s: %s", device.slug, exc)


async def _call_adapter_setup(device: Device, session_id: str, agent_profile: dict | None) -> None:
    """Call adapter.setup_embodiment_session with the device's embodiment manifest."""
    from app.adapters.registry import get_adapter

    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    embodiment_manifest_raw = device.embodiment_manifest_json
    manifest_dict = json.loads(embodiment_manifest_raw) if embodiment_manifest_raw else {}

    profile = agent_profile.get("profile", {}) if agent_profile else {}
    character_vars = (
        {"appearance": profile.get("appearance", {}), "emotions": profile.get("emotions", {})}
        if profile else None
    )

    try:
        await adapter.setup_embodiment_session(session_id, manifest_dict, character_vars)
    except NotImplementedError:
        logger.debug(
            "DBS: adapter %s does not implement setup_embodiment_session", device.protocol
        )
    except Exception as exc:
        logger.warning("DBS: setup_embodiment_session failed for %s: %s", device.slug, exc)


async def _call_adapter_teardown(device: Device, session_id: str) -> None:
    """Call adapter.teardown_embodiment_session."""
    from app.adapters.registry import get_adapter

    connection = json.loads(device.connection_json or "{}")
    adapter = get_adapter(device.protocol, device.host, connection)

    try:
        await adapter.teardown_embodiment_session(session_id)
    except NotImplementedError:
        logger.debug(
            "DBS: adapter %s does not implement teardown_embodiment_session", device.protocol
        )
    except Exception as exc:
        logger.warning("DBS: teardown_embodiment_session failed for %s: %s", device.slug, exc)


# ── Active-session queries ────────────────────────────────────────────────────

async def get_active_session_on_device(
    db: AsyncSession, device_id: str
) -> EmbodimentSession | None:
    """Return the current non-released session on a device, or None."""
    result = await db.execute(
        select(EmbodimentSession)
        .join(EmbodimentSessionDevice, EmbodimentSessionDevice.session_id == EmbodimentSession.session_id)
        .where(
            EmbodimentSessionDevice.device_id == device_id,
            EmbodimentSessionDevice.is_active == True,  # noqa: E712
            EmbodimentSession.state != "released",
        )
        .order_by(EmbodimentSession.z_index.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Session release ────────────────────────────────────────────────────────────

async def release_session(
    db: AsyncSession,
    session: EmbodimentSession,
    source: str = "explicit",
) -> None:
    """
    Mark an embodiment session as released.
    Tears down the adapter on the primary device and marks all session devices inactive.
    """
    if session.state == "released":
        return

    # Tear down adapter on primary device
    if session.primary_device_id:
        primary_device = await db.get(Device, session.primary_device_id)
        if primary_device:
            await _call_adapter_teardown(primary_device, session.session_id)

    # Mark all session devices inactive
    for sd in session.devices:
        if sd.is_active:
            sd.is_active = False
            sd.disconnected_at = _now()

    session.state = "released"
    session.released_at = _now()
    await db.commit()
    logger.info(
        "DBS: released embodiment session %s (agent=%s, source=%s)",
        session.session_id, session.agent_id, source,
    )


# ── Session creation ──────────────────────────────────────────────────────────

async def create_session(
    db: AsyncSession,
    agent_id: str,
    am_session_id: str | None,
    device_id: str | None,
    group_id: str | None,
    z_index: int,
    permission_plan: str,
    timeout_seconds: int | None,
) -> EmbodimentSession:
    """
    Create a new EmbodimentSession with preemption logic.

    Preemption rules (per device):
    - No active session → create immediately.
    - Existing Z > new Z → reject with a 409 OccupiedError.
    - Existing Z ≤ new Z → release existing, create new.
    - Equal Z → new wins (latest takes over).

    Raises OccupiedError if blocked by a higher-priority session.
    Raises ValueError on bad input.
    """
    # Resolve the primary device
    primary_device: Device | None = None

    if device_id:
        primary_device = await db.get(Device, device_id)
        if not primary_device:
            raise ValueError(f"Device '{device_id}' not found")
    elif group_id:
        # Pick the "primary" role member from the group
        result = await db.execute(
            select(DeviceGroupMember)
            .options(selectinload(DeviceGroupMember.device))
            .where(
                DeviceGroupMember.group_id == group_id,
                DeviceGroupMember.role == "primary",
            )
            .limit(1)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise ValueError(
                f"Group '{group_id}' has no 'primary' role member — cannot pick primary device"
            )
        primary_device = member.device
        device_id = primary_device.device_id
    else:
        raise ValueError("Either device_id or group_id must be provided")

    # Preemption check
    existing = await get_active_session_on_device(db, primary_device.device_id)
    if existing:
        if existing.z_index > z_index:
            raise OccupiedError(
                device_id=primary_device.device_id,
                holder_agent_id=existing.agent_id,
                holder_session_id=existing.session_id,
                holder_z=existing.z_index,
            )
        # Release the existing session (new Z wins or equal Z — latest takes over)
        await db.execute(
            select(EmbodimentSession)
            .options(selectinload(EmbodimentSession.devices))
            .where(EmbodimentSession.session_id == existing.session_id)
        )
        # Reload with devices relationship
        existing_full_result = await db.execute(
            select(EmbodimentSession)
            .options(selectinload(EmbodimentSession.devices))
            .where(EmbodimentSession.session_id == existing.session_id)
        )
        existing_full = existing_full_result.scalar_one()
        await release_session(db, existing_full, source="preempted")

    # Compute expiry
    expires_at: datetime | None = None
    if permission_plan == "timeout" and timeout_seconds:
        expires_at = _now() + timedelta(seconds=timeout_seconds)

    # Resolve am_session_id: if not provided, create a new AM session
    resolved_am_session_id = am_session_id
    if not resolved_am_session_id:
        resolved_am_session_id = await _create_am_session(agent_id)

    # Create session record
    session = EmbodimentSession(
        agent_id=agent_id,
        am_session_id=resolved_am_session_id,
        primary_device_id=primary_device.device_id,
        z_index=z_index,
        permission_plan=permission_plan,
        expires_at=expires_at,
        state="streaming",
        group_id=group_id,
    )
    db.add(session)
    await db.flush()  # get session_id

    # Add primary device as session device
    session_device = EmbodimentSessionDevice(
        session_id=session.session_id,
        device_id=primary_device.device_id,
        role="primary_embodiment",
        is_active=True,
    )
    db.add(session_device)
    await db.commit()

    # Fetch agent profile for avatar delivery
    agent_profile = await _fetch_agent_profile(agent_id)

    # Setup adapter and push character vars
    await _call_adapter_setup(primary_device, session.session_id, agent_profile)
    await _setup_avatar_on_device(primary_device, agent_profile)

    # Inject system message into AgentManager session
    if resolved_am_session_id:
        await _inject_am_system_message(
            resolved_am_session_id,
            f"You are now embodied on device '{primary_device.name}' (slug: {primary_device.slug}). "
            f"Your embodiment session ID is {session.session_id}. "
            f"Use the embody.* tools to interact with this device.",
        )

    logger.info(
        "DBS: created embodiment session %s — agent=%s device=%s z=%d plan=%s",
        session.session_id, agent_id, primary_device.slug, z_index, permission_plan,
    )
    return session


# ── Re-embody ─────────────────────────────────────────────────────────────────

async def re_embody(
    db: AsyncSession,
    session: EmbodimentSession,
    new_device_id: str,
    release_previous: bool = True,
) -> EmbodimentSession:
    """
    Move a session's primary embodiment to a new device.
    If release_previous is False, the old primary becomes an aux_display.
    Runs preemption check on the new device.
    Conversation (am_session_id) is preserved — call is uninterrupted.
    """
    new_device = await db.get(Device, new_device_id)
    if not new_device:
        raise ValueError(f"Device '{new_device_id}' not found")

    old_device_id = session.primary_device_id

    # Preemption check on new device
    existing_new = await get_active_session_on_device(db, new_device_id)
    if existing_new and existing_new.session_id != session.session_id:
        if existing_new.z_index > session.z_index:
            raise OccupiedError(
                device_id=new_device_id,
                holder_agent_id=existing_new.agent_id,
                holder_session_id=existing_new.session_id,
                holder_z=existing_new.z_index,
            )
        existing_full_result = await db.execute(
            select(EmbodimentSession)
            .options(selectinload(EmbodimentSession.devices))
            .where(EmbodimentSession.session_id == existing_new.session_id)
        )
        await release_session(db, existing_full_result.scalar_one(), source="preempted")

    # Teardown old primary device
    if old_device_id:
        old_device = await db.get(Device, old_device_id)
        if old_device:
            await _call_adapter_teardown(old_device, session.session_id)

    # Update old session device record
    if old_device_id:
        old_sd_result = await db.execute(
            select(EmbodimentSessionDevice).where(
                EmbodimentSessionDevice.session_id == session.session_id,
                EmbodimentSessionDevice.device_id == old_device_id,
                EmbodimentSessionDevice.is_active == True,  # noqa: E712
            )
        )
        old_sd = old_sd_result.scalar_one_or_none()
        if old_sd:
            if release_previous:
                old_sd.is_active = False
                old_sd.disconnected_at = _now()
            else:
                old_sd.role = "aux_display"

    # Add new primary device record
    new_sd = EmbodimentSessionDevice(
        session_id=session.session_id,
        device_id=new_device_id,
        role="primary_embodiment",
        is_active=True,
    )
    db.add(new_sd)

    # Update session
    session.primary_device_id = new_device_id
    await db.commit()

    # Setup adapter and avatar on new device
    agent_profile = await _fetch_agent_profile(session.agent_id)
    await _call_adapter_setup(new_device, session.session_id, agent_profile)
    await _setup_avatar_on_device(new_device, agent_profile)

    # Inject re-embody system message
    if session.am_session_id:
        await _inject_am_system_message(
            session.am_session_id,
            f"You have moved to device '{new_device.name}' (slug: {new_device.slug}). "
            f"Your session continues — the conversation is uninterrupted.",
        )

    logger.info(
        "DBS: re-embodied session %s from %s → %s", session.session_id, old_device_id, new_device.slug
    )
    return session


# ── Aux device management ─────────────────────────────────────────────────────

async def aux_connect(
    db: AsyncSession,
    session: EmbodimentSession,
    device_id: str,
    role: str,
) -> EmbodimentSessionDevice:
    """Add an auxiliary device to an existing session."""
    device = await db.get(Device, device_id)
    if not device:
        raise ValueError(f"Device '{device_id}' not found")

    # Check it's not already active in this session
    existing_result = await db.execute(
        select(EmbodimentSessionDevice).where(
            EmbodimentSessionDevice.session_id == session.session_id,
            EmbodimentSessionDevice.device_id == device_id,
            EmbodimentSessionDevice.is_active == True,  # noqa: E712
        )
    )
    if existing_result.scalar_one_or_none():
        raise ValueError(f"Device '{device_id}' is already an active participant in this session")

    sd = EmbodimentSessionDevice(
        session_id=session.session_id,
        device_id=device_id,
        role=role,
        is_active=True,
    )
    db.add(sd)
    await db.commit()
    await db.refresh(sd)

    logger.info(
        "DBS: added aux device %s (%s) to session %s", device.slug, role, session.session_id
    )
    return sd


async def aux_disconnect(
    db: AsyncSession,
    session: EmbodimentSession,
    device_id: str,
) -> None:
    """Remove an auxiliary device from a session."""
    result = await db.execute(
        select(EmbodimentSessionDevice).where(
            EmbodimentSessionDevice.session_id == session.session_id,
            EmbodimentSessionDevice.device_id == device_id,
            EmbodimentSessionDevice.is_active == True,  # noqa: E712
        )
    )
    sd = result.scalar_one_or_none()
    if not sd:
        raise ValueError(f"Device '{device_id}' is not an active participant in this session")
    if sd.role == "primary_embodiment":
        raise ValueError("Cannot remove the primary embodiment device via aux_disconnect — use re_embody")

    sd.is_active = False
    sd.disconnected_at = _now()
    await db.commit()
    logger.info("DBS: removed aux device %s from session %s", device_id, session.session_id)


# ── Timeout sweep ─────────────────────────────────────────────────────────────

async def expire_timed_out_sessions(db: AsyncSession) -> int:
    """
    Release all EmbodimentSessions whose permission_plan='timeout' and expires_at < now().
    Returns count of sessions released.
    Called by the background task in main.py every 30 seconds.
    """
    now = _now()
    result = await db.execute(
        select(EmbodimentSession)
        .options(selectinload(EmbodimentSession.devices))
        .where(
            and_(
                EmbodimentSession.state != "released",
                EmbodimentSession.permission_plan == "timeout",
                EmbodimentSession.expires_at <= now,
            )
        )
    )
    expired = result.scalars().all()
    count = 0
    for session in expired:
        await release_session(db, session, source="timeout")
        count += 1

    if count:
        logger.info("DBS: timeout sweep released %d session(s)", count)
    return count


# ── Session state transitions ─────────────────────────────────────────────────

async def set_session_state(
    db: AsyncSession,
    session: EmbodimentSession,
    new_state: str,
) -> None:
    """Transition a session between streaming and ambient states."""
    if new_state not in ("streaming", "ambient"):
        raise ValueError(f"Invalid state transition target: '{new_state}'")
    if session.state == "released":
        raise ValueError("Cannot transition a released session")

    old_state = session.state
    session.state = new_state
    await db.commit()
    logger.info(
        "DBS: session %s transitioned %s → %s", session.session_id, old_state, new_state
    )


# ── Exception types ───────────────────────────────────────────────────────────

class OccupiedError(Exception):
    """Raised when a device is held by a higher-priority session."""
    def __init__(
        self,
        device_id: str,
        holder_agent_id: str,
        holder_session_id: str,
        holder_z: int,
    ):
        self.device_id = device_id
        self.holder_agent_id = holder_agent_id
        self.holder_session_id = holder_session_id
        self.holder_z = holder_z
        super().__init__(
            f"Device {device_id} is occupied by agent {holder_agent_id} (z={holder_z})"
        )
