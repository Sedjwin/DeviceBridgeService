"""
Register embody.* canonical tools in ToolGateway on service startup.

These are static HTTP tools pointing to DBS embodiment endpoints.
They are admin-granted per agent (not auto-granted) using the standard TG grant flow.
The registration is idempotent — existing tools are updated, not duplicated.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Skill markdown injected into agent context when they enable each tool.
_SKILL_MD: dict[str, str] = {
    "embody.session_create": """\
## embody.session_create

Claim physical embodiment on a device or group.

**Parameters:**
- `agent_id` (string, required): Your agent ID.
- `device_id` (string): Target device ID — provide this OR `group_id`.
- `group_id` (string): Target group ID — DBS picks the primary device from the group.
- `z_index` (int, default 0): Priority. Higher values preempt lower active sessions.
- `permission_plan` (string, default "active"): `"active"` — holds until released; `"timeout"` — auto-releases after N seconds; `"ambient"` — holds display but pauses audio loop.
- `timeout_seconds` (int): Required when `permission_plan="timeout"`.
- `am_session_id` (string): Existing AgentManager session ID to continue (optional).

**Returns:** `{session_id, state, primary_device_id, ...}`. Store `session_id` — pass it to all subsequent `embody.*` calls.

**Usage:**
1. Call `embody.session_create` with `device_id` or `group_id`.
2. Use the returned `session_id` in all subsequent embody calls.
3. Call `embody.session_release` when done. You may hold embodiment across turns.
""",

    "embody.session_release": """\
## embody.session_release

Release your current embodiment session. The device returns to idle state.

**Parameters:**
- `session_id` (string, required): Session ID returned by `embody.session_create`.
""",

    "embody.speak": """\
## embody.speak

Synthesise speech and play it through the device speaker.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `text` (string, required): Text to speak.
- `voice` (string, default "glados"): VoiceService voice name.
- `expression` (string): Optional avatar expression to set alongside speech (e.g. "happy").
- `aux_device_ids` (list[string]): Additional device IDs to also output audio to simultaneously.
""",

    "embody.show_avatar": """\
## embody.show_avatar

