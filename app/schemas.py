from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Manifest Sub-schemas ───────────────────────────────────────────────────────

VALID_AUDIO_INPUT_TRANSPORTS = {
    "websocket_stream", "http_upload", "wake_word", "push_to_talk", "button", None
}
VALID_AUDIO_OUTPUT_TRANSPORTS = {"websocket_stream", "http_push", "url_pull", None}
VALID_AVATAR_TYPES = {"variable_render", "simple_sprite", "agent_controlled", "none"}
VALID_DISPLAY_TYPES = {"tft", "oled", "epaper", "rgb_matrix", "none"}


class AudioInputManifest(BaseModel):
    transport: str | None = None
    wake_word: str | None = None
    silence_timeout_ms: int = 2000
    sample_rate: int = 16000
    format: str = "pcm_s16le"
    configurable: list[str] = []

    @model_validator(mode="after")
    def _check_transport(self) -> "AudioInputManifest":
        if self.transport not in VALID_AUDIO_INPUT_TRANSPORTS:
            raise ValueError(
                f"audio_input.transport must be one of {sorted(t for t in VALID_AUDIO_INPUT_TRANSPORTS if t)!r} or null"
            )
        return self


class AudioOutputManifest(BaseModel):
    transport: str | None = None
    sample_rate: int = 22050

    @model_validator(mode="after")
    def _check_transport(self) -> "AudioOutputManifest":
        if self.transport not in VALID_AUDIO_OUTPUT_TRANSPORTS:
            raise ValueError(
                f"audio_output.transport must be one of {sorted(t for t in VALID_AUDIO_OUTPUT_TRANSPORTS if t)!r} or null"
            )
        return self


class AvatarManifest(BaseModel):
    type: str = "none"
    expression_states: list[str] = []

    @model_validator(mode="after")
    def _check_type(self) -> "AvatarManifest":
        if self.type not in VALID_AVATAR_TYPES:
            raise ValueError(f"avatar.type must be one of {sorted(VALID_AVATAR_TYPES)!r}")
        return self


class DisplayManifest(BaseModel):
    width: int = 0
    height: int = 0
    type: str = "none"

    @model_validator(mode="after")
    def _check_type(self) -> "DisplayManifest":
        if self.type not in VALID_DISPLAY_TYPES:
            raise ValueError(f"display.type must be one of {sorted(VALID_DISPLAY_TYPES)!r}")
        return self


class CameraManifest(BaseModel):
    supported: bool = False


class DeviceManifest(BaseModel):
    """
    Standardised capability manifest stored in Device.manifest_json.
    All fields optional — omitted means unsupported.
    """
    audio_input: AudioInputManifest | None = None
    audio_output: AudioOutputManifest | None = None
    avatar: AvatarManifest | None = None
    display: DisplayManifest | None = None
    camera: CameraManifest | None = None
    settings_writable: list[str] = []


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
    # Structured embodiment manifest (optional at registration; validated if provided)
    embodiment_manifest: dict[str, Any] | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    connection: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    input: dict[str, Any] | None = None
    notes: str | None = None
    enabled: bool | None = None
    embodiment_manifest: dict[str, Any] | None = None


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
    embodiment_manifest: dict[str, Any] | None
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


# ── Device Groups ──────────────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    name: str
    slug: str
    default_agent_id: str | None = None
    notes: str = ""


class GroupUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    default_agent_id: str | None = None
    notes: str | None = None
    enabled: bool | None = None


class GroupMemberAdd(BaseModel):
    device_id: str
    role: str = "primary"
    # role: "primary" | "aux_speaker" | "aux_display" | "sensor" | "input_terminal"


class GroupMemberOut(BaseModel):
    id: int
    group_id: str
    device_id: str
    device_name: str = ""
    device_slug: str = ""
    role: str

    model_config = {"from_attributes": True}


class GroupOut(BaseModel):
    group_id: str
    name: str
    slug: str
    default_agent_id: Optional[str]
    notes: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    members: list[GroupMemberOut] = []

    model_config = {"from_attributes": True}


class GroupListItem(BaseModel):
    group_id: str
    name: str
    slug: str
    default_agent_id: Optional[str]
    enabled: bool
    member_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Embodiment Sessions ────────────────────────────────────────────────────────

class EmbodimentSessionCreate(BaseModel):
    agent_id: str
    am_session_id: str | None = None
    device_id: str | None = None    # specific device OR
    group_id: str | None = None     # group (DBS picks primary device)
    z_index: int = 0
    permission_plan: str = "active"  # "active" | "ambient" | "timeout"
    timeout_seconds: int | None = None  # required if permission_plan = "timeout"

    @model_validator(mode="after")
    def _check_target(self) -> "EmbodimentSessionCreate":
        if not self.device_id and not self.group_id:
            raise ValueError("Either device_id or group_id must be provided")
        if self.permission_plan not in ("active", "ambient", "timeout"):
            raise ValueError("permission_plan must be 'active', 'ambient', or 'timeout'")
        if self.permission_plan == "timeout" and not self.timeout_seconds:
            raise ValueError("timeout_seconds is required when permission_plan='timeout'")
        return self


class EmbodimentSessionDeviceOut(BaseModel):
    id: int
    session_id: str
    device_id: str
    device_name: str = ""
    device_slug: str = ""
    role: str
    connected_at: datetime
    disconnected_at: Optional[datetime]
    is_active: bool

    model_config = {"from_attributes": True}


class EmbodimentSessionOut(BaseModel):
    session_id: str
    agent_id: str
    am_session_id: Optional[str]
    primary_device_id: Optional[str]
    primary_device_name: str = ""
    primary_device_slug: str = ""
    z_index: int
    permission_plan: str
    expires_at: Optional[datetime]
    state: str
    group_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    released_at: Optional[datetime]
    devices: list[EmbodimentSessionDeviceOut] = []

    model_config = {"from_attributes": True}


class ReEmbodyRequest(BaseModel):
    device_id: str
    release_previous: bool = True  # if False, previous becomes aux_display


class AuxConnectRequest(BaseModel):
    device_id: str
    role: str = "aux_speaker"
    # role: "aux_speaker" | "aux_display" | "sensor_feed" | "input_terminal"


class SpeakEmbodimentRequest(BaseModel):
    text: str
    voice: str = "glados"
    expression: str | None = None
    aux_device_ids: list[str] = []


class ShowAvatarRequest(BaseModel):
    expression: str
    avatar_id: str | None = None  # reserved for future; ignored for now


class ShowImageRequest(BaseModel):
    image_b64: str
    caption: str | None = None


class ShowTextRequest(BaseModel):
    text: str
    color: str = "#FFFFFF"
    scroll: bool = False


class ConfigureRequest(BaseModel):
    settings: dict[str, Any]


# ── Device Events ──────────────────────────────────────────────────────────────

class DeviceEventCreate(BaseModel):
    event_type: str  # "wake_word" | "button_press" | "motion" | "custom"
    payload: dict[str, Any] = {}


class DeviceEventOut(BaseModel):
    id: int
    device_slug: str
    device_id: str
    event_type: str
    payload_json: str
    session_id: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Audio Upload ───────────────────────────────────────────────────────────────

class AudioUploadResult(BaseModel):
    transcript: str
    audio_b64: str
    expression: str | None = None
    text: str | None = None


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
    active_embodiment_sessions: int
    executions_today: int
    executions_7d: int
    failed_7d: int
