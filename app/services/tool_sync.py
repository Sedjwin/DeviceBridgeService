"""Sync device capabilities into ToolGateway as HTTP tools."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, DeviceCapability

logger = logging.getLogger(__name__)


def _tool_name(device_slug: str, capability_name: str) -> str:
    return f"device.{device_slug}.{capability_name}"


def _skill_md(device_name: str, device_slug: str, cap: dict[str, Any]) -> str:
    """Generate the skill_md markdown that gets injected into the agent's context."""
    tool_name = _tool_name(device_slug, cap["name"])
    params = cap.get("parameters", {})

    param_lines = []
    for pname, pdef in params.items():
        req  = " *(required)*" if pdef.get("required") else ""
        desc = pdef.get("description", "")
        default = f" — default `{pdef['default']}`" if "default" in pdef else ""
        param_lines.append(f"- `{pname}` ({pdef.get('type','any')}){req}: {desc}{default}")

    params_section = "\n".join(param_lines) if param_lines else "_No parameters._"

    return f"""## {tool_name}

**Device:** {device_name}
**Capability:** {cap['name']}

{cap.get('description', '')}

**Parameters:**
{params_section}

**Usage example:**
```json
{json.dumps({p: f"<{d.get('type','value')}>" for p, d in params.items()}, indent=2)}
```
"""


async def sync_device_to_toolgateway(
    db: AsyncSession,
    device: Device,
) -> tuple[int, int]:
    """
    Register/update all capabilities of a device in ToolGateway.
    Returns (synced_count, failed_count).
    """
    key = settings.toolgateway_service_key
    if not key:
        logger.warning("DBS: toolgateway_service_key not set — skipping tool sync for %s", device.slug)
        return 0, 0

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    synced = 0
    failed = 0

    result = await db.execute(
        select(DeviceCapability).where(DeviceCapability.device_id == device.device_id)
    )
    caps = result.scalars().all()

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Fetch existing tools so we can upsert
        try:
            r = await client.get(f"{settings.toolgateway_url}/api/tools", headers=headers)
            existing = {t["name"]: t for t in (r.json() if r.status_code == 200 else [])}
        except Exception as exc:
            logger.warning("DBS: could not fetch TG tools: %s", exc)
            existing = {}

        manifest = json.loads(device.manifest_json) if device.manifest_json else {}
        cap_defs = {c["name"]: c for c in manifest.get("capabilities", [])}

        for cap in caps:
            tool_name = _tool_name(device.slug, cap.name)
            cap_def   = cap_defs.get(cap.name, {"name": cap.name, "description": cap.description, "parameters": {}})

            payload = {
                "name":         tool_name,
                "description":  cap.description or cap_def.get("description", ""),
                "category":     "custom_local",
                "kind":         "http",
                "endpoint_url": f"http://{settings.host}:{settings.port}/api/execute/{device.slug}/{cap.name}",
                "method":       "POST",
                "state":        "active",
                "enabled":      True,
                "capabilities": ["network_access"],
                "metadata": {
                    "device_id":        device.device_id,
                    "device_name":      device.name,
                    "device_slug":      device.slug,
                    "device_protocol":  device.protocol,
                    "capability":       cap.name,
                    "auto_registered":  True,
                },
                "skill_md": _skill_md(device.name, device.slug, cap_def),
            }

            try:
                if tool_name in existing:
                    # Update existing tool
                    tool_id = existing[tool_name]["tool_id"]
                    r = await client.patch(
                        f"{settings.toolgateway_url}/api/tools/{tool_id}",
                        json={k: v for k, v in payload.items() if k != "name"},
                        headers=headers,
                    )
                    tg_tool_id = tool_id
                else:
                    # Create new tool
                    r = await client.post(
                        f"{settings.toolgateway_url}/api/tools",
                        json=payload,
                        headers=headers,
                    )
                    tg_tool_id = r.json().get("tool_id") if r.status_code == 201 else None

                if r.status_code in (200, 201):
                    cap.tg_tool_id   = tg_tool_id
                    cap.tg_tool_name = tool_name
                    cap.synced_at    = datetime.now(timezone.utc).replace(tzinfo=None)
                    synced += 1
                    logger.info("DBS: synced tool %s (HTTP %d)", tool_name, r.status_code)
                else:
                    logger.warning("DBS: failed to sync %s — TG returned %d: %s", tool_name, r.status_code, r.text[:200])
                    failed += 1

            except Exception as exc:
                logger.warning("DBS: exception syncing %s: %s", tool_name, exc)
                failed += 1

    await db.commit()
    return synced, failed


async def retire_device_tools(device: Device) -> int:
    """Retire all ToolGateway tools belonging to a device (on device deletion)."""
    key = settings.toolgateway_service_key
    if not key:
        return 0

    headers = {"Authorization": f"Bearer {key}"}
    retired = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for cap in device.capabilities:
            if not cap.tg_tool_id:
                continue
            try:
                r = await client.delete(
                    f"{settings.toolgateway_url}/api/tools/{cap.tg_tool_id}",
                    headers=headers,
                )
                if r.status_code in (200, 204):
                    retired += 1
            except Exception as exc:
                logger.warning("DBS: failed to retire tool %s: %s", cap.tg_tool_name, exc)

    return retired
