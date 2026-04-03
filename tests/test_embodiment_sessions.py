"""Tests for embodiment session lifecycle: creation, preemption, release, re_embody, aux."""
from __future__ import annotations

import pytest
import respx
import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceGroup, DeviceGroupMember, EmbodimentSession, EmbodimentSessionDevice
from app.config import settings
from app.services import embodiment_manager as em
from tests.conftest import USER_HEADERS, ADMIN_HEADERS, mock_usermanager, mock_agentmanager


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def device(db: AsyncSession) -> Device:
    d = Device(
        name="Wall Screen",
        slug="wall-screen-01",
        type="display",
        protocol="http_rest",
        host="192.168.1.10",
        connection_json='{"http_port": 8080}',
        manifest_json="{}",
        embodiment_manifest_json=None,
        status="online",
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.fixture
async def device2(db: AsyncSession) -> Device:
    d = Device(
        name="Cooker Screen",
        slug="cooker-screen-01",
        type="display",
        protocol="http_rest",
        host="192.168.1.20",
        connection_json='{"http_port": 8080}',
        manifest_json="{}",
        status="online",
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.fixture
async def group_with_device(db: AsyncSession, device: Device) -> DeviceGroup:
    g = DeviceGroup(name="Kitchen", slug="kitchen", default_agent_id="chef-agent")
    db.add(g)
    await db.flush()
    m = DeviceGroupMember(group_id=g.group_id, device_id=device.device_id, role="primary")
    db.add(m)
    await db.commit()
    await db.refresh(g)
    return g


def _mock_all_external(respx_mock, agent_id: str = "agent-1"):
    """Mock all external service calls for session creation."""
    mock_usermanager(respx_mock, admin=False)  # non-admin principal (agent key)
    mock_agentmanager(respx_mock, agent_id=agent_id)
    # ToolGateway not called during session creation


# ── Session creation ──────────────────────────────────────────────────────────

class TestCreateSession:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_with_device_id(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={
                "agent_id": "agent-1",
                "device_id": device.device_id,
                "z_index": 0,
                "permission_plan": "active",
            },
            headers=USER_HEADERS,
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["state"] == "streaming"
        assert data["agent_id"] == "agent-1"
        assert data["primary_device_id"] == device.device_id
        assert data["permission_plan"] == "active"
        assert data["z_index"] == 0
        # Session device should be recorded
        assert len(data["devices"]) == 1
        assert data["devices"][0]["role"] == "primary_embodiment"

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_with_group_id(
        self, client: AsyncClient, group_with_device: DeviceGroup, device: Device
    ):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={
                "agent_id": "agent-1",
                "group_id": group_with_device.group_id,
                "permission_plan": "active",
            },
            headers=USER_HEADERS,
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["primary_device_id"] == device.device_id
        assert data["group_id"] == group_with_device.group_id

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_requires_device_or_group(self, client: AsyncClient):
        mock_usermanager(respx.mock, admin=False)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_plan_requires_seconds(self, client: AsyncClient, device: Device):
        mock_usermanager(respx.mock, admin=False)
        r = await client.post(
            "/api/embodiment/sessions",
            json={
                "agent_id": "agent-1",
                "device_id": device.device_id,
                "permission_plan": "timeout",
                # missing timeout_seconds
            },
            headers=USER_HEADERS,
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_plan_with_seconds(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={
                "agent_id": "agent-1",
                "device_id": device.device_id,
                "permission_plan": "timeout",
                "timeout_seconds": 300,
            },
            headers=USER_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["expires_at"] is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_am_session_id_returned(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r.status_code == 201
        # AgentManager was mocked to return "am-session-1"
        assert r.json()["am_session_id"] == "am-session-1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_provided_am_session_id_preserved(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={
                "agent_id": "agent-1",
                "device_id": device.device_id,
                "am_session_id": "existing-am-session-99",
                "permission_plan": "active",
            },
            headers=USER_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["am_session_id"] == "existing-am-session-99"


# ── Preemption ────────────────────────────────────────────────────────────────

class TestPreemption:
    @pytest.mark.asyncio
    @respx.mock
    async def test_lower_z_blocked_by_higher(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)

        # High-priority session (z=5)
        r1 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "z_index": 5, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r1.status_code == 201

        # Low-priority session (z=1) should be rejected
        r2 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-2", "device_id": device.device_id, "z_index": 1, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail["error"] == "device_occupied"
        assert detail["holder_z"] == 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_equal_z_new_wins(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)

        r1 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "z_index": 0, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r1.status_code == 201
        first_session_id = r1.json()["session_id"]

        r2 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-2", "device_id": device.device_id, "z_index": 0, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 201
        # First session should now be released
        r_get = await client.get(f"/api/embodiment/sessions/{first_session_id}", headers=USER_HEADERS)
        assert r_get.json()["state"] == "released"

    @pytest.mark.asyncio
    @respx.mock
    async def test_higher_z_preempts_lower(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)

        r1 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "z_index": 0, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r1.status_code == 201
        old_session_id = r1.json()["session_id"]

        r2 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-2", "device_id": device.device_id, "z_index": 10, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 201

        # Old session should be released
        r_old = await client.get(f"/api/embodiment/sessions/{old_session_id}", headers=USER_HEADERS)
        assert r_old.json()["state"] == "released"


# ── Session retrieval ─────────────────────────────────────────────────────────

class TestGetSession:
    @pytest.mark.asyncio
    @respx.mock
    async def test_get_existing_session(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        r2 = await client.get(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)
        assert r2.status_code == 200
        assert r2.json()["session_id"] == sid

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_not_found(self, client: AsyncClient):
        mock_usermanager(respx.mock, admin=False)
        r = await client.get("/api/embodiment/sessions/nonexistent", headers=USER_HEADERS)
        assert r.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_sessions(self, client: AsyncClient, device: Device, device2: Device):
        _mock_all_external(respx.mock)
        await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device2.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        r = await client.get("/api/embodiment/sessions", headers=USER_HEADERS)
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_sessions_filter_by_state(
        self, client: AsyncClient, device: Device, device2: Device
    ):
        _mock_all_external(respx.mock)
        r1 = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid1 = r1.json()["session_id"]

        # Release the first session
        await client.delete(f"/api/embodiment/sessions/{sid1}", headers=USER_HEADERS)

        # Create a second session
        await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device2.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )

        # Filter by streaming only
        r = await client.get("/api/embodiment/sessions?state=streaming", headers=USER_HEADERS)
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["state"] == "streaming"


# ── Session release ───────────────────────────────────────────────────────────

class TestReleaseSession:
    @pytest.mark.asyncio
    @respx.mock
    async def test_release_sets_state_released(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        del_r = await client.delete(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)
        assert del_r.status_code == 204

        r2 = await client.get(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)
        assert r2.json()["state"] == "released"
        assert r2.json()["released_at"] is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_release_idempotent(self, client: AsyncClient, device: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        await client.delete(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)
        r2 = await client.delete(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)
        assert r2.status_code == 204  # idempotent


# ── Re-embody ─────────────────────────────────────────────────────────────────

class TestReEmbody:
    @pytest.mark.asyncio
    @respx.mock
    async def test_re_embody_changes_primary_device(
        self, client: AsyncClient, device: Device, device2: Device
    ):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]
        assert r.json()["primary_device_id"] == device.device_id

        r2 = await client.post(
            f"/api/embodiment/sessions/{sid}/re_embody",
            json={"device_id": device2.device_id, "release_previous": True},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 200
        assert r2.json()["primary_device_id"] == device2.device_id

    @pytest.mark.asyncio
    @respx.mock
    async def test_re_embody_old_becomes_aux_when_not_released(
        self, client: AsyncClient, device: Device, device2: Device
    ):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        r2 = await client.post(
            f"/api/embodiment/sessions/{sid}/re_embody",
            json={"device_id": device2.device_id, "release_previous": False},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 200
        # Old device should now appear as aux_display
        devices_in_session = r2.json()["devices"]
        roles = {d["device_id"]: d["role"] for d in devices_in_session if d["is_active"]}
        assert roles.get(device.device_id) == "aux_display"
        assert roles.get(device2.device_id) == "primary_embodiment"

    @pytest.mark.asyncio
    @respx.mock
    async def test_re_embody_released_session_rejected(
        self, client: AsyncClient, device: Device, device2: Device
    ):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]
        await client.delete(f"/api/embodiment/sessions/{sid}", headers=USER_HEADERS)

        r2 = await client.post(
            f"/api/embodiment/sessions/{sid}/re_embody",
            json={"device_id": device2.device_id},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 409


# ── Aux devices ───────────────────────────────────────────────────────────────

class TestAuxDevices:
    @pytest.mark.asyncio
    @respx.mock
    async def test_aux_connect(self, client: AsyncClient, device: Device, device2: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        r2 = await client.post(
            f"/api/embodiment/sessions/{sid}/aux_connect",
            json={"device_id": device2.device_id, "role": "aux_speaker"},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 201
        assert r2.json()["role"] == "aux_speaker"
        assert r2.json()["device_id"] == device2.device_id

    @pytest.mark.asyncio
    @respx.mock
    async def test_aux_connect_invalid_role(
        self, client: AsyncClient, device: Device, device2: Device
    ):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        r2 = await client.post(
            f"/api/embodiment/sessions/{sid}/aux_connect",
            json={"device_id": device2.device_id, "role": "invalid_role"},
            headers=USER_HEADERS,
        )
        assert r2.status_code == 422

    @pytest.mark.asyncio
    @respx.mock
    async def test_aux_disconnect(self, client: AsyncClient, device: Device, device2: Device):
        _mock_all_external(respx.mock)
        r = await client.post(
            "/api/embodiment/sessions",
            json={"agent_id": "agent-1", "device_id": device.device_id, "permission_plan": "active"},
            headers=USER_HEADERS,
        )
        sid = r.json()["session_id"]

        await client.post(
            f"/api/embodiment/sessions/{sid}/aux_connect",
            json={"device_id": device2.device_id, "role": "aux_speaker"},
            headers=USER_HEADERS,
        )
        r2 = await client.delete(
            f"/api/embodiment/sessions/{sid}/aux/{device2.device_id}",
            headers=USER_HEADERS,
        )
        assert r2.status_code == 204


# ── Embodiment manager unit tests ─────────────────────────────────────────────

class TestEmbodimentManagerService:
    @pytest.mark.asyncio
    async def test_expire_timed_out_sessions(self, db: AsyncSession, device: Device):
        """Sessions with expired timeout should be released by sweep task."""
        from datetime import datetime, timezone, timedelta

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        session = EmbodimentSession(
            agent_id="agent-1",
            primary_device_id=device.device_id,
            permission_plan="timeout",
            expires_at=past,
            state="streaming",
        )
        db.add(session)
        await db.flush()
        await db.refresh(session)  # ensure session_id is populated from DB
        sd = EmbodimentSessionDevice(
            session_id=session.session_id,
            device_id=device.device_id,
            role="primary_embodiment",
            is_active=True,
        )
        db.add(sd)
        await db.commit()

        count = await em.expire_timed_out_sessions(db)
        assert count == 1

        await db.refresh(session)
        assert session.state == "released"

    @pytest.mark.asyncio
    async def test_expire_leaves_active_sessions_alone(self, db: AsyncSession, device: Device):
        """Sessions without timeout plan or future expiry must not be touched."""
        from datetime import datetime, timezone, timedelta

        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s1 = EmbodimentSession(
            agent_id="a1",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="streaming",
        )
        s2 = EmbodimentSession(
            agent_id="a2",
            primary_device_id=device.device_id,
            permission_plan="timeout",
            expires_at=future,
            state="streaming",
        )
        db.add_all([s1, s2])
        await db.commit()

        count = await em.expire_timed_out_sessions(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_set_session_state_streaming_to_ambient(
        self, db: AsyncSession, device: Device
    ):
        session = EmbodimentSession(
            agent_id="a1",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="streaming",
        )
        db.add(session)
        await db.commit()

        await em.set_session_state(db, session, "ambient")
        assert session.state == "ambient"

    @pytest.mark.asyncio
    async def test_set_session_state_invalid_raises(self, db: AsyncSession, device: Device):
        session = EmbodimentSession(
            agent_id="a1",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="streaming",
        )
        db.add(session)
        await db.commit()

        with pytest.raises(ValueError, match="Invalid state transition"):
            await em.set_session_state(db, session, "released")

    @pytest.mark.asyncio
    async def test_get_active_session_on_device(self, db: AsyncSession, device: Device):
        session = EmbodimentSession(
            agent_id="a1",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="streaming",
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

        found = await em.get_active_session_on_device(db, device.device_id)
        assert found is not None
        assert found.session_id == session.session_id

    @pytest.mark.asyncio
    async def test_get_active_session_returns_none_when_released(
        self, db: AsyncSession, device: Device
    ):
        session = EmbodimentSession(
            agent_id="a1",
            primary_device_id=device.device_id,
            permission_plan="active",
            state="released",
        )
        db.add(session)
        await db.flush()
        sd = EmbodimentSessionDevice(
            session_id=session.session_id,
            device_id=device.device_id,
            role="primary_embodiment",
            is_active=False,
        )
        db.add(sd)
        await db.commit()

        found = await em.get_active_session_on_device(db, device.device_id)
        assert found is None
