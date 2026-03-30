"""Audio bridge endpoints — speak/listen on a device."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_admin_principal
from app.database import get_db
from app.models import Device
from app.adapters.registry import get_adapter
from app.schemas import SpeakRequest, ListenRequest, ListenResult
from app.services.audio_router import speak_on_device, listen_on_device

router = APIRouter(prefix="/api/devices", tags=["audio"])
logger = logging.getLogger(__name__)


@router.post("/{device_id}/audio/speak")
async def speak(
    device_id: str,
    body: SpeakRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Synthesise speech via VoiceService and route it to a device.
    The device must support audio output (has audio config with speaker).
    """
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    audio_config = json.loads(device.audio_json) if device.audio_json else None
    if not audio_config or not audio_config.get("has_speaker"):
        raise HTTPException(422, f"Device '{device.name}' does not have audio output capability")

    connection = json.loads(device.connection_json or "{}")
    adapter    = get_adapter(device.protocol, device.host, connection)

    try:
        result = await speak_on_device(
            adapter, body.text, body.voice, session_id=body.session_id
        )
        return result
    except NotImplementedError:
        raise HTTPException(422, f"Protocol '{device.protocol}' does not support audio output")
    except Exception as exc:
        logger.error("DBS audio/speak error device=%s: %s", device.slug, exc)
        raise HTTPException(500, str(exc))


@router.post("/{device_id}/audio/listen", response_model=ListenResult)
async def listen(
    device_id: str,
    body: ListenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Capture audio from a device and transcribe via VoiceService STT.
    The device must support audio input (has_mic in audio config).
    """
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    audio_config = json.loads(device.audio_json) if device.audio_json else None
    if not audio_config or not audio_config.get("has_mic"):
        raise HTTPException(422, f"Device '{device.name}' does not have audio input capability")

    connection = json.loads(device.connection_json or "{}")
    adapter    = get_adapter(device.protocol, device.host, connection)

    try:
        result = await listen_on_device(adapter, body.duration_s, session_id=body.session_id)
        return ListenResult(transcript=result["transcript"], duration_s=body.duration_s)
    except NotImplementedError:
        raise HTTPException(422, f"Protocol '{device.protocol}' does not support audio input")
    except Exception as exc:
        logger.error("DBS audio/listen error device=%s: %s", device.slug, exc)
        raise HTTPException(500, str(exc))
