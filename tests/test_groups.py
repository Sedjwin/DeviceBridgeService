"""Tests for the device groups router."""
from __future__ import annotations

import pytest
import respx
import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceGroup, DeviceGroupMember
from app.config import settings
from tests.conftest import ADMIN_HEADERS, mock_usermanager


@pytest.fixture
async def sample_device(db: AsyncSession) -> Device:
    """Create and persist a sample device for use in group membership tests."""
    d = Device(
        name="Test LED Matrix",
        slug="test-led-01",
        type="display",
        protocol="wled",
        host="192.168.1.50",
        connection_json='{"http_port": 80}',
        manifest_json="{}",
        status="unknown",
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.fixture
async def sample_group(db: AsyncSession) -> DeviceGroup:
    g = DeviceGroup(name="Kitchen", slug="kitchen", default_agent_id="chef-agent")
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g


class TestListGroups:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: AsyncClient):
        r = await client.get("/api/groups")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_groups(self, client: AsyncClient, sample_group: DeviceGroup):
        r = await client.get("/api/groups")
        assert r.status_code == 200
        groups = r.json()
        assert len(groups) == 1
        assert groups[0]["slug"] == "kitchen"
        assert groups[0]["default_agent_id"] == "chef-agent"


class TestCreateGroup:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_success(self, client: AsyncClient):
        mock_usermanager(respx.mock)

        r = await client.post(
            "/api/groups",
            json={"name": "Living Room", "slug": "living-room", "notes": "TV area"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["slug"] == "living-room"
        assert data["name"] == "Living Room"
        assert data["notes"] == "TV area"
        assert data["enabled"] is True
        assert data["members"] == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_requires_admin(self, client: AsyncClient):
        mock_usermanager(respx.mock, admin=False)
        r = await client.post(
            "/api/groups",
            json={"name": "Test", "slug": "test"},
            headers={"Authorization": "Bearer user-token"},
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_duplicate_slug_rejected(
        self, client: AsyncClient, sample_group: DeviceGroup
    ):
        mock_usermanager(respx.mock)
        r = await client.post(
            "/api/groups",
            json={"name": "Kitchen 2", "slug": "kitchen"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]


class TestGetGroup:
    @pytest.mark.asyncio
    async def test_get_existing(self, client: AsyncClient, sample_group: DeviceGroup):
        r = await client.get(f"/api/groups/{sample_group.group_id}")
        assert r.status_code == 200
        assert r.json()["slug"] == "kitchen"

    @pytest.mark.asyncio
    async def test_get_not_found(self, client: AsyncClient):
        r = await client.get("/api/groups/nonexistent-id")
        assert r.status_code == 404


class TestUpdateGroup:
    @pytest.mark.asyncio
    @respx.mock
    async def test_update_name(self, client: AsyncClient, sample_group: DeviceGroup):
        mock_usermanager(respx.mock)
        r = await client.patch(
            f"/api/groups/{sample_group.group_id}",
            json={"name": "Kitchen Remodelled"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Kitchen Remodelled"
        # slug unchanged
        assert r.json()["slug"] == "kitchen"

    @pytest.mark.asyncio
    @respx.mock
    async def test_update_slug_conflict(
        self, client: AsyncClient, sample_group: DeviceGroup, db: AsyncSession
    ):
        mock_usermanager(respx.mock)
        # Create a second group
        g2 = DeviceGroup(name="Workshop", slug="workshop")
        db.add(g2)
        await db.commit()

        r = await client.patch(
            f"/api/groups/{sample_group.group_id}",
            json={"slug": "workshop"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    @respx.mock
    async def test_update_default_agent(self, client: AsyncClient, sample_group: DeviceGroup):
        mock_usermanager(respx.mock)
        r = await client.patch(
            f"/api/groups/{sample_group.group_id}",
            json={"default_agent_id": "new-agent-99"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["default_agent_id"] == "new-agent-99"


class TestDeleteGroup:
    @pytest.mark.asyncio
    @respx.mock
    async def test_delete_success(self, client: AsyncClient, sample_group: DeviceGroup):
        mock_usermanager(respx.mock)
        r = await client.delete(
            f"/api/groups/{sample_group.group_id}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Verify gone
        r2 = await client.get(f"/api/groups/{sample_group.group_id}")
        assert r2.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_delete_not_found(self, client: AsyncClient):
        mock_usermanager(respx.mock)
        r = await client.delete("/api/groups/does-not-exist", headers=ADMIN_HEADERS)
        assert r.status_code == 404


class TestGroupMembership:
    @pytest.mark.asyncio
    @respx.mock
    async def test_add_device_to_group(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        r = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "primary"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["device_id"] == sample_device.device_id
        assert data["role"] == "primary"
        assert data["device_slug"] == "test-led-01"

    @pytest.mark.asyncio
    @respx.mock
    async def test_add_invalid_role_rejected(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        r = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "master"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    @respx.mock
    async def test_add_device_not_found(
        self, client: AsyncClient, sample_group: DeviceGroup
    ):
        mock_usermanager(respx.mock)
        r = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": "nonexistent", "role": "primary"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_duplicate_membership_rejected(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        payload = {"device_id": sample_device.device_id, "role": "primary"}
        await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json=payload,
            headers=ADMIN_HEADERS,
        )
        r = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json=payload,
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    @respx.mock
    async def test_device_can_join_group_different_role(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        r1 = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "primary"},
            headers=ADMIN_HEADERS,
        )
        assert r1.status_code == 201
        r2 = await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "aux_speaker"},
            headers=ADMIN_HEADERS,
        )
        assert r2.status_code == 201

    @pytest.mark.asyncio
    @respx.mock
    async def test_remove_device_from_group(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "primary"},
            headers=ADMIN_HEADERS,
        )
        r = await client.delete(
            f"/api/groups/{sample_group.group_id}/devices/{sample_device.device_id}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Verify group now has no members
        r2 = await client.get(f"/api/groups/{sample_group.group_id}")
        assert r2.json()["members"] == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_remove_nonmember_not_found(
        self, client: AsyncClient, sample_group: DeviceGroup
    ):
        mock_usermanager(respx.mock)
        r = await client.delete(
            f"/api/groups/{sample_group.group_id}/devices/not-a-member",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_group_lists_members(
        self, client: AsyncClient, sample_group: DeviceGroup, sample_device: Device
    ):
        mock_usermanager(respx.mock)
        await client.post(
            f"/api/groups/{sample_group.group_id}/devices",
            json={"device_id": sample_device.device_id, "role": "primary"},
            headers=ADMIN_HEADERS,
        )
        r = await client.get(f"/api/groups/{sample_group.group_id}")
        assert len(r.json()["members"]) == 1
        assert r.json()["members"][0]["device_slug"] == "test-led-01"
