"""SQLAlchemy models for DeviceBridgeService."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Device(Base):
    """A registered physical or virtual device."""
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # type: display | speaker | controller | composite
    type: Mapped[str] = mapped_column(String, nullable=False, default="display")
    # protocol: wled | http_rest | pi_bridge | websocket | esp_http | esp_ws
    protocol: Mapped[str] = mapped_column(String, nullable=False, default="wled")
    host: Mapped[str] = mapped_column(String, nullable=False)
    connection_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # status: online | offline | unknown
    status: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Structured embodiment manifest (DeviceManifest schema) stored as JSON
    embodiment_manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    capabilities = relationship("DeviceCapability", back_populates="device", cascade="all, delete-orphan")
    presences = relationship("DevicePresence", back_populates="device", cascade="all, delete-orphan")
    logs = relationship("DeviceExecutionLog", back_populates="device", cascade="all, delete-orphan")
    events = relationship("DeviceEvent", back_populates="device", cascade="all, delete-orphan")
    group_memberships = relationship("DeviceGroupMember", back_populates="device", cascade="all, delete-orphan")
    embodiment_session_devices = relationship("EmbodimentSessionDevice", back_populates="device")


class DeviceCapability(Base):
    """A single callable capability on a device, mapped to a ToolGateway tool."""
    __tablename__ = "device_capabilities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tg_tool_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tg_tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    device = relationship("Device", back_populates="capabilities")


class DevicePresence(Base):
    """Legacy presence model — tracks which device an agent session is routed through.
    Superseded by EmbodimentSession for new-style sessions; kept for backwards compatibility."""
    __tablename__ = "device_presences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    device = relationship("Device", back_populates="presences")


class DeviceExecutionLog(Base):
    """Audit log for every command executed on a device."""
    __tablename__ = "device_execution_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False)
    device_slug: Mapped[str] = mapped_column(String, nullable=False)
    capability: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # status: ok | failed
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # source: tool_gateway | admin_test | audio_bridge | internal | embodiment
    source: Mapped[str] = mapped_column(String, nullable=False, default="tool_gateway")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    device = relationship("Device", back_populates="logs")


# ── Device Groups ──────────────────────────────────────────────────────────────

class DeviceGroup(Base):
    """
    Groups devices that share a physical location or context.
    Enables wake-word routing to a default agent and multi-device coordination.
    """
    __tablename__ = "device_groups"

    group_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # AgentManager agent ID activated on wake-word for this group
    default_agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    memberships = relationship("DeviceGroupMember", back_populates="group", cascade="all, delete-orphan")
    sessions = relationship("EmbodimentSession", back_populates="group")


class DeviceGroupMember(Base):
    """Association between a DeviceGroup and a Device with a role.
    A device can be in multiple groups with different roles."""
    __tablename__ = "device_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("device_groups.group_id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    # role: "primary" | "aux_speaker" | "aux_display" | "sensor" | "input_terminal"
    role: Mapped[str] = mapped_column(String, nullable=False, default="primary")

    group = relationship("DeviceGroup", back_populates="memberships")
    device = relationship("Device", back_populates="group_memberships")


# ── Embodiment Sessions ────────────────────────────────────────────────────────

class EmbodimentSession(Base):
    """
    An embodiment session represents an agent's physical presence on one or more devices.
    Replaces DevicePresence for the new embodiment model.
    """
    __tablename__ = "embodiment_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # AgentManager conversation session ID — links physical session to ongoing conversation
    am_session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # Primary device (where avatar lives, main audio I/O)
    primary_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.device_id", ondelete="SET NULL"), nullable=True
    )

    # Priority — higher z_index preempts lower
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # permission_plan: "active" | "ambient" | "timeout"
    permission_plan: Mapped[str] = mapped_column(String, nullable=False, default="active")
    # Set for "timeout" plan; null for others
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # state: "streaming" | "ambient" | "released"
    state: Mapped[str] = mapped_column(String, nullable=False, default="streaming", index=True)

    # Set if session is group-scoped
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("device_groups.group_id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    primary_device = relationship("Device", foreign_keys=[primary_device_id])
    group = relationship("DeviceGroup", back_populates="sessions")
    devices = relationship(
        "EmbodimentSessionDevice", back_populates="session", cascade="all, delete-orphan"
    )


class EmbodimentSessionDevice(Base):
    """
    A device participating in an embodiment session.
    A session can hold multiple devices (e.g. avatar on wall screen + music on loudspeaker).
    """
    __tablename__ = "embodiment_session_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("embodiment_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    # role: "primary_embodiment" | "aux_speaker" | "aux_display" | "sensor_feed" | "input_terminal"
    role: Mapped[str] = mapped_column(String, nullable=False, default="primary_embodiment")
    connected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    session = relationship("EmbodimentSession", back_populates="devices")
    device = relationship("Device", back_populates="embodiment_session_devices")


# ── Device Events ──────────────────────────────────────────────────────────────

class DeviceEvent(Base):
    """Incoming events from physical devices (wake word, button press, motion, etc.)."""
    __tablename__ = "device_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_slug: Mapped[str] = mapped_column(String, nullable=False)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    # event_type: "wake_word" | "button_press" | "motion" | "custom"
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Resulting embodiment session_id if one was created/resumed
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    device = relationship("Device", back_populates="events")
