from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import AgentSummary


def test_admin_page_serves_html() -> None:
    with TestClient(app) as client:
        res = client.get("/admin")
        assert res.status_code == 200
        assert "DeviceBridgeService Admin" in res.text


def test_mapping_suggestion_uses_agent_profile(monkeypatch) -> None:
    device_id = f"admin-dev-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    async def fake_get_agents() -> list[AgentSummary]:
        return [
            AgentSummary(
                agent_id=agent_id,
                name="Test Agent",
                profile={
                    "emotions": ["curious", "bored"],
                    "actions": ["scan", "head_tilt"],
                },
            )
        ]

    from app.routers import admin

    monkeypatch.setattr(admin, "get_agents", fake_get_agents)

    with TestClient(app) as client:
        put_res = client.put(
            f"/api/devices/{device_id}/capabilities",
            json={
                "name": "Admin Test Device",
                "model": "esp32s3",
                "firmware_version": "0.1.0",
                "capabilities": {
                    "render_modes": ["line", "shape"],
                    "animations": ["neutral_blink", "scan_sweep", "head_tilt"],
                    "audio_codecs": ["wav"],
                    "sample_rates": [22050],
                    "mic_enabled": True,
                    "mic_format": "pcm16",
                },
            },
        )
        assert put_res.status_code == 200

        suggest = client.post(
            "/api/admin/mappings/suggest",
            json={
                "agent_id": agent_id,
                "device_id": device_id,
                "preferred_render_mode": "line",
            },
        )
        assert suggest.status_code == 200
        payload = suggest.json()
        assert payload["agent_id"] == agent_id
        assert "curious" in payload["emotion_map"]
        assert "scan" in payload["action_map"]


def test_mapping_suggestion_passthrough_mode(monkeypatch) -> None:
    device_id = f"admin-dev-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    async def fake_get_agents() -> list[AgentSummary]:
        return [
            AgentSummary(
                agent_id=agent_id,
                name="Passthrough Agent",
                profile={
                    "emotions": ["curious_mode"],
                    "actions": ["scan_sweep"],
                },
            )
        ]

    from app.routers import admin

    monkeypatch.setattr(admin, "get_agents", fake_get_agents)

    with TestClient(app) as client:
        put_res = client.put(
            f"/api/devices/{device_id}/capabilities",
            json={
                "name": "Passthrough Device",
                "model": "esp32s3",
                "firmware_version": "0.1.0",
                "capabilities": {
                    "render_modes": ["line", "shape"],
                    "animations": ["neutral_blink"],
                    "audio_codecs": ["wav"],
                    "sample_rates": [22050],
                    "mic_enabled": True,
                    "mic_format": "pcm16",
                    "accepts_model_directives": True,
                },
            },
        )
        assert put_res.status_code == 200

        suggest = client.post(
            "/api/admin/mappings/suggest",
            json={
                "agent_id": agent_id,
                "device_id": device_id,
                "preferred_render_mode": "line",
            },
        )
        assert suggest.status_code == 200
        payload = suggest.json()
        assert payload["emotion_map"]["curious_mode"]["animation"] == "curious_mode"
        assert payload["action_map"]["scan_sweep"]["animation"] == "scan_sweep"