Update the avatar expression displayed on the device screen.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `expression` (string, required): Expression state name (must be in device's `expression_states`).
""",

    "embody.show_image": """\
## embody.show_image

Display a base64-encoded image on the device screen.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `image_b64` (string, required): Base64-encoded PNG or JPEG image.
- `caption` (string): Optional text caption to overlay.
""",

    "embody.show_text": """\
## embody.show_text

Display text on the device screen.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `text` (string, required): Text to display.
- `color` (string, default "#FFFFFF"): Hex colour string.
- `scroll` (bool, default false): Scroll the text across the display.
""",

    "embody.configure": """\
## embody.configure

Push runtime settings to the device (e.g. silence_timeout_ms, wake_word).
Only keys listed in the device's `settings_writable` manifest field are accepted.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `settings` (object, required): Dict of setting keys → values. E.g. `{"silence_timeout_ms": 3000}`.
""",

    "embody.re_embody": """\
## embody.re_embody

Move your embodiment to a different device. The conversation continues uninterrupted.

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `device_id` (string, required): Target device ID to move to.
- `release_previous` (bool, default true): If false, the previous device becomes an aux_display.
""",

    "embody.aux_connect": """\
## embody.aux_connect

Add an auxiliary device to your session (e.g. a loudspeaker or secondary display).

**Parameters:**
- `session_id` (string, required): Active embodiment session ID.
- `device_id` (string, required): Device to add.
- `role` (string, default "aux_speaker"): `"aux_speaker"`, `"aux_display"`, `"sensor_feed"`, or `"input_terminal"`.
""",
}

# Tool definitions — name → endpoint path template and HTTP method
_TOOLS: list[dict] = [
    {
        "name": "embody.session_create",
        "description": "Claim physical embodiment on a device or group. Returns a session_id to use in subsequent embody.* calls.",
        "method": "POST",
        "path": "/api/embodiment/sessions",
    },
    {
        "name": "embody.session_release",
        "description": "Release an active embodiment session, returning the device to idle.",
        "method": "DELETE",
        "path": "/api/embodiment/sessions/{session_id}",
    },
    {
        "name": "embody.speak",
        "description": "Synthesise speech and play it through the embodied device speaker, with optional avatar expression.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/speak",
    },
    {
        "name": "embody.show_avatar",
        "description": "Update the avatar expression displayed on the embodied device screen.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/show_avatar",
    },
    {
        "name": "embody.show_image",
        "description": "Display a base64-encoded image on the embodied device screen.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/show_image",
    },
    {
        "name": "embody.show_text",
        "description": "Display text on the embodied device screen.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/show_text",
    },
    {
        "name": "embody.configure",
        "description": "Push runtime settings (e.g. silence_timeout_ms, wake_word) to the embodied device.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/configure",
    },
    {
        "name": "embody.re_embody",
        "description": "Move embodiment to a different device while preserving the conversation session.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/re_embody",
    },
    {
        "name": "embody.aux_connect",
        "description": "Add an auxiliary device (speaker, display, sensor) to an active embodiment session.",
        "method": "POST",
        "path": "/api/embodiment/sessions/{session_id}/aux_connect",
    },
]


async def register_embody_tools() -> tuple[int, int]:
    """
    Register all embody.* tools in ToolGateway.
    Idempotent — existing tools with the same name are updated (PATCH), not duplicated.
    Returns (registered, failed).
    """
    key = settings.toolgateway_service_key
    if not key:
        logger.warning(
            "DBS: toolgateway_service_key not set — skipping embody.* tool registration"
        )
        return 0, 0

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    base_url = f"http://{settings.host}:{settings.port}"

    registered = 0
    failed = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Fetch existing tools to detect which already exist
        try:
            r = await client.get(f"{settings.toolgateway_url}/api/tools", headers=headers)
            existing: dict[str, dict] = {
                t["name"]: t
                for t in (r.json() if r.status_code == 200 else [])
                if t.get("name", "").startswith("embody.")
            }
        except Exception as exc:
            logger.warning("DBS: could not fetch TG tools for embody.* registration: %s", exc)
            existing = {}

        for tool_def in _TOOLS:
            name = tool_def["name"]
            endpoint_url = f"{base_url}{tool_def['path']}"
            payload = {
                "name": name,
                "description": tool_def["description"],
                "category": "embodiment",
                "kind": "http",
                "endpoint_url": endpoint_url,
                "method": tool_def["method"],
                "state": "active",
                "enabled": True,
                "capabilities": ["network_access"],
                "metadata": {
                    "auto_registered": True,
                    "service": "DeviceBridgeService",
                    "tool_family": "embody",
                },
                "skill_md": _SKILL_MD.get(name, ""),
            }

            try:
                if name in existing:
                    tool_id = existing[name]["tool_id"]
                    r = await client.patch(
                        f"{settings.toolgateway_url}/api/tools/{tool_id}",
                        json={k: v for k, v in payload.items() if k != "name"},
                        headers=headers,
                    )
                    status_ok = r.status_code == 200
                else:
                    r = await client.post(
                        f"{settings.toolgateway_url}/api/tools",
                        json=payload,
                        headers=headers,
                    )
                    status_ok = r.status_code == 201

                if status_ok:
                    registered += 1
                    logger.info("DBS: registered embody tool '%s' (HTTP %d)", name, r.status_code)
                else:
                    logger.warning(
                        "DBS: failed to register embody tool '%s' — TG returned %d: %s",
                        name, r.status_code, r.text[:200],
                    )
                    failed += 1

            except Exception as exc:
                logger.warning("DBS: exception registering embody tool '%s': %s", name, exc)
                failed += 1

    logger.info(
        "DBS: embody.* tool registration complete — registered=%d failed=%d", registered, failed
    )
    return registered, failed
