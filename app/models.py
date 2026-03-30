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
    # protocol: wled | http_rest | pi_bridge | websocket
    protocol: Mapped[str] = mapped_column(String, nullable=False, default="wled")
    host: Mapped[str] = mapped_column(String, nullable=False)
    connection_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # e.g. {"http_port": 80, "udp_port": 21324}
    # status: online | offline | unknown
    status: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    display_json: Mapped[str] = mapped_column(Text, nullable=True)
    # e.g. {"width": 64, "height": 64, "type": "rgb_matrix", "max_fps": 62}
    audio_json: Mapped[str] = mapped_column(Text, nullable=True)
    # e.g. {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}
    input_json: Mapped[str] = mapped_column(Text, nullable=True)
    # e.g. {"has_mic": true, "has_keyboard": false}
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    capabilities = relationship("DeviceCapability", back_populates="device", cascade="all, delete-orphan")
    presences = relationship("DevicePresence", back_populates="device", cascade="all, delete-orphan")
    logs = relationship("DeviceExecutionLog", back_populates="device", cascade="all, delete-orphan")


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
    """Tracks which device an agent session is currently routed through."""
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
    # source: tool_gateway | admin_test | audio_bridge | internal
    source: Mapped[str] = mapped_column(String, nullable=False, default="tool_gateway")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    device = relationship("Device", back_populates="logs")
