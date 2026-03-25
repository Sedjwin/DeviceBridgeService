from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceCapabilities(BaseModel):
    render_modes: list[str] = Field(default_factory=lambda: ["line", "shape"])
    max_fps: int = 30
    frame_budget_ms: int = 33
    texture_kb: int = 512
    animations: list[str] = Field(default_factory=lambda: ["neutral_blink"])
    audio_methods: list[str] = Field(default_factory=lambda: ["inline", "url"])
    audio_codecs: list[str] = Field(default_factory=lambda: ["wav"])
    sample_rates: list[int] = Field(default_factory=lambda: [22050])
    preferred_sample_rate: int | None = None
    preferred_audio_method: str | None = None
    max_inline_audio_bytes: int | None = None
    stream_prebuffer_ms: int | None = None
    mic_enabled: bool = True
    mic_format: str = "pcm16"
    accepts_model_directives: bool = False
    default_agent_id: str = ""


class DeviceUpsert(BaseModel):
    name: str = "Unnamed Device"
    model: str = "unknown"
    firmware_version: str = "unknown"
    api_key: str = ""
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class DeviceOut(BaseModel):
    device_id: str
    name: str
    model: str
    firmware_version: str
    online: bool
    capabilities: DeviceCapabilities
    created_at: datetime
    updated_at: datetime


class MappingRule(BaseModel):
    animation: str
    render_mode: str | None = None
    intensity: float = 1.0
    duration_ms: int | None = None
    fallback: list[str] = Field(default_factory=lambda: ["neutral_blink"])


class MappingUpsert(BaseModel):
    agent_id: str
    preferred_render_mode: str = "line"
    emotion_map: dict[str, MappingRule] = Field(default_factory=dict)
    action_map: dict[str, MappingRule] = Field(default_factory=dict)


class MappingOut(BaseModel):
    agent_id: str
    device_id: str
    preferred_render_mode: str
    emotion_map: dict[str, MappingRule]
    action_map: dict[str, MappingRule]


class TimelineEvent(BaseModel):
    t: int = 0
    type: Literal["emotion", "action", "viseme"]
    value: str | int


class AgentOutputIn(BaseModel):
    text: str = ""
    audio_base64: str | None = None
    sample_rate: int | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    profile: dict[str, Any] | None = None
    voice_config: dict[str, Any] | None = None


class SessionStartIn(BaseModel):
    agent_id: str
    device_id: str
    upstream_session_id: str = ""
    profile: dict[str, Any] | None = None
    voice_config: dict[str, Any] | None = None


class SessionOut(BaseModel):
    session_id: str
    agent_id: str
    device_id: str
    upstream_session_id: str
    active: bool


class SessionStopOut(BaseModel):
    session_id: str
    active: bool


class AgentAudioIn(BaseModel):
    audio_base64: str
    sample_rate: int = 22050
    visemes: list[TimelineEvent] = Field(default_factory=list)


class AgentTimelineIn(BaseModel):
    timeline: list[TimelineEvent] = Field(default_factory=list)
    profile: dict[str, Any] | None = None


class DeviceHello(BaseModel):
    type: Literal["hello"]
    name: str = "Unnamed Device"
    model: str = "unknown"
    firmware_version: str = "unknown"
    api_key: str = ""
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class DeviceStatus(BaseModel):
    type: Literal["device.status"]
    fps: float = 0
    buffer_level: float = 0
    battery: float = 0
    temperature_c: float = 0
    extra: dict[str, Any] = Field(default_factory=dict)


class DeviceAck(BaseModel):
    type: Literal["ack", "nack"]
    command_id: str
    ok: bool = True
    error: str | None = None


class DeviceMicChunk(BaseModel):
    type: Literal["mic.chunk"]
    session_id: str
    audio_base64: str
    sample_rate: int = 16000


class DeviceCommand(BaseModel):
    command_id: str
    type: str
    payload: dict[str, Any]


class AgentSummary(BaseModel):
    agent_id: str
    name: str
    profile: dict[str, Any] | None = None
    voice_enabled: bool = False
    voice_config: dict[str, Any] | None = None


class MappingSuggestIn(BaseModel):
    agent_id: str
    device_id: str
    preferred_render_mode: str | None = None


class MappingSuggestOut(BaseModel):
    agent_id: str
    device_id: str
    preferred_render_mode: str
    emotion_map: dict[str, MappingRule]
    action_map: dict[str, MappingRule]
