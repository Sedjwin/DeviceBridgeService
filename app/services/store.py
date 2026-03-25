from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentDeviceMapping, BridgeSession, Device, SessionEvent, TelemetrySample
from app.schemas import DeviceCapabilities, MappingRule


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _json_loads(raw: str) -> dict:
    if not raw:
        return {}
    return json.loads(raw)


async def upsert_device(
    session: AsyncSession,
    *,
    device_id: str,
    name: str,
    model: str,
    firmware_version: str,
    api_key: str,
    capabilities: DeviceCapabilities,
) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        device = Device(device_id=device_id)
        session.add(device)
    device.name = name
    device.model = model
    device.firmware_version = firmware_version
    if api_key:
        device.api_key = api_key
    device.capabilities_json = _json_dumps(capabilities.model_dump())
    device.updated_at = utc_now()
    await session.commit()
    await session.refresh(device)
    return device


async def list_devices(session: AsyncSession) -> list[Device]:
    rows = await session.execute(select(Device).order_by(Device.created_at.asc()))
    return list(rows.scalars().all())


def device_capabilities_dict(device: Device) -> dict:
    return _json_loads(device.capabilities_json)


async def get_or_create_mapping(session: AsyncSession, *, agent_id: str, device_id: str) -> AgentDeviceMapping:
    stmt = select(AgentDeviceMapping).where(
        AgentDeviceMapping.agent_id == agent_id,
        AgentDeviceMapping.device_id == device_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        return row
    row = AgentDeviceMapping(agent_id=agent_id, device_id=device_id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def parse_mapping(mapping: AgentDeviceMapping) -> tuple[dict[str, MappingRule], dict[str, MappingRule]]:
    emotion_raw = _json_loads(mapping.emotion_map_json)
    action_raw = _json_loads(mapping.action_map_json)
    emotion_map = {k: MappingRule.model_validate(v) for k, v in emotion_raw.items()}
    action_map = {k: MappingRule.model_validate(v) for k, v in action_raw.items()}
    return emotion_map, action_map


async def save_mapping(
    session: AsyncSession,
    *,
    mapping: AgentDeviceMapping,
    preferred_render_mode: str,
    emotion_map: dict[str, MappingRule],
    action_map: dict[str, MappingRule],
) -> AgentDeviceMapping:
    mapping.preferred_render_mode = preferred_render_mode
    mapping.emotion_map_json = _json_dumps({k: v.model_dump() for k, v in emotion_map.items()})
    mapping.action_map_json = _json_dumps({k: v.model_dump() for k, v in action_map.items()})
    mapping.updated_at = utc_now()
    await session.commit()
    await session.refresh(mapping)
    return mapping


async def create_bridge_session(
    session: AsyncSession,
    *,
    agent_id: str,
    device_id: str,
    upstream_session_id: str,
) -> BridgeSession:
    row = BridgeSession(agent_id=agent_id, device_id=device_id, upstream_session_id=upstream_session_id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def end_bridge_session(session: AsyncSession, *, session_id: str) -> BridgeSession | None:
    row = await session.get(BridgeSession, session_id)
    if row is None:
        return None
    row.active = False
    row.ended_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


async def get_bridge_session(session: AsyncSession, *, session_id: str) -> BridgeSession | None:
    return await session.get(BridgeSession, session_id)


async def list_bridge_sessions(
    session: AsyncSession,
    *,
    device_id: str | None = None,
    limit: int = 100,
) -> list[BridgeSession]:
    stmt = select(BridgeSession).order_by(BridgeSession.started_at.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(BridgeSession.device_id == device_id)
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def add_session_event(session: AsyncSession, *, session_id: str, event_type: str, payload: dict) -> SessionEvent:
    row = SessionEvent(session_id=session_id, event_type=event_type, payload_json=_json_dumps(payload))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_session_events(session: AsyncSession, *, session_id: str, limit: int = 200) -> list[SessionEvent]:
    stmt = (
        select(SessionEvent)
        .where(SessionEvent.session_id == session_id)
        .order_by(SessionEvent.created_at.asc())
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def add_telemetry(session: AsyncSession, *, device_id: str, payload: dict) -> TelemetrySample:
    row = TelemetrySample(
        device_id=device_id,
        fps=float(payload.get("fps", 0)),
        buffer_level=float(payload.get("buffer_level", 0)),
        battery=float(payload.get("battery", 0)),
        temperature_c=float(payload.get("temperature_c", 0)),
        raw_json=_json_dumps(payload),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def parse_event_payload(raw_json: str) -> dict:
    return _json_loads(raw_json)
