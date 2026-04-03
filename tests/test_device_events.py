"""Tests for the device events endpoint and wake-word routing."""
from __future__ import annotations

import pytest
import respx
import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceGroup, DeviceGroupMember, EmbodimentSession, EmbodimentSessionDevice
from app.config import settings
from tests.conftest import mock_agentmanager


@pytest.fixture
async def device(db: AsyncSession) -> Device:
    d = Device(
        name="Chef Wall Screen",
        slug="chef-wall-screen",
        type="display",
        protocol="http_rest",
        host="192.168.1.10",
        connection_json='{"http_port": 8080}',
        manifest_json="{}",
        status="online",
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.fixture
async def group(db: AsyncSession, device: Device) -> DeviceGroup:
    g = DeviceGroup(name="Kitchen", slug="kitchen", default_agent_id="chef-agent")
    db.add(g)
    await db.flush()
    m = DeviceGroupMember(group_id=g.group_id, device_id=device.device_id, role="primary")
    db.add(m)
    await db.commit()
    await db.refresh(g)
    return g


class TestDeviceEventReceive:
    @pytest.mark.asyncio
    async def test_unknown_device_returns_404(self, client: AsyncClient):
        r = await client.post(
            "/api/devices/nonexistent-slug/events",
            json={"event_type": "wake_word", "payload": {"word": "chef"}},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_custom_event_logged(self, client: AsyncClient, device: Device):
        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "custom", "payload": {"data": "temperature:72F"}},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["event_type"] == "custom"
        assert data["device_slug"] == device.slug
        assert data["session_id"] is None  # no session created for custom events

    @pytest.mark.asyncio
    async def test_motion_event_logged(self, client: AsyncClient, device: Device):
        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "motion", "payload": {}},
        )
        assert r.status_code == 201
        assert r.json()["session_id"] is None


class TestWakeWordRouting:
    @pytest.mark.asyncio
    @respx.mock
    async def test_wake_word_creates_session_when_no_active_session(
        self, client: AsyncClient, device: Device, group: DeviceGroup
    ):
        """Wake word on a device in a group with default_agent_id should auto-create session."""
        mock_agentmanager(respx.mock, agent_id="chef-agent")

        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "wake_word", "payload": {"word": "chef"}},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["session_id"] is not None

    @pytest.mark.asyncio
    async def test_wake_word_no_group_no_session_created(
        self, client: AsyncClient, device: Device
    ):
        """Device not in any group — wake word is logged but no session created."""
        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "wake_word", "payload": {"word": "hey"}},
        )
        assert r.status_code == 201
        assert r.json()["session_id"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_wake_word_resumes_ambient_session(
        self, client: AsyncClient, device: Device, db: AsyncSession
    ):
        """Wake word on a device with an ambient session transitions it to streaming."""
        mock_agentmanager(respx.mock, agent_id="chef-agent")

        # Create an ambient session on the device
        session = EmbodimentSession(
            agent_id="chef-agent",
            am_session_id="am-session-ambient",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="ambient",
        )
        db.add(session)
        await db.flush()
        sd = EmbodimentSessionDevice(
            session_id=session.session_id,
            device_id=device.device_id,
            role="primary_embodiment",
            is_active=True,
        )
        db.add(sd)
        await db.commit()

        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "wake_word", "payload": {"word": "chef"}},
        )
        assert r.status_code == 201
        assert r.json()["session_id"] == session.session_id

        # Session should now be streaming
        await db.refresh(session)
        assert session.state == "streaming"

    @pytest.mark.asyncio
    @respx.mock
    async def test_wake_word_does_not_preempt_streaming_session(
        self, client: AsyncClient, device: Device, group: DeviceGroup, db: AsyncSession
    ):
        """Wake word when a streaming session already exists — existing session untouched."""
        mock_agentmanager(respx.mock, agent_id="chef-agent")

        # Create an active streaming session
        session = EmbodimentSession(
            agent_id="chef-agent",
            am_session_id="am-existing",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="streaming",
            z_index=0,
        )
        db.add(session)
        await db.flush()
        sd = EmbodimentSessionDevice(
            session_id=session.session_id,
            device_id=device.device_id,
            role="primary_embodiment",
            is_active=True,
        )
        db.add(sd)
        await db.commit()

        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "wake_word", "payload": {"word": "chef"}},
        )
        # Event is accepted
        assert r.status_code == 201
        # But the original session is unchanged (same session id returned or None for collision)
        await db.refresh(session)
        assert session.state == "streaming"  # not changed


class TestButtonPressRouting:
    @pytest.mark.asyncio
    @respx.mock
    async def test_button_press_creates_session(
        self, client: AsyncClient, device: Device, group: DeviceGroup
    ):
        mock_agentmanager(respx.mock, agent_id="chef-agent")
        r = await client.post(
            f"/api/devices/{device.slug}/events",
            json={"event_type": "button_press", "payload": {"button": "A"}},
        )
        assert r.status_code == 201
        assert r.json()["session_id"] is not None
