from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models import Device
from app.schemas import DeviceCapabilities, DeviceOut, DeviceUpsert, MappingOut, MappingUpsert
from app.services import store
from app.services.device_hub import hub

router = APIRouter(prefix="/api", tags=["devices"])


@router.get("/devices", response_model=list[DeviceOut])
async def get_devices(db: AsyncSession = Depends(get_db)) -> list[DeviceOut]:
    rows = await store.list_devices(db)
    out: list[DeviceOut] = []
    for row in rows:
        out.append(
            DeviceOut(
                device_id=row.device_id,
                name=row.name,
                model=row.model,
                firmware_version=row.firmware_version,
                online=hub.is_online(row.device_id),
                capabilities=row.capabilities_json
                and DeviceCapabilities.model_validate(store.device_capabilities_dict(row))
                or DeviceCapabilities(),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return out


@router.put("/devices/{device_id}/capabilities", response_model=DeviceOut)
async def put_device_capabilities(device_id: str, payload: DeviceUpsert, db: AsyncSession = Depends(get_db)) -> DeviceOut:
    row = await store.upsert_device(
        db,
        device_id=device_id,
        name=payload.name,
        model=payload.model,
        firmware_version=payload.firmware_version,
        api_key=payload.api_key,
        capabilities=payload.capabilities,
    )
    return DeviceOut(
        device_id=row.device_id,
        name=row.name,
        model=row.model,
        firmware_version=row.firmware_version,
        online=hub.is_online(row.device_id),
        capabilities=payload.capabilities,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.put("/devices/{device_id}/mappings", response_model=MappingOut)
async def put_device_mapping(device_id: str, payload: MappingUpsert, db: AsyncSession = Depends(get_db)) -> MappingOut:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    row = await store.get_or_create_mapping(db, agent_id=payload.agent_id, device_id=device_id)
    row = await store.save_mapping(
        db,
        mapping=row,
        preferred_render_mode=payload.preferred_render_mode,
        emotion_map=payload.emotion_map,
        action_map=payload.action_map,
    )
    emotion_map, action_map = store.parse_mapping(row)
    return MappingOut(
        agent_id=row.agent_id,
        device_id=row.device_id,
        preferred_render_mode=row.preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )


@router.get("/devices/{device_id}/mappings/{agent_id}", response_model=MappingOut)
async def get_device_mapping(device_id: str, agent_id: str, db: AsyncSession = Depends(get_db)) -> MappingOut:
    row = await store.get_or_create_mapping(db, agent_id=agent_id, device_id=device_id)
    emotion_map, action_map = store.parse_mapping(row)
    return MappingOut(
        agent_id=row.agent_id,
        device_id=row.device_id,
        preferred_render_mode=row.preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )
