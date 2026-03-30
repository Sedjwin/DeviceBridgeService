from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Device ────────────────────────────────────────────────────────────────────

class DeviceRegister(BaseModel):
    """Register a new device. Provide either a full manifest or individual fields."""
    manifest: dict[str, Any] | None = None  # Full manifest JSON — preferred
    # -- OR individual fields --
    name: str | None = None
    slug: str | None = None
    type: str = "display"
    protocol: str = "wled"
    host: str | None = None
    connection: dict[str, Any] = {}
    display: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    input: dict[str, Any] | None = None
    notes: str = ""
    capabilities: list[dict[str, Any]] = []


class DeviceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    connection: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    input: dict[str, Any] | None = None
    notes: str | None = None
    enabled: bool | None = None


class CapabilityOut(BaseModel):
    id: int
    device_id: str
    name: str
    description: str
    parameters: dict[str, Any]
    tg_tool_id: Optional[str]
    tg_tool_name: Optional[str]
    synced_at: Optional[datetime]

    model_config = {"from_attributes": True}


class DeviceOut(BaseModel):
    device_id: str
    name: str
    slug: str
    type: str
    protocol: str
    host: str
    connection: dict[str, Any]
    status: str
    display: dict[str, Any] | None
    audio: dict[str, Any] | None
    input: dict[str, Any] | None
    notes: str
    enabled: bool
    last_seen: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    capabilities: list[CapabilityOut] = []

    model_config = {"from_attributes": True}


class DeviceListItem(BaseModel):
    device_id: str
    name: str
    slug: str
    type: str
    protocol: str
    host: str
    status: str
    enabled: bool
    capability_count: int
    synced_count: int
    last_seen: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Presence ──────────────────────────────────────────────────────────────────

class PresenceCreate(BaseModel):
    session_id: str
    device_id: str
    agent_id: str


class PresenceTransfer(BaseModel):
    new_device_id: str


class PresenceOut(BaseModel):
    id: int
    session_id: str
    device_id: str
    agent_id: str
    is_active: bool
    acquired_at: datetime
    released_at: Optional[datetime]
    device_name: str = ""

    model_config = {"from_attributes": True}


# ── Execution ─────────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    """Used by admin test console."""
    payload: dict[str, Any] = {}
    session_id: str | None = None
    agent_id: str | None = None


class ExecuteResult(BaseModel):
    status: str
    capability: str
    device: str
    data: dict[str, Any] = {}
    error: str | None = None
    duration_ms: int | None = None


# ── Execution Log ─────────────────────────────────────────────────────────────

class ExecutionLogOut(BaseModel):
    id: int
    device_id: str
    device_slug: str
    capability: str
    payload_json: str
    result_json: str
    status: str
    error: Optional[str]
    duration_ms: Optional[int]
    session_id: Optional[str]
    agent_id: Optional[str]
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Audio ─────────────────────────────────────────────────────────────────────

class SpeakRequest(BaseModel):
    text: str
    voice: str = "glados"
    session_id: str | None = None
    agent_id: str | None = None


class ListenRequest(BaseModel):
    duration_s: float = 5.0
    session_id: str | None = None


class ListenResult(BaseModel):
    transcript: str
    duration_s: float


# ── Ping ──────────────────────────────────────────────────────────────────────

class PingResult(BaseModel):
    online: bool
    latency_ms: float | None = None
    info: dict[str, Any] = {}


# ── Stats ─────────────────────────────────────────────────────────────────────

class StatsOut(BaseModel):
    total_devices: int
    online: int
    offline: int
    unknown: int
    total_capabilities: int
    synced_capabilities: int
    active_presences: int
    executions_today: int
    executions_7d: int
    failed_7d: int
