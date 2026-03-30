"""
Execution endpoint — called by ToolGateway when an agent uses a device tool.

POST /api/execute/{slug}/{capability}
  No auth required: ToolGateway is the auth layer.
  Body: the agent's tool call payload (passed through directly from TG).
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Device, DeviceCapability, DeviceExecutionLog
from app.adapters.registry import get_adapter

router = APIRouter(tags=["execute"])
logger = logging.getLogger(__name__)


@router.post("/api/execute/{slug}/{capability_name}")
async def execute_capability(
    slug: str,
    capability_name: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
):
    t0 = time.monotonic()

    # Resolve device
    result = await db.execute(select(Device).where(Device.slug == slug))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"No device with slug '{slug}'")
    if not device.enabled:
        raise HTTPException(503, f"Device '{slug}' is disabled")

    # Resolve capability
    cap_result = await db.execute(
        select(DeviceCapability).where(
            DeviceCapability.device_id == device.device_id,
            DeviceCapability.name == capability_name,
        )
    )
    cap = cap_result.scalar_one_or_none()
    if not cap:
        raise HTTPException(404, f"Capability '{capability_name}' not found on device '{slug}'")

    # Execute
    connection = json.loads(device.connection_json or "{}")
    adapter    = get_adapter(device.protocol, device.host, connection)

    session_id = payload.pop("_session_id", None)
    agent_id   = payload.pop("_agent_id", None)

    try:
        data = await adapter.execute(capability_name, payload)
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "ok"
        error  = None
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "failed"
        error  = str(exc)
        data   = {}
        logger.error("DBS execute error device=%s cap=%s: %s", slug, capability_name, exc)

    # Audit log
    log = DeviceExecutionLog(
        device_id=device.device_id,
        device_slug=slug,
        capability=capability_name,
        payload_json=json.dumps(payload),
        result_json=json.dumps(data),
        status=status,
        error=error,
        duration_ms=duration_ms,
        session_id=session_id,
        agent_id=agent_id,
        source="tool_gateway",
    )
    db.add(log)
    await db.commit()

    if status == "failed":
        raise HTTPException(500, f"Device execution failed: {error}")

    return {"status": "ok", "capability": capability_name, "device": slug,
            "data": data, "duration_ms": duration_ms}
